/**
 * Phase 3C-2 — runtime lifecycle and route exports.
 *
 * Covers A, B, N, O, P, Q, R, S, T, U, V, W, X, Y, Z.
 *
 * No test contacts Apple, Redis or Vercel: the JWS decoder, the Transaction History
 * transport and the Redis client are injected, and every environment is a fixture.
 */

import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import exchangeRoute from "../auth/exchange.js";
import editionRoute from "../edition.js";
import type { RawEnv } from "../_lib/env.js";
import { LiveAppleEntitlementVerifier } from "../_lib/runtime-dependencies.js";
import { MemorySecurityLogger } from "../_lib/security-logging.js";
import {
  VercelRuntimeError,
  assertDeploymentSafety,
  getVercelRuntime,
  handleEditionRequest,
  handleExchangeRequest,
  peekVercelRuntimeForTests,
  primeVercelRuntimeForTests,
  resetVercelRuntimeForTests,
} from "../_lib/vercel-runtime.js";
import { loadRuntimeConfig } from "../_lib/runtime-config.js";
import {
  FIXTURE_DEVICE_JWS,
  FIXTURE_HMAC_SECRET_PRODUCTION,
  FIXTURE_HISTORY_JWS,
  FIXTURE_ORIGINAL_TRANSACTION_ID,
  FIXTURE_REDIS_TOKEN_PRODUCTION,
  FakeSignedTransactionDecoder,
  FakeTransactionHistoryFetcher,
  FixedClock,
  codeOf,
  decodedTransaction,
  developmentEnv,
  memoryRedis,
  productionEnv,
  sandboxEnv,
} from "./runtime-fixtures.js";

/**
 * U (first half): captured the instant this module finished importing — BEFORE any test
 * body runs. Importing the two route modules and the runtime module must not have built
 * anything, so this snapshot must be null.
 */
const RUNTIME_AT_IMPORT_TIME = peekVercelRuntimeForTests();

const API_DIR = resolve(dirname(fileURLToPath(import.meta.url)), "..");

type Harness = {
  env: RawEnv;
  decoder: FakeSignedTransactionDecoder;
  fetcher: FakeTransactionHistoryFetcher;
  logger: MemorySecurityLogger;
};

function harness(
  environment: "Production" | "Sandbox" = "Production",
  overrides: RawEnv = {},
): Harness {
  const env =
    environment === "Production"
      ? productionEnv(overrides).env
      : sandboxEnv(overrides).env;
  return {
    env,
    decoder: new FakeSignedTransactionDecoder(decodedTransaction(environment)),
    fetcher: new FakeTransactionHistoryFetcher(),
    logger: new MemorySecurityLogger(),
  };
}

function prime(h: Harness): ReturnType<typeof primeVercelRuntimeForTests> {
  return primeVercelRuntimeForTests({
    env: h.env,
    transports: {
      clock: new FixedClock(),
      logger: h.logger,
      redisClient: memoryRedis(),
      appleDecoder: h.decoder,
      transactionHistoryFetcher: h.fetcher,
      sleep: async () => {},
    },
  });
}

function exchangeRequest(body?: Record<string, unknown>, headers: Record<string, string> = {}): Request {
  return new Request("https://signals.example/api/auth/exchange", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-vercel-forwarded-for": "203.0.113.55",
      "x-vercel-id": "iad1::abcde-1234567890123-0a1b2c3d",
      ...headers,
    },
    body: JSON.stringify(
      body ?? {
        signedTransactionInfo: FIXTURE_DEVICE_JWS,
        appVersion: "1.4.0",
        selectorVersion: 3,
      },
    ),
  });
}

function editionRequest(token: string | null): Request {
  return new Request("https://signals.example/api/edition", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-vercel-forwarded-for": "203.0.113.55",
      ...(token === null ? {} : { authorization: `Bearer ${token}` }),
    },
    body: JSON.stringify({
      date: "2026-07-27",
      active: { mode: "custom", regions: ["japan"], topics: ["tech"] },
      pending: null,
      selectorVersion: 3,
      storyCount: 5,
    }),
  });
}

async function tokenFrom(response: Response): Promise<string | undefined> {
  const body = (await response.clone().json()) as { accessToken?: string };
  return body.accessToken;
}

test.beforeEach(() => {
  resetVercelRuntimeForTests();
});

// ── A / B. the route exports reach the existing handlers ──────────────────────────────

test("A. POST /api/auth/exchange reaches the composed exchange handler", async () => {
  const h = harness();
  prime(h);

  const response = await exchangeRoute.fetch(exchangeRequest());
  assert.equal(response.status, 200);
  const token = await tokenFrom(response);
  assert.ok(token, "no token issued");

  // Both halves of the live composition ran through the route export.
  assert.ok(h.decoder.calls.includes(FIXTURE_DEVICE_JWS));
  assert.ok(h.decoder.calls.includes(FIXTURE_HISTORY_JWS));
  assert.equal(h.fetcher.calls, 1);
});

test("B. POST /api/edition reaches the composed edition handler", async () => {
  const h = harness();
  prime(h);

  const token = await tokenFrom(await exchangeRoute.fetch(exchangeRequest()));
  assert.ok(token);

  // The route export really is the CONNECTED handler: it authenticates, then attempts a
  // Custom Mix. This harness configures no storage, so the pool read is unavailable and
  // the route answers with the single stable public failure code.
  const response = await editionRoute.fetch(editionRequest(token));
  assert.equal(response.status, 503);
  assert.equal(await codeOf(response), "custom_mix_unavailable");
});

test("B2. the edition route still rejects a missing or invalid token", async () => {
  prime(harness());
  assert.equal((await editionRoute.fetch(editionRequest(null))).status, 401);
  assert.equal((await editionRoute.fetch(editionRequest("nonsense"))).status, 401);
});

test("C. a GET on either route is 405 with Allow: POST and never builds a runtime", async () => {
  for (const route of [exchangeRoute, editionRoute]) {
    const response = await route.fetch(
      new Request("https://signals.example/api/edition", { method: "GET" }),
    );
    assert.equal(response.status, 405);
    assert.equal(response.headers.get("Allow"), "POST");
    assert.equal(await codeOf(response), "invalid_request");
  }
  assert.equal(peekVercelRuntimeForTests(), null, "a rejected method built a runtime");
});

// ── K. security headers survive the whole route ───────────────────────────────────────

test("K. every route response carries the mandatory headers", async () => {
  const h = harness();
  prime(h);
  const token = await tokenFrom(await exchangeRoute.fetch(exchangeRequest()));

  for (const response of [
    await exchangeRoute.fetch(exchangeRequest()),
    await editionRoute.fetch(editionRequest(token ?? "")),
    await editionRoute.fetch(editionRequest(null)),
    await exchangeRoute.fetch(new Request("https://x.test/y", { method: "GET" })),
  ]) {
    assert.equal(response.headers.get("cache-control"), "private, no-store");
    assert.equal(response.headers.get("pragma"), "no-cache");
    assert.equal(response.headers.get("x-content-type-options"), "nosniff");
    assert.equal(response.headers.get("content-type"), "application/json; charset=utf-8");
  }
});

// ── N. kill switch ────────────────────────────────────────────────────────────────────

test("N. the kill switch closes both routes before any body is read", async () => {
  const h = harness("Production", { CUSTOM_MIX_API_ENABLED: "false" });
  prime(h);

  for (const response of [
    await exchangeRoute.fetch(exchangeRequest()),
    await editionRoute.fetch(editionRequest("anything")),
  ]) {
    assert.equal(response.status, 503);
    assert.equal(await codeOf(response), "custom_mix_disabled");
  }
  assert.equal(h.decoder.calls.length, 0);
  assert.equal(h.fetcher.calls, 0);
});

// ── O. missing configuration fails closed ─────────────────────────────────────────────

test("O. a Preview deployment with no configuration fails closed", async () => {
  const response = await handleExchangeRequest(exchangeRequest(), {
    VERCEL_ENV: "preview",
  });
  assert.equal(response.status, 503);
  assert.equal(await codeOf(response), "verification_unavailable");
  assert.equal(await tokenFrom(response), undefined);
  assert.equal(peekVercelRuntimeForTests(), null, "a failed init was cached");
});

test("O2. a partially configured deployment fails closed, and says nothing about why", async () => {
  const partial = sandboxEnv({ KV_REST_API_TOKEN: undefined }).env;
  const response = await handleEditionRequest(editionRequest("x"), {
    ...partial,
    VERCEL_ENV: "preview",
  });
  assert.equal(response.status, 503);
  const text = await response.text();
  assert.equal(text, JSON.stringify({ error: { code: "verification_unavailable" } }));
  assert.ok(!text.includes("KV_REST_API"), "a variable name leaked to the client");
});

// ── P / Q. deployment target and mode must agree ──────────────────────────────────────

test("P. a Preview deployment cannot construct the Production environment", async () => {
  const production = loadRuntimeConfig(productionEnv().env);
  assert.throws(
    () => assertDeploymentSafety(production, { VERCEL_ENV: "preview" }),
    (error: unknown) =>
      error instanceof VercelRuntimeError &&
      error.reason === "preview_requires_sandbox_configuration",
  );

  const response = await handleExchangeRequest(exchangeRequest(), {
    ...productionEnv().env,
    VERCEL_ENV: "preview",
  });
  assert.equal(response.status, 503);
  assert.equal(peekVercelRuntimeForTests(), null);
});

test("P2. a Preview deployment on Sandbox configuration is permitted and isolated", () => {
  const h = harness("Sandbox", { VERCEL_ENV: "preview" });
  const runtime = prime(h);
  assert.equal(runtime.config.mode, "sandbox");
  assert.equal(runtime.dependencies.environment, "Sandbox");
  assert.equal(runtime.dependencies.appleIdentity.appAppleId, undefined);
  assert.equal(runtime.dependencies.namespaces.root, "signals:sandbox:sandbox");
  assert.notEqual(runtime.dependencies.namespaces.root, "signals:production:production");
  // The pool namespace is reserved and distinct, and nothing connects to it.
  assert.equal(runtime.dependencies.namespaces.pool, "signals:sandbox:sandbox:pool");
});

test("Q. a Production deployment cannot fall back to Sandbox or Development", async () => {
  for (const [label, config] of [
    ["sandbox", loadRuntimeConfig(sandboxEnv().env)],
    ["development", loadRuntimeConfig(developmentEnv().env)],
  ] as const) {
    assert.throws(
      () => assertDeploymentSafety(config, { VERCEL_ENV: "production" }),
      VercelRuntimeError,
      `${label} was accepted by a Production deployment`,
    );
  }

  const response = await handleExchangeRequest(exchangeRequest(), {
    ...sandboxEnv().env,
    VERCEL_ENV: "production",
  });
  assert.equal(response.status, 503);
  assert.equal(await tokenFrom(response), undefined);
});

test("Q2. an unknown deployment target is refused", () => {
  const production = loadRuntimeConfig(productionEnv().env);
  assert.throws(
    () => assertDeploymentSafety(production, { VERCEL_ENV: "staging" }),
    (error: unknown) =>
      error instanceof VercelRuntimeError && error.reason === "unknown_deployment_target",
  );
});

test("Q3. Development mode is never reachable from the production route path", async () => {
  const development = loadRuntimeConfig(developmentEnv().env);
  assert.throws(
    () => assertDeploymentSafety(development, {}),
    (error: unknown) =>
      error instanceof VercelRuntimeError &&
      error.reason === "development_runtime_not_permitted",
  );

  const response = await handleExchangeRequest(exchangeRequest(), developmentEnv().env);
  assert.equal(response.status, 503);
  assert.equal(peekVercelRuntimeForTests(), null);
});

// ── R / S / T. lifecycle ──────────────────────────────────────────────────────────────

test("R. a successful runtime is built once and reused within the instance", () => {
  const h = harness();
  const env = { ...h.env };

  const first = getVercelRuntime(env);
  const second = getVercelRuntime(env);
  assert.equal(first, second, "the runtime was rebuilt");
  assert.equal(peekVercelRuntimeForTests(), first);

  // Proof it is genuinely cached: a later call with BROKEN configuration still succeeds,
  // because nothing is re-read. Configuration changes require a cold start.
  const third = getVercelRuntime({ SIGNALS_DEPLOY_ENV: "production" });
  assert.equal(third, first);
});

test("S. a failed initialisation caches nothing and does not poison the instance", async () => {
  // Three different failures in a row, each leaving the holder empty.
  for (const badEnv of [
    {},
    { SIGNALS_DEPLOY_ENV: "production" },
    { ...productionEnv({ CUSTOM_MIX_POOL_TIMEZONE: "UTC" }).env },
  ]) {
    assert.throws(() => getVercelRuntime(badEnv));
    assert.equal(peekVercelRuntimeForTests(), null, "a failure was cached");
  }

  // And the very next request, with good configuration, succeeds.
  const good = getVercelRuntime(productionEnv().env);
  assert.ok(good);
  assert.equal(peekVercelRuntimeForTests(), good);
});

test("S2. a failing route request leaves no partial runtime behind", async () => {
  const response = await handleExchangeRequest(exchangeRequest(), { VERCEL_ENV: "preview" });
  assert.equal(response.status, 503);
  assert.equal(peekVercelRuntimeForTests(), null);
});

test("T. the reset hook clears the holder", () => {
  const first = getVercelRuntime(productionEnv().env);
  assert.equal(peekVercelRuntimeForTests(), first);

  resetVercelRuntimeForTests();
  assert.equal(peekVercelRuntimeForTests(), null);

  const second = getVercelRuntime(productionEnv().env);
  assert.notEqual(second, first, "reset did not force a rebuild");
});

// ── U. import-time inertness ──────────────────────────────────────────────────────────

test("U. importing the route and runtime modules builds nothing", () => {
  assert.equal(
    RUNTIME_AT_IMPORT_TIME,
    null,
    "importing a route module constructed a runtime",
  );
});

test("U2. no Phase 3C-2 module executes anything at the top level", () => {
  // Same rule the Phase 3C-1 suite applies to every API module; re-asserted here for the
  // new files specifically, since these are the ones Vercel imports on every cold start.
  for (const relative of [
    "_lib/vercel-request.ts",
    "_lib/vercel-response.ts",
    "_lib/vercel-runtime.ts",
    "auth/exchange.ts",
    "edition.ts",
  ]) {
    const source = readFileSync(join(API_DIR, relative), "utf8");
    for (const [index, line] of source.split("\n").entries()) {
      if (!/^[A-Za-z_$]/.test(line)) continue; // only column-0 statements
      assert.ok(
        /^(import|export|const|let|type|interface|class|function|declare|async function)\b/.test(
          line,
        ),
        `${relative}:${index + 1} is an executable top-level statement`,
      );
    }
  }
});

test("U3. the route modules import the runtime lazily, not statically", () => {
  for (const relative of ["auth/exchange.ts", "edition.ts"]) {
    const source = readFileSync(join(API_DIR, relative), "utf8");
    assert.ok(
      /await import\(["'][^"']*vercel-runtime\.js["']\)/.test(source),
      `${relative} does not import the runtime lazily`,
    );
    assert.ok(
      !/^import .*vercel-runtime/m.test(source),
      `${relative} imports the runtime statically`,
    );
  }
});

// ── V. no secret in a response, a log or an error ─────────────────────────────────────

test("V. no response, log event or error carries a secret, a proof or an identifier", async () => {
  const h = harness();
  const runtime = prime(h);

  const ok = await exchangeRoute.fetch(exchangeRequest());
  const token = (await tokenFrom(ok)) ?? "";
  await editionRoute.fetch(editionRequest(token));
  await editionRoute.fetch(editionRequest(null));
  await exchangeRoute.fetch(exchangeRequest({ malformed: true }));

  const logged = JSON.stringify(h.logger.events);
  const responses = JSON.stringify([
    await (await editionRoute.fetch(editionRequest(null))).text(),
    await (await exchangeRoute.fetch(exchangeRequest({ malformed: true }))).text(),
  ]);

  const forbidden: [string, string][] = [
    ["device JWS", FIXTURE_DEVICE_JWS],
    ["history JWS", FIXTURE_HISTORY_JWS],
    ["access token", token],
    ["originalTransactionId", FIXTURE_ORIGINAL_TRANSACTION_ID],
    ["subject", runtime.dependencies.tokens.deriveSubject(FIXTURE_ORIGINAL_TRANSACTION_ID, "Production")],
    ["token HMAC secret", FIXTURE_HMAC_SECRET_PRODUCTION],
    ["Redis token", FIXTURE_REDIS_TOKEN_PRODUCTION],
    ["Apple private key", runtime.config.apple?.privateKeyPem ?? ""],
    ["Authorization value", "Bearer "],
  ];
  for (const [label, value] of forbidden) {
    if (!value) continue;
    assert.ok(!logged.includes(value), `${label} leaked into a log event`);
    assert.ok(!responses.includes(value), `${label} leaked into an error response`);
  }

  assert.ok(h.logger.events.length > 0, "the routes logged nothing at all");
});

test("V2. the request id from Vercel is what the route logs", async () => {
  const h = harness();
  prime(h);
  await exchangeRoute.fetch(
    exchangeRequest(undefined, { "x-vercel-id": "iad1::traceable-0001" }),
  );
  assert.ok(
    h.logger.events.some((event) => event.requestId === "iad1::traceable-0001"),
    `request ids seen: ${h.logger.events.map((e) => e.requestId).join(", ")}`,
  );
});

test("V3. a spoofed x-vercel-id is replaced before it can reach a log", async () => {
  const h = harness();
  prime(h);
  await exchangeRoute.fetch(
    exchangeRequest(undefined, { "x-vercel-id": 'x","injected":"yes' }),
  );
  const logged = JSON.stringify(h.logger.events);
  assert.ok(!logged.includes("injected"), "a forged request id reached the log");
});

/** Comments state intent; only executable code can violate an architectural boundary. */
function stripSourceComments(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
}

// ── W / X / Y / Z. the connected route keeps its boundaries ────────────────────────────────────

test("W. no route or transport module reads a published edition, manifest or the filesystem", () => {
  // Phase 3E-1 connected the EDITORIAL pool, and only through the composition layer. The
  // static Free edition (`latest.json`, `editions/`) stays a CDN artifact that no server
  // module ever opens, and no route touches the filesystem.
  const forbidden = ["latest.json", "editions/", "readFileSync", "readFile"];
  for (const relative of [
    "_lib/vercel-request.ts",
    "_lib/vercel-response.ts",
    "_lib/vercel-runtime.ts",
    "auth/exchange.ts",
    "edition.ts",
  ]) {
    // EXECUTABLE CODE only: `edition.ts` documents that the CLIENT falls back to the
    // static `latest.json`. Describing the client's behaviour is not reading the file.
    const source = stripSourceComments(readFileSync(join(API_DIR, relative), "utf8"));
    for (const needle of forbidden) {
      assert.ok(!source.includes(needle), `${relative} references ${needle}`);
    }
  }
  // The route performs no pool read and holds no credential: it receives an orchestration
  // result. Only the composition layer may name the store.
  const route = stripSourceComments(readFileSync(join(API_DIR, "edition.ts"), "utf8"));
  for (const needle of [
    "upstash",
    "KV_REST_API_URL",
    "KV_REST_API_TOKEN",
    "KV_REST_API_WRITE_TOKEN",
    "process.env",
    "editorial-mix-source",
    "signals:editorial-mix-pool",
  ]) {
    assert.ok(!route.includes(needle), `edition.ts references ${needle}`);
  }
});

test("X. no module can invoke the selector", () => {
  const forbidden = ["child_process", "spawn", "execFile", "exec(", "python", "selection.py"];
  for (const relative of [
    "_lib/vercel-request.ts",
    "_lib/vercel-response.ts",
    "_lib/vercel-runtime.ts",
    "auth/exchange.ts",
    "edition.ts",
  ]) {
    const source = readFileSync(join(API_DIR, relative), "utf8");
    for (const needle of forbidden) {
      assert.ok(!source.includes(needle), `${relative} references ${needle}`);
    }
  }
});

test("Y. an unavailable edition carries only the stable code, and never internals", async () => {
  const h = harness();
  prime(h);
  const token = await tokenFrom(await exchangeRoute.fetch(exchangeRequest()));

  const response = await editionRoute.fetch(editionRequest(token ?? ""));
  const body = (await response.json()) as Record<string, unknown>;
  assert.deepEqual(Object.keys(body).sort(), ["code", "status"]);
  assert.equal(body.status, "unavailable");
  assert.equal(body.code, "custom_mix_unavailable");
  // No partial edition, and no diagnostic surface of any kind.
  for (const key of ["stories", "articles", "items", "edition", "mix", "pool", "signals",
                     "reason", "detail", "selector", "candidateLogs"]) {
    assert.equal(key in body, false, `the edition response carried ${key}`);
  }
  const text = JSON.stringify(body).toLowerCase();
  for (const leak of ["upstash", "kv_rest", "signals:editorial", "candidate_pool"]) {
    assert.ok(!text.includes(leak), `the failure body leaked ${leak}`);
  }
});

test("Z. the production route path never reaches a fake verifier", async () => {
  const h = harness();
  const runtime = prime(h);
  // The composed verifier is the live one, with Apple's current-state check mandatory.
  assert.ok(runtime.dependencies.verifier instanceof LiveAppleEntitlementVerifier);

  // And neither route module can even name the development factory.
  for (const relative of ["auth/exchange.ts", "edition.ts", "_lib/vercel-runtime.ts"]) {
    const source = readFileSync(join(API_DIR, relative), "utf8");
    assert.ok(
      !/createDevelopmentVercelRuntime\s*\(/.test(source.replace(/export function [^(]*\(/g, "")),
      `${relative} calls the development entry point`,
    );
  }
});

test("Z2. the development entry point refuses every non-development mode", async () => {
  const { createDevelopmentVercelRuntime } = await import("../_lib/vercel-runtime.js");
  assert.throws(
    () =>
      createDevelopmentVercelRuntime({
        env: productionEnv().env,
        overrides: {
          fakeVerifier: { verifySignedTransaction: async () => ({}) as never },
        },
      }),
    (error: unknown) =>
      error instanceof VercelRuntimeError &&
      error.reason === "development_entry_point_requires_development_mode",
  );
  // Using it does not populate the holder the production routes read.
  assert.equal(peekVercelRuntimeForTests(), null);
});
