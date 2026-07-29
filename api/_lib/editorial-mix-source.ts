/**
 * Editorial Mix Pool retrieval boundary. (Phase 3E-1)
 *
 * The sibling of `mix-pool-source.ts`. That module reads the RAW selector pool at
 * `signals:mix-pool:v1:<date>`, which the daily workflow never publishes; this one reads
 * the ENRICHED pool at `signals:editorial-mix-pool:v1:<date>`, which it does — the exact
 * key produced by `pipeline/editorial_mix_pool_publisher.py`.
 *
 * READ-ONLY BY CONSTRUCTION. The store handed in here is built from `KV_REST_API_TOKEN`,
 * the read-only credential. `KV_REST_API_WRITE_TOKEN` is a publisher secret that exists
 * only in GitHub Actions; it is never read by any API module, and a test asserts the name
 * appears nowhere in the request path.
 *
 * WHAT THIS GUARANTEES before a candidate reaches the selector:
 *   • exact-UTC-date retrieval, with no silent previous-day fallback
 *   • a bounded read: timeout, byte cap, empty-response rejection
 *   • full schema validation via `editorial-mix-pool-schema.ts`
 *   • BOTH identities re-derived from the candidates and compared
 *   • the artifact's OWN date compared against the requested date
 *   • freshness bounds, including a clock-skew ceiling on `generatedAt`
 *   • the surviving candidate count within [MINIMUM_PUBLISHABLE_POOL_SIZE, TARGET_POOL_SIZE]
 *   • deep-frozen candidates, so nothing downstream can mutate or reorder the stored pool
 *   • stable internal reason codes; a raw provider message never escapes
 *
 * Pure: no filesystem, no environment read, no logging, no clock beyond the injected `now`.
 * The caller receives a structured result and decides what to log and what to return.
 */

import {
  MAX_POOL_AGE_MS,
  MAX_FUTURE_SKEW_MS,
  MAX_POOL_BYTES,
  READ_TIMEOUT_MS,
  isValidUtcDate,
  type MixPoolReasonCode,
  type PoolObjectStore,
} from "./mix-pool-source.js";
import {
  EDITORIAL_ARTIFACT_TYPE,
  EDITORIAL_SCHEMA_VERSION,
  MINIMUM_PUBLISHABLE_POOL_SIZE,
  TARGET_POOL_SIZE,
  editorialPoolIdentityOf,
  extractSelectorCandidates,
  selectorPoolIdentityOf,
  validateEditorialMixPool,
  type EnrichedCandidate,
} from "./editorial-mix-pool-schema.js";
import type { JsonValue } from "./mix-pool-schema.js";
import type { MixCandidate } from "./custom-mix-types.js";

/** Shared with `pipeline/editorial_mix_pool_publisher.py`. Both sides must agree exactly. */
export const EDITORIAL_KEY_NAMESPACE = "signals:editorial-mix-pool";
export const EDITORIAL_KEY_VERSION = "v1";

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

export type EditorialPoolReadResult =
  | {
      ok: true;
      /** The selector rows, in stored order. Fed to `selectCustomMix` unchanged. */
      candidates: readonly MixCandidate[];
      /** The full enriched rows, so the chosen five can be assembled into a feed. */
      enriched: readonly EnrichedCandidate[];
      metadata: {
        date: string;
        key: string;
        schemaVersion: number;
        selectorVersion: number;
        editorialVersion: number;
        candidateCount: number;
        byteLength: number;
        /** Prefixes only — a whole identity is a content fingerprint. */
        selectorPoolIdentityPrefix: string;
        editorialPoolIdentityPrefix: string;
      };
    }
  | { ok: false; reason: MixPoolReasonCode };

/**
 * `signals:editorial-mix-pool:v1:YYYY-MM-DD`.
 *
 * UTC date only — no subject, no identity, no preferences. Two users with identical
 * preferences read the same key, which is what makes the selection cacheable and the
 * output deterministic.
 */
export function editorialMixPoolKey(date: string): string {
  if (!DATE_RE.test(date)) throw new Error("editorial mix pool date must be YYYY-MM-DD");
  return `${EDITORIAL_KEY_NAMESPACE}:${EDITORIAL_KEY_VERSION}:${date}`;
}

/** Recursively freeze so nothing downstream can mutate or reorder the stored pool. */
function deepFreeze<T>(value: T): T {
  if (value !== null && typeof value === "object") {
    for (const child of Object.values(value as Record<string, unknown>)) deepFreeze(child);
    Object.freeze(value);
  }
  return value;
}

export type EditorialPoolReaderOptions = {
  store: PoolObjectStore | null;
  /** Injected so freshness is testable without a real clock. */
  now?: () => number;
  maxBytes?: number;
  timeoutMs?: number;
  maxAgeMs?: number;
  maxFutureSkewMs?: number;
};

/**
 * Read, validate and return the Editorial Mix Pool for an exact UTC date.
 *
 * Every failure is a reason code from the existing provider-neutral vocabulary. A raw
 * provider message, URL, token or artifact fragment never escapes — the caller gets a code
 * and safe metadata, nothing else.
 */
export async function readEditorialMixPool(
  date: string,
  options: EditorialPoolReaderOptions,
): Promise<EditorialPoolReadResult> {
  if (!options.store) return { ok: false, reason: "candidate_pool_not_configured" };
  if (!isValidUtcDate(date)) return { ok: false, reason: "candidate_pool_invalid_date" };

  const maxBytes = options.maxBytes ?? MAX_POOL_BYTES;
  const key = editorialMixPoolKey(date);

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

  let artifact: unknown;
  try {
    artifact = JSON.parse(text);
  } catch {
    return { ok: false, reason: "candidate_pool_invalid_json" };
  }

  const validation = validateEditorialMixPool(artifact);
  if (!validation.valid) {
    if (
      validation.errors.some(
        (e) =>
          e.includes("schemaVersion") ||
          e.includes("selectorVersion") ||
          e.includes("editorialVersion") ||
          e.includes("artifactType") ||
          e.includes("generatorVersion"),
      )
    ) {
      return { ok: false, reason: "candidate_pool_version_incompatible" };
    }
    if (validation.errors.some((e) => e.includes("Identity") || e.includes("identity"))) {
      return { ok: false, reason: "candidate_pool_identity_mismatch" };
    }
    return { ok: false, reason: "candidate_pool_schema_invalid" };
  }

  const record = artifact as Record<string, JsonValue>;
  if (record.artifactType !== EDITORIAL_ARTIFACT_TYPE) {
    return { ok: false, reason: "candidate_pool_version_incompatible" };
  }
  if (record.schemaVersion !== EDITORIAL_SCHEMA_VERSION) {
    return { ok: false, reason: "candidate_pool_version_incompatible" };
  }

  const candidatesRaw = record.candidates;
  if (!Array.isArray(candidatesRaw)) {
    return { ok: false, reason: "candidate_pool_schema_invalid" };
  }
  const enriched = candidatesRaw as unknown as EnrichedCandidate[];

  // Re-derive BOTH identities rather than trusting the stored values alone. This detects
  // corruption; it does not prove authorship, which is why the store must stay private.
  if (selectorPoolIdentityOf(artifact) !== record.selectorPoolIdentity) {
    return { ok: false, reason: "candidate_pool_identity_mismatch" };
  }
  if (editorialPoolIdentityOf(candidatesRaw as JsonValue[]) !== record.editorialPoolIdentity) {
    return { ok: false, reason: "candidate_pool_identity_mismatch" };
  }

  // The artifact must prove its own date; the key alone is not evidence.
  if (record.date !== date) return { ok: false, reason: "candidate_pool_date_mismatch" };

  // The product contract, enforced on the way IN as well as on the way out.
  if (enriched.length < MINIMUM_PUBLISHABLE_POOL_SIZE) {
    return { ok: false, reason: "candidate_pool_empty" };
  }
  if (enriched.length > TARGET_POOL_SIZE) {
    return { ok: false, reason: "candidate_pool_schema_invalid" };
  }

  const generatedAt = Date.parse(String(record.generatedAt));
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

  const frozen = deepFreeze(enriched.slice()) as readonly EnrichedCandidate[];
  const selectorRows = extractSelectorCandidates(artifact) as unknown as MixCandidate[];

  return {
    ok: true,
    candidates: deepFreeze(selectorRows) as readonly MixCandidate[],
    enriched: frozen,
    metadata: {
      date,
      key,
      schemaVersion: record.schemaVersion as number,
      selectorVersion: record.selectorVersion as number,
      editorialVersion: record.editorialVersion as number,
      candidateCount: enriched.length,
      byteLength: raw.byteLength,
      selectorPoolIdentityPrefix: String(record.selectorPoolIdentity).slice(0, 12),
      editorialPoolIdentityPrefix: String(record.editorialPoolIdentity).slice(0, 12),
    },
  };
}

/**
 * The orchestrator's candidate source, backed by the Editorial Mix Pool.
 *
 * Returning `null` for every failure keeps the orchestrator's existing shape: it already
 * treats a null pool as "no Custom Mix today" and falls back. `onResult` lets the caller
 * observe the precise reason code without this module logging anything itself.
 */
export function createEditorialCandidateSource(
  options: EditorialPoolReaderOptions & {
    onResult?: (result: EditorialPoolReadResult) => void;
  },
): {
  loadCandidates(
    date: string,
  ): Promise<{ candidates: MixCandidate[]; enriched: EnrichedCandidate[] } | null>;
} {
  return {
    async loadCandidates(date: string) {
      const result = await readEditorialMixPool(date, options);
      options.onResult?.(result);
      if (!result.ok) return null;
      return {
        candidates: result.candidates as MixCandidate[],
        enriched: result.enriched as EnrichedCandidate[],
      };
    },
  };
}
