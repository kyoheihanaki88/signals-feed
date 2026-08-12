/**
 * Runtime-init failure diagnostics — allowlisted classification logging.
 *
 * The Production incident this exists for: a runtime-construction failure returns 503
 * `verification_unavailable` with NO server-side record of which contract failed. These
 * tests pin the diagnostic added to `vercel-runtime.ts`:
 *
 *   • the HTTP response is byte-for-byte the same fail-closed 503 as before;
 *   • one `runtime_init_failed` event is logged through the SecurityLogger abstraction;
 *   • the classification comes from a FIXED allowlist chosen by instanceof, and the
 *     recorded codes are only the mechanical issue/reason fields of the known classes;
 *   • unknown errors record the classification alone — message/stack are never read;
 *   • no environment value, PEM, secret or identifier can appear in the event;
 *   • successful initialisation logs nothing.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { AppleRootCertificateError } from "../_lib/apple-root-certificates.js";
import { RuntimeConfigError } from "../_lib/runtime-config.js";
import { RuntimeCompositionError } from "../_lib/runtime-dependencies.js";
import { MemorySecurityLogger } from "../_lib/security-logging.js";
import {
  VercelRuntimeError,
  classifyRuntimeInitFailure,
  handleEditionRequest,
  handleExchangeRequest,
  primeVercelRuntimeForTests,
  resetRuntimeInitDiagnosticLoggerForTests,
  resetVercelRuntimeForTests,
  setRuntimeInitDiagnosticLoggerForTests,
} from "../_lib/vercel-runtime.js";
import {
  FIXTURE_DEVICE_JWS,
  FakeSignedTransactionDecoder,
  FakeTransactionHistoryFetcher,
  FixedClock,
  codeOf,
  decodedTransaction,
  developmentEnv,
  memoryRedis,
  productionEnv,
} from "./runtime-fixtures.js";

const VERCEL_REQUEST_ID = "iad1::abcde-1234567890123-0a1b2c3d";

function exchangeRequest(headers: Record<string, string> = {}): Request {
  return new Request("https://signals.example/api/auth/exchange", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-vercel-id": VERCEL_REQUEST_ID,
      ...headers,
    },
    body: JSON.stringify({
      signedTransactionInfo: FIXTURE_DEVICE_JWS,
      selectorVersion: 1,
    }),
  });
}

/** Every diagnostic test runs isolated: no cached runtime, a capturing logger. */
async function withDiagnostics(
  run: (logger: MemorySecurityLogger) => Promise<void>,
): Promise<void> {
  resetVercelRuntimeForTests();
  const logger = new MemorySecurityLogger();
  setRuntimeInitDiagnosticLoggerForTests(logger);
  try {
    await run(logger);
  } finally {
    resetRuntimeInitDiagnosticLoggerForTests();
    resetVercelRuntimeForTests();
  }
}

// ── A. the classification allowlist, one class at a time ────────────────────────────────

test("A1. RuntimeConfigError classifies as runtime_config with its issues", () => {
  const result = classifyRuntimeInitFailure(
    new RuntimeConfigError(["SIGNALS_DEPLOY_ENV is required"]),
  );
  assert.equal(result.classification, "runtime_config");
  assert.deepEqual(result.codes, ["SIGNALS_DEPLOY_ENV is required"]);
});

test("A2. VercelRuntimeError classifies as vercel_runtime with its reason", () => {
  const result = classifyRuntimeInitFailure(
    new VercelRuntimeError("production_requires_production_configuration"),
  );
  assert.equal(result.classification, "vercel_runtime");
  assert.deepEqual(result.codes, ["production_requires_production_configuration"]);
});

test("A3. RuntimeCompositionError classifies as runtime_composition with its reason", () => {
  const result = classifyRuntimeInitFailure(
    new RuntimeCompositionError("token_key_material_unusable"),
  );
  assert.equal(result.classification, "runtime_composition");
  assert.deepEqual(result.codes, ["token_key_material_unusable"]);
});

test("A4. AppleRootCertificateError classifies as apple_root_certificate with its reason", () => {
  const result = classifyRuntimeInitFailure(
    new AppleRootCertificateError("manifest file is missing or unreadable"),
  );
  assert.equal(result.classification, "apple_root_certificate");
  assert.deepEqual(result.codes, ["manifest file is missing or unreadable"]);
});

test("A5. any other error is unknown, and its message is never recorded", () => {
  const secretish = new Error(
    "-----BEGIN PRIVATE KEY----- MIGfake+secret+material -----END PRIVATE KEY-----",
  );
  const result = classifyRuntimeInitFailure(secretish);
  assert.equal(result.classification, "unknown");
  assert.deepEqual(result.codes, []);
});

test("A6. non-Error throwables are unknown too", () => {
  assert.equal(classifyRuntimeInitFailure("a thrown string").classification, "unknown");
  assert.equal(classifyRuntimeInitFailure(42).classification, "unknown");
  assert.equal(classifyRuntimeInitFailure(undefined).classification, "unknown");
});

// ── B. the handle path: response unchanged, one event, no values ────────────────────────

test("B1. a missing contract fails 503 verification_unavailable and logs runtime_config", async () => {
  await withDiagnostics(async (logger) => {
    const response = await handleExchangeRequest(exchangeRequest(), {});
    assert.equal(response.status, 503);
    assert.equal(await codeOf(response), "verification_unavailable");

    assert.equal(logger.events.length, 1);
    const event = logger.events[0]!;
    assert.equal(event.reasonCode, "runtime_init_failed");
    assert.equal(event.route, "/api/auth/exchange");
    assert.equal(event.status, 503);
    assert.equal(event.requestId, VERCEL_REQUEST_ID);
    assert.equal(event.initFailure?.classification, "runtime_config");
    assert.ok(
      event.initFailure?.codes?.some((code) => code.includes("SIGNALS_DEPLOY_ENV")),
      "the failing VARIABLE must be named",
    );
  });
});

test("B2. the edition route logs with its own route name", async () => {
  await withDiagnostics(async (logger) => {
    const request = new Request("https://signals.example/api/edition", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: "{}",
    });
    const response = await handleEditionRequest(request, {});
    assert.equal(response.status, 503);
    assert.equal(logger.events[0]?.route, "/api/edition");
  });
});

test("B3. a development configuration on the production entry logs vercel_runtime", async () => {
  await withDiagnostics(async (logger) => {
    const response = await handleExchangeRequest(
      exchangeRequest(),
      developmentEnv().env,
    );
    assert.equal(response.status, 503);
    assert.equal(await codeOf(response), "verification_unavailable");
    assert.equal(logger.events[0]?.initFailure?.classification, "vercel_runtime");
    assert.deepEqual(logger.events[0]?.initFailure?.codes, [
      "development_runtime_not_permitted",
    ]);
  });
});

test("B4. no environment VALUE reaches the event — only variable names", async () => {
  await withDiagnostics(async (logger) => {
    // A realistic broken deployment: every secret present, one contract value absent.
    // Each secret category carries a UNIQUE non-empty sentinel so a leak of any one of
    // them is individually detectable in the serialized event.
    const sentinels = {
      hmacSecret: "sentinel-hmac-pepper-0123456789abcdef0123456789",
      kvToken: "sentinel-kv-rest-token-not-a-real-credential",
      kvUrl: "https://sentinel-kv-fixture.example.upstash.io",
      issuerId: "sentine1-0000-4000-8000-feedfacecafe",
      keyId: "SENTINELKY9",
      appAppleId: "1234509876",
    };
    const { env, tokenSigning, applePrivateKey } = productionEnv({
      SIGNALS_DEPLOY_ENV: undefined,
      SIGNALS_TOKEN_HMAC_SECRET: sentinels.hmacSecret,
      KV_REST_API_TOKEN: sentinels.kvToken,
      KV_REST_API_URL: sentinels.kvUrl,
      APP_STORE_ISSUER_ID: sentinels.issuerId,
      APP_STORE_KEY_ID: sentinels.keyId,
      APPLE_APP_APPLE_ID: sentinels.appAppleId,
    });
    await handleExchangeRequest(exchangeRequest(), env);

    assert.equal(logger.events.length, 1);
    const serialized = JSON.stringify(logger.events);
    // None of the values that were IN the environment may appear in the event. The PEM
    // entries use the first base64 body line — unique per generated key and never empty.
    const forbidden: Array<[name: string, value: string]> = [
      ["pem marker", "BEGIN PRIVATE KEY"],
      ["token signing key body", tokenSigning.privateKeyPem.split("\n")[1] ?? ""],
      ["token public key body", tokenSigning.publicKeyPem.split("\n")[1] ?? ""],
      ["apple private key body", applePrivateKey.privateKeyPem.split("\n")[1] ?? ""],
      ["hmac secret", sentinels.hmacSecret],
      ["kv token", sentinels.kvToken],
      ["kv url", sentinels.kvUrl],
      ["issuer id", sentinels.issuerId],
      ["key id", sentinels.keyId],
      ["app apple id", sentinels.appAppleId],
      ["bundle id", String(env.APPLE_BUNDLE_ID ?? "")],
      ["device jws", FIXTURE_DEVICE_JWS], // nothing from the request body either
    ];
    // Guard the test itself first: an empty forbidden value would match EVERY string
    // (`"".includes` is always true) and make the leak check vacuous.
    for (const [name, value] of forbidden) {
      assert.ok(value.length > 0, `forbidden-value fixture for ${name} is empty`);
    }
    for (const [name, value] of forbidden) {
      assert.ok(!serialized.includes(value), `event leaked the ${name}`);
    }
  });
});

test("B5. an unsafe x-vercel-id is replaced, never echoed into the log", async () => {
  await withDiagnostics(async (logger) => {
    // The same spoof fixture as vercel-runtime.test.ts V3: a value `Headers` ACCEPTS
    // (no CR/LF — the platform would reject those before any app code runs) but that
    // fails `deriveRequestId`'s bounded pattern, because `"` and `,` are log-injection
    // characters in a JSON line.
    const hostile = 'x","injected":"yes';
    await handleExchangeRequest(
      exchangeRequest({ "x-vercel-id": hostile }),
      {},
    );
    const requestId = logger.events[0]?.requestId ?? "";
    assert.notEqual(requestId, hostile);
    assert.ok(!JSON.stringify(logger.events).includes("injected"));
    assert.match(requestId, /^[A-Za-z0-9-]{36}$/); // a generated UUID
  });
});

test("B6. a throwing logger still returns the same 503", async () => {
  resetVercelRuntimeForTests();
  setRuntimeInitDiagnosticLoggerForTests({
    log() {
      throw new Error("logger transport failed");
    },
  });
  try {
    const response = await handleExchangeRequest(exchangeRequest(), {});
    assert.equal(response.status, 503);
    assert.equal(await codeOf(response), "verification_unavailable");
  } finally {
    resetRuntimeInitDiagnosticLoggerForTests();
    resetVercelRuntimeForTests();
  }
});

// ── C. successful initialisation logs nothing ───────────────────────────────────────────

test("C1. a healthy runtime emits no runtime_init_failed event", async () => {
  await withDiagnostics(async (logger) => {
    primeVercelRuntimeForTests({
      env: productionEnv().env,
      transports: {
        clock: new FixedClock(),
        logger: new MemorySecurityLogger(),
        redisClient: memoryRedis(),
        appleDecoder: new FakeSignedTransactionDecoder(
          decodedTransaction("Production"),
        ),
        transactionHistoryFetcher: new FakeTransactionHistoryFetcher(),
        sleep: async () => {},
      },
    });
    // An invalid body exercises the normal route path: the 400 proves the runtime was
    // built and serving, and the diagnostic logger must have stayed silent.
    const emptyBody = new Request("https://signals.example/api/auth/exchange", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: "{}",
    });
    const response = await handleExchangeRequest(emptyBody);
    assert.equal(response.status, 400);
    assert.equal(await codeOf(response), "invalid_request");
    assert.equal(logger.events.length, 0);
  });
});
