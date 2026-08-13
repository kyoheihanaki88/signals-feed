/**
 * Shared fixtures for the Phase 3C-1 runtime composition tests.
 *
 * EVERYTHING here is generated or obviously fake. There is no real App Store Connect key,
 * no real issuer id, no real app id, no real Redis credential. Key material is generated
 * per test process with `generateKeyPairSync`, so nothing sensitive is ever committed.
 *
 * No test in this phase contacts Apple or Redis: the Apple JWS decoder, the Transaction
 * History transport and the Redis client are all injected.
 */

import { generateKeyPairSync } from "node:crypto";

import {
  Environment,
  InAppOwnershipType,
  Type,
  type HistoryResponse,
  type JWSTransactionDecodedPayload,
} from "@apple/app-store-server-library";

import type { TransactionHistoryFetcher } from "../_lib/apple-client.js";
import type { SignedTransactionDecoder } from "../_lib/apple-verifier-real.js";
import type { SignalsEnvironment } from "../_lib/apple-verifier.js";
import {
  FakeRedisClient,
  RedisUnavailableError,
  type RedisClient,
  type RedisCommand,
} from "../_lib/redis-client.js";
import type { RawEnv } from "../_lib/env.js";
import type { Clock } from "../_lib/signals-token.js";

export const FIXTURE_BUNDLE_ID = "com.kyohei.Signals";
export const FIXTURE_PRODUCT_ID = "com.signalsapp.pro.lifetime";
/** Obvious placeholder — never a real App Store app id. */
export const FIXTURE_APP_APPLE_ID = "1234567890";
export const FIXTURE_ORIGINAL_TRANSACTION_ID = "2000000999999999";
/** Compact-JWS-shaped, but not a real signed transaction. */
export const FIXTURE_DEVICE_JWS = "eyJhbGciOiJFUzI1NiJ9.e30.c2lnbmF0dXJl";
export const FIXTURE_HISTORY_JWS = "history-signed-transaction-placeholder";

export const FIXTURE_HMAC_SECRET_PRODUCTION =
  "test-only-production-pepper-at-least-32-bytes";
export const FIXTURE_HMAC_SECRET_SANDBOX =
  "test-only-sandbox-pepper-at-least-32-bytes-x";
export const FIXTURE_REDIS_TOKEN_PRODUCTION =
  "test-only-production-kv-token-not-a-real-credential";
export const FIXTURE_REDIS_TOKEN_SANDBOX =
  "test-only-sandbox-kv-token-not-a-real-credential";

export type Keypair = { privateKeyPem: string; publicKeyPem: string };

export function generateEs256Keypair(): Keypair {
  const pair = generateKeyPairSync("ec", {
    namedCurve: "prime256v1",
    privateKeyEncoding: { type: "pkcs8", format: "pem" },
    publicKeyEncoding: { type: "spki", format: "pem" },
  });
  return { privateKeyPem: pair.privateKey, publicKeyPem: pair.publicKey };
}

/** Turn a real PEM into the single-line form a dashboard field produces. */
export function escapeNewlines(pem: string): string {
  return pem.replace(/\n/g, "\\n");
}

export class FixedClock implements Clock {
  constructor(public value: number = Date.UTC(2026, 6, 27, 12, 0, 0)) {}

  nowMs(): number {
    return this.value;
  }

  advance(milliseconds: number): void {
    this.value += milliseconds;
  }
}

// ── environment maps ──────────────────────────────────────────────────────────────────

export type EnvFixture = {
  env: RawEnv;
  tokenSigning: Keypair;
  applePrivateKey: Keypair;
};

function baseTokenEnv(signing: Keypair): RawEnv {
  return {
    SIGNALS_TOKEN_SIGNING_KID: "signals-2026-07",
    SIGNALS_TOKEN_SIGNING_KEY: signing.privateKeyPem,
    SIGNALS_TOKEN_PUBLIC_KEY: signing.publicKeyPem,
    SIGNALS_TOKEN_ISSUER: "signals-auth",
    SIGNALS_TOKEN_AUDIENCE: "signals-custom-mix",
    SIGNALS_TOKEN_TTL_SECONDS: "900",
    SIGNALS_TOKEN_CLOCK_SKEW_SECONDS: "60",
    CUSTOM_MIX_API_ENABLED: "true",
    CUSTOM_MIX_SELECTOR_VERSION: "2",
    CUSTOM_MIX_STORY_COUNT: "5",
    CUSTOM_MIX_POOL_TIMEZONE: "America/New_York",
  };
}

export function productionEnv(overrides: RawEnv = {}): EnvFixture {
  const tokenSigning = generateEs256Keypair();
  const applePrivateKey = generateEs256Keypair();
  const env: RawEnv = {
    SIGNALS_DEPLOY_ENV: "production",
    APPLE_ENVIRONMENT: "Production",
    APPLE_BUNDLE_ID: FIXTURE_BUNDLE_ID,
    APPLE_PRODUCT_ID: FIXTURE_PRODUCT_ID,
    APPLE_APP_APPLE_ID: FIXTURE_APP_APPLE_ID,
    APP_STORE_ISSUER_ID: "00000000-0000-0000-0000-000000000000",
    APP_STORE_KEY_ID: "TESTKEYID01",
    APP_STORE_PRIVATE_KEY: applePrivateKey.privateKeyPem,
    ...baseTokenEnv(tokenSigning),
    SIGNALS_TOKEN_HMAC_SECRET: FIXTURE_HMAC_SECRET_PRODUCTION,
    KV_REST_API_URL: "https://production-fixture.example.upstash.io",
    KV_REST_API_TOKEN: FIXTURE_REDIS_TOKEN_PRODUCTION,
    ...overrides,
  };
  for (const [key, value] of Object.entries(overrides)) {
    if (value === undefined) delete env[key];
  }
  return { env, tokenSigning, applePrivateKey };
}

export function sandboxEnv(overrides: RawEnv = {}): EnvFixture {
  const tokenSigning = generateEs256Keypair();
  const applePrivateKey = generateEs256Keypair();
  const env: RawEnv = {
    SIGNALS_DEPLOY_ENV: "sandbox",
    APPLE_ENVIRONMENT: "Sandbox",
    APPLE_BUNDLE_ID: FIXTURE_BUNDLE_ID,
    APPLE_PRODUCT_ID: FIXTURE_PRODUCT_ID,
    APP_STORE_ISSUER_ID: "11111111-1111-1111-1111-111111111111",
    APP_STORE_KEY_ID: "TESTKEYID02",
    APP_STORE_PRIVATE_KEY: applePrivateKey.privateKeyPem,
    ...baseTokenEnv(tokenSigning),
    SIGNALS_TOKEN_SIGNING_KID: "signals-sandbox-2026-07",
    SIGNALS_TOKEN_HMAC_SECRET: FIXTURE_HMAC_SECRET_SANDBOX,
    KV_REST_API_URL: "https://sandbox-fixture.example.upstash.io",
    KV_REST_API_TOKEN: FIXTURE_REDIS_TOKEN_SANDBOX,
    ...overrides,
  };
  for (const [key, value] of Object.entries(overrides)) {
    if (value === undefined) delete env[key];
  }
  return { env, tokenSigning, applePrivateKey };
}

export function developmentEnv(overrides: RawEnv = {}): EnvFixture {
  const tokenSigning = generateEs256Keypair();
  const applePrivateKey = generateEs256Keypair();
  const env: RawEnv = {
    SIGNALS_DEPLOY_ENV: "development",
    ...baseTokenEnv(tokenSigning),
    SIGNALS_TOKEN_HMAC_SECRET: FIXTURE_HMAC_SECRET_PRODUCTION,
    ...overrides,
  };
  for (const [key, value] of Object.entries(overrides)) {
    if (value === undefined) delete env[key];
  }
  return { env, tokenSigning, applePrivateKey };
}

// ── Apple seams ───────────────────────────────────────────────────────────────────────

export function decodedTransaction(
  environment: SignalsEnvironment,
  overrides: Partial<JWSTransactionDecodedPayload> = {},
): JWSTransactionDecodedPayload {
  return {
    bundleId: FIXTURE_BUNDLE_ID,
    productId: FIXTURE_PRODUCT_ID,
    type: Type.NON_CONSUMABLE,
    inAppOwnershipType: InAppOwnershipType.PURCHASED,
    environment:
      environment === "Production" ? Environment.PRODUCTION : Environment.SANDBOX,
    originalTransactionId: FIXTURE_ORIGINAL_TRANSACTION_ID,
    transactionId: FIXTURE_ORIGINAL_TRANSACTION_ID,
    ...overrides,
  } as JWSTransactionDecodedPayload;
}

/** Injected in place of `SignedDataVerifier`; the entitlement RULES still run for real. */
export class FakeSignedTransactionDecoder implements SignedTransactionDecoder {
  readonly calls: string[] = [];
  private readonly byToken = new Map<string, JWSTransactionDecodedPayload | Error>();

  constructor(private fallback: JWSTransactionDecodedPayload | Error) {}

  set(token: string, value: JWSTransactionDecodedPayload | Error): this {
    this.byToken.set(token, value);
    return this;
  }

  setDefault(value: JWSTransactionDecodedPayload | Error): this {
    this.fallback = value;
    return this;
  }

  async verifyAndDecodeTransaction(
    signedTransactionInfo: string,
  ): Promise<JWSTransactionDecodedPayload> {
    this.calls.push(signedTransactionInfo);
    const value = this.byToken.get(signedTransactionInfo) ?? this.fallback;
    if (value instanceof Error) throw value;
    return structuredClone(value);
  }
}

/** Injected in place of the real App Store Server API transport. */
export class FakeTransactionHistoryFetcher implements TransactionHistoryFetcher {
  calls = 0;
  error: Error | undefined;
  response: HistoryResponse = {
    signedTransactions: [FIXTURE_HISTORY_JWS],
    hasMore: false,
  };

  async getTransactionHistory(): Promise<HistoryResponse> {
    this.calls += 1;
    if (this.error) throw this.error;
    return structuredClone(this.response);
  }
}

// ── Redis seams ───────────────────────────────────────────────────────────────────────

/**
 * Wraps the in-memory client and fails ONLY the commands a predicate selects, so a test can
 * take down exactly one store (the rate limiter, the denylist, the idempotency claim)
 * without disturbing the others.
 */
export class SelectiveFailureRedisClient implements RedisClient {
  constructor(
    private readonly inner: RedisClient,
    public failWhen: (command: RedisCommand) => boolean = () => false,
  ) {}

  async command<T = unknown>(command: RedisCommand): Promise<T> {
    if (this.failWhen(command)) throw new RedisUnavailableError("simulated outage");
    return this.inner.command<T>(command);
  }

  async pipeline<T = unknown>(commands: RedisCommand[]): Promise<T[]> {
    if (commands.some((command) => this.failWhen(command))) {
      throw new RedisUnavailableError("simulated outage");
    }
    return this.inner.pipeline<T>(commands);
  }
}

export function keyContains(fragment: string): (command: RedisCommand) => boolean {
  return (command) => command.slice(1).some((part) => String(part).includes(fragment));
}

export function memoryRedis(): FakeRedisClient {
  return new FakeRedisClient();
}

// ── request builders ──────────────────────────────────────────────────────────────────

export function exchangeRequest(
  body: Record<string, unknown> = {
    signedTransactionInfo: FIXTURE_DEVICE_JWS,
    appVersion: "1.4.0",
    selectorVersion: 2,
  },
  headers: Record<string, string> = {},
): Request {
  return new Request("https://example.test/api/auth/exchange", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-forwarded-for": "198.51.100.7",
      ...headers,
    },
    body: JSON.stringify(body),
  });
}

export function editionRequest(token: string | null): Request {
  return new Request("https://example.test/api/edition", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      ...(token === null ? {} : { authorization: `Bearer ${token}` }),
    },
    body: JSON.stringify({
      date: "2026-07-27",
      active: { mode: "custom", regions: ["japan"], topics: ["tech"] },
      pending: null,
      selectorVersion: 2,
      storyCount: 5,
    }),
  });
}

export async function bodyOf(response: Response): Promise<Record<string, unknown>> {
  return (await response.clone().json()) as Record<string, unknown>;
}

export async function codeOf(response: Response): Promise<string> {
  const body = (await bodyOf(response)) as {
    error?: { code?: string };
    code?: string;
  };
  return body.error?.code ?? body.code ?? "";
}
