/**
 * Vercel request adapter. (Phase 3C-2)
 *
 * Vercel's Node runtime delivers `/api` functions a Web-standard `Request` when the module
 * exports a `fetch` handler, so this adapter's job is not format translation — it is
 * NORMALISATION AND DISTRUST. It turns whatever arrived off the internet into the narrow,
 * already-validated shape the Phase 3B-1 routes expect, and it rebuilds the headers from
 * scratch so nothing a client sent can reach the route unexamined.
 *
 * Three things it exists to get right:
 *
 *   1. CLIENT IP. `x-forwarded-for` is client-appendable and is therefore ignored outright.
 *      Only headers Vercel's own edge sets are trusted. When none is present the request is
 *      bucketed as "unknown" — deliberately the STRICTER outcome, since every unattributable
 *      request then shares one rate-limit bucket.
 *
 *   2. SIZE BEFORE PARSE. `Content-Length` is checked first, then the body stream is read
 *      through a hard cap that aborts mid-stream. A lying or absent `Content-Length` cannot
 *      get an oversized body buffered, and nothing is parsed until it is known to be small.
 *
 *   3. AMBIGUITY IS REJECTION. A repeated `Content-Type`, `Authorization` or
 *      `Content-Length` is a request smuggling / header confusion signal, not something to
 *      resolve by picking the first value. It fails the request.
 *
 * This module has no logger and never stringifies a body, a header value or a token.
 */

import { randomUUID } from "node:crypto";

import { errorResponse, methodNotAllowed } from "./vercel-response.js";

/** Set by Vercel's edge; a client cannot forge these through the platform. */
export const TRUSTED_CLIENT_IP_HEADERS = ["x-vercel-forwarded-for", "x-real-ip"] as const;

/**
 * Client-controllable forwarding headers. Listed so the intent is explicit and testable:
 * these are never consulted, and any value a client supplies is stripped before the
 * request reaches a route.
 */
export const UNTRUSTED_FORWARDING_HEADERS = [
  "x-forwarded-for",
  "forwarded",
  "client-ip",
  "true-client-ip",
  "cf-connecting-ip",
  "x-client-ip",
  "x-cluster-client-ip",
] as const;

/** Headers that must appear at most once; a repeat is a rejection, not a choice. */
const SINGLE_VALUE_HEADERS = ["content-type", "authorization", "content-length"] as const;

const REQUEST_ID_PATTERN = /^[A-Za-z0-9:_.-]{1,128}$/;
const IPV4_OR_IPV6 = /^[0-9a-fA-F:.]{3,45}$/;

export type AdaptOptions = {
  /** Hard body cap for this route. Exchange 16 KiB, edition 8 KiB. */
  maxBodyBytes: number;
  /** Injectable for deterministic tests. */
  generateRequestId?: () => string;
};

export type AdaptResult =
  | { ok: true; request: Request; clientIp: string; requestId: string }
  | { ok: false; response: Response };

/**
 * Read a header that must be single-valued.
 * Returns `undefined` when absent and `null` when ambiguous.
 *
 * Node joins repeated headers with ", " before a `Request` ever sees them, so a comma in a
 * header that cannot legitimately contain one is the detectable signature of a repeat.
 */
export function readSingleHeader(
  headers: Headers,
  name: string,
): string | undefined | null {
  const raw = headers.get(name);
  if (raw === null) return undefined;
  const value = raw.trim();
  if (value.length === 0) return undefined;
  if (value.includes(",")) return null; // repeated / ambiguous
  return value;
}

/**
 * The client IP, from trusted headers only.
 *
 * `x-vercel-forwarded-for` may legitimately carry a chain; the FIRST entry is the client as
 * Vercel observed it. Anything that does not look like an address becomes "unknown".
 */
export function deriveClientIp(headers: Headers): string {
  for (const name of TRUSTED_CLIENT_IP_HEADERS) {
    const raw = headers.get(name);
    if (raw === null) continue;
    const candidate = raw.split(",")[0]?.trim() ?? "";
    if (candidate.length > 0 && candidate.length <= 45 && IPV4_OR_IPV6.test(candidate)) {
      return candidate;
    }
  }
  // No trusted attribution: share one bucket rather than trust the client's own claim.
  return "unknown";
}

/**
 * A request id for correlation. Vercel's `x-vercel-id` is preferred, but only after it is
 * proven to be a bounded, safe token — an unvalidated id would be a log-injection vector.
 */
export function deriveRequestId(
  headers: Headers,
  generate: () => string = () => randomUUID(),
): string {
  const supplied = headers.get("x-vercel-id");
  if (supplied !== null) {
    const value = supplied.trim();
    if (REQUEST_ID_PATTERN.test(value)) return value;
  }
  return generate();
}

type BodyOutcome =
  | { ok: true; text: string }
  | { ok: false; status: 400 | 413 };

/**
 * Read the body through a hard cap.
 *
 * The stream is abandoned the moment the cap is exceeded, so an oversized or unbounded body
 * is never fully buffered. Nothing is parsed here beyond a validity check.
 */
export async function readBoundedBody(
  request: Request,
  maxBodyBytes: number,
): Promise<BodyOutcome> {
  const declared = readSingleHeader(request.headers, "content-length");
  if (declared === null) return { ok: false, status: 400 }; // ambiguous
  if (declared !== undefined) {
    if (!/^\d+$/.test(declared)) return { ok: false, status: 400 };
    if (Number.parseInt(declared, 10) > maxBodyBytes) return { ok: false, status: 413 };
  }

  const stream = request.body;
  if (stream === null) {
    // No stream: fall back to the buffered form, still capped.
    const text = await request.text();
    if (Buffer.byteLength(text, "utf8") > maxBodyBytes) return { ok: false, status: 413 };
    return text.length === 0 ? { ok: false, status: 400 } : { ok: true, text };
  }

  const reader = stream.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      if (value === undefined) continue;
      total += value.byteLength;
      if (total > maxBodyBytes) {
        // Stop pulling immediately — do not buffer the remainder.
        await reader.cancel().catch(() => undefined);
        return { ok: false, status: 413 };
      }
      chunks.push(value);
    }
  } catch {
    return { ok: false, status: 400 };
  } finally {
    reader.releaseLock();
  }

  if (total === 0) return { ok: false, status: 400 };
  return { ok: true, text: Buffer.concat(chunks).toString("utf8") };
}

/**
 * Validate and normalise an incoming request.
 *
 * On success the returned `Request` is REBUILT: it carries only the headers a route is
 * entitled to see, with the client IP replaced by the trusted value under the name the
 * Phase 3B-1 routes already read. On failure the caller returns the response verbatim.
 */
export async function adaptVercelRequest(
  request: Request,
  options: AdaptOptions,
): Promise<AdaptResult> {
  if (request.method !== "POST") {
    return { ok: false, response: methodNotAllowed() };
  }

  const contentType = readSingleHeader(request.headers, "content-type");
  if (
    contentType === null ||
    contentType === undefined ||
    !contentType.toLowerCase().startsWith("application/json")
  ) {
    // Matches the existing route contract exactly: a bad content type is `400
    // invalid_request`, not 415, so the iOS client sees no new status code.
    return { ok: false, response: errorResponse(400, "invalid_request") };
  }

  const authorization = readSingleHeader(request.headers, "authorization");
  if (authorization === null) {
    return { ok: false, response: errorResponse(400, "invalid_request") };
  }

  const body = await readBoundedBody(request, options.maxBodyBytes);
  if (!body.ok) {
    return { ok: false, response: errorResponse(body.status, "invalid_request") };
  }

  // Validity only — the route still owns schema validation.
  try {
    const parsed: unknown = JSON.parse(body.text);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      return { ok: false, response: errorResponse(400, "invalid_request") };
    }
  } catch {
    return { ok: false, response: errorResponse(400, "invalid_request") };
  }

  const clientIp = deriveClientIp(request.headers);
  const requestId = deriveRequestId(request.headers, options.generateRequestId);

  // Rebuilt from nothing: any header not set here cannot reach the route.
  const headers = new Headers({
    "content-type": "application/json",
    "content-length": String(Buffer.byteLength(body.text, "utf8")),
    "x-forwarded-for": clientIp,
    "x-signals-request-id": requestId,
  });
  if (authorization !== undefined) headers.set("authorization", authorization);

  return {
    ok: true,
    clientIp,
    requestId,
    request: new Request(request.url, {
      method: "POST",
      headers,
      body: body.text,
    }),
  };
}
