/**
 * Phase 3C-1 — composed route behaviour.
 *
 * Covers Q (handler guard), R, S, T, U, V, W, X, Y, Z plus the idempotency contract.
 *
 * Every dependency is real EXCEPT three transports: the Apple JWS decoder, the Transaction
 * History HTTP call and the Redis connection. No test contacts Apple or Redis, and no test
 * reads an edition, `latest.json` or a mix pool.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { RealAppleEntitlementVerifier } from "../_lib/apple-verifier-real.js";
import {
  InAppOwnershipType,
  VerificationException,
  VerificationStatus,
  type HistoryResponse,
} from "@apple/app-store-server-library";
import { loadAppleRootCertificates } from "../_lib/apple-root-certificates.js";
import type { RawEnv } from "../_lib/env.js";
import { loadRuntimeConfig, type RuntimeConfig } from "../_lib/runtime-config.js";
import {
  RuntimeCompositionError,
  createRuntimeDependencies,
  type RuntimeDependencies,
} from "../_lib/runtime-dependencies.js";
import {
  createProductionEditionHandler,
  createProductionExchangeHandler,
} from "../_lib/runtime-factory.js";
import { MemorySecurityLogger } from "../_lib/security-logging.js";
import {
  FIXTURE_DEVICE_JWS,
  FIXTURE_HMAC_SECRET_PRODUCTION,
  FIXTURE_HISTORY_JWS,
  FIXTURE_ORIGINAL_TRANSACTION_ID,
  FIXTURE_REDIS_TOKEN_PRODUCTION,
  FakeSignedTransactionDecoder,
  FakeTransactionHistoryFetcher,
  FixedClock,
  SelectiveFailureRedisClient,
  codeOf,
  decodedTransaction,
  editionRequest,
  exchangeRequest,
  keyContains,
  memoryRedis,
  productionEnv,
  sandboxEnv,
} from "./runtime-fixtures.js";
import type { FakeRedisClient, RedisClient } from "../_lib/redis-client.js";

type Stack = {
  config: RuntimeConfig;
  deps: RuntimeDependencies;
  decoder: FakeSignedTransactionDecoder;
  fetcher: FakeTransactionHistoryFetcher;
  logger: MemorySecurityLogger;
  memory: FakeRedisClient;
  clock: FixedClock;
  exchange: (request: Request) => Promise<Response>;
  edition: (request: Request) => Promise<Response>;
};

type StackOptions = {
  env?: RawEnv;
  environment?: "Production" | "Sandbox";
  redisWrapper?: (memory: FakeRedisClient) => RedisClient;
  fetcher?: FakeTransactionHistoryFetcher;
};

function buildStack(options: StackOptions = {}): Stack {
  const environment = options.environment ?? "Production";
  const env =
    options.env ??
    (environment === "Production" ? productionEnv().env : sandboxEnv().env);
  const config = loadRuntimeConfig(env);

  const decoder = new FakeSignedTransactionDecoder(decodedTransaction(environment));
  const fetcher = options.fetcher ?? new FakeTransactionHistoryFetcher();
  const logger = new MemorySecurityLogger();
  const memory = memoryRedis();
  const clock = new FixedClock();

  const deps = createRuntimeDependencies(config, {
    clock,
    logger,
    requestId: () => "request-opaque-3c1",
    redisClient: options.redisWrapper ? options.redisWrapper(memory) : memory,
    appleDecoder: decoder,
    transactionHistoryFetcher: fetcher,
    sleep: async () => {},
  });

  return {
    config,
    deps,
    decoder,
    fetcher,
    logger,
    memory,
    clock,
    exchange: createProductionExchangeHandler(config, deps),
    edition: createProductionEditionHandler(config, deps),
  };
}

async function accessTokenOf(response: Response): Promise<string | undefined> {
  const body = (await response.clone().json()) as { accessToken?: string };
  return body.accessToken;
}

// ── V. the happy path ─────────────────────────────────────────────────────────────────

test("V. a fully composed exchange issues a verifiable Signals token", async () => {
  const stack = buildStack();
  const response = await stack.exchange(exchangeRequest());

  assert.equal(response.status, 200);
  const token = await accessTokenOf(response);
  assert.ok(token, "no access token was issued");

  const claims = stack.deps.tokens.verify({
    token,
    expectedEnvironment: "Production",
  });
  assert.equal(claims.environment, "Production");
  assert.deepEqual(claims.scope, ["custom_mix"]);
  assert.equal(claims.product, "com.signalsapp.pro.lifetime");
  assert.equal(claims.exp - claims.iat, 900);
  // The pseudonymous subject is derived — never the Apple identifier.
  assert.notEqual(claims.sub, FIXTURE_ORIGINAL_TRANSACTION_ID);

  // Both halves ran: the device proof AND Apple's current state.
  assert.ok(stack.decoder.calls.includes(FIXTURE_DEVICE_JWS));
  assert.ok(stack.decoder.calls.includes(FIXTURE_HISTORY_JWS));
  assert.equal(stack.fetcher.calls, 1);
});

// ── Q. the live current-state check cannot be wired out ───────────────────────────────

test("Q. the exchange handler refuses a verifier that skips Apple's current state", () => {
  const stack = buildStack();
  const bare = new RealAppleEntitlementVerifier({
    environment: "Production",
    bundleId: "com.kyohei.Signals",
    appAppleId: 1234567890,
    rootCertificates: loadAppleRootCertificates(),
    enableOnlineChecks: false,
    decoder: stack.decoder,
  });

  assert.throws(
    () =>
      createProductionExchangeHandler(stack.config, {
        ...stack.deps,
        verifier: bare,
      }),
    (error: unknown) =>
      error instanceof RuntimeCompositionError &&
      error.reason === "live_current_state_required",
  );
});

test("Q2. the handlers refuse in-memory stores in a real deployment", () => {
  const stack = buildStack();
  assert.throws(
    () =>
      createProductionExchangeHandler(stack.config, {
        ...stack.deps,
        revocations: { async isRevoked() { return false; } },
      }),
    (error: unknown) =>
      error instanceof RuntimeCompositionError &&
      error.reason === "persistent_revocation_store_required",
  );
  assert.throws(
    () =>
      createProductionEditionHandler(stack.config, {
        ...stack.deps,
        editionLimiter: { async consume() { return { allowed: true as const }; } },
      }),
    (error: unknown) =>
      error instanceof RuntimeCompositionError &&
      error.reason === "persistent_rate_limiter_required",
  );
});

// ── R. Apple's current state is unavailable ───────────────────────────────────────────

test("R. a failed current-state lookup prevents token issuance", async () => {
  const fetcher = new FakeTransactionHistoryFetcher();
  fetcher.error = new Error("connection reset");
  const stack = buildStack({ fetcher });

  const response = await stack.exchange(exchangeRequest());
  assert.equal(response.status, 503);
  assert.equal(await codeOf(response), "verification_unavailable");
  assert.equal(await accessTokenOf(response), undefined);
  assert.ok(fetcher.calls > 1, "the client should have retried before giving up");
});

test("R2. a revoked entitlement reported by Apple prevents token issuance", async () => {
  const stack = buildStack();
  // Apple's history says the purchase was refunded.
  stack.decoder.set(
    FIXTURE_HISTORY_JWS,
    decodedTransaction("Production", { revocationDate: 1_780_000_000_000 }),
  );

  const response = await stack.exchange(exchangeRequest());
  assert.equal(response.status, 401);
  assert.equal(await codeOf(response), "revoked");
  assert.equal(await accessTokenOf(response), undefined);
});

test("R3. a device proof whose signature does not verify prevents token issuance", async () => {
  const stack = buildStack();
  stack.decoder.set(
    FIXTURE_DEVICE_JWS,
    new VerificationException(VerificationStatus.VERIFICATION_FAILURE),
  );

  const response = await stack.exchange(exchangeRequest());
  assert.equal(response.status, 401);
  assert.equal(await codeOf(response), "invalid_proof");
  assert.equal(await accessTokenOf(response), undefined);
  assert.equal(stack.fetcher.calls, 0, "Apple must not be contacted for a bad proof");
});

test("R4. a family-shared purchase is not an entitlement for this account", async () => {
  const stack = buildStack();
  stack.decoder.set(
    FIXTURE_DEVICE_JWS,
    decodedTransaction("Production", {
      inAppOwnershipType: InAppOwnershipType.FAMILY_SHARED,
    }),
  );

  const response = await stack.exchange(exchangeRequest());
  assert.equal(response.status, 401);
  assert.equal(await codeOf(response), "wrong_ownership");
  assert.equal(await accessTokenOf(response), undefined);
  assert.equal(stack.fetcher.calls, 0);
});

test("R5. a transient verification failure is 503, never a 401", async () => {
  const stack = buildStack();
  stack.decoder.set(
    FIXTURE_DEVICE_JWS,
    new VerificationException(VerificationStatus.RETRYABLE_VERIFICATION_FAILURE),
  );

  const response = await stack.exchange(exchangeRequest());
  assert.equal(response.status, 503);
  assert.equal(await codeOf(response), "verification_unavailable");
});

// ── S / T / U. every persistent store fails closed ────────────────────────────────────

test("S. an unavailable revocation store prevents token issuance", async () => {
  const stack = buildStack({
    redisWrapper: (memory) =>
      new SelectiveFailureRedisClient(memory, keyContains(":ent:")),
  });

  const response = await stack.exchange(exchangeRequest());
  assert.equal(response.status, 503);
  assert.equal(await codeOf(response), "verification_unavailable");
  assert.equal(await accessTokenOf(response), undefined);
});

test("T. an unavailable rate limiter prevents token issuance", async () => {
  const stack = buildStack({
    redisWrapper: (memory) =>
      new SelectiveFailureRedisClient(memory, keyContains(":rl:")),
  });

  const response = await stack.exchange(exchangeRequest());
  assert.equal(response.status, 503);
  assert.equal(await codeOf(response), "verification_unavailable");
  assert.equal(await accessTokenOf(response), undefined);
  assert.equal(stack.fetcher.calls, 0, "the limiter runs before any Apple call");
});

test("U. an unavailable idempotency store prevents the exchange from running at all", async () => {
  const stack = buildStack({
    redisWrapper: (memory) =>
      new SelectiveFailureRedisClient(memory, keyContains(":idem:")),
  });

  const response = await stack.exchange(exchangeRequest());
  assert.equal(response.status, 503);
  assert.equal(await codeOf(response), "verification_unavailable");
  assert.equal(await accessTokenOf(response), undefined);
  assert.equal(stack.decoder.calls.length, 0, "nothing ran without retry-safety");
});

test("U2. an idempotency store that cannot record the outcome fails closed", async () => {
  // The claim (SET NX) succeeds; recording the result (GET) does not.
  let seenSet = false;
  const stack = buildStack({
    redisWrapper: (memory) =>
      new SelectiveFailureRedisClient(memory, (command) => {
        const isIdem = command.slice(1).some((part) => String(part).includes(":idem:"));
        if (!isIdem) return false;
        if (String(command[0]).toUpperCase() === "SET" && !seenSet) {
          seenSet = true;
          return false;
        }
        return String(command[0]).toUpperCase() === "GET";
      }),
  });

  const response = await stack.exchange(exchangeRequest());
  assert.equal(response.status, 503);
  assert.equal(await codeOf(response), "verification_unavailable");
  assert.equal(await accessTokenOf(response), undefined);
});

// ── idempotency contract ──────────────────────────────────────────────────────────────

test("a retried FAILURE replays the stored outcome without contacting Apple again", async () => {
  const stack = buildStack();
  stack.decoder.set(
    FIXTURE_DEVICE_JWS,
    decodedTransaction("Production", { productId: "com.signalsapp.something.else" }),
  );

  const first = await stack.exchange(exchangeRequest());
  assert.equal(first.status, 401);
  const firstCode = await codeOf(first);
  const callsAfterFirst = stack.decoder.calls.length;

  const second = await stack.exchange(exchangeRequest());
  assert.equal(second.status, 401);
  assert.equal(await codeOf(second), firstCode);
  assert.equal(
    stack.decoder.calls.length,
    callsAfterFirst,
    "the retry re-ran verification instead of replaying",
  );
});

test("the same proof submitted with contradictory metadata is a conflict, not a retry", async () => {
  const stack = buildStack();
  const first = await stack.exchange(exchangeRequest());
  assert.equal(first.status, 200);

  const conflicting = await stack.exchange(
    exchangeRequest({
      signedTransactionInfo: FIXTURE_DEVICE_JWS,
      appVersion: "9.9.9",
      selectorVersion: 1,
    }),
  );
  assert.equal(conflicting.status, 400);
  assert.equal(await codeOf(conflicting), "invalid_request");
});

test("a duplicate still in flight is refused rather than run twice", async () => {
  let release: (() => void) | undefined;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });

  class GatedFetcher extends FakeTransactionHistoryFetcher {
    override async getTransactionHistory(): Promise<HistoryResponse> {
      await gate;
      return super.getTransactionHistory();
    }
  }

  const stack = buildStack({ fetcher: new GatedFetcher() });
  const inFlight = stack.exchange(exchangeRequest());
  // Let the first request reach the gate before the duplicate arrives.
  await new Promise((resolve) => setImmediate(resolve));

  const duplicate = await stack.exchange(exchangeRequest());
  assert.equal(duplicate.status, 503);
  assert.equal(await codeOf(duplicate), "verification_unavailable");
  assert.equal(duplicate.headers.get("Retry-After"), "1");

  release?.();
  const first = await inFlight;
  assert.equal(first.status, 200);
});

test("the stored idempotency record holds no token, no JWS and no Apple identifier", async () => {
  const stack = buildStack();
  const response = await stack.exchange(exchangeRequest());
  const token = await accessTokenOf(response);
  assert.ok(token);

  const idempotencyKeys = stack.memory.keys().filter((key) => key.includes(":idem:"));
  assert.equal(idempotencyKeys.length, 1);
  const stored = stack.memory.peek(idempotencyKeys[0]) ?? "";

  for (const secret of [token, FIXTURE_DEVICE_JWS, FIXTURE_ORIGINAL_TRANSACTION_ID]) {
    assert.ok(!stored.includes(secret), "sensitive material was persisted");
  }
  const record = JSON.parse(stored) as { result?: Record<string, unknown> };
  assert.deepEqual(record.result, { status: 200, reasonCode: "ok" });
  assert.ok(stored.length < 512, "the stored record is not bounded");
});

test("no persistent key contains a raw Apple identifier, IP or token", async () => {
  const stack = buildStack();
  const response = await stack.exchange(exchangeRequest());
  const token = await accessTokenOf(response);

  for (const key of stack.memory.keys()) {
    assert.ok(key.startsWith("signals:production:production:"), `unnamespaced key: ${key}`);
    for (const secret of [
      FIXTURE_ORIGINAL_TRANSACTION_ID,
      FIXTURE_DEVICE_JWS,
      "198.51.100.7",
      token ?? "",
    ]) {
      if (secret) assert.ok(!key.includes(secret), `a raw value leaked into a key: ${key}`);
    }
  }
});

// ── W / X. the edition boundary ───────────────────────────────────────────────────────

test("W. a composed edition request with a valid token reaches selector_not_connected", async () => {
  const stack = buildStack();
  const exchanged = await stack.exchange(exchangeRequest());
  const token = await accessTokenOf(exchanged);
  assert.ok(token);

  const response = await stack.edition(editionRequest(token));
  assert.equal(response.status, 503);
  const body = (await response.json()) as { status?: string; code?: string };
  assert.equal(body.status, "not_connected");
  assert.equal(body.code, "selector_not_connected");
  // Nothing resembling an edition came back.
  assert.equal("stories" in body, false);
});

test("W2. the edition route still rejects a missing or malformed token", async () => {
  const stack = buildStack();
  assert.equal((await stack.edition(editionRequest(null))).status, 401);
  assert.equal((await stack.edition(editionRequest("not-a-token"))).status, 401);
});

test("X. a Sandbox token is rejected by Production edition dependencies", async () => {
  const sandbox = buildStack({ environment: "Sandbox" });
  const sandboxExchange = await sandbox.exchange(exchangeRequest());
  assert.equal(sandboxExchange.status, 200);
  const sandboxToken = await accessTokenOf(sandboxExchange);
  assert.ok(sandboxToken);

  const production = buildStack();
  const response = await production.edition(editionRequest(sandboxToken));
  assert.ok(response.status === 401 || response.status === 403, `status ${response.status}`);
  assert.notEqual(await codeOf(response), "selector_not_connected");

  // And the Sandbox stack accepts its own token, so the rejection is about isolation.
  assert.equal((await sandbox.edition(editionRequest(sandboxToken))).status, 503);
});

// ── Y. the kill switch ────────────────────────────────────────────────────────────────

test("Y. the kill switch closes both routes", async () => {
  const stack = buildStack({ env: productionEnv({ CUSTOM_MIX_API_ENABLED: "false" }).env });
  assert.equal(stack.deps.killSwitch.customMixEnabled, false);

  const exchange = await stack.exchange(exchangeRequest());
  assert.equal(exchange.status, 503);
  assert.equal(await codeOf(exchange), "custom_mix_disabled");
  assert.equal(await accessTokenOf(exchange), undefined);

  const edition = await stack.edition(editionRequest("anything"));
  assert.equal(edition.status, 503);
  assert.equal(await codeOf(edition), "custom_mix_disabled");

  assert.equal(stack.decoder.calls.length, 0);
  assert.equal(stack.fetcher.calls, 0);
  assert.equal(stack.memory.keys().length, 0, "a disabled route must touch no storage");
});

test("Y2. a kill switch that disagrees with the composed dependencies refuses to wire", () => {
  const stack = buildStack();
  assert.throws(
    () =>
      createProductionExchangeHandler(stack.config, {
        ...stack.deps,
        killSwitch: { customMixEnabled: false },
      }),
    (error: unknown) =>
      error instanceof RuntimeCompositionError &&
      error.reason === "kill_switch_disagrees_with_config",
  );
});

// ── Z. logs and errors carry nothing sensitive ────────────────────────────────────────

test("Z. no log event or error carries a secret, a proof, a token or an identifier", async () => {
  const stack = buildStack();

  const ok = await stack.exchange(exchangeRequest());
  const token = (await accessTokenOf(ok)) ?? "";
  await stack.edition(editionRequest(token));
  await stack.edition(editionRequest(null));

  const failing = buildStack({
    redisWrapper: (memory) => new SelectiveFailureRedisClient(memory, keyContains(":ent:")),
  });
  await failing.exchange(exchangeRequest());

  const events = [...stack.logger.events, ...failing.logger.events];
  assert.ok(events.length >= 4, "expected the routes to have logged");
  const serialized = JSON.stringify(events);

  const forbidden: [string, string][] = [
    ["JWS", FIXTURE_DEVICE_JWS],
    ["history JWS", FIXTURE_HISTORY_JWS],
    ["access token", token],
    ["Apple originalTransactionId", FIXTURE_ORIGINAL_TRANSACTION_ID],
    ["pseudonymous subject", stack.deps.tokens.deriveSubject(FIXTURE_ORIGINAL_TRANSACTION_ID, "Production")],
    ["token HMAC secret", FIXTURE_HMAC_SECRET_PRODUCTION],
    ["Redis token", FIXTURE_REDIS_TOKEN_PRODUCTION],
    ["Apple private key", stack.config.apple?.privateKeyPem ?? "unused"],
    ["client IP", "198.51.100.7"],
    ["region", "japan"],
    ["topic", "tech"],
    ["Authorization header", "Bearer "],
  ];
  for (const [label, value] of forbidden) {
    if (!value) continue;
    assert.ok(!serialized.includes(value), `${label} leaked into a log event`);
  }

  // What IS allowed must actually be there, or the logs would be useless.
  for (const event of events) {
    assert.ok(["/api/auth/exchange", "/api/edition"].includes(event.route));
    assert.equal(typeof event.status, "number");
    assert.equal(typeof event.reasonCode, "string");
    assert.equal(typeof event.latencyMs, "number");
    assert.equal(typeof event.requestId, "string");
    assert.deepEqual(
      Object.keys(event).filter(
        (key) =>
          ![
            "route",
            "status",
            "reasonCode",
            "latencyMs",
            "requestId",
            "selectorVersion",
            "environment",
            "rateLimitBucket",
          ].includes(key),
      ),
      [],
      "an unexpected field appeared in a security log event",
    );
  }
});

test("Z2. an error thrown by composition never carries key material", () => {
  const fixture = productionEnv();
  try {
    createRuntimeDependencies(
      { ...loadRuntimeConfig(fixture.env), storage: null } as unknown as RuntimeConfig,
      { redisClient: memoryRedis() },
    );
  } catch (error) {
    const text = `${(error as Error).message}${(error as Error).stack ?? ""}`;
    assert.ok(!text.includes(fixture.applePrivateKey.privateKeyPem));
    assert.ok(!text.includes(FIXTURE_REDIS_TOKEN_PRODUCTION));
    assert.ok(!text.includes(FIXTURE_HMAC_SECRET_PRODUCTION));
  }
});

// ── no production data is read ────────────────────────────────────────────────────────

test("a full composed flow reads no edition, no latest.json and no mix pool", async () => {
  const stack = buildStack();
  const exchanged = await stack.exchange(exchangeRequest());
  const token = await accessTokenOf(exchanged);
  const edition = await stack.edition(editionRequest(token ?? ""));

  assert.equal(edition.status, 503);
  // Only auth-related namespaces were ever written; the pool namespace stays untouched.
  for (const key of stack.memory.keys()) {
    assert.ok(
      !key.startsWith(stack.deps.namespaces.pool),
      `the reserved pool namespace was written: ${key}`,
    );
  }
});
