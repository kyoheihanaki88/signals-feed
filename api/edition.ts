import type { SignalsEnvironment } from "./_lib/apple-verifier.js";
import {
  AuthenticationError,
  authenticateEdition,
  type EditionAuthenticatorDependencies,
} from "./_lib/auth-middleware.js";
import {
  ContractValidationError,
  validateEditionRequest,
} from "./_lib/custom-mix-contract.js";
import type { SecurityLogger } from "./_lib/security-logging.js";
import type { Clock } from "./_lib/signals-token.js";

const MAX_BODY_BYTES = 8 * 1_024;

type EditionDependencies = EditionAuthenticatorDependencies & {
  logger: SecurityLogger;
  clock: Clock;
  requestId: () => string;
};

export type EditionConfig = {
  enabled: boolean;
  environment: SignalsEnvironment;
  isDateAllowed?: (date: string) => boolean;
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

export function createEditionHandler(
  config: EditionConfig,
  dependencies: EditionDependencies,
): (request: Request) => Promise<Response> {
  return async (request: Request): Promise<Response> => {
    const startedAt = dependencies.clock.nowMs();
    const requestId = dependencies.requestId();
    let status = 500;
    let reasonCode = "internal_error";

    const finish = (response: Response, reason: string): Response => {
      status = response.status;
      reasonCode = reason;
      dependencies.logger.log({
        route: "/api/edition",
        status,
        reasonCode,
        latencyMs: Math.max(0, dependencies.clock.nowMs() - startedAt),
        requestId,
        selectorVersion: 1,
        environment: config.environment,
        ...(reason === "rate_limited"
          ? { rateLimitBucket: "token_edition" as const }
          : {}),
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
    if (
      !request.headers
        .get("content-type")
        ?.toLowerCase()
        .startsWith("application/json")
    ) {
      return finish(errorResponse(400, "invalid_request"), "invalid_request");
    }

    try {
      await authenticateEdition(
        {
          authorization: request.headers.get("authorization"),
          expectedEnvironment: config.environment,
          nowMs: dependencies.clock.nowMs(),
        },
        dependencies,
      );
    } catch (error) {
      if (error instanceof AuthenticationError) {
        return finish(
          errorResponse(error.status, error.code, error.retryAfterSeconds),
          error.code,
        );
      }
      return finish(errorResponse(401, "invalid_token"), "invalid_token");
    }

    const contentLength = Number(request.headers.get("content-length") ?? "0");
    if (Number.isFinite(contentLength) && contentLength > MAX_BODY_BYTES) {
      return finish(errorResponse(413, "invalid_request"), "invalid_request");
    }
    const text = await request.text();
    if (
      Buffer.byteLength(text, "utf8") === 0 ||
      Buffer.byteLength(text, "utf8") > MAX_BODY_BYTES
    ) {
      const statusCode =
        Buffer.byteLength(text, "utf8") > MAX_BODY_BYTES ? 413 : 400;
      return finish(
        errorResponse(statusCode, "invalid_request"),
        "invalid_request",
      );
    }
    let body: unknown;
    try {
      body = JSON.parse(text);
    } catch {
      return finish(errorResponse(400, "invalid_request"), "invalid_request");
    }

    let contract;
    try {
      contract = validateEditionRequest(body);
    } catch (error) {
      if (error instanceof ContractValidationError) {
        const statusCode =
          error.code === "unsupported_selector_version" ? 422 : 400;
        return finish(errorResponse(statusCode, error.code), error.code);
      }
      return finish(errorResponse(400, "invalid_request"), "invalid_request");
    }
    if (config.isDateAllowed && !config.isDateAllowed(contract.date)) {
      return finish(errorResponse(400, "invalid_request"), "invalid_request");
    }

    return finish(
      jsonResponse(503, {
        status: "not_connected",
        code: "selector_not_connected",
      }),
      "selector_not_connected",
    );
  };
}

/**
 * Vercel route entry point for POST /api/edition. (Phase 3C-2)
 *
 * Dynamically imported for the same two reasons as the exchange route: this module must
 * stay import-safe, and `vercel-runtime` depends on `createEditionHandler` above.
 *
 * Authentication runs for real; the response still stops at `503 selector_not_connected`.
 * No edition, manifest or candidate pool is read, and no selector is invoked.
 */
export default {
  async fetch(request: Request): Promise<Response> {
    const { handleEditionRequest } = await import("./_lib/vercel-runtime.js");
    return handleEditionRequest(request);
  },
};
