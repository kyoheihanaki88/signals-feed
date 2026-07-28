import {
  AppleVerificationError,
  type AppleEntitlementVerifier,
  type SignalsEnvironment,
} from "../_lib/apple-verifier.js";
import type { RateLimiter } from "../_lib/rate-limit.js";
import type { RevocationStore } from "../_lib/revocation-store.js";
import type { SecurityLogger } from "../_lib/security-logging.js";
import {
  PRO_PRODUCT_ID,
  TOKEN_SCOPE,
  type Clock,
  type SignalsTokenService,
} from "../_lib/signals-token.js";

const BUNDLE_ID = "com.kyohei.Signals";
const MAX_BODY_BYTES = 16 * 1_024;
const MAX_JWS_BYTES = 12 * 1_024;
const MAX_APP_VERSION_BYTES = 64;

type ExchangeDependencies = {
  verifier: AppleEntitlementVerifier;
  tokens: SignalsTokenService;
  revocations: RevocationStore;
  ipLimiter: RateLimiter;
  subjectLimiter: RateLimiter;
  logger: SecurityLogger;
  clock: Clock;
  requestId: () => string;
};

export type ExchangeConfig = {
  enabled: boolean;
  environment: SignalsEnvironment;
};

function jsonResponse(
  status: number,
  body: Record<string, unknown>,
  retryAfterSeconds?: number,
): Response {
  const headers = new Headers({
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "private, no-store",
    Pragma: "no-cache",
  });
  if (retryAfterSeconds !== undefined) {
    headers.set("Retry-After", String(retryAfterSeconds));
  }
  return new Response(JSON.stringify(body), { status, headers });
}

function errorResponse(
  status: number,
  code: string,
  retryAfterSeconds?: number,
): Response {
  return jsonResponse(status, { error: { code } }, retryAfterSeconds);
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function looksLikeCompactJws(value: string): boolean {
  const parts = value.split(".");
  return (
    parts.length === 3 &&
    parts.every(
      (part) => part.length > 0 && /^[A-Za-z0-9_-]+$/.test(part),
    )
  );
}

async function readValidatedRequest(
  request: Request,
): Promise<
  | {
      ok: true;
      value: {
        signedTransactionInfo: string;
        appVersion?: string;
        selectorVersion: 1;
      };
    }
  | { ok: false; response: Response }
> {
  if (
    !request.headers
      .get("content-type")
      ?.toLowerCase()
      .startsWith("application/json")
  ) {
    return { ok: false, response: errorResponse(400, "invalid_request") };
  }
  const contentLength = Number(request.headers.get("content-length") ?? "0");
  if (Number.isFinite(contentLength) && contentLength > MAX_BODY_BYTES) {
    return { ok: false, response: errorResponse(413, "invalid_request") };
  }

  const text = await request.text();
  if (
    Buffer.byteLength(text, "utf8") === 0 ||
    Buffer.byteLength(text, "utf8") > MAX_BODY_BYTES
  ) {
    return {
      ok: false,
      response: errorResponse(
        Buffer.byteLength(text, "utf8") > MAX_BODY_BYTES ? 413 : 400,
        "invalid_request",
      ),
    };
  }

  let body: unknown;
  try {
    body = JSON.parse(text);
  } catch {
    return { ok: false, response: errorResponse(400, "invalid_request") };
  }
  if (!isObject(body)) {
    return { ok: false, response: errorResponse(400, "invalid_request") };
  }
  if (
    Object.keys(body).some(
      (key) =>
        !["signedTransactionInfo", "appVersion", "selectorVersion"].includes(
          key,
        ),
    )
  ) {
    return { ok: false, response: errorResponse(400, "invalid_request") };
  }
  if (
    typeof body.signedTransactionInfo !== "string" ||
    body.signedTransactionInfo.length === 0 ||
    Buffer.byteLength(body.signedTransactionInfo, "utf8") > MAX_JWS_BYTES ||
    !looksLikeCompactJws(body.signedTransactionInfo) ||
    body.selectorVersion !== 1 ||
    (body.appVersion !== undefined &&
      (typeof body.appVersion !== "string" ||
        body.appVersion.length === 0 ||
        Buffer.byteLength(body.appVersion, "utf8") >
          MAX_APP_VERSION_BYTES))
  ) {
    return { ok: false, response: errorResponse(400, "invalid_request") };
  }
  return {
    ok: true,
    value: {
      signedTransactionInfo: body.signedTransactionInfo,
      ...(body.appVersion === undefined
        ? {}
        : { appVersion: body.appVersion as string }),
      selectorVersion: 1,
    },
  };
}

function clientIp(request: Request): string {
  return (
    request.headers.get("x-forwarded-for")?.split(",")[0]?.trim() ||
    request.headers.get("x-real-ip") ||
    "unknown"
  );
}

export function createAuthExchangeHandler(
  config: ExchangeConfig,
  dependencies: ExchangeDependencies,
): (request: Request) => Promise<Response> {
  return async (request: Request): Promise<Response> => {
    const startedAt = dependencies.clock.nowMs();
    const requestId = dependencies.requestId();
    let status = 500;
    let reasonCode = "internal_error";
    let environment: SignalsEnvironment | undefined;
    let rateLimitBucket:
      | "ip_exchange"
      | "subject_exchange"
      | undefined;

    const finish = (response: Response, reason: string): Response => {
      status = response.status;
      reasonCode = reason;
      dependencies.logger.log({
        route: "/api/auth/exchange",
        status,
        reasonCode,
        latencyMs: Math.max(0, dependencies.clock.nowMs() - startedAt),
        requestId,
        selectorVersion: 1,
        ...(environment === undefined ? {} : { environment }),
        ...(rateLimitBucket === undefined ? {} : { rateLimitBucket }),
      });
      return response;
    };

    if (request.method !== "POST") {
      return finish(errorResponse(405, "invalid_request"), "invalid_request");
    }
    if (!config.enabled) {
      return finish(
        errorResponse(503, "custom_mix_disabled"),
        "custom_mix_disabled",
      );
    }

    const parsed = await readValidatedRequest(request);
    if (!parsed.ok) {
      return finish(parsed.response, "invalid_request");
    }

    rateLimitBucket = "ip_exchange";
    try {
      const decision = await dependencies.ipLimiter.consume(
        clientIp(request),
        dependencies.clock.nowMs(),
      );
      if (!decision.allowed) {
        return finish(
          errorResponse(429, "rate_limited", decision.retryAfterSeconds),
          "rate_limited",
        );
      }
    } catch {
      return finish(
        errorResponse(503, "verification_unavailable"),
        "verification_unavailable",
      );
    }

    let entitlement;
    try {
      entitlement = await dependencies.verifier.verifySignedTransaction({
        signedTransactionInfo: parsed.value.signedTransactionInfo,
        expectedEnvironment: config.environment,
      });
    } catch (error) {
      if (error instanceof AppleVerificationError) {
        const statusCode =
          error.code === "verification_unavailable" ? 503 : 401;
        return finish(errorResponse(statusCode, error.code), error.code);
      }
      return finish(errorResponse(401, "invalid_proof"), "invalid_proof");
    }

    environment = entitlement.environment;
    if (entitlement.environment !== config.environment) {
      return finish(
        errorResponse(401, "unsupported_environment"),
        "unsupported_environment",
      );
    }
    if (entitlement.bundleId !== BUNDLE_ID) {
      return finish(errorResponse(401, "wrong_bundle"), "wrong_bundle");
    }
    if (entitlement.productId !== PRO_PRODUCT_ID) {
      return finish(errorResponse(401, "wrong_product"), "wrong_product");
    }
    if (entitlement.productType !== "NON_CONSUMABLE") {
      return finish(
        errorResponse(401, "wrong_product_type"),
        "wrong_product_type",
      );
    }
    if (entitlement.ownershipType !== "PURCHASED") {
      return finish(
        errorResponse(401, "wrong_ownership"),
        "wrong_ownership",
      );
    }
    if (entitlement.revoked) {
      return finish(errorResponse(401, "revoked"), "revoked");
    }
    if (!/^[0-9]{1,32}$/.test(entitlement.originalTransactionId)) {
      return finish(errorResponse(401, "invalid_proof"), "invalid_proof");
    }

    const subject = dependencies.tokens.deriveSubject(
      entitlement.originalTransactionId,
      entitlement.environment,
    );
    try {
      if (await dependencies.revocations.isRevoked(subject)) {
        return finish(errorResponse(401, "revoked"), "revoked");
      }
      rateLimitBucket = "subject_exchange";
      const decision = await dependencies.subjectLimiter.consume(
        subject,
        dependencies.clock.nowMs(),
      );
      if (!decision.allowed) {
        return finish(
          errorResponse(429, "rate_limited", decision.retryAfterSeconds),
          "rate_limited",
        );
      }
    } catch {
      return finish(
        errorResponse(503, "verification_unavailable"),
        "verification_unavailable",
      );
    }

    const issued = dependencies.tokens.issue({
      subject,
      environment: entitlement.environment,
    });
    const response = jsonResponse(200, {
      accessToken: issued.accessToken,
      expiresAt: new Date(issued.claims.exp * 1_000).toISOString(),
      scope: [TOKEN_SCOPE],
    });
    return finish(response, "ok");
  };
}

/**
 * Vercel route entry point for POST /api/auth/exchange. (Phase 3C-2)
 *
 * The runtime is imported DYNAMICALLY, for two reasons: importing this module must stay
 * free of side effects (no config read, no certificate read, no dependency construction),
 * and `vercel-runtime` reaches back to `createAuthExchangeHandler` above — a static import
 * here would close that cycle. Everything real happens on the first request.
 */
export default {
  async fetch(request: Request): Promise<Response> {
    const { handleExchangeRequest } = await import("../_lib/vercel-runtime.js");
    return handleExchangeRequest(request);
  },
};
