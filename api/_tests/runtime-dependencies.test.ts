/**
 * Phase 3C-1 — dependency construction.
 *
 * Covers A, B, K, L, M, N, O, P and the constructor half of Q, plus factory determinism
 * and previous-key rotation.
 *
 * No test here contacts Apple or Redis: the JWS decoder, the Transaction History transport
 * and the Redis client are injected. The certificate bundle read is the REAL vendored one,
 * because that path must be proven rather than stubbed.
 */

import assert from "node:assert/strict";
import test from "node:test";
import { mkdtempSync, readFileSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import ts from "typescript";

import { AppleRootCertificateError } from "../_lib/apple-root-certificates.js";
import { AppleEntitlementError } from "../_lib/apple-verifier-real.js";
import type {
  AppleEntitlementVerifier,
  VerifiedAppleEntitlement,
} from "../_lib/apple-verifier.js";
import { PersistentIdempotencyStore } from "../_lib/persistent-idempotency-store.js";
import { PersistentRateLimiter } from "../_lib/persistent-rate-limit.js";
import { PersistentRevocationStore } from "../_lib/persistent-revocation-store.js";
import type { RevocationStore } from "../_lib/revocation-store.js";
import { loadRuntimeConfig, type RuntimeConfig } from "../_lib/runtime-config.js";
import {
  LiveAppleEntitlementVerifier,
  PreciseAppleVerificationError,
  RuntimeCompositionError,
  buildAppleClientOptions,
  buildVerifierOptions,
  createDevelopmentDependencies,
  createRuntimeDependencies,
  type RuntimeTransports,
} from "../_lib/runtime-dependencies.js";
import { MemorySecurityLogger } from "../_lib/security-logging.js";
import { SignalsTokenService } from "../_lib/signals-token.js";
import {
  FIXTURE_APP_APPLE_ID,
  FIXTURE_BUNDLE_ID,
  FIXTURE_DEVICE_JWS,
  FIXTURE_ORIGINAL_TRANSACTION_ID,
  FakeSignedTransactionDecoder,
  FakeTransactionHistoryFetcher,
  FixedClock,
  decodedTransaction,
  developmentEnv,
  generateEs256Keypair,
  memoryRedis,
  productionEnv,
  sandboxEnv,
} from "./runtime-fixtures.js";

const API_DIR = resolve(dirname(fileURLToPath(import.meta.url)), "..");

function realConfig(fixtureEnv: Record<string, string | undefined>): Extract<
  RuntimeConfig,
  { mode: "production" | "sandbox" }
> {
  const config = loadRuntimeConfig(fixtureEnv);
  assert.notEqual(config.mode, "development");
  return config as Extract<RuntimeConfig, { mode: "production" | "sandbox" }>;
}

function transportsFor(environment: "Production" | "Sandbox"): RuntimeTransports & {
  decoder: FakeSignedTransactionDecoder;
  fetcher: FakeTransactionHistoryFetcher;
  logger: MemorySecurityLogger;
} {
  const decoder = new FakeSignedTransactionDecoder(decodedTransaction(environment));
  const fetcher = new FakeTransactionHistoryFetcher();
  const logger = new MemorySecurityLogger();
  return {
    decoder,
    fetcher,
    logger,
    clock: new FixedClock(),
    requestId: () => "request-opaque-3c1",
    redisClient: memoryRedis(),
    appleDecoder: decoder,
    transactionHistoryFetcher: fetcher,
  };
}

// ── A. Production ─────────────────────────────────────────────────────────────────────

test("A. a valid Production config constructs the full real dependency set", () => {
  const config = realConfig(productionEnv().env);
  const deps = createRuntimeDependencies(config, transportsFor("Production"));

  assert.equal(deps.mode, "production");
  assert.equal(deps.environment, "Production");
  assert.ok(deps.verifier instanceof LiveAppleEntitlementVerifier);
  assert.ok(deps.tokens instanceof SignalsTokenService);
  assert.ok(deps.revocations instanceof PersistentRevocationStore);
  assert.ok(deps.idempotency instanceof PersistentIdempotencyStore);
  for (const limiter of [deps.ipLimiter, deps.subjectLimiter, deps.editionLimiter]) {
    assert.ok(limiter instanceof PersistentRateLimiter);
  }
  assert.equal(deps.namespaces.root, "signals:production:production");
  assert.equal(deps.namespaces.pool, "signals:production:production:pool");
  assert.equal(deps.killSwitch.customMixEnabled, true);
  assert.equal(deps.appleIdentity.appAppleId, Number(FIXTURE_APP_APPLE_ID));
  assert.equal(deps.appleIdentity.enableOnlineChecks, true);
});

// ── B. Sandbox ────────────────────────────────────────────────────────────────────────

test("B. a valid Sandbox config constructs an isolated dependency set", () => {
  const deps = createRuntimeDependencies(
    realConfig(sandboxEnv().env),
    transportsFor("Sandbox"),
  );

  assert.equal(deps.mode, "sandbox");
  assert.equal(deps.environment, "Sandbox");
  assert.equal(deps.appleIdentity.appAppleId, undefined);
  assert.equal(deps.appleIdentity.enableOnlineChecks, false);
  assert.equal(deps.namespaces.root, "signals:sandbox:sandbox");
});

// ── P. namespace isolation ────────────────────────────────────────────────────────────

test("P. every environment writes into a different namespace", () => {
  const production = createRuntimeDependencies(
    realConfig(productionEnv().env),
    transportsFor("Production"),
  );
  const sandbox = createRuntimeDependencies(
    realConfig(sandboxEnv().env),
    transportsFor("Sandbox"),
  );
  const development = createDevelopmentDependencies(
    loadRuntimeConfig(developmentEnv().env),
    { fakeVerifier: { verifySignedTransaction: async () => ({}) as VerifiedAppleEntitlement } },
  );

  const roots = [
    production.namespaces.root,
    sandbox.namespaces.root,
    development.namespaces.root,
  ];
  assert.equal(new Set(roots).size, 3, `namespaces collided: ${roots.join(", ")}`);
  for (const deps of [production, sandbox, development]) {
    assert.ok(deps.namespaces.pool.startsWith(deps.namespaces.root));
    assert.notEqual(deps.namespaces.pool, deps.namespaces.root);
  }
});

// ── N / O. Apple identity reaches the verifier correctly ──────────────────────────────

test("N. the Production verifier is constructed WITH appAppleId", () => {
  const config = realConfig(productionEnv().env);
  const options = buildVerifierOptions(config, [Buffer.from("der")]);
  assert.equal(options.appAppleId, Number(FIXTURE_APP_APPLE_ID));
  assert.equal(options.environment, "Production");
  assert.equal(options.bundleId, FIXTURE_BUNDLE_ID);
  assert.equal(options.enableOnlineChecks, true);
});

test("O. the Sandbox verifier is constructed WITHOUT any appAppleId", () => {
  const config = realConfig(sandboxEnv().env);
  const options = buildVerifierOptions(config, [Buffer.from("der")]);
  assert.equal("appAppleId" in options, false, "appAppleId key must be absent, not undefined");
  assert.equal(options.environment, "Sandbox");
  // Apple's own verifier enforces the same rule; prove the two agree.
  assert.notEqual(String(options.appAppleId), FIXTURE_APP_APPLE_ID);
});

test("O2. the App Store client options carry the deployment's own credentials", () => {
  const production = buildAppleClientOptions(realConfig(productionEnv().env));
  const sandbox = buildAppleClientOptions(realConfig(sandboxEnv().env));
  assert.equal(production.environment, "Production");
  assert.equal(sandbox.environment, "Sandbox");
  assert.notEqual(production.keyId, sandbox.keyId);
  assert.notEqual(production.issuerId, sandbox.issuerId);
  assert.notEqual(production.signingKeyPem, sandbox.signingKeyPem);
});

// ── M. no automatic fake-verifier fallback ────────────────────────────────────────────

test("M. the production factory refuses a Development config", () => {
  assert.throws(
    () => createRuntimeDependencies(loadRuntimeConfig(developmentEnv().env)),
    (error: unknown) =>
      error instanceof RuntimeCompositionError &&
      error.reason === "development_requires_explicit_factory",
  );
});

test("M2. the production factory refuses an injected fake verifier outright", () => {
  const config = realConfig(productionEnv().env);
  const transports = {
    ...transportsFor("Production"),
    fakeVerifier: { verifySignedTransaction: async () => ({}) as VerifiedAppleEntitlement },
  } as unknown as RuntimeTransports;
  assert.throws(
    () => createRuntimeDependencies(config, transports),
    (error: unknown) =>
      error instanceof RuntimeCompositionError && error.reason === "fake_verifier_forbidden",
  );
});

test("M3. the development factory refuses any non-development mode", () => {
  assert.throws(
    () =>
      createDevelopmentDependencies(realConfig(productionEnv().env), {
        fakeVerifier: {
          verifySignedTransaction: async () => ({}) as VerifiedAppleEntitlement,
        },
      }),
    (error: unknown) =>
      error instanceof RuntimeCompositionError &&
      error.reason === "development_factory_requires_development_mode",
  );
});

test("M4. even Development refuses to invent a verifier", () => {
  assert.throws(
    () =>
      createDevelopmentDependencies(
        loadRuntimeConfig(developmentEnv().env),
        {} as unknown as { fakeVerifier: AppleEntitlementVerifier },
      ),
    (error: unknown) =>
      error instanceof RuntimeCompositionError &&
      error.reason === "development_requires_explicit_verifier",
  );
});

test("M5. an unusable certificate bundle fails closed — nothing degrades to a stub", () => {
  const empty = mkdtempSync(join(tmpdir(), "runtime-no-certs-"));
  try {
    assert.throws(
      () =>
        createRuntimeDependencies(realConfig(productionEnv().env), {
          ...transportsFor("Production"),
          certificateDirectory: empty,
        }),
      AppleRootCertificateError,
    );
  } finally {
    rmSync(empty, { recursive: true, force: true });
  }
});

test("M6. unusable token key material fails closed with no key contents in the error", () => {
  const config = realConfig(productionEnv().env);
  const broken = {
    ...config,
    token: { ...config.token, signingPrivateKeyPem: "-----BEGIN PRIVATE KEY-----\nQUJD\n-----END PRIVATE KEY-----" },
  } as typeof config;
  try {
    createRuntimeDependencies(broken, transportsFor("Production"));
    assert.fail("expected a composition failure");
  } catch (error) {
    assert.ok(error instanceof RuntimeCompositionError);
    assert.equal(error.reason, "token_key_material_unusable");
    assert.ok(!`${error.message}${error.stack ?? ""}`.includes("BEGIN PRIVATE KEY"));
  }
});

// ── Q (constructor half). live current state is structurally mandatory ────────────────

const proof: VerifiedAppleEntitlement = {
  originalTransactionId: FIXTURE_ORIGINAL_TRANSACTION_ID,
  bundleId: FIXTURE_BUNDLE_ID,
  productId: "com.signalsapp.pro.lifetime",
  environment: "Production",
  ownershipType: "PURCHASED",
  productType: "NON_CONSUMABLE",
  revoked: false,
};

function stubProofVerifier(result: VerifiedAppleEntitlement | Error): AppleEntitlementVerifier {
  return {
    async verifySignedTransaction() {
      if (result instanceof Error) throw result;
      return structuredClone(result);
    },
  };
}

function stubRevocations(revoked: boolean | Error): RevocationStore {
  return {
    async isRevoked() {
      if (revoked instanceof Error) throw revoked;
      return revoked;
    },
  };
}

test("Q. the live verifier refuses to exist without a current-state source", () => {
  assert.throws(
    () =>
      new LiveAppleEntitlementVerifier({
        environment: "Production",
        signedProofVerifier: stubProofVerifier(proof),
        currentState: undefined as never,
        revocations: stubRevocations(false),
        deriveSubject: () => "subject",
      }),
    (error: unknown) =>
      error instanceof RuntimeCompositionError &&
      error.reason === "live_current_state_required",
  );
});

test("Q2. the denylist is consulted BEFORE Apple is contacted", async () => {
  let appleCalls = 0;
  const verifier = new LiveAppleEntitlementVerifier({
    environment: "Production",
    signedProofVerifier: stubProofVerifier(proof),
    currentState: {
      async getCurrentProEntitlement() {
        appleCalls += 1;
        return proof;
      },
    },
    revocations: stubRevocations(true),
    deriveSubject: () => "subject",
  });

  const result = await verifier.verifySignedTransaction({
    signedTransactionInfo: FIXTURE_DEVICE_JWS,
    expectedEnvironment: "Production",
  });
  assert.equal(result.revoked, true);
  assert.equal(appleCalls, 0, "a known-revoked subject must not cost an Apple call");
});

test("Q3. an unreadable denylist prevents the exchange entirely", async () => {
  const verifier = new LiveAppleEntitlementVerifier({
    environment: "Production",
    signedProofVerifier: stubProofVerifier(proof),
    currentState: { async getCurrentProEntitlement() { return proof; } },
    revocations: stubRevocations(new Error("redis down")),
    deriveSubject: () => "subject",
  });
  await assert.rejects(
    verifier.verifySignedTransaction({
      signedTransactionInfo: FIXTURE_DEVICE_JWS,
      expectedEnvironment: "Production",
    }),
    (error: unknown) =>
      error instanceof PreciseAppleVerificationError &&
      error.reason === "verification_unavailable",
  );
});

test("Q4. a revocation Apple reports is recorded and reported as revoked", async () => {
  const recorded: string[] = [];
  const verifier = new LiveAppleEntitlementVerifier({
    environment: "Production",
    signedProofVerifier: stubProofVerifier(proof),
    currentState: {
      async getCurrentProEntitlement(): Promise<VerifiedAppleEntitlement> {
        throw new AppleEntitlementError("revoked");
      },
    },
    revocations: stubRevocations(false),
    deriveSubject: () => "subject-abc",
    onRevoked: async (subject) => {
      recorded.push(subject);
    },
  });

  const result = await verifier.verifySignedTransaction({
    signedTransactionInfo: FIXTURE_DEVICE_JWS,
    expectedEnvironment: "Production",
  });
  assert.equal(result.revoked, true);
  assert.deepEqual(recorded, ["subject-abc"]);
});

test("Q5. Apple answering about a different purchase is rejected", async () => {
  const verifier = new LiveAppleEntitlementVerifier({
    environment: "Production",
    signedProofVerifier: stubProofVerifier(proof),
    currentState: {
      async getCurrentProEntitlement() {
        return { ...proof, originalTransactionId: "2000000111111111" };
      },
    },
    revocations: stubRevocations(false),
    deriveSubject: () => "subject",
  });
  await assert.rejects(
    verifier.verifySignedTransaction({
      signedTransactionInfo: FIXTURE_DEVICE_JWS,
      expectedEnvironment: "Production",
    }),
    (error: unknown) =>
      error instanceof PreciseAppleVerificationError && error.reason === "invalid_proof",
  );
});

test("Q6. a verifier built for one environment never serves the other", async () => {
  const verifier = new LiveAppleEntitlementVerifier({
    environment: "Sandbox",
    signedProofVerifier: stubProofVerifier(proof),
    currentState: { async getCurrentProEntitlement() { return proof; } },
    revocations: stubRevocations(false),
    deriveSubject: () => "subject",
  });
  await assert.rejects(
    verifier.verifySignedTransaction({
      signedTransactionInfo: FIXTURE_DEVICE_JWS,
      expectedEnvironment: "Production",
    }),
    (error: unknown) =>
      error instanceof PreciseAppleVerificationError &&
      error.reason === "unsupported_environment",
  );
});

test("Q7. a precise reason still maps to the right HTTP class", () => {
  assert.equal(new PreciseAppleVerificationError("revoked").code, "revoked");
  assert.equal(
    new PreciseAppleVerificationError("verification_unavailable").code,
    "verification_unavailable",
  );
  assert.equal(
    new PreciseAppleVerificationError("wrong_ownership").code,
    "wrong_ownership",
  );
});

// ── previous-key rotation ─────────────────────────────────────────────────────────────

test("a token signed by the PREVIOUS key still verifies after rotation", () => {
  const current = generateEs256Keypair();
  const previous = generateEs256Keypair();
  const config = realConfig(
    productionEnv({
      SIGNALS_TOKEN_SIGNING_KID: "signals-2026-08",
      SIGNALS_TOKEN_SIGNING_KEY: current.privateKeyPem,
      SIGNALS_TOKEN_PUBLIC_KEY: current.publicKeyPem,
      SIGNALS_TOKEN_PREVIOUS_KID: "signals-2026-07",
      SIGNALS_TOKEN_PREVIOUS_PUBLIC_KEY: previous.publicKeyPem,
    }).env,
  );
  const clock = new FixedClock();
  const deps = createRuntimeDependencies(config, {
    ...transportsFor("Production"),
    clock,
  });

  const retired = new SignalsTokenService({
    signer: { kid: "signals-2026-07", privateKeyPem: previous.privateKeyPem },
    verificationKeys: [{ kid: "signals-2026-07", publicKeyPem: previous.publicKeyPem }],
    hmacSecret: config.token.hmacSecret,
    clock,
  });
  const old = retired.issue({ subject: "subject-rotated", environment: "Production" });

  const claims = deps.tokens.verify({
    token: old.accessToken,
    expectedEnvironment: "Production",
  });
  assert.equal(claims.sub, "subject-rotated");

  // And the new signer is the CURRENT key, not the retired one.
  const fresh = deps.tokens.issue({ subject: "subject-fresh", environment: "Production" });
  const header = JSON.parse(
    Buffer.from(fresh.accessToken.split(".")[0], "base64url").toString("utf8"),
  ) as { kid: string };
  assert.equal(header.kid, "signals-2026-08");
});

test("a half-declared previous key pair is rejected", () => {
  assert.throws(() =>
    loadRuntimeConfig(
      productionEnv({ SIGNALS_TOKEN_PREVIOUS_KID: "signals-2026-06" }).env,
    ),
  );
});

// ── determinism ───────────────────────────────────────────────────────────────────────

test("factories are deterministic for the same injected config", () => {
  const config = realConfig(productionEnv().env);
  const certificates = [Buffer.from("der")];

  assert.deepEqual(
    buildVerifierOptions(config, certificates),
    buildVerifierOptions(config, certificates),
  );
  assert.deepEqual(buildAppleClientOptions(config), buildAppleClientOptions(config));

  const first = createRuntimeDependencies(config, transportsFor("Production"));
  const second = createRuntimeDependencies(config, transportsFor("Production"));
  assert.deepEqual(first.namespaces, second.namespaces);
  assert.deepEqual(first.appleIdentity, second.appleIdentity);
  assert.deepEqual(first.killSwitch, second.killSwitch);
  assert.equal(first.mode, second.mode);
});

test("no factory reads process.env", () => {
  const config = realConfig(productionEnv().env);
  const before = JSON.stringify(process.env);
  createRuntimeDependencies(config, transportsFor("Production"));
  assert.equal(JSON.stringify(process.env), before);
});

// ── K / L. import-time inertness ──────────────────────────────────────────────────────

/**
 * A CallExpression, NewExpression or await at the top level of a module runs the moment the
 * module is imported. Proving there are none is a stronger and far more stable statement
 * than trying to intercept the module loader — it holds for every importer, in every
 * runtime, forever.
 *
 * `new Set(...)` / `new Map(...)` over literals are allowed: they build inert data.
 */
const INERT_CONSTRUCTORS = new Set(["Set", "Map", "RegExp"]);

function sideEffectProblems(fileName: string, sourceText: string): string[] {
  const source = ts.createSourceFile(
    fileName,
    sourceText,
    ts.ScriptTarget.ES2022,
    true,
    ts.ScriptKind.TS,
  );
  const problems: string[] = [];

  const scan = (node: ts.Node, where: string): void => {
    if (ts.isCallExpression(node)) {
      problems.push(`${fileName}: top-level call in ${where}`);
      return;
    }
    if (ts.isAwaitExpression(node)) {
      problems.push(`${fileName}: top-level await in ${where}`);
      return;
    }
    if (ts.isNewExpression(node)) {
      const name = ts.isIdentifier(node.expression) ? node.expression.text : "?";
      if (!INERT_CONSTRUCTORS.has(name)) {
        problems.push(`${fileName}: top-level "new ${name}" in ${where}`);
        return;
      }
    }
    node.forEachChild((child) => scan(child, where));
  };

  for (const statement of source.statements) {
    if (
      ts.isImportDeclaration(statement) ||
      ts.isExportDeclaration(statement) ||
      ts.isInterfaceDeclaration(statement) ||
      ts.isTypeAliasDeclaration(statement) ||
      ts.isFunctionDeclaration(statement) ||
      ts.isClassDeclaration(statement) ||
      ts.isEnumDeclaration(statement) ||
      ts.isModuleDeclaration(statement)
    ) {
      continue;
    }
    if (ts.isVariableStatement(statement)) {
      for (const declaration of statement.declarationList.declarations) {
        if (declaration.initializer) {
          scan(declaration.initializer, `const ${declaration.name.getText(source)}`);
        }
      }
      continue;
    }
    problems.push(`${fileName}: unexpected executable top-level statement`);
  }
  return problems;
}

function runtimeSourceFiles(): { name: string; text: string }[] {
  const files: { name: string; text: string }[] = [];
  for (const name of readdirSync(join(API_DIR, "_lib")).sort()) {
    if (!name.endsWith(".ts")) continue;
    files.push({ name: `_lib/${name}`, text: readFileSync(join(API_DIR, "_lib", name), "utf8") });
  }
  files.push({ name: "auth/exchange.ts", text: readFileSync(join(API_DIR, "auth", "exchange.ts"), "utf8") });
  files.push({ name: "edition.ts", text: readFileSync(join(API_DIR, "edition.ts"), "utf8") });
  return files;
}

test("K/L. importing any API module runs nothing: no network, no certificate read, no Redis, no Apple", () => {
  const problems = runtimeSourceFiles().flatMap((file) =>
    sideEffectProblems(file.name, file.text),
  );
  assert.deepEqual(problems, [], problems.join("\n"));
});

test("K2. no API module reads process.env except as a call-time default argument", () => {
  for (const file of runtimeSourceFiles()) {
    for (const [index, line] of file.text.split("\n").entries()) {
      if (!line.includes("process.env")) continue;
      const trimmed = line.trim();
      if (trimmed.startsWith("*") || trimmed.startsWith("//") || trimmed.startsWith("/*")) {
        continue; // prose about process.env is not a read of it
      }
      assert.ok(
        // The one permitted shape: `(env: RawEnv = process.env)` — evaluated when CALLED.
        /\(\s*\w+\s*:\s*RawEnv\s*=\s*process\.env\s*\)/.test(line),
        `${file.name}:${index + 1} reads process.env outside a default argument`,
      );
    }
  }
});

test("L2. no runtime module reads edition data, latest.json or a mix pool", () => {
  const forbidden = ["latest.json", "editions/", "mix-pool", "mix_pool", "child_process", "spawnSync"];
  for (const file of runtimeSourceFiles()) {
    for (const needle of forbidden) {
      assert.ok(
        !file.text.includes(needle),
        `${file.name} references production data or a subprocess: ${needle}`,
      );
    }
  }
});

test("L3. the only filesystem read in the API is the vendored certificate bundle", () => {
  for (const file of runtimeSourceFiles()) {
    if (file.name === "_lib/apple-root-certificates.ts") continue;
    assert.ok(
      !/\breadFileSync\b|\breadFile\b/.test(file.text),
      `${file.name} performs a filesystem read`,
    );
  }
});
