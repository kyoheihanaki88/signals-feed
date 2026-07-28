/**
 * Route wiring for a real deployment. (Phase 3C-1)
 *
 * The Phase 3B-1 route factories are unchanged and still own all request validation and
 * response shaping. This module only decides WHICH dependencies they receive, and adds the
 * one thing the exchange route cannot express by itself: retry-safety.
 *
 * What the guards here buy: `createProductionExchangeHandler` refuses to build unless the
 * verifier is a `LiveAppleEntitlementVerifier` and every store is the persistent one. That
 * makes "issued a token from a signed transaction alone" and "authorised against process
 * memory" impossible to reach by wiring mistake rather than by convention.
 *
 * The edition route deliberately stops at `503 selector_not_connected`. It reads no
 * published edition file, no daily manifest, no candidate pool, and invokes no selector
 * process — a source scan in the Phase 3C-1 tests asserts exactly that.
 */

import { createAuthExchangeHandler } from "../auth/exchange.js";
import { createEditionHandler } from "../edition.js";
import { createDisconnectedEditionOrchestrator } from "./edition-orchestrator.js";
import {
  IdempotencyConflictError,
  PersistentIdempotencyStore,
  type IdempotencyRecord,
} from "./persistent-idempotency-store.js";
import { PersistentRateLimiter } from "./persistent-rate-limit.js";
import { PersistentRevocationStore } from "./persistent-revocation-store.js";
import { isRealDeployment, type RuntimeConfig } from "./runtime-config.js";
import {
  LiveAppleEntitlementVerifier,
  RuntimeCompositionError,
  type RuntimeDependencies,
} from "./runtime-dependencies.js";
import type { SecurityLogEvent } from "./security-logging.js";
import { deriveKeyComponent } from "./subject-hash.js";

export type RouteHandler = (request: Request) => Promise<Response>;

const EXCHANGE_ROUTE = "/api/auth/exchange";
const MAX_BODY_BYTES = 16 * 1_024;

/**
 * The only reason codes a stored idempotency record may replay. An unrecognised value is
 * treated as untrusted input and collapsed, so a corrupted record cannot inject a new code
 * into the API surface.
 */
const REPLAYABLE_REASON_CODES = new Set([
  "invalid_request",
  "custom_mix_disabled",
  "invalid_proof",
  "unsupported_environment",
  "wrong_bundle",
  "wrong_product",
  "wrong_product_type",
  "wrong_ownership",
  "revoked",
]);

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

function assertRealWiring(
  config: RuntimeConfig,
  dependencies: RuntimeDependencies,
  requireLiveVerifier: boolean,
): void {
  if (!isRealDeployment(config)) return; // development composes its own stack explicitly
  if (config.customMix.enabled !== dependencies.killSwitch.customMixEnabled) {
    throw new RuntimeCompositionError("kill_switch_disagrees_with_config");
  }
  if (dependencies.environment !== config.apple.environment) {
    throw new RuntimeCompositionError("environment_mismatch");
  }
  if (!(dependencies.revocations instanceof PersistentRevocationStore)) {
    throw new RuntimeCompositionError("persistent_revocation_store_required");
  }
  if (!(dependencies.idempotency instanceof PersistentIdempotencyStore)) {
    throw new RuntimeCompositionError("persistent_idempotency_store_required");
  }
  for (const limiter of [
    dependencies.ipLimiter,
    dependencies.subjectLimiter,
    dependencies.editionLimiter,
  ]) {
    if (!(limiter instanceof PersistentRateLimiter)) {
      throw new RuntimeCompositionError("persistent_rate_limiter_required");
    }
  }
  if (requireLiveVerifier && !(dependencies.verifier instanceof LiveAppleEntitlementVerifier)) {
    // A token must never be issued from a signed transaction alone.
    throw new RuntimeCompositionError("live_current_state_required");
  }
}

type ExchangeProbe = {
  signedTransactionInfo: string;
  selectorVersion: unknown;
  appVersion: unknown;
};

/**
 * Peek at the body WITHOUT consuming it, purely to derive a retry key.
 * Anything unexpected returns null and the request is passed straight through, so the
 * route keeps sole authority over what a valid request is.
 */
async function peekExchangeBody(request: Request): Promise<ExchangeProbe | null> {
  try {
    const text = await request.clone().text();
    if (
      Buffer.byteLength(text, "utf8") === 0 ||
      Buffer.byteLength(text, "utf8") > MAX_BODY_BYTES
    ) {
      return null;
    }
    const body: unknown = JSON.parse(text);
    if (typeof body !== "object" || body === null || Array.isArray(body)) return null;
    const record = body as Record<string, unknown>;
    if (
      typeof record.signedTransactionInfo !== "string" ||
      record.signedTransactionInfo.length === 0
    ) {
      return null;
    }
    return {
      signedTransactionInfo: record.signedTransactionInfo,
      selectorVersion: record.selectorVersion,
      appVersion: record.appVersion,
    };
  } catch {
    return null;
  }
}

/**
 * Retry key and request fingerprint.
 *
 * Both are HMAC-derived before they leave this function, so the raw JWS never reaches the
 * store, the Redis key space, or any log. The KEY covers the proof alone — a genuine retry
 * of the same purchase. The FINGERPRINT additionally covers the request metadata, so the
 * same proof replayed with contradictory metadata is a conflict rather than a retry.
 */
function retryIdentity(
  probe: ExchangeProbe,
  environment: string,
): { key: string; fingerprint: string } {
  const proof = `${environment}:${probe.signedTransactionInfo}`;
  return {
    key: deriveKeyComponent(`signals:exchange:key:${proof}`),
    fingerprint: deriveKeyComponent(
      `signals:exchange:fingerprint:${proof}:${String(probe.selectorVersion)}:${
        probe.appVersion === undefined ? "" : String(probe.appVersion)
      }`,
    ),
  };
}

/** Read the error code from a non-2xx route response. Never touches a 2xx body. */
async function reasonCodeOf(response: Response): Promise<string> {
  if (response.status < 400) return "ok";
  try {
    const body = (await response.clone().json()) as {
      error?: { code?: unknown };
      code?: unknown;
    };
    const code = body.error?.code ?? body.code;
    return typeof code === "string" ? code : "invalid_request";
  } catch {
    return "invalid_request";
  }
}

/** Should a stored outcome be replayed, or must the condition be re-evaluated? */
function isReplayable(record: IdempotencyRecord): boolean {
  const status = Number(record.result?.status ?? 0);
  const reasonCode = String(record.result?.reasonCode ?? "");
  return (
    record.state === "completed" &&
    status >= 400 &&
    status < 500 &&
    status !== 429 && // transient: the window may have moved on
    REPLAYABLE_REASON_CODES.has(reasonCode)
  );
}

/**
 * The authenticated exchange route, wired to real Apple verification, a live current-state
 * check, persistent limits, persistent revocation state, persistent idempotency and the
 * Signals token issuer.
 */
export function createProductionExchangeHandler(
  config: RuntimeConfig,
  dependencies: RuntimeDependencies,
): RouteHandler {
  assertRealWiring(config, dependencies, true);

  const inner = createAuthExchangeHandler(
    {
      enabled: config.customMix.enabled,
      environment: dependencies.environment,
    },
    {
      verifier: dependencies.verifier,
      tokens: dependencies.tokens,
      revocations: dependencies.revocations,
      ipLimiter: dependencies.ipLimiter,
      subjectLimiter: dependencies.subjectLimiter,
      logger: dependencies.logger,
      clock: dependencies.clock,
      requestId: dependencies.requestId,
    },
  );

  const logShortCircuit = (
    response: Response,
    reasonCode: string,
    startedAt: number,
  ): Response => {
    const event: SecurityLogEvent = {
      route: EXCHANGE_ROUTE,
      status: response.status,
      reasonCode,
      latencyMs: Math.max(0, dependencies.clock.nowMs() - startedAt),
      requestId: dependencies.requestId(),
      selectorVersion: config.customMix.selectorVersion,
      environment: dependencies.environment,
    };
    dependencies.logger.log(event);
    return response;
  };

  return async (request: Request): Promise<Response> => {
    // The route owns method and kill-switch handling; do not duplicate its answers.
    if (request.method !== "POST" || !config.customMix.enabled) {
      return inner(request);
    }

    const probe = await peekExchangeBody(request);
    if (!probe) return inner(request); // malformed — let the route return its 400/413

    const startedAt = dependencies.clock.nowMs();
    const { key, fingerprint } = retryIdentity(probe, dependencies.environment);

    let claim;
    try {
      claim = await dependencies.idempotency.claim(key, fingerprint, startedAt);
    } catch (error) {
      if (error instanceof IdempotencyConflictError) {
        // Same proof, contradictory metadata. Not a retry — a bad request.
        return logShortCircuit(
          errorResponse(400, "invalid_request"),
          "invalid_request",
          startedAt,
        );
      }
      // Fail closed: without retry-safety the exchange is not performed at all.
      return logShortCircuit(
        errorResponse(503, "verification_unavailable"),
        "verification_unavailable",
        startedAt,
      );
    }

    if (!claim.claimed) {
      if (claim.record.state === "in_progress") {
        // A duplicate is in flight. Fail closed and let the client come back.
        return logShortCircuit(
          errorResponse(503, "verification_unavailable", 1),
          "verification_unavailable",
          startedAt,
        );
      }
      if (isReplayable(claim.record)) {
        const status = Number(claim.record.result?.status);
        const reasonCode = String(claim.record.result?.reasonCode);
        return logShortCircuit(errorResponse(status, reasonCode), reasonCode, startedAt);
      }
      // A previously SUCCESSFUL exchange cannot be replayed byte-for-byte, because the
      // token was deliberately never stored. Re-running produces an equivalent result:
      // the same subject, environment and scope, with a fresh lifetime.
      return inner(request);
    }

    const response = await inner(request);

    // Bounded, non-sensitive metadata only: a status and a reason code. Never a token,
    // never the JWS, never an Apple identifier.
    try {
      await dependencies.idempotency.complete(
        key,
        { status: response.status, reasonCode: await reasonCodeOf(response) },
        dependencies.clock.nowMs(),
      );
    } catch {
      // Fail closed: if the outcome cannot be recorded, a retry could duplicate the work.
      return logShortCircuit(
        errorResponse(503, "verification_unavailable"),
        "verification_unavailable",
        startedAt,
      );
    }

    return response;
  };
}

/**
 * The authenticated edition route: Signals token verification, persistent per-token rate
 * limit, persistent revocation check and contract validation — stopping, by design, at
 * `503 selector_not_connected`.
 */
export function createProductionEditionHandler(
  config: RuntimeConfig,
  dependencies: RuntimeDependencies,
): RouteHandler {
  assertRealWiring(config, dependencies, false);

  return createEditionHandler(
    {
      enabled: config.customMix.enabled,
      environment: dependencies.environment,
    },
    {
      tokens: dependencies.tokens,
      revocations: dependencies.revocations,
      limiter: dependencies.editionLimiter,
      logger: dependencies.logger,
      clock: dependencies.clock,
      requestId: dependencies.requestId,
      // Custom Mix stays unreachable: this orchestrator has no candidate source, so every
      // request resolves to `standard_candidates_unavailable` and the route answers
      // `503 selector_not_connected` exactly as before.
      orchestrator: createDisconnectedEditionOrchestrator(config.customMix.enabled),
    },
  );
}
