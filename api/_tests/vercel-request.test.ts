/**
 * Phase 3C-2 — request adapter.
 *
 * Covers C, D, E, F, G, H, I, J and the header-rebuild guarantee.
 */

import assert from "node:assert/strict";
import test from "node:test";

import {
  TRUSTED_CLIENT_IP_HEADERS,
  UNTRUSTED_FORWARDING_HEADERS,
  adaptVercelRequest,
  deriveClientIp,
  deriveRequestId,
  readBoundedBody,
  readSingleHeader,
} from "../_lib/vercel-request.js";
import { codeOf } from "./runtime-fixtures.js";

const MAX = 16 * 1_024;

function request(
  headers: Record<string, string>,
  body: string,
  method = "POST",
): Request {
  return new Request("https://signals.example/api/auth/exchange", {
    method,
    headers,
    ...(method === "GET" ? {} : { body }),
  });
}

function jsonRequest(
  headers: Record<string, string> = {},
  body: Record<string, unknown> = { signedTransactionInfo: "a.b.c", selectorVersion: 2 },
): Request {
  return request({ "content-type": "application/json", ...headers }, JSON.stringify(body));
}

// ── C. method ─────────────────────────────────────────────────────────────────────────

test("C. a non-POST method is 405 with Allow: POST", async () => {
  for (const method of ["GET", "PUT", "DELETE", "PATCH", "HEAD"]) {
    const result = await adaptVercelRequest(
      new Request("https://signals.example/api/edition", { method }),
      { maxBodyBytes: MAX },
    );
    assert.equal(result.ok, false);
    if (result.ok) return;
    assert.equal(result.response.status, 405);
    assert.equal(result.response.headers.get("Allow"), "POST");
    assert.equal(await codeOf(result.response), "invalid_request");
  }
});

// ── D. malformed JSON ─────────────────────────────────────────────────────────────────

test("D. malformed JSON is 400 invalid_request", async () => {
  for (const body of ["{", "not json", '{"a":}', "[1,2,3]", '"a string"', "42"]) {
    const result = await adaptVercelRequest(
      request({ "content-type": "application/json" }, body),
      { maxBodyBytes: MAX },
    );
    assert.equal(result.ok, false, `body ${body} was accepted`);
    if (result.ok) return;
    assert.equal(result.response.status, 400);
    assert.equal(await codeOf(result.response), "invalid_request");
  }
});

test("D2. an empty body is 400", async () => {
  const result = await adaptVercelRequest(
    request({ "content-type": "application/json" }, ""),
    { maxBodyBytes: MAX },
  );
  assert.equal(result.ok, false);
  if (!result.ok) assert.equal(result.response.status, 400);
});

// ── E. oversized body ─────────────────────────────────────────────────────────────────

test("E. a body over the cap is 413, by declared length", async () => {
  const oversized = JSON.stringify({ pad: "x".repeat(200) });
  const result = await adaptVercelRequest(
    request({ "content-type": "application/json" }, oversized),
    { maxBodyBytes: 64 },
  );
  assert.equal(result.ok, false);
  if (!result.ok) {
    assert.equal(result.response.status, 413);
    assert.equal(await codeOf(result.response), "invalid_request");
  }
});

test("E2. a body that LIES about its length is still capped mid-stream", async () => {
  // A chunked stream with no content-length: the cap must come from the read, not a header.
  const chunks = [new TextEncoder().encode("x".repeat(1_000))];
  let pulls = 0;
  const stream = new ReadableStream<Uint8Array>({
    pull(controller) {
      pulls += 1;
      if (pulls > 50) {
        controller.close();
        return;
      }
      controller.enqueue(chunks[0]);
    },
  });

  const streamed = new Request("https://signals.example/api/auth/exchange", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: stream,
    duplex: "half",
  } as RequestInit & { duplex: "half" });

  const result = await adaptVercelRequest(streamed, { maxBodyBytes: 2_048 });
  assert.equal(result.ok, false);
  if (!result.ok) assert.equal(result.response.status, 413);
  // Proof the stream was abandoned rather than drained: far fewer than 50 pulls happened.
  assert.ok(pulls < 10, `the oversized stream was buffered (${pulls} pulls)`);
});

test("E3. a non-numeric or repeated content-length is rejected before any read", async () => {
  const bad = await readBoundedBody(
    new Request("https://signals.example/x", {
      method: "POST",
      headers: { "content-type": "application/json", "content-length": "abc" },
      body: "{}",
    }),
    MAX,
  );
  assert.equal(bad.ok, false);
  if (!bad.ok) assert.equal(bad.status, 400);
});

// ── F. content type ───────────────────────────────────────────────────────────────────

test("F. a wrong or missing content type is 400 invalid_request (existing contract)", async () => {
  for (const contentType of [
    "text/plain",
    "application/x-www-form-urlencoded",
    "application/octet-stream",
    "",
  ]) {
    const result = await adaptVercelRequest(
      request(contentType ? { "content-type": contentType } : {}, "{}"),
      { maxBodyBytes: MAX },
    );
    assert.equal(result.ok, false, `content-type ${contentType} was accepted`);
    if (result.ok) return;
    assert.equal(result.response.status, 400);
    assert.equal(await codeOf(result.response), "invalid_request");
  }
});

test("F2. a charset parameter on application/json is accepted", async () => {
  const result = await adaptVercelRequest(
    jsonRequest({ "content-type": "application/json; charset=utf-8" }),
    { maxBodyBytes: MAX },
  );
  assert.equal(result.ok, true);
});

// ── ambiguous headers ─────────────────────────────────────────────────────────────────

test("a repeated content-type, authorization or content-length is rejected", async () => {
  assert.equal(
    readSingleHeader(new Headers({ "content-type": "application/json, text/plain" }), "content-type"),
    null,
  );
  assert.equal(readSingleHeader(new Headers(), "content-type"), undefined);
  assert.equal(
    readSingleHeader(new Headers({ authorization: "Bearer a" }), "authorization"),
    "Bearer a",
  );

  const ambiguousAuth = await adaptVercelRequest(
    jsonRequest({ authorization: "Bearer one, Bearer two" }),
    { maxBodyBytes: MAX },
  );
  assert.equal(ambiguousAuth.ok, false);
  if (!ambiguousAuth.ok) assert.equal(ambiguousAuth.response.status, 400);
});

// ── G. Authorization forwarded, never logged ──────────────────────────────────────────

test("G. Authorization is forwarded verbatim to the route", async () => {
  const result = await adaptVercelRequest(jsonRequest({ authorization: "Bearer abc.def.ghi" }), {
    maxBodyBytes: MAX,
  });
  assert.equal(result.ok, true);
  if (!result.ok) return;
  assert.equal(result.request.headers.get("authorization"), "Bearer abc.def.ghi");
});

test("G2. the adapter module contains no logging of any kind", async () => {
  const { readFileSync } = await import("node:fs");
  const { dirname, join, resolve } = await import("node:path");
  const { fileURLToPath } = await import("node:url");
  const source = readFileSync(
    resolve(dirname(fileURLToPath(import.meta.url)), "..", "_lib", "vercel-request.ts"),
    "utf8",
  );
  assert.ok(!/\bconsole\./.test(source), "the request adapter logs");
  assert.ok(!/\blogger\s*[.(]/.test(source), "the request adapter uses a logger");
  assert.ok(!/SecurityLogger/.test(source), "the request adapter imports a logger");
});

// ── H / I. client IP ──────────────────────────────────────────────────────────────────

test("H. the trusted Vercel header supplies the client IP", () => {
  assert.equal(
    deriveClientIp(new Headers({ "x-vercel-forwarded-for": "203.0.113.9" })),
    "203.0.113.9",
  );
  assert.equal(deriveClientIp(new Headers({ "x-real-ip": "203.0.113.10" })), "203.0.113.10");
  // A chain: the first entry is the client as Vercel observed it.
  assert.equal(
    deriveClientIp(new Headers({ "x-vercel-forwarded-for": "203.0.113.11, 70.0.0.1" })),
    "203.0.113.11",
  );
  // Trusted beats untrusted whenever both are present.
  assert.equal(
    deriveClientIp(
      new Headers({ "x-vercel-forwarded-for": "203.0.113.12", "x-forwarded-for": "10.0.0.1" }),
    ),
    "203.0.113.12",
  );
});

test("I. every untrusted forwarding header is ignored, and stripped from the request", async () => {
  for (const header of UNTRUSTED_FORWARDING_HEADERS) {
    assert.equal(
      deriveClientIp(new Headers({ [header]: "10.6.6.6" })),
      "unknown",
      `${header} was trusted`,
    );
  }

  const spoofed = await adaptVercelRequest(
    jsonRequest({ "x-forwarded-for": "10.6.6.6", "true-client-ip": "10.6.6.7" }),
    { maxBodyBytes: MAX },
  );
  assert.equal(spoofed.ok, true);
  if (!spoofed.ok) return;
  assert.equal(spoofed.clientIp, "unknown");
  // The route reads x-forwarded-for; it must see the DERIVED value, not the client's.
  assert.equal(spoofed.request.headers.get("x-forwarded-for"), "unknown");
  assert.equal(spoofed.request.headers.get("true-client-ip"), null);
});

test("I2. a malformed trusted header falls back to the strict bucket", () => {
  for (const value of ["", "not-an-ip!", "x".repeat(60), "<script>"]) {
    assert.equal(deriveClientIp(new Headers({ "x-vercel-forwarded-for": value })), "unknown");
  }
});

test("I3. the trusted header list is exactly the Vercel-set ones", () => {
  assert.deepEqual([...TRUSTED_CLIENT_IP_HEADERS], ["x-vercel-forwarded-for", "x-real-ip"]);
});

// ── J. request id ─────────────────────────────────────────────────────────────────────

test("J. a well-formed x-vercel-id is used as the request id", () => {
  assert.equal(
    deriveRequestId(new Headers({ "x-vercel-id": "iad1::abcde-1234567890123-0a1b2c3d" })),
    "iad1::abcde-1234567890123-0a1b2c3d",
  );
});

test("J2. an unsafe or oversized request id is discarded, not sanitised in place", () => {
  for (const value of ['evil" ,"injected":"1', "with spaces", "x".repeat(129), ""]) {
    const generated = deriveRequestId(
      new Headers({ "x-vercel-id": value }),
      () => "generated-id",
    );
    assert.equal(generated, "generated-id", `unsafe id accepted: ${JSON.stringify(value)}`);
  }
});

test("J2b. a newline can never reach the adapter — the platform rejects it first", () => {
  // Defence in depth: header injection is refused at the Headers boundary, so the
  // adapter's charset check is the second line, not the only one.
  assert.throws(() => new Headers({ "x-vercel-id": "line\nbreak" }), TypeError);
});

test("J3. a generated id is unique per request", () => {
  const first = deriveRequestId(new Headers());
  const second = deriveRequestId(new Headers());
  assert.notEqual(first, second);
  assert.ok(first.length >= 16);
});

// ── header rebuild ────────────────────────────────────────────────────────────────────

test("the forwarded request is rebuilt: only allowed headers survive", async () => {
  const result = await adaptVercelRequest(
    jsonRequest({
      authorization: "Bearer token",
      cookie: "session=abc",
      "x-vercel-forwarded-for": "203.0.113.20",
      "x-secret-debug": "leak-me",
      origin: "https://evil.example",
    }),
    { maxBodyBytes: MAX },
  );
  assert.equal(result.ok, true);
  if (!result.ok) return;

  const names: string[] = [];
  result.request.headers.forEach((_value, name) => names.push(name));
  assert.deepEqual(names.sort(), [
    "authorization",
    "content-length",
    "content-type",
    "x-forwarded-for",
    "x-signals-request-id",
  ]);
  assert.equal(result.request.headers.get("cookie"), null);
  assert.equal(result.request.headers.get("x-secret-debug"), null);
  assert.equal(result.request.headers.get("origin"), null);
  assert.equal(result.request.method, "POST");
});

test("the forwarded body is byte-identical and content-length is recomputed", async () => {
  const body = { signedTransactionInfo: "a.b.c", selectorVersion: 2, appVersion: "1.4.0" };
  const result = await adaptVercelRequest(jsonRequest({}, body), { maxBodyBytes: MAX });
  assert.equal(result.ok, true);
  if (!result.ok) return;

  const text = await result.request.text();
  assert.deepEqual(JSON.parse(text), body);
  assert.equal(
    result.request.headers.get("content-length"),
    String(Buffer.byteLength(text, "utf8")),
  );
});

test("no request id or IP is ever taken from the body", async () => {
  const result = await adaptVercelRequest(
    jsonRequest({}, { requestId: "attacker", clientIp: "1.2.3.4", signedTransactionInfo: "a.b.c" }),
    { maxBodyBytes: MAX },
  );
  assert.equal(result.ok, true);
  if (!result.ok) return;
  assert.notEqual(result.requestId, "attacker");
  assert.notEqual(result.clientIp, "1.2.3.4");
});
