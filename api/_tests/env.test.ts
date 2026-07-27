import assert from "node:assert/strict";
import test from "node:test";
import {
  EnvironmentContractError,
  loadApiConfig,
  normalizePem,
  usesFakeAppleVerification,
  type RawEnv,
} from "../_lib/env.js";

// ── dummy values only — nothing here is a real key, token or credential ──────────────
const DUMMY_PRIVATE_KEY =
  "-----BEGIN PRIVATE KEY-----\nMIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQg\n-----END PRIVATE KEY-----";
const DUMMY_PUBLIC_KEY =
  "-----BEGIN PUBLIC KEY-----\nMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE\n-----END PUBLIC KEY-----";
const DUMMY_HMAC = "x".repeat(48);

function tokenVars(): RawEnv {
  return {
    SIGNALS_TOKEN_SIGNING_KID: "kid-current",
    SIGNALS_TOKEN_SIGNING_KEY: DUMMY_PRIVATE_KEY,
    SIGNALS_TOKEN_PUBLIC_KEY: DUMMY_PUBLIC_KEY,
    SIGNALS_TOKEN_HMAC_SECRET: DUMMY_HMAC,
  };
}

function productionEnv(overrides: RawEnv = {}): RawEnv {
  return {
    SIGNALS_DEPLOY_ENV: "production",
    APPLE_ENVIRONMENT: "Production",
    APPLE_BUNDLE_ID: "com.kyohei.Signals",
    APPLE_APP_APPLE_ID: "1234567890",
    APP_STORE_ISSUER_ID: "dummy-issuer",
    APP_STORE_KEY_ID: "DUMMYKEYID",
    APP_STORE_PRIVATE_KEY: DUMMY_PRIVATE_KEY,
    KV_REST_API_URL: "https://example.upstash.io",
    KV_REST_API_TOKEN: "dummy-token",
    ...tokenVars(),
    ...overrides,
  };
}

function sandboxEnv(overrides: RawEnv = {}): RawEnv {
  return {
    ...productionEnv(),
    SIGNALS_DEPLOY_ENV: "sandbox",
    APPLE_ENVIRONMENT: "Sandbox",
    APPLE_APP_APPLE_ID: undefined,
    ...overrides,
  };
}

test("valid Production configuration parses", () => {
  const config = loadApiConfig(productionEnv());
  assert.equal(config.deployEnvironment, "production");
  assert.ok(config.apple);
  assert.equal(config.apple.environment, "Production");
  assert.equal(config.apple.bundleId, "com.kyohei.Signals");
  assert.equal(config.apple.appAppleId, 1234567890);
  assert.equal(config.redis?.namespace, "signals:production:production");
  assert.equal(usesFakeAppleVerification(config), false);
});

test("valid Sandbox/TestFlight configuration parses without appAppleId", () => {
  const config = loadApiConfig(sandboxEnv());
  assert.equal(config.deployEnvironment, "sandbox");
  assert.equal(config.apple?.environment, "Sandbox");
  assert.equal(config.apple?.appAppleId, undefined);
  assert.equal(config.redis?.namespace, "signals:sandbox:sandbox");
});

test("Production and Sandbox get different Redis namespaces", () => {
  const production = loadApiConfig(productionEnv());
  const sandbox = loadApiConfig(sandboxEnv());
  assert.notEqual(production.redis?.namespace, sandbox.redis?.namespace);
});

test("valid Preview configuration is fake-verification only", () => {
  const config = loadApiConfig({ SIGNALS_DEPLOY_ENV: "preview", ...tokenVars() });
  assert.equal(config.deployEnvironment, "preview");
  assert.equal(config.apple, null);
  assert.equal(config.redis, null);
  assert.equal(usesFakeAppleVerification(config), true);
});

test("missing required variable is rejected", () => {
  const env = productionEnv({ APP_STORE_KEY_ID: undefined });
  assert.throws(
    () => loadApiConfig(env),
    (error: unknown) =>
      error instanceof EnvironmentContractError &&
      error.issues.some((issue) => issue.includes("APP_STORE_KEY_ID")),
  );
});

test("empty-string variable counts as missing", () => {
  assert.throws(
    () => loadApiConfig(productionEnv({ APPLE_BUNDLE_ID: "   " })),
    (error: unknown) =>
      error instanceof EnvironmentContractError &&
      error.issues.some((issue) => issue.includes("APPLE_BUNDLE_ID")),
  );
});

test("malformed private key is rejected", () => {
  assert.throws(
    () => loadApiConfig(productionEnv({ APP_STORE_PRIVATE_KEY: "not-a-pem" })),
    (error: unknown) =>
      error instanceof EnvironmentContractError &&
      error.issues.some((issue) => issue.includes("APP_STORE_PRIVATE_KEY")),
  );
});

test("multiline private key supplied with escaped newlines is accepted", () => {
  const escaped = DUMMY_PRIVATE_KEY.replace(/\n/g, "\\n");
  const config = loadApiConfig(productionEnv({ APP_STORE_PRIVATE_KEY: escaped }));
  assert.ok(config.apple?.privateKeyPem.includes("\n"));
  assert.ok(config.apple?.privateKeyPem.startsWith("-----BEGIN PRIVATE KEY-----"));
});

test("normalizePem handles CRLF and escaped newlines", () => {
  assert.equal(normalizePem("a\\nb"), "a\nb");
  assert.equal(normalizePem("a\r\nb"), "a\nb");
});

test("missing Production appAppleId is rejected", () => {
  assert.throws(
    () => loadApiConfig(productionEnv({ APPLE_APP_APPLE_ID: undefined })),
    (error: unknown) =>
      error instanceof EnvironmentContractError &&
      error.issues.some((issue) => issue.includes("APPLE_APP_APPLE_ID")),
  );
});

test("non-numeric Production appAppleId is rejected", () => {
  assert.throws(
    () => loadApiConfig(productionEnv({ APPLE_APP_APPLE_ID: "not-a-number" })),
    (error: unknown) =>
      error instanceof EnvironmentContractError &&
      error.issues.some((issue) => issue.includes("numeric")),
  );
});

test("Production credentials present in Preview are rejected", () => {
  assert.throws(
    () =>
      loadApiConfig({
        SIGNALS_DEPLOY_ENV: "preview",
        ...tokenVars(),
        APP_STORE_PRIVATE_KEY: DUMMY_PRIVATE_KEY,
        APP_STORE_ISSUER_ID: "dummy-issuer",
      }),
    (error: unknown) =>
      error instanceof EnvironmentContractError &&
      error.issues.some((issue) => issue.includes("must not be set in Preview")),
  );
});

test("Apple environment contradicting the deployment is rejected", () => {
  assert.throws(
    () => loadApiConfig(productionEnv({ APPLE_ENVIRONMENT: "Sandbox" })),
    (error: unknown) =>
      error instanceof EnvironmentContractError &&
      error.issues.some((issue) => issue.includes("contradicts")),
  );
  assert.throws(
    () => loadApiConfig(sandboxEnv({ APPLE_ENVIRONMENT: "Production" })),
    (error: unknown) => error instanceof EnvironmentContractError,
  );
});

test("unknown deploy environment is rejected", () => {
  assert.throws(
    () => loadApiConfig({ SIGNALS_DEPLOY_ENV: "staging", ...tokenVars() }),
    (error: unknown) => error instanceof EnvironmentContractError,
  );
});

test("missing Redis configuration is rejected outside Preview", () => {
  assert.throws(
    () =>
      loadApiConfig(
        productionEnv({ KV_REST_API_URL: undefined, KV_REST_API_TOKEN: undefined }),
      ),
    (error: unknown) =>
      error instanceof EnvironmentContractError &&
      error.issues.some((issue) => issue.includes("KV_REST_API_URL")),
  );
});

test("half-configured Redis is rejected", () => {
  assert.throws(
    () => loadApiConfig(productionEnv({ KV_REST_API_TOKEN: undefined })),
    (error: unknown) => error instanceof EnvironmentContractError,
  );
});

test("non-https Redis URL is rejected", () => {
  assert.throws(
    () => loadApiConfig(productionEnv({ KV_REST_API_URL: "http://example.upstash.io" })),
    (error: unknown) =>
      error instanceof EnvironmentContractError &&
      error.issues.some((issue) => issue.includes("https")),
  );
});

test("a half-configured previous verification key is rejected", () => {
  assert.throws(
    () => loadApiConfig(productionEnv({ SIGNALS_TOKEN_PREVIOUS_KID: "kid-old" })),
    (error: unknown) =>
      error instanceof EnvironmentContractError &&
      error.issues.some((issue) => issue.includes("PREVIOUS")),
  );
});

test("a complete previous verification key pair is accepted", () => {
  const config = loadApiConfig(
    productionEnv({
      SIGNALS_TOKEN_PREVIOUS_KID: "kid-old",
      SIGNALS_TOKEN_PREVIOUS_PUBLIC_KEY: DUMMY_PUBLIC_KEY,
    }),
  );
  assert.equal(config.token.previousKid, "kid-old");
  assert.ok(config.token.previousPublicKeyPem);
});

test("a short HMAC secret is rejected", () => {
  assert.throws(
    () => loadApiConfig(productionEnv({ SIGNALS_TOKEN_HMAC_SECRET: "too-short" })),
    (error: unknown) =>
      error instanceof EnvironmentContractError &&
      error.issues.some((issue) => issue.includes("32 characters")),
  );
});

test("issuer and audience default to the Phase 3B-1 contract", () => {
  const config = loadApiConfig(productionEnv());
  assert.equal(config.token.issuer, "signals-auth");
  assert.equal(config.token.audience, "signals-custom-mix");
});

test("approved limit overrides are honored and validated", () => {
  const config = loadApiConfig(productionEnv({ SIGNALS_RATE_EXCHANGE_IP_PER_MIN: "25" }));
  assert.equal(config.limits.exchangePerIpPerMinute, 25);
  assert.throws(
    () => loadApiConfig(productionEnv({ SIGNALS_RATE_EXCHANGE_IP_PER_MIN: "0" })),
    (error: unknown) => error instanceof EnvironmentContractError,
  );
  assert.throws(
    () => loadApiConfig(productionEnv({ SIGNALS_APPLE_BREAKER_THRESHOLD: "abc" })),
    (error: unknown) => error instanceof EnvironmentContractError,
  );
});

test("secret values never appear in thrown errors", () => {
  const secret = "SUPER-SECRET-KEY-MATERIAL-DO-NOT-LEAK";
  try {
    loadApiConfig(
      productionEnv({
        APP_STORE_PRIVATE_KEY: secret,
        KV_REST_API_TOKEN: `${secret}-token`,
      }),
    );
    assert.fail("expected the contract to reject this configuration");
  } catch (error) {
    assert.ok(error instanceof EnvironmentContractError);
    const serialized = `${error.message} ${JSON.stringify(error.issues)} ${error.stack ?? ""}`;
    assert.ok(!serialized.includes(secret), "secret material leaked into the error");
  }
});
