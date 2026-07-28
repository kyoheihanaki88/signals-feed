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
import type { EditionOrchestrator } from "./_lib/edition-orchestrator.js";
import { assembleSignalsFeed } from "./_lib/editorial-mix-feed.js";
import type { SecurityLogger } from "./_lib/security-logging.js";
import type { Clock } from "./_lib/signals-token.js";

const MAX_BODY_BYTES = 8 * 1_024;

type EditionDependencies = EditionAuthenticatorDependencies & {
  logger: SecurityLogger;
  clock: Clock;
  requestId: () => string;
  /**
   * The Custom Mix orchestration seam. (Phase 3D-2, connected in 3E-1)
   *
   * When absent the route answers `custom_mix_unavailable` — the same safe fallback every
   * other non-selection path uses. When present it decides which path a verified-Pro
   * request takes; only `custom_mix_pro` produces a 200.
   */
  orchestrator?: EditionOrchestrator;
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

/**
 * The single public failure shape for "no Custom Mix today".
 *
 * 503 matches the route's existing convention for temporary unavailability
 * (`custom_mix_disabled` already uses it), and tells the client to fall back rather than
 * to treat the request as malformed.
 */
function unavailableResponse(): Response {
  return jsonResponse(503, {
    status: "unavailable",
    code: "custom_mix_unavailable",
  });
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

    // Reaching this point proves the caller is server-verified Pro: a Signals token is
    // only ever issued against a live Apple entitlement check. The orchestrator decides
    // which path that Pro request takes.
    //
    // FALLBACK CONTRACT. Every path except `custom_mix_pro` answers 503
    // `custom_mix_unavailable`, and the client falls back to the static `latest.json` it
    // already uses. That single public code covers an unconfigured store, a missing or
    // stale key, a malformed pool, a provider refusal and an unusable selection alike:
    // the distinctions are preserved internally, in `reasonCode`, for logs and tests — the
    // public body never names a provider, a key, a credential or a validation failure.
    if (!dependencies.orchestrator) {
      return finish(unavailableResponse(), "standard_selector_unavailable");
    }

    const outcome = await dependencies.orchestrator({ contract });
    if (outcome.path !== "custom_mix_pro") {
      return finish(unavailableResponse(), outcome.path);
    }

    // The selection is exactly `storyCount` enriched stories in selector order. The adapter
    // is pure and throws on any shape it cannot serve; a throw here falls back rather than
    // returning a partial or malformed edition.
    let feed;
    try {
      feed = assembleSignalsFeed(contract.date, outcome.selected);
    } catch {
      return finish(unavailableResponse(), "standard_selector_unavailable");
    }

    return finish(jsonResponse(200, feed as unknown as Record<string, unknown>), outcome.path);
  };
}

/**
 * Vercel route entry point for POST /api/edition. (Phase 3C-2)
 *
 * Dynamically imported for the same two reasons as the exchange route: this module must
 * stay import-safe, and `vercel-runtime` depends on `createEditionHandler` above.
 *
 * Authentication runs for real. A successful Custom Mix selection returns the SignalsFeed
 * document the iOS client already decodes; every other outcome returns
 * `503 custom_mix_unavailable` and the client falls back to the static `latest.json`.
 */
export default {
  async fetch(request: Request): Promise<Response> {
    const { handleEditionRequest } = await import("./_lib/vercel-runtime.js");
    return handleEditionRequest(request);
  },
};
