/**
 * Vercel response adapter. (Phase 3C-2)
 *
 * Every byte that leaves a Custom Mix route passes through `harden()`. It guarantees the
 * four security headers regardless of what the route produced, and — more importantly —
 * it uses an ALLOWLIST for everything else. A header a route never intended to expose
 * (a framework's `x-powered-by`, a debug header, an upstream `set-cookie`) cannot survive
 * this function, because only headers named here are copied forward.
 *
 * The body is passed through untouched. That is deliberate: the 200 body legitimately
 * carries the access token, and rewriting it would be the wrong place to enforce privacy.
 * What must never appear is a stack trace, an Apple payload, a raw JWS, a transaction id or
 * a Redis detail — and the only bodies this module CREATES are `{"error":{"code":"…"}}`
 * with a code drawn from a fixed set.
 */

/** Headers a Custom Mix response is allowed to carry beyond the mandatory ones. */
const PRESERVED_HEADERS = ["retry-after", "allow"] as const;

export const MANDATORY_RESPONSE_HEADERS: Readonly<Record<string, string>> = {
  "content-type": "application/json; charset=utf-8",
  // Authenticated, per-user responses must never touch a shared or browser cache.
  "cache-control": "private, no-store",
  pragma: "no-cache",
  "x-content-type-options": "nosniff",
};

/**
 * Stable, client-visible error codes. A code outside this set is a bug, and `errorResponse`
 * collapses it rather than inventing new API surface at runtime.
 */
export const STABLE_ERROR_CODES = new Set([
  "invalid_request",
  "custom_mix_disabled",
  "rate_limited",
  "verification_unavailable",
  "invalid_proof",
  "unsupported_environment",
  "wrong_bundle",
  "wrong_product",
  "wrong_product_type",
  "wrong_ownership",
  "wrong_scope",
  "wrong_environment",
  "revoked",
  "missing_token",
  "invalid_token",
  "expired_token",
  "invalid_region",
  "invalid_topic",
  "unsupported_selector_version",
  "custom_mix_unavailable",
]);

function baseHeaders(): Headers {
  const headers = new Headers();
  for (const [name, value] of Object.entries(MANDATORY_RESPONSE_HEADERS)) {
    headers.set(name, value);
  }
  return headers;
}

/**
 * Re-emit a response with the mandatory headers applied and every other header dropped
 * unless explicitly preserved. Status and body are untouched.
 */
export function harden(response: Response): Response {
  const headers = baseHeaders();
  for (const name of PRESERVED_HEADERS) {
    const value = response.headers.get(name);
    if (value !== null) headers.set(name, value);
  }
  return new Response(response.body, { status: response.status, headers });
}

/** A JSON body with the mandatory headers. Used only for envelopes this module owns. */
export function jsonResponse(
  status: number,
  body: Record<string, unknown>,
  extra: { retryAfterSeconds?: number; allow?: string } = {},
): Response {
  const headers = baseHeaders();
  if (extra.retryAfterSeconds !== undefined) {
    headers.set("Retry-After", String(Math.max(0, Math.trunc(extra.retryAfterSeconds))));
  }
  if (extra.allow !== undefined) headers.set("Allow", extra.allow);
  return new Response(JSON.stringify(body), { status, headers });
}

/** The standard error envelope. An unrecognised code is collapsed, never echoed. */
export function errorResponse(
  status: number,
  code: string,
  extra: { retryAfterSeconds?: number; allow?: string } = {},
): Response {
  const safeCode = STABLE_ERROR_CODES.has(code) ? code : "invalid_request";
  return jsonResponse(status, { error: { code: safeCode } }, extra);
}

/** 405 for anything that is not POST, carrying the required `Allow` header. */
export function methodNotAllowed(): Response {
  return errorResponse(405, "invalid_request", { allow: "POST" });
}

/**
 * The single response for "the runtime could not be built" or "something threw".
 *
 * Fail closed, and say nothing: no message, no reason, no stack. The operator learns what
 * happened from the security log's reason code; the client learns only to retry later.
 */
export function failClosed(): Response {
  return errorResponse(503, "verification_unavailable");
}
