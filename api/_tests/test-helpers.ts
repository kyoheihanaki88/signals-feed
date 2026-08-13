import {
  createPrivateKey,
  createSign,
  generateKeyPairSync,
} from "node:crypto";
import {
  AppleVerificationError,
  type AppleEntitlementVerifier,
  type SignalsEnvironment,
  type VerifiedAppleEntitlement,
  type VerifySignedTransactionInput,
} from "../_lib/apple-verifier.js";
import {
  InMemorySlidingWindowRateLimiter,
  type RateLimiter,
} from "../_lib/rate-limit.js";
import { InMemoryRevocationStore } from "../_lib/revocation-store.js";
import { MemorySecurityLogger } from "../_lib/security-logging.js";
import {
  PRO_PRODUCT_ID,
  SignalsTokenService,
  type Clock,
  type SignalsTokenClaims,
} from "../_lib/signals-token.js";

export class FixedClock implements Clock {
  constructor(public value: number = Date.UTC(2026, 6, 27, 12, 0, 0)) {}

  nowMs(): number {
    return this.value;
  }

  advance(milliseconds: number): void {
    this.value += milliseconds;
  }
}

export class FakeAppleVerifier implements AppleEntitlementVerifier {
  calls: VerifySignedTransactionInput[] = [];
  error:
    | AppleVerificationError
    | Error
    | undefined;

  constructor(public entitlement: VerifiedAppleEntitlement) {}

  async verifySignedTransaction(
    input: VerifySignedTransactionInput,
  ): Promise<VerifiedAppleEntitlement> {
    this.calls.push(input);
    if (this.error) {
      throw this.error;
    }
    return structuredClone(this.entitlement);
  }
}

export function validEntitlement(
  environment: SignalsEnvironment = "Production",
): VerifiedAppleEntitlement {
  return {
    originalTransactionId: "2000000999999999",
    bundleId: "com.kyohei.Signals",
    productId: PRO_PRODUCT_ID,
    environment,
    ownershipType: "PURCHASED",
    productType: "NON_CONSUMABLE",
    revoked: false,
  };
}

export function generateTestKey(kid: string): {
  kid: string;
  privateKeyPem: string;
  publicKeyPem: string;
} {
  const pair = generateKeyPairSync("ec", {
    namedCurve: "prime256v1",
    privateKeyEncoding: { type: "pkcs8", format: "pem" },
    publicKeyEncoding: { type: "spki", format: "pem" },
  });
  return {
    kid,
    privateKeyPem: pair.privateKey,
    publicKeyPem: pair.publicKey,
  };
}

export function createTokenFixture(options?: {
  environment?: SignalsEnvironment;
  signingKey?: ReturnType<typeof generateTestKey>;
  verificationKeys?: ReturnType<typeof generateTestKey>[];
  clock?: FixedClock;
  hmacSecret?: string;
}): {
  key: ReturnType<typeof generateTestKey>;
  clock: FixedClock;
  tokens: SignalsTokenService;
} {
  const key = options?.signingKey ?? generateTestKey("current");
  const clock = options?.clock ?? new FixedClock();
  const verificationKeys = options?.verificationKeys ?? [key];
  const tokens = new SignalsTokenService({
    signer: key,
    verificationKeys,
    hmacSecret:
      options?.hmacSecret ??
      "test-only-hmac-secret-with-at-least-thirty-two-bytes",
    clock,
    randomJti: () => "deterministic-jti",
  });
  return { key, clock, tokens };
}

export function createDependencies(
  environment: SignalsEnvironment = "Production",
): {
  clock: FixedClock;
  verifier: FakeAppleVerifier;
  tokens: SignalsTokenService;
  revocations: InMemoryRevocationStore;
  logger: MemorySecurityLogger;
  ipLimiter: RateLimiter;
  subjectLimiter: RateLimiter;
  editionLimiter: RateLimiter;
  requestId: () => string;
} {
  const { clock, tokens } = createTokenFixture();
  return {
    clock,
    verifier: new FakeAppleVerifier(validEntitlement(environment)),
    tokens,
    revocations: new InMemoryRevocationStore(),
    logger: new MemorySecurityLogger(),
    ipLimiter: new InMemorySlidingWindowRateLimiter(10, 60_000),
    subjectLimiter: new InMemorySlidingWindowRateLimiter(20, 3_600_000),
    editionLimiter: new InMemorySlidingWindowRateLimiter(30, 3_600_000),
    requestId: () => "request-opaque-001",
  };
}

export const TEST_JWS = "eyJhbGciOiJFUzI1NiJ9.e30.c2lnbmF0dXJl";

export function exchangeRequest(
  body: Record<string, unknown> = {
    signedTransactionInfo: TEST_JWS,
    appVersion: "1.0",
    selectorVersion: 2,
  },
  headers: Record<string, string> = {},
): Request {
  return new Request("https://example.test/api/auth/exchange", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-forwarded-for": "198.51.100.2",
      ...headers,
    },
    body: JSON.stringify(body),
  });
}

export function editionBody(overrides: Record<string, unknown> = {}): {
  date: string;
  active: {
    mode: string;
    regions: string[];
    topics: string[];
  };
  pending: null;
  selectorVersion: number;
  storyCount: number;
} {
  return {
    date: "2026-07-27",
    // v2: topics are a strict allowlist, so the shared default body selects NO topic
    // filter — tests that exercise the allowlist pass their own topics explicitly.
    active: { mode: "custom", regions: ["japan"], topics: [] },
    pending: null,
    selectorVersion: 2,
    storyCount: 5,
    ...overrides,
  };
}

export function editionRequest(
  token: string | null,
  body: Record<string, unknown> = editionBody(),
): Request {
  return new Request("https://example.test/api/edition", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      ...(token === null ? {} : { authorization: `Bearer ${token}` }),
    },
    body: JSON.stringify(body),
  });
}

export function signClaims(
  key: ReturnType<typeof generateTestKey>,
  claims: Record<string, unknown>,
): string {
  const header = Buffer.from(
    JSON.stringify({ alg: "ES256", typ: "JWT", kid: key.kid }),
  ).toString("base64url");
  const payload = Buffer.from(JSON.stringify(claims)).toString("base64url");
  const input = `${header}.${payload}`;
  const signer = createSign("SHA256");
  signer.update(input);
  signer.end();
  const signature = signer
    .sign({
      key: createPrivateKey(key.privateKeyPem),
      dsaEncoding: "ieee-p1363",
    })
    .toString("base64url");
  return `${input}.${signature}`;
}

export function claimsFor(
  tokens: SignalsTokenService,
  environment: SignalsEnvironment = "Production",
): SignalsTokenClaims {
  const subject = tokens.deriveSubject("transaction-for-test", environment);
  return tokens.issue({ subject, environment }).claims;
}

export async function responseCode(response: Response): Promise<string> {
  const body = (await response.json()) as {
    error?: { code?: string };
    code?: string;
  };
  return body.error?.code ?? body.code ?? "";
}

export { AppleVerificationError };
