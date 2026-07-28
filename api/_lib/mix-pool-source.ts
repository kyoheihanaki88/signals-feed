/**
 * Candidate-pool retrieval boundary. (Phase 3D-3B)
 *
 * NOT PRODUCTION-CONNECTED. No storage provider is provisioned in this repository:
 * `KV_REST_API_URL`/`KV_REST_API_TOKEN` appear only in `api/` source and tests — never in a
 * workflow, `vercel.json` or an env file — GitHub Actions holds no KV write credential, and
 * the only object storage is the PUBLIC audio R2 bucket. So this module defines the
 * provider-neutral contract and every safety check, and stops there. Nothing here is
 * imported by `/api/edition`, the orchestrator or the runtime factory.
 *
 * WHAT THIS BOUNDARY GUARANTEES once a provider exists:
 *   • exact-UTC-date retrieval, with no silent previous-day fallback
 *   • a bounded read: timeout, byte cap, empty-response rejection
 *   • full schema validation via `mix-pool-schema.ts`, including the numeric contract
 *   • `poolIdentity` re-derived from the candidates and compared
 *   • the artifact's OWN date compared against the requested date
 *   • freshness bounds, including a clock-skew ceiling on `generatedAt`
 *   • deep-frozen candidates, so a caller cannot mutate the validated pool
 *   • stable internal reason codes; a raw provider message never escapes
 *
 * TRUST MODEL. `poolIdentity` detects CORRUPTION — content that no longer matches the
 * envelope. It does NOT prove authorship: anyone able to write the key could also write a
 * matching identity. That is acceptable only if the store is private and both the workflow
 * write path and the API read path are authenticated. If the eventual store allows
 * untrusted writes, an HMAC signature over the canonical bytes is required, keyed from the
 * existing secret-management conventions and never exposed to a client.
 *
 * Pure: no filesystem, no environment reads, no logging. The caller receives a structured
 * result and decides what to log.
 */

import {
  MIX_POOL_SCHEMA_VERSION,
  canonicalMixPoolBytes,
  mixPoolIdentity,
  parseMixPoolArtifact,
  type JsonValue,
} from "./mix-pool-schema.js";
import type { MixCandidate } from "./custom-mix-types.js";

/** Namespace version travels in the key so a schema bump cannot collide with old data. */
export const MIX_POOL_KEY_NAMESPACE = "signals:mix-pool";
export const MIX_POOL_KEY_VERSION = "v1";

/** Operational bounds. All durations are milliseconds; all dates are UTC. */
export const MAX_POOL_BYTES = 2 * 1_024 * 1_024;
export const READ_TIMEOUT_MS = 2_000;
/** A pool generated further ahead than this is a clock or key error, not a fresh pool. */
export const MAX_FUTURE_SKEW_MS = 10 * 60 * 1_000;
/** Matches the edition retention the app can meaningfully open late. */
export const MAX_POOL_AGE_MS = 8 * 24 * 60 * 60 * 1_000;
/** Suggested provider TTL. Slightly beyond MAX_POOL_AGE_MS so expiry is never the gate. */
export const POOL_TTL_SECONDS = 9 * 24 * 60 * 60;

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

export type MixPoolReasonCode =
  | "candidate_pool_not_configured"
  | "candidate_pool_missing"
  | "candidate_pool_timeout"
  | "candidate_pool_provider_error"
  | "candidate_pool_too_large"
  | "candidate_pool_empty"
  | "candidate_pool_invalid_json"
  | "candidate_pool_schema_invalid"
  | "candidate_pool_version_incompatible"
  | "candidate_pool_identity_mismatch"
  | "candidate_pool_date_mismatch"
  | "candidate_pool_stale"
  | "candidate_pool_future_timestamp"
  | "candidate_pool_invalid_date";

export type MixPoolReadResult =
  | {
      ok: true;
      candidates: readonly MixCandidate[];
      metadata: {
        date: string;
        key: string;
        schemaVersion: number;
        selectorVersion: number;
        candidateCount: number;
        byteLength: number;
        /** A short prefix only — never the whole identity, which is a content fingerprint. */
        poolIdentityPrefix: string;
      };
    }
  | { ok: false; reason: MixPoolReasonCode };

/**
 * The provider seam. An implementation returns the stored bytes for a key, or null when
 * the key is absent. It must never throw for "not found" — that is a normal outcome.
 */
export interface PoolObjectStore {
  get(key: string, options: { timeoutMs: number; maxBytes: number }): Promise<Uint8Array | null>;
}

/**
 * Deterministic key construction, shared with the Python publisher.
 * `signals:mix-pool:v1:YYYY-MM-DD` — UTC date only, no identity, no preferences.
 */
export function mixPoolKey(date: string): string {
  if (!DATE_RE.test(date)) throw new Error("mix pool date must be YYYY-MM-DD");
  return `${MIX_POOL_KEY_NAMESPACE}:${MIX_POOL_KEY_VERSION}:${date}`;
}

/** True only for a real UTC calendar date. Never consults the local timezone. */
export function isValidUtcDate(date: string): boolean {
  if (!DATE_RE.test(date)) return false;
  const [year, month, day] = date.split("-").map(Number);
  const probe = new Date(Date.UTC(year, month - 1, day));
  return (
    probe.getUTCFullYear() === year &&
    probe.getUTCMonth() === month - 1 &&
    probe.getUTCDate() === day
  );
}

/** Recursively freeze so a consumer cannot mutate the validated pool. */
function deepFreeze<T>(value: T): T {
  if (value !== null && typeof value === "object") {
    for (const child of Object.values(value as Record<string, unknown>)) deepFreeze(child);
    Object.freeze(value);
  }
  return value;
}

export type MixPoolReaderOptions = {
  store: PoolObjectStore | null;
  /** Injected so freshness is testable without a real clock. */
  now?: () => number;
  maxBytes?: number;
  timeoutMs?: number;
  maxAgeMs?: number;
  maxFutureSkewMs?: number;
};

/**
 * Read, validate and return the candidate pool for an exact UTC date.
 *
 * Every failure is a reason code. A raw provider message, URL, token or artifact fragment
 * never escapes — the caller gets a code and safe metadata, nothing else.
 */
export async function readMixPool(
  date: string,
  options: MixPoolReaderOptions,
): Promise<MixPoolReadResult> {
  if (!options.store) return { ok: false, reason: "candidate_pool_not_configured" };
  if (!isValidUtcDate(date)) return { ok: false, reason: "candidate_pool_invalid_date" };

  const maxBytes = options.maxBytes ?? MAX_POOL_BYTES;
  const key = mixPoolKey(date);

  let raw: Uint8Array | null;
  try {
    raw = await options.store.get(key, {
      timeoutMs: options.timeoutMs ?? READ_TIMEOUT_MS,
      maxBytes,
    });
  } catch (error) {
    // Distinguish only the categories the caller can act on; never surface the message.
    const name = error instanceof Error ? error.name : "";
    if (name === "AbortError" || name === "TimeoutError") {
      return { ok: false, reason: "candidate_pool_timeout" };
    }
    return { ok: false, reason: "candidate_pool_provider_error" };
  }

  if (raw === null) return { ok: false, reason: "candidate_pool_missing" };
  if (raw.byteLength === 0) return { ok: false, reason: "candidate_pool_empty" };
  if (raw.byteLength > maxBytes) return { ok: false, reason: "candidate_pool_too_large" };

  let text: string;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(raw);
  } catch {
    return { ok: false, reason: "candidate_pool_invalid_json" };
  }

  const parsed = parseMixPoolArtifact(text);
  if (!parsed.ok) {
    // `parseMixPoolArtifact` reports malformed JSON with exactly this message.
    if (parsed.errors[0] === "artifact is not valid JSON") {
      return { ok: false, reason: "candidate_pool_invalid_json" };
    }
    if (parsed.errors.some((e) => e.includes("unsupported schemaVersion") ||
                                  e.includes("unsupported selectorVersion") ||
                                  e.includes("provenance.generatorVersion"))) {
      return { ok: false, reason: "candidate_pool_version_incompatible" };
    }
    if (parsed.errors.some((e) => e.includes("poolIdentity"))) {
      return { ok: false, reason: "candidate_pool_identity_mismatch" };
    }
    return { ok: false, reason: "candidate_pool_schema_invalid" };
  }

  const artifact = parsed.artifact as Record<string, JsonValue>;

  // Re-derive the identity rather than trusting the stored value alone.
  if (mixPoolIdentity(artifact.candidates as JsonValue[]) !== artifact.poolIdentity) {
    return { ok: false, reason: "candidate_pool_identity_mismatch" };
  }
  // The artifact proves its own date; the key alone is not evidence.
  if (artifact.date !== date) return { ok: false, reason: "candidate_pool_date_mismatch" };
  if (artifact.schemaVersion !== MIX_POOL_SCHEMA_VERSION) {
    return { ok: false, reason: "candidate_pool_version_incompatible" };
  }

  const generatedAt = Date.parse(String(artifact.generatedAt));
  if (!Number.isFinite(generatedAt)) {
    return { ok: false, reason: "candidate_pool_schema_invalid" };
  }
  const now = (options.now ?? (() => Date.now()))();
  if (generatedAt - now > (options.maxFutureSkewMs ?? MAX_FUTURE_SKEW_MS)) {
    return { ok: false, reason: "candidate_pool_future_timestamp" };
  }
  if (now - generatedAt > (options.maxAgeMs ?? MAX_POOL_AGE_MS)) {
    return { ok: false, reason: "candidate_pool_stale" };
  }

  const candidates = deepFreeze(
    (artifact.candidates as unknown as MixCandidate[]).slice(),
  ) as readonly MixCandidate[];

  return {
    ok: true,
    candidates,
    metadata: {
      date,
      key,
      schemaVersion: artifact.schemaVersion as number,
      selectorVersion: artifact.selectorVersion as number,
      candidateCount: artifact.candidateCount as number,
      byteLength: raw.byteLength,
      poolIdentityPrefix: String(artifact.poolIdentity).slice(0, 12),
    },
  };
}

/**
 * Adapt the reader to the orchestrator's `MixCandidateSource` shape.
 *
 * Returning `null` for every failure is deliberate: the orchestrator already treats a null
 * pool as `standard_candidates_unavailable`, so a storage problem degrades to the existing
 * disconnected behaviour instead of surfacing a new error class. `onResult` lets a caller
 * observe the reason code without this module logging anything itself.
 */
export function createMixPoolCandidateSource(
  options: MixPoolReaderOptions & {
    onResult?: (result: MixPoolReadResult) => void;
  },
): { loadCandidates(date: string): Promise<MixCandidate[] | null> } {
  return {
    async loadCandidates(date: string): Promise<MixCandidate[] | null> {
      const result = await readMixPool(date, options);
      options.onResult?.(result);
      return result.ok ? (result.candidates as MixCandidate[]) : null;
    },
  };
}

/** Canonical bytes for a pool, exposed so a publisher check can be mirrored in tests. */
export function poolCanonicalBytes(artifact: JsonValue): Buffer {
  return canonicalMixPoolBytes(artifact);
}
