/**
 * Phase 3C-1 — runtime configuration contract.
 *
 * Covers C, D, E, F, G, H, I, J from the required list plus secret normalization, mode
 * separation, and the rule that an error names a variable and never its value.
 */

import assert from "node:assert/strict";
import test from "node:test";
import { createPrivateKey, generateKeyPairSync } from "node:crypto";

import {
  RuntimeConfigError,
  isRealDeployment,
  loadRuntimeConfig,
} from "../_lib/runtime-config.js";
import {
  FIXTURE_APP_APPLE_ID,
  FIXTURE_HMAC_SECRET_PRODUCTION,
  FIXTURE_PRODUCT_ID,
  FIXTURE_REDIS_TOKEN_PRODUCTION,
  developmentEnv,
  escapeNewlines,
  generateEs256Keypair,
  productionEnv,
  sandboxEnv,
} from "./runtime-fixtures.js";

function issuesOf(run: () => unknown): string[] {
  try {
    run();
  } catch (error) {
    assert.ok(error instanceof RuntimeConfigError, `expected RuntimeConfigError, got ${error}`);
    return [...error.issues];
  }
  assert.fail("expected the configuration to be rejected");
}

/** Everything a thrown error is allowed to see: nothing. */
function serialize(error: unknown): string {
  const e = error as Error & { issues?: string[] };
  return `${e?.name ?? ""} ${e?.message ?? ""} ${e?.stack ?? ""} ${(e?.issues ?? []).join(" ")}`;
}

// ── happy paths ───────────────────────────────────────────────────────────────────────

test("a valid Production environment produces a Production runtime config", () => {
  const { env, applePrivateKey } = productionEnv();
  const config = loadRuntimeConfig(env);

  assert.equal(config.mode, "production");
  assert.ok(isRealDeployment(config));
  assert.equal(config.apple?.environment, "Production");
  assert.equal(config.apple?.appAppleId, Number(FIXTURE_APP_APPLE_ID));
  assert.equal(config.apple?.productId, FIXTURE_PRODUCT_ID);
  assert.equal(config.apple?.privateKeyPem, applePrivateKey.privateKeyPem.trim());
  assert.equal(config.apple?.enableOnlineChecks, true);
  assert.equal(config.customMix.enabled, true);
  assert.equal(config.customMix.selectorVersion, 2);
  assert.equal(config.customMix.storyCount, 5);
  assert.equal(config.customMix.poolTimezone, "America/New_York");
  assert.equal(config.token.ttlSeconds, 900);
  assert.equal(config.token.clockSkewSeconds, 60);
  assert.equal(config.storage?.namespace, "signals:production:production");
  assert.equal(config.storage?.poolNamespace, "signals:production:production:pool");
});

test("a valid Sandbox environment produces an isolated Sandbox runtime config", () => {
  const config = loadRuntimeConfig(sandboxEnv().env);

  assert.equal(config.mode, "sandbox");
  assert.equal(config.apple?.environment, "Sandbox");
  assert.equal(config.apple?.appAppleId, undefined);
  // Sandbox uses Apple's test certificate chain; online checks are meaningless there.
  assert.equal(config.apple?.enableOnlineChecks, false);
  assert.equal(config.storage?.namespace, "signals:sandbox:sandbox");
});

test("Development requires no Apple credentials and refuses to hold them", () => {
  const config = loadRuntimeConfig(developmentEnv().env);
  assert.equal(config.mode, "development");
  assert.equal(config.apple, null);
  assert.equal(isRealDeployment(config), false);

  const leaked = developmentEnv({ APPLE_PRODUCT_ID: FIXTURE_PRODUCT_ID });
  assert.ok(
    issuesOf(() => loadRuntimeConfig(leaked.env)).some((issue) =>
      issue.includes("APPLE_PRODUCT_ID"),
    ),
  );
});

test("`preview` remains accepted as the deploy alias for Development", () => {
  const config = loadRuntimeConfig(
    developmentEnv({ SIGNALS_DEPLOY_ENV: "preview" }).env,
  );
  assert.equal(config.mode, "development");
});

// ── C. missing appAppleId in Production ───────────────────────────────────────────────

test("C. Production without APPLE_APP_APPLE_ID fails", () => {
  const issues = issuesOf(() =>
    loadRuntimeConfig(productionEnv({ APPLE_APP_APPLE_ID: undefined }).env),
  );
  assert.ok(issues.some((issue) => issue.includes("APPLE_APP_APPLE_ID")));
});

test("C2. a non-positive APPLE_APP_APPLE_ID fails", () => {
  for (const value of ["0", "-1", "abc", "12.5"]) {
    const issues = issuesOf(() =>
      loadRuntimeConfig(productionEnv({ APPLE_APP_APPLE_ID: value }).env),
    );
    assert.ok(
      issues.some((issue) => issue.includes("APPLE_APP_APPLE_ID")),
      `value ${value} was accepted`,
    );
  }
});

test("C3. Sandbox must not carry an app id — a Production value cannot be reused", () => {
  const issues = issuesOf(() =>
    loadRuntimeConfig(sandboxEnv({ APPLE_APP_APPLE_ID: FIXTURE_APP_APPLE_ID }).env),
  );
  assert.ok(issues.some((issue) => issue.includes("APPLE_APP_APPLE_ID")));
});

test("C4. a Production deployment declaring APPLE_ENVIRONMENT=Sandbox is a contradiction", () => {
  const issues = issuesOf(() =>
    loadRuntimeConfig(productionEnv({ APPLE_ENVIRONMENT: "Sandbox" }).env),
  );
  assert.ok(issues.some((issue) => issue.includes("APPLE_ENVIRONMENT")));
});

// ── D / E. Apple private key ──────────────────────────────────────────────────────────

test("D. a missing APP_STORE_PRIVATE_KEY fails", () => {
  const issues = issuesOf(() =>
    loadRuntimeConfig(productionEnv({ APP_STORE_PRIVATE_KEY: undefined }).env),
  );
  assert.ok(issues.some((issue) => issue.includes("APP_STORE_PRIVATE_KEY")));
});

test("E. a malformed APP_STORE_PRIVATE_KEY fails without leaking a single byte of it", () => {
  // PEM-SHAPED but not a key: this is exactly the value that must never be echoed.
  const bogusBody = "TOTALLYNOTAKEYbutBase64Shaped0123456789abcdefgh=";
  const bogus = `-----BEGIN PRIVATE KEY-----\n${bogusBody}\n-----END PRIVATE KEY-----`;

  try {
    loadRuntimeConfig(productionEnv({ APP_STORE_PRIVATE_KEY: bogus }).env);
    assert.fail("expected the malformed key to be rejected");
  } catch (error) {
    assert.ok(error instanceof RuntimeConfigError);
    assert.ok(error.issues.some((issue) => issue.includes("APP_STORE_PRIVATE_KEY")));
    const text = serialize(error);
    assert.ok(!text.includes(bogusBody), "the key body leaked into the error");
    assert.ok(!text.includes("BEGIN PRIVATE KEY"), "PEM contents leaked into the error");
  }
});

test("E2. a PEM-shaped token signing key that is not real key material fails", () => {
  const issues = issuesOf(() =>
    loadRuntimeConfig(
      productionEnv({
        SIGNALS_TOKEN_SIGNING_KEY:
          "-----BEGIN PRIVATE KEY-----\nQUJD\n-----END PRIVATE KEY-----",
      }).env,
    ),
  );
  assert.ok(issues.some((issue) => issue.includes("SIGNALS_TOKEN_SIGNING_KEY")));
});

test("E3. a non-P-256 signing key is rejected — ES256 is the only accepted shape", () => {
  const rsa = generateKeyPairSync("rsa", {
    modulusLength: 2048,
    privateKeyEncoding: { type: "pkcs8", format: "pem" },
    publicKeyEncoding: { type: "spki", format: "pem" },
  });
  const issues = issuesOf(() =>
    loadRuntimeConfig(
      productionEnv({
        SIGNALS_TOKEN_SIGNING_KEY: rsa.privateKey,
        SIGNALS_TOKEN_PUBLIC_KEY: rsa.publicKey,
      }).env,
    ),
  );
  assert.ok(issues.some((issue) => issue.includes("SIGNALS_TOKEN_SIGNING_KEY")));
});

// ── F. Redis credentials ──────────────────────────────────────────────────────────────

test("F. missing Redis credentials fail in Production", () => {
  const issues = issuesOf(() =>
    loadRuntimeConfig(
      productionEnv({ KV_REST_API_URL: undefined, KV_REST_API_TOKEN: undefined }).env,
    ),
  );
  assert.ok(issues.some((issue) => issue.includes("KV_REST_API_URL")));
});

test("F2. a half-configured or non-https Redis endpoint fails", () => {
  assert.ok(
    issuesOf(() => loadRuntimeConfig(productionEnv({ KV_REST_API_TOKEN: undefined }).env)).some(
      (issue) => issue.includes("KV_REST_API_TOKEN"),
    ),
  );
  assert.ok(
    issuesOf(() =>
      loadRuntimeConfig(productionEnv({ KV_REST_API_URL: "http://insecure.example" }).env),
    ).some((issue) => issue.includes("KV_REST_API_URL")),
  );
  assert.ok(
    issuesOf(() =>
      loadRuntimeConfig(productionEnv({ KV_REST_API_URL: "not-a-url" }).env),
    ).some((issue) => issue.includes("KV_REST_API_URL")),
  );
});

// ── G. token TTL ──────────────────────────────────────────────────────────────────────

test("G. a token TTL other than 900 seconds fails", () => {
  for (const value of ["1800", "60", "0", "nine hundred"]) {
    const issues = issuesOf(() =>
      loadRuntimeConfig(productionEnv({ SIGNALS_TOKEN_TTL_SECONDS: value }).env),
    );
    assert.ok(
      issues.some((issue) => issue.includes("SIGNALS_TOKEN_TTL_SECONDS")),
      `TTL ${value} was accepted`,
    );
  }
});

test("G2. a clock skew other than the compiled 60 seconds fails", () => {
  const issues = issuesOf(() =>
    loadRuntimeConfig(productionEnv({ SIGNALS_TOKEN_CLOCK_SKEW_SECONDS: "600" }).env),
  );
  assert.ok(issues.some((issue) => issue.includes("SIGNALS_TOKEN_CLOCK_SKEW_SECONDS")));
});

test("G3. a token issuer or audience that disagrees with the service fails", () => {
  assert.ok(
    issuesOf(() =>
      loadRuntimeConfig(productionEnv({ SIGNALS_TOKEN_ISSUER: "someone-else" }).env),
    ).some((issue) => issue.includes("SIGNALS_TOKEN_ISSUER")),
  );
  assert.ok(
    issuesOf(() =>
      loadRuntimeConfig(productionEnv({ SIGNALS_TOKEN_AUDIENCE: "other-audience" }).env),
    ).some((issue) => issue.includes("SIGNALS_TOKEN_AUDIENCE")),
  );
});

// ── H / I / J. Custom Mix invariants ──────────────────────────────────────────────────

test("H. a selector version other than the supported one (2) fails", () => {
  for (const value of ["1", "0", "v1"]) {
    assert.ok(
      issuesOf(() =>
        loadRuntimeConfig(productionEnv({ CUSTOM_MIX_SELECTOR_VERSION: value }).env),
      ).some((issue) => issue.includes("CUSTOM_MIX_SELECTOR_VERSION")),
      `selector version ${value} was accepted`,
    );
  }
  assert.ok(
    issuesOf(() =>
      loadRuntimeConfig(productionEnv({ CUSTOM_MIX_SELECTOR_VERSION: undefined }).env),
    ).some((issue) => issue.includes("CUSTOM_MIX_SELECTOR_VERSION")),
  );
});

test("I. a story count other than 5 fails", () => {
  for (const value of ["4", "6", "five"]) {
    assert.ok(
      issuesOf(() => loadRuntimeConfig(productionEnv({ CUSTOM_MIX_STORY_COUNT: value }).env)).some(
        (issue) => issue.includes("CUSTOM_MIX_STORY_COUNT"),
      ),
      `story count ${value} was accepted`,
    );
  }
  assert.ok(
    issuesOf(() =>
      loadRuntimeConfig(productionEnv({ CUSTOM_MIX_STORY_COUNT: undefined }).env),
    ).some((issue) => issue.includes("CUSTOM_MIX_STORY_COUNT")),
  );
});

test("J. a pool timezone other than America/New_York fails", () => {
  for (const value of ["UTC", "Asia/Tokyo", "America/Chicago"]) {
    assert.ok(
      issuesOf(() =>
        loadRuntimeConfig(productionEnv({ CUSTOM_MIX_POOL_TIMEZONE: value }).env),
      ).some((issue) => issue.includes("CUSTOM_MIX_POOL_TIMEZONE")),
      `timezone ${value} was accepted`,
    );
  }
});

test("J2. the kill switch must be declared explicitly as true or false", () => {
  assert.ok(
    issuesOf(() =>
      loadRuntimeConfig(productionEnv({ CUSTOM_MIX_API_ENABLED: undefined }).env),
    ).some((issue) => issue.includes("CUSTOM_MIX_API_ENABLED")),
  );
  assert.ok(
    issuesOf(() => loadRuntimeConfig(productionEnv({ CUSTOM_MIX_API_ENABLED: "1" }).env)).some(
      (issue) => issue.includes("CUSTOM_MIX_API_ENABLED"),
    ),
  );
  assert.equal(
    loadRuntimeConfig(productionEnv({ CUSTOM_MIX_API_ENABLED: "false" }).env).customMix.enabled,
    false,
  );
});

// ── unknown environment values ────────────────────────────────────────────────────────

test("an unknown SIGNALS_DEPLOY_ENV fails", () => {
  for (const value of ["staging", "PRODUCTION_2", ""]) {
    assert.throws(
      () => loadRuntimeConfig(productionEnv({ SIGNALS_DEPLOY_ENV: value }).env),
      RuntimeConfigError,
      `deploy env ${value} was accepted`,
    );
  }
});

test("an unknown APPLE_ENVIRONMENT fails", () => {
  assert.ok(
    issuesOf(() => loadRuntimeConfig(productionEnv({ APPLE_ENVIRONMENT: "Staging" }).env)).some(
      (issue) => issue.includes("APPLE_ENVIRONMENT"),
    ),
  );
});

// ── secret normalization ──────────────────────────────────────────────────────────────

test("escaped \\n newlines are normalized for every PEM variable", () => {
  const fixture = productionEnv();
  const escaped = productionEnv({
    APP_STORE_PRIVATE_KEY: escapeNewlines(fixture.applePrivateKey.privateKeyPem),
    SIGNALS_TOKEN_SIGNING_KEY: escapeNewlines(fixture.tokenSigning.privateKeyPem),
    SIGNALS_TOKEN_PUBLIC_KEY: escapeNewlines(fixture.tokenSigning.publicKeyPem),
  });

  const config = loadRuntimeConfig(escaped.env);
  assert.ok(!config.apple?.privateKeyPem.includes("\\n"), "escaped newlines survived");
  assert.equal(config.apple?.privateKeyPem, fixture.applePrivateKey.privateKeyPem.trim());
  // The strongest possible check: the normalized value is a usable key.
  assert.doesNotThrow(() => createPrivateKey(config.apple!.privateKeyPem));
  assert.doesNotThrow(() => createPrivateKey(config.token.signingPrivateKeyPem));
});

test("CRLF line endings are normalized too", () => {
  const fixture = productionEnv();
  const crlf = fixture.applePrivateKey.privateKeyPem.replace(/\n/g, "\r\n");
  const config = loadRuntimeConfig(
    productionEnv({ APP_STORE_PRIVATE_KEY: crlf }).env,
  );
  assert.doesNotThrow(() => createPrivateKey(config.apple!.privateKeyPem));
});

test("loadRuntimeConfig never mutates the environment map it is handed", () => {
  const { env } = productionEnv({
    APP_STORE_PRIVATE_KEY: escapeNewlines(generateEs256Keypair().privateKeyPem),
  });
  const before = JSON.stringify(env);
  try {
    loadRuntimeConfig(env);
  } catch {
    /* the escaped key belongs to a different pair; either outcome is fine here */
  }
  assert.equal(JSON.stringify(env), before, "the environment map was mutated");
});

test("loadRuntimeConfig never touches process.env", () => {
  const before = JSON.stringify(process.env);
  loadRuntimeConfig(productionEnv().env);
  assert.equal(JSON.stringify(process.env), before, "process.env was mutated");
});

// ── Z (config half). no secret value ever appears in an error ─────────────────────────

test("Z. rejection messages name variables and never their values", () => {
  const fixture = productionEnv({
    KV_REST_API_URL: "not-a-url",
    CUSTOM_MIX_POOL_TIMEZONE: "Asia/Tokyo",
    CUSTOM_MIX_STORY_COUNT: "6",
  });
  const secrets = [
    fixture.applePrivateKey.privateKeyPem,
    fixture.tokenSigning.privateKeyPem,
    FIXTURE_HMAC_SECRET_PRODUCTION,
    FIXTURE_REDIS_TOKEN_PRODUCTION,
    "00000000-0000-0000-0000-000000000000",
    "TESTKEYID01",
  ];

  try {
    loadRuntimeConfig(fixture.env);
    assert.fail("expected rejection");
  } catch (error) {
    const text = serialize(error);
    for (const secret of secrets) {
      assert.ok(!text.includes(secret), `a secret value leaked: ${secret.slice(0, 12)}…`);
    }
    // The variable names, by contrast, must be present — that is the whole point.
    assert.ok(text.includes("KV_REST_API_URL"));
    assert.ok(text.includes("CUSTOM_MIX_POOL_TIMEZONE"));
    assert.ok(text.includes("CUSTOM_MIX_STORY_COUNT"));
  }
});

test("every reported issue is reported at once, not one per attempt", () => {
  const issues = issuesOf(() =>
    loadRuntimeConfig(
      productionEnv({
        CUSTOM_MIX_STORY_COUNT: "6",
        CUSTOM_MIX_SELECTOR_VERSION: "3",
        CUSTOM_MIX_POOL_TIMEZONE: "UTC",
      }).env,
    ),
  );
  assert.ok(issues.length >= 3, `expected several issues, got ${issues.length}`);
});
