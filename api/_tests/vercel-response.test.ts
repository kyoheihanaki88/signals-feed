/**
 * Phase 3C-2 — response adapter.
 *
 * Covers K, L, M and the "never leaks" half of V.
 */

import assert from "node:assert/strict";
import test from "node:test";

import {
  MANDATORY_RESPONSE_HEADERS,
  STABLE_ERROR_CODES,
  errorResponse,
  failClosed,
  harden,
  jsonResponse,
  methodNotAllowed,
} from "../_lib/vercel-response.js";

function assertMandatoryHeaders(response: Response): void {
  assert.equal(response.headers.get("cache-control"), "private, no-store");
  assert.equal(response.headers.get("pragma"), "no-cache");
  assert.equal(response.headers.get("x-content-type-options"), "nosniff");
  assert.equal(response.headers.get("content-type"), "application/json; charset=utf-8");
}

// ── K. security headers are unconditional ─────────────────────────────────────────────

test("K. every response this module produces carries the four mandatory headers", () => {
  for (const response of [
    jsonResponse(200, { ok: true }),
    errorResponse(400, "invalid_request"),
    errorResponse(401, "revoked"),
    errorResponse(429, "rate_limited", { retryAfterSeconds: 30 }),
    methodNotAllowed(),
    failClosed(),
  ]) {
    assertMandatoryHeaders(response);
  }
});

test("K2. harden() imposes the mandatory headers on a response that lacks them", () => {
  const raw = new Response(JSON.stringify({ ok: true }), {
    status: 200,
    headers: { "content-type": "text/html", "cache-control": "public, max-age=31536000" },
  });
  const hardened = harden(raw);
  assertMandatoryHeaders(hardened);
  assert.equal(hardened.status, 200);
});

test("K3. harden() drops every header outside the allowlist", async () => {
  const leaky = new Response(JSON.stringify({ ok: true }), {
    status: 200,
    headers: {
      "set-cookie": "session=secret",
      "x-powered-by": "Express",
      "x-debug-token": "eyJhbGciOiJFUzI1NiJ9.e30.sig",
      "access-control-allow-origin": "*",
      server: "internal-1",
    },
  });
  const hardened = harden(leaky);

  const entries: [string, string][] = [];
  hardened.headers.forEach((value, name) => entries.push([name, value]));
  assert.deepEqual(
    entries.map(([name]) => name).sort(),
    ["cache-control", "content-type", "pragma", "x-content-type-options"],
  );
  const serialized = JSON.stringify(entries);
  assert.ok(!serialized.includes("secret"));
  assert.ok(!serialized.includes("eyJhbGciOiJFUzI1NiJ9"));
  // The body is deliberately untouched.
  assert.deepEqual(await hardened.json(), { ok: true });
});

test("K4. no response is ever publicly cacheable", () => {
  for (const response of [
    harden(new Response("{}", { headers: { "cache-control": "public, max-age=31536000, immutable" } })),
    harden(new Response("{}", { headers: { "cdn-cache-control": "public" } })),
    jsonResponse(200, {}),
  ]) {
    const cacheControl = response.headers.get("cache-control") ?? "";
    assert.ok(cacheControl.includes("no-store"), cacheControl);
    assert.ok(!cacheControl.includes("public"), cacheControl);
    assert.equal(response.headers.get("cdn-cache-control"), null);
  }
});

// ── L. Retry-After ────────────────────────────────────────────────────────────────────

test("L. Retry-After is preserved through harden()", () => {
  const limited = new Response(JSON.stringify({ error: { code: "rate_limited" } }), {
    status: 429,
    headers: { "Retry-After": "42" },
  });
  const hardened = harden(limited);
  assert.equal(hardened.status, 429);
  assert.equal(hardened.headers.get("Retry-After"), "42");
  assertMandatoryHeaders(hardened);
});

test("L2. Retry-After is emitted as a whole, non-negative number", () => {
  assert.equal(errorResponse(429, "rate_limited", { retryAfterSeconds: 12.7 }).headers.get("Retry-After"), "12");
  assert.equal(errorResponse(429, "rate_limited", { retryAfterSeconds: -5 }).headers.get("Retry-After"), "0");
  assert.equal(errorResponse(429, "rate_limited").headers.get("Retry-After"), null);
});

test("L3. Allow is preserved through harden()", () => {
  const hardened = harden(methodNotAllowed());
  assert.equal(hardened.status, 405);
  assert.equal(hardened.headers.get("Allow"), "POST");
});

// ── M. selector_not_connected ─────────────────────────────────────────────────────────

test("M. the selector_not_connected body survives hardening unchanged", async () => {
  const route = new Response(
    JSON.stringify({ status: "not_connected", code: "selector_not_connected" }),
    { status: 503, headers: { "content-type": "application/json; charset=utf-8" } },
  );
  const hardened = harden(route);
  assert.equal(hardened.status, 503);
  assert.deepEqual(await hardened.json(), {
    status: "not_connected",
    code: "selector_not_connected",
  });
  assertMandatoryHeaders(hardened);
});

test("M2. selector_not_connected is a stable code", () => {
  assert.ok(STABLE_ERROR_CODES.has("selector_not_connected"));
});

// ── stable error codes ────────────────────────────────────────────────────────────────

test("an unrecognised error code is collapsed, never echoed to the client", async () => {
  for (const injected of [
    "Error: ENOENT /var/task/api/_certs/apple/AppleRootCA-G3.cer",
    "<script>alert(1)</script>",
    "eyJhbGciOiJFUzI1NiJ9.e30.signature",
    "2000000999999999",
    "totally_new_code",
  ]) {
    const response = errorResponse(500, injected);
    const body = (await response.json()) as { error: { code: string } };
    assert.equal(body.error.code, "invalid_request");
    assert.ok(!JSON.stringify(body).includes(injected.slice(0, 12)));
  }
});

test("every code the routes can emit is in the stable set", () => {
  // Drawn from exchange.ts, edition.ts, auth-middleware.ts and custom-mix-contract.ts.
  for (const code of [
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
    "revoked",
    "missing_token",
    "invalid_token",
    "expired_token",
    "wrong_scope",
    "wrong_environment",
    "invalid_region",
    "invalid_topic",
    "unsupported_selector_version",
    "selector_not_connected",
  ]) {
    assert.ok(STABLE_ERROR_CODES.has(code), `${code} is missing from the stable set`);
  }
});

// ── V (response half). nothing leaks ──────────────────────────────────────────────────

test("V. failClosed() says nothing beyond a retryable code", async () => {
  const response = failClosed();
  assert.equal(response.status, 503);
  const text = await response.text();
  assert.equal(text, JSON.stringify({ error: { code: "verification_unavailable" } }));
  assert.ok(!/at |Error:|\/var\/task|node_modules/.test(text), "a stack trace leaked");
});

test("V2. the module never serialises an Error object", async () => {
  const { readFileSync } = await import("node:fs");
  const { dirname, join, resolve } = await import("node:path");
  const { fileURLToPath } = await import("node:url");
  const source = readFileSync(
    resolve(dirname(fileURLToPath(import.meta.url)), "..", "_lib", "vercel-response.ts"),
    "utf8",
  );
  assert.ok(!/\.stack\b/.test(source), "the response adapter touches a stack");
  assert.ok(!/\bconsole\./.test(source), "the response adapter logs");
  assert.ok(!/error\.message/.test(source), "the response adapter echoes an error message");
});

test("V3. the mandatory header set is exactly the four required ones", () => {
  assert.deepEqual(Object.keys(MANDATORY_RESPONSE_HEADERS).sort(), [
    "cache-control",
    "content-type",
    "pragma",
    "x-content-type-options",
  ]);
});
