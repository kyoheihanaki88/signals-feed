/**
 * Concrete Upstash Redis REST implementation of `PoolObjectStore`. (Phase 3D-3D)
 *
 * NOT PROVISIONED, NOT CONNECTED. No Upstash database exists for this project yet and no
 * credential is present in Vercel, GitHub Actions, `vercel.json` or any env file. This
 * module is the code that becomes operational the moment `KV_REST_API_URL` and
 * `KV_REST_API_TOKEN` are supplied by the caller — and nothing more. It is deliberately
 * not imported by `/api/edition`, the edition orchestrator, the runtime factory or the
 * Vercel runtime, and `api/_tests/runtime-dependencies.test.ts` enforces that.
 *
 * REQUEST FORM — CORRECTED IN PHASE 3D-3F.1 AGAINST REAL ENDPOINT EVIDENCE.
 *
 *   root POST, body `["PING"]`  ->  HTTP 400
 *   URL command  GET /ping      ->  HTTP 200  {"result":"PONG"}
 *
 * Phase 3D-3D used the root JSON-array form. Upstash documents it, but THIS deployment
 * rejects it, so the reader now uses the URL-command form exclusively: `GET {base}/get/{key}`.
 * That is not a fallback — the root form is never constructed here, and a regression test
 * asserts it is absent from the source.
 *
 * KEY ENCODING. `encodePathSegment` percent-encodes the key as one path segment and leaves
 * `:` literal, exactly matching `encode_segment` in `pipeline/upstash_mix_pool_transport.py`.
 * A cross-language test pins the two together, because a divergence would make this reader
 * request a different object than the publisher wrote — silently, with a valid-looking
 * `result: null` rather than an error.
 *
 * BYTE EXACTNESS. Upstash answers with a JSON envelope, `{"result": "<the stored string>"}`.
 * The stored artifact is canonical UTF-8 JSON, so it survives the envelope's string
 * escaping unchanged: parse the envelope, take `result` as a JavaScript string, encode it
 * as UTF-8, and the bytes are the publisher's bytes. This module NEVER re-serialises the
 * artifact — it does not parse, sort, normalise or re-stringify the stored JSON, because
 * doing so would change the canonical bytes and break `poolIdentity`.
 *
 * WHAT IT DOES NOT DO: no logging, no filesystem, no `process.env` read, no module-global
 * mutable cache, no retry (the caller owns the request budget), no dependency — the
 * platform `fetch` is sufficient. Every failure is a stable internal reason code; a raw
 * Upstash message, the REST URL and the token never appear in anything this module returns
 * or throws.
 */

import {
  MAX_POOL_BYTES,
  READ_TIMEOUT_MS,
  readMixPool,
  type MixPoolReadResult,
  type MixPoolReasonCode,
  type PoolObjectStore,
} from "./mix-pool-source.js";
import type { MixCandidate } from "./custom-mix-types.js";

/** Stable, provider-specific reason codes. Mapped to the neutral codes at the boundary. */
export type UpstashReasonCode =
  | "upstash_not_configured"
  | "upstash_partial_configuration"
  | "upstash_insecure_url"
  | "upstash_timeout"
  | "upstash_provider_error"
  | "upstash_invalid_response"
  | "upstash_missing_key"
  | "upstash_non_string_value"
  | "upstash_value_too_large";

/** The credential pair the API read path needs. Read-only use; never a write token. */
export type UpstashCredentials = {
  restUrl: string;
  restToken: string;
};

/** Only the two variables the READ path may consume. A write token has no place here. */
export type UpstashEnvSource = {
  KV_REST_API_URL?: string | undefined;
  KV_REST_API_TOKEN?: string | undefined;
};

export type UpstashConfigResult =
  | { ok: true; credentials: UpstashCredentials }
  | { ok: false; reason: "upstash_not_configured" | "upstash_partial_configuration" | "upstash_insecure_url" };

/**
 * A failure that reached the transport. Carries a code only — never a provider message,
 * a URL, a token or a fragment of the stored artifact.
 */
export class UpstashStoreError extends Error {
  readonly reason: UpstashReasonCode;

  constructor(reason: UpstashReasonCode) {
    super(reason);
    this.name = "UpstashStoreError";
    this.reason = reason;
  }
}

/**
 * Translate a provider code into the provider-neutral candidate-pool vocabulary. This is
 * the ONLY place the two vocabularies meet, so the rest of the system never learns which
 * provider is behind the seam.
 */
export function mapUpstashReason(reason: UpstashReasonCode): MixPoolReasonCode {
  switch (reason) {
    case "upstash_not_configured":
    case "upstash_partial_configuration":
    case "upstash_insecure_url":
      return "candidate_pool_not_configured";
    case "upstash_missing_key":
      return "candidate_pool_missing";
    case "upstash_timeout":
      return "candidate_pool_timeout";
    case "upstash_value_too_large":
      return "candidate_pool_too_large";
    case "upstash_invalid_response":
    case "upstash_non_string_value":
    case "upstash_provider_error":
      return "candidate_pool_provider_error";
  }
}

/**
 * Validate the credential pair. Fails CLOSED and distinguishes the three states the
 * operator needs to tell apart: nothing set, half set, and set but unusable.
 *
 * `env` is an ordinary object passed in by the caller — this module never reads
 * `process.env` itself, so importing it can never couple a runtime to a credential.
 */
export function resolveUpstashCredentials(env: UpstashEnvSource): UpstashConfigResult {
  const restUrl = typeof env.KV_REST_API_URL === "string" ? env.KV_REST_API_URL.trim() : "";
  const restToken = typeof env.KV_REST_API_TOKEN === "string" ? env.KV_REST_API_TOKEN.trim() : "";

  if (!restUrl && !restToken) return { ok: false, reason: "upstash_not_configured" };
  if (!restUrl || !restToken) return { ok: false, reason: "upstash_partial_configuration" };

  let parsed: URL;
  try {
    parsed = new URL(restUrl);
  } catch {
    return { ok: false, reason: "upstash_insecure_url" };
  }
  // Plaintext would put a bearer token on the wire. Refuse rather than downgrade.
  if (parsed.protocol !== "https:") return { ok: false, reason: "upstash_insecure_url" };
  if (parsed.search || parsed.hash) return { ok: false, reason: "upstash_insecure_url" };

  // Normalise to an origin + path prefix with no trailing slash, so command URLs are exact.
  const normalised = `${parsed.origin}${parsed.pathname.replace(/\/+$/, "")}`;
  return { ok: true, credentials: { restUrl: normalised, restToken } };
}

export type UpstashStoreOptions = {
  /** Injected so tests never touch a network. Defaults to the platform `fetch`. */
  fetchImpl?: typeof fetch;
  /** Hard ceiling on the decoded artifact. Defaults to the reader's own cap. */
  maxBytes?: number;
  /** Applies to the whole request, including the body read. */
  timeoutMs?: number;
};

/**
 * The JSON envelope adds escaping and framing on top of the artifact. This multiplier is
 * the worst realistic case (every character escaped as `\uXXXX`) plus framing slack, so an
 * oversized RESPONSE is refused while it streams, before it is ever fully buffered.
 */
const ENVELOPE_OVERHEAD_FACTOR = 6;
const ENVELOPE_FRAMING_BYTES = 1_024;

/**
 * Percent-encode ONE Redis argument as its own URL path segment.
 *
 * `encodeURIComponent` escapes `:` as `%3A`; Python's `quote(arg, safe=":")` does not.
 * Both are valid URLs, but they are DIFFERENT request paths, and only one of them matches
 * the key the publisher wrote. RFC 3986 admits `:` inside a path segment and Upstash's own
 * documentation uses it unencoded, so the literal form wins and this function restores it.
 * Everything else that could redirect the request — `/`, `?`, `#`, space, non-ASCII — stays
 * escaped.
 */
export function encodePathSegment(argument: string): string {
  return encodeURIComponent(argument).replace(/%3A/g, ":");
}

/** Build a URL-command target: `{base}/arg1/arg2/…`. The only URL builder in this module. */
export function commandUrl(base: string, ...args: readonly string[]): string {
  return `${base}/${args.map(encodePathSegment).join("/")}`;
}

function envelopeCeiling(maxBytes: number): number {
  return maxBytes * ENVELOPE_OVERHEAD_FACTOR + ENVELOPE_FRAMING_BYTES;
}

/**
 * Read the body with a hard byte cap, aborting as soon as the cap is passed.
 * Returns `null` when the cap is exceeded, so an enormous response can never be buffered.
 */
async function readCappedText(response: Response, cap: number): Promise<string | null> {
  const declared = response.headers.get("content-length");
  if (declared !== null) {
    const length = Number(declared);
    // A declared length beyond the cap is refused before a single body byte is read.
    if (Number.isFinite(length) && length > cap) return null;
  }

  const body = response.body;
  if (!body) {
    const text = await response.text();
    return new TextEncoder().encode(text).byteLength > cap ? null : text;
  }

  const reader = body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      if (value) {
        total += value.byteLength;
        if (total > cap) {
          await reader.cancel();
          return null;
        }
        chunks.push(value);
      }
    }
  } finally {
    reader.releaseLock();
  }

  const joined = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    joined.set(chunk, offset);
    offset += chunk.byteLength;
  }
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(joined);
  } catch {
    return null;
  }
}

/**
 * Build a `PoolObjectStore` backed by Upstash REST.
 *
 * Returns a RESULT rather than throwing, so an unconfigured deployment is an ordinary
 * outcome the caller reports as `candidate_pool_not_configured` — not a startup crash.
 * The API needs read credentials only; nothing here can write.
 */
export function createUpstashPoolStore(
  env: UpstashEnvSource,
  options: UpstashStoreOptions = {},
):
  | { ok: true; store: PoolObjectStore }
  | { ok: false; reason: "upstash_not_configured" | "upstash_partial_configuration" | "upstash_insecure_url" } {
  const resolved = resolveUpstashCredentials(env);
  if (!resolved.ok) return resolved;

  const { restUrl, restToken } = resolved.credentials;
  const doFetch = options.fetchImpl ?? fetch;
  const defaultMaxBytes = options.maxBytes ?? MAX_POOL_BYTES;
  const defaultTimeoutMs = options.timeoutMs ?? READ_TIMEOUT_MS;

  const store: PoolObjectStore = {
    async get(key, callOptions) {
      const maxBytes = callOptions?.maxBytes ?? defaultMaxBytes;
      const timeoutMs = callOptions?.timeoutMs ?? defaultTimeoutMs;

      if (typeof key !== "string" || key.length === 0) {
        throw new UpstashStoreError("upstash_provider_error");
      }

      // One command, one round trip. `GET {base}/get/{key}` — the key is the only thing in
      // the URL, percent-encoded as a single segment, and there is no request body at all.
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeoutMs);

      let response: Response;
      try {
        response = await doFetch(commandUrl(restUrl, "get", key), {
          method: "GET",
          headers: {
            authorization: `Bearer ${restToken}`,
          },
          signal: controller.signal,
          // A pool read must never be served from an intermediary cache.
          cache: "no-store",
          redirect: "error",
        });
      } catch (error) {
        const name = error instanceof Error ? error.name : "";
        if (name === "AbortError" || name === "TimeoutError") {
          throw new UpstashStoreError("upstash_timeout");
        }
        // Whatever the provider or the platform said, the caller learns only this.
        throw new UpstashStoreError("upstash_provider_error");
      } finally {
        clearTimeout(timer);
      }

      // 404 from Upstash is a routing error, not a missing key: a missing key is a 200
      // whose `result` is null. Treating 404 as "missing" would hide a wrong REST URL.
      if (!response.ok) throw new UpstashStoreError("upstash_provider_error");

      let text: string | null;
      try {
        text = await readCappedText(response, envelopeCeiling(maxBytes));
      } catch (error) {
        const name = error instanceof Error ? error.name : "";
        if (name === "AbortError" || name === "TimeoutError") {
          throw new UpstashStoreError("upstash_timeout");
        }
        throw new UpstashStoreError("upstash_provider_error");
      }
      if (text === null) throw new UpstashStoreError("upstash_value_too_large");

      let envelope: unknown;
      try {
        envelope = JSON.parse(text);
      } catch {
        throw new UpstashStoreError("upstash_invalid_response");
      }
      if (envelope === null || typeof envelope !== "object" || Array.isArray(envelope)) {
        throw new UpstashStoreError("upstash_invalid_response");
      }

      const record = envelope as Record<string, unknown>;
      // An `error` field alongside a 200 is Upstash reporting a command failure. The prose
      // is deliberately discarded: it can echo the key and, in some shapes, the command.
      if ("error" in record) throw new UpstashStoreError("upstash_provider_error");
      if (!("result" in record)) throw new UpstashStoreError("upstash_invalid_response");

      const result = record.result;
      // The one true "absent" signal.
      if (result === null) return null;
      // A number, object or array means someone wrote this key with a different contract.
      if (typeof result !== "string") throw new UpstashStoreError("upstash_non_string_value");

      // The publisher's exact bytes: encode the stored string, never re-serialise the JSON.
      const bytes = new TextEncoder().encode(result);
      if (bytes.byteLength > maxBytes) throw new UpstashStoreError("upstash_value_too_large");
      return bytes;
    },
  };

  return { ok: true, store };
}

/**
 * Read and fully validate today's pool from Upstash, in the provider-NEUTRAL vocabulary.
 *
 * This is the mapping boundary. `readMixPool` deliberately collapses every transport
 * failure into `candidate_pool_provider_error` because it knows nothing about providers;
 * here we know, so a captured `UpstashStoreError` is translated into the precise neutral
 * code (`candidate_pool_timeout`, `candidate_pool_missing`, `candidate_pool_too_large`, …)
 * before the result leaves. The capture variable is per-call closure state — this module
 * holds no mutable data across calls and caches nothing.
 *
 * Still not wired to `/api/edition`. Connecting it is a separate, explicitly approved step.
 */
export async function readMixPoolFromUpstash(
  date: string,
  options: UpstashStoreOptions & {
    env: UpstashEnvSource;
    now?: () => number;
    maxAgeMs?: number;
    maxFutureSkewMs?: number;
  },
): Promise<MixPoolReadResult> {
  const created = createUpstashPoolStore(options.env, options);
  if (!created.ok) return { ok: false, reason: mapUpstashReason(created.reason) };

  let captured: UpstashReasonCode | null = null;
  const observed: PoolObjectStore = {
    async get(key, callOptions) {
      try {
        return await created.store.get(key, callOptions);
      } catch (error) {
        if (error instanceof UpstashStoreError) captured = error.reason;
        throw error;
      }
    },
  };

  const result = await readMixPool(date, {
    store: observed,
    ...(options.now ? { now: options.now } : {}),
    ...(options.maxBytes !== undefined ? { maxBytes: options.maxBytes } : {}),
    ...(options.timeoutMs !== undefined ? { timeoutMs: options.timeoutMs } : {}),
    ...(options.maxAgeMs !== undefined ? { maxAgeMs: options.maxAgeMs } : {}),
    ...(options.maxFutureSkewMs !== undefined ? { maxFutureSkewMs: options.maxFutureSkewMs } : {}),
  });

  const providerReason: UpstashReasonCode | null = captured;
  if (!result.ok && providerReason !== null) {
    return { ok: false, reason: mapUpstashReason(providerReason) };
  }
  return result;
}

/**
 * Adapt the Upstash reader to the orchestrator's candidate-source shape.
 *
 * Provided for completeness of the boundary and exercised only by tests; NOTHING in the
 * production runtime imports it. `onResult` lets a caller observe the neutral reason code
 * without this module logging anything — logging remains the caller's decision.
 */
export function createUpstashCandidateSource(
  options: Parameters<typeof readMixPoolFromUpstash>[1] & {
    onResult?: (result: MixPoolReadResult) => void;
  },
): { loadCandidates(date: string): Promise<MixCandidate[] | null> } {
  return {
    async loadCandidates(date: string): Promise<MixCandidate[] | null> {
      const result = await readMixPoolFromUpstash(date, options);
      options.onResult?.(result);
      return result.ok ? (result.candidates as MixCandidate[]) : null;
    },
  };
}
