/**
 * Signals Pro monthly subscription — backend entitlement rules (Phase 2).
 *
 * 1.0 ships two products that unlock the same Pro. The rules pinned here, exactly as
 * decided before implementation:
 *   • Lifetime `com.signalsapp.pro.lifetime`: NO expiry check ever; refunds/revocations
 *     reject. Existing lifetime owners must be completely unaffected by this change.
 *   • Monthly `com.signalsapp.pro.monthly`: `expiresDate` must be verified, present and
 *     in the future; refunds/revocations reject.
 *   • Unknown product IDs reject (`wrong_product`).
 *   • Bundle/environment/ownership checks are unchanged for both.
 * Plus the history-scan regression: a dead transaction (expired monthly, refund) must
 * not stop the scan before a still-valid entitlement (e.g. the lifetime) is found.
 */

import assert from "node:assert/strict";
import test from "node:test";
import {
  Environment,
  InAppOwnershipType,
  Type,
  type HistoryResponse,
  type JWSTransactionDecodedPayload,
} from "@apple/app-store-server-library";
import {
  AppleEntitlementError,
  RealAppleEntitlementVerifier,
  SIGNALS_BUNDLE_ID,
  SIGNALS_PRO_MONTHLY_PRODUCT_ID,
  SIGNALS_PRO_PRODUCT_ID,
  SIGNALS_PRO_PRODUCT_IDS,
  assertSignalsEntitlement,
  type SignedTransactionDecoder,
} from "../_lib/apple-verifier-real.js";
import {
  AppleEntitlementClient,
  type TransactionHistoryFetcher,
} from "../_lib/apple-client.js";

const ORIGINAL_TRANSACTION_ID = "2000000123456789";
const NOW_MS = 1_770_000_000_000;
const IN_ONE_HOUR = NOW_MS + 3_600_000;
const AN_HOUR_AGO = NOW_MS - 3_600_000;
const now = () => NOW_MS;

function lifetimePayload(
  overrides: Partial<JWSTransactionDecodedPayload> = {},
): JWSTransactionDecodedPayload {
  return {
    originalTransactionId: ORIGINAL_TRANSACTION_ID,
    transactionId: "2000000987654321",
    bundleId: SIGNALS_BUNDLE_ID,
    productId: SIGNALS_PRO_PRODUCT_ID,
    type: Type.NON_CONSUMABLE,
    inAppOwnershipType: InAppOwnershipType.PURCHASED,
    environment: Environment.PRODUCTION,
    ...overrides,
  } as JWSTransactionDecodedPayload;
}

function monthlyPayload(
  overrides: Partial<JWSTransactionDecodedPayload> = {},
): JWSTransactionDecodedPayload {
  return lifetimePayload({
    productId: SIGNALS_PRO_MONTHLY_PRODUCT_ID,
    type: Type.AUTO_RENEWABLE_SUBSCRIPTION,
    expiresDate: IN_ONE_HOUR,
    ...overrides,
  });
}

function reasonOf(fn: () => unknown): string {
  try {
    fn();
  } catch (error) {
    assert.ok(error instanceof AppleEntitlementError);
    return error.reason;
  }
  return "(no error)";
}

// ── A. lifetime regression: completely unaffected ────────────────────────────────────

test("A1. a valid lifetime purchase is accepted exactly as before", () => {
  const entitlement = assertSignalsEntitlement(lifetimePayload(), "Production", now);
  assert.equal(entitlement.productId, SIGNALS_PRO_PRODUCT_ID);
  assert.equal(entitlement.productType, "NON_CONSUMABLE");
  assert.equal(entitlement.revoked, false);
  assert.equal(entitlement.expiresDate, undefined);
});

test("A2. the lifetime is exempt from expiry — even a past expiresDate is ignored", () => {
  const entitlement = assertSignalsEntitlement(
    lifetimePayload({ expiresDate: AN_HOUR_AGO }),
    "Production",
    now,
  );
  assert.equal(entitlement.productType, "NON_CONSUMABLE");
});

test("A3. a refunded lifetime is still rejected", () => {
  assert.equal(
    reasonOf(() =>
      assertSignalsEntitlement(lifetimePayload({ revocationDate: AN_HOUR_AGO }), "Production", now)),
    "revoked",
  );
});

// ── B. monthly acceptance and expiry ─────────────────────────────────────────────────

test("B1. an active monthly subscription is accepted with its expiry", () => {
  const entitlement = assertSignalsEntitlement(monthlyPayload(), "Production", now);
  assert.equal(entitlement.productId, SIGNALS_PRO_MONTHLY_PRODUCT_ID);
  assert.equal(entitlement.productType, "AUTO_RENEWABLE_SUBSCRIPTION");
  assert.equal(entitlement.expiresDate, IN_ONE_HOUR);
});

test("B2. an expired monthly is rejected as expired", () => {
  assert.equal(
    reasonOf(() =>
      assertSignalsEntitlement(monthlyPayload({ expiresDate: AN_HOUR_AGO }), "Production", now)),
    "expired",
  );
});

test("B3. a monthly expiring exactly now is already lapsed", () => {
  assert.equal(
    reasonOf(() =>
      assertSignalsEntitlement(monthlyPayload({ expiresDate: NOW_MS }), "Production", now)),
    "expired",
  );
});

test("B4. a monthly with NO verified expiry is rejected, never trusted open-ended", () => {
  assert.equal(
    reasonOf(() =>
      assertSignalsEntitlement(monthlyPayload({ expiresDate: undefined }), "Production", now)),
    "expired",
  );
});

test("B5. a refunded monthly is rejected even while inside its paid period", () => {
  assert.equal(
    reasonOf(() =>
      assertSignalsEntitlement(monthlyPayload({ revocationDate: AN_HOUR_AGO }), "Production", now)),
    "revoked",
  );
});

test("B6. a family-shared monthly is not this account's purchase", () => {
  assert.equal(
    reasonOf(() =>
      assertSignalsEntitlement(
        monthlyPayload({ inAppOwnershipType: InAppOwnershipType.FAMILY_SHARED }),
        "Production",
        now,
      )),
    "wrong_ownership",
  );
});

// ── C. product identity is strict ────────────────────────────────────────────────────

test("C1. unknown product IDs are rejected", () => {
  assert.equal(
    reasonOf(() =>
      assertSignalsEntitlement(lifetimePayload({ productId: "com.signalsapp.pro.yearly" }),
                               "Production", now)),
    "wrong_product",
  );
});

test("C2. the monthly ID must carry the subscription type", () => {
  assert.equal(
    reasonOf(() =>
      assertSignalsEntitlement(monthlyPayload({ type: Type.NON_CONSUMABLE }), "Production", now)),
    "wrong_product_type",
  );
});

test("C3. the lifetime ID must carry the non-consumable type", () => {
  assert.equal(
    reasonOf(() =>
      assertSignalsEntitlement(lifetimePayload({ type: Type.AUTO_RENEWABLE_SUBSCRIPTION }),
                               "Production", now)),
    "wrong_product_type",
  );
});

test("C4. the product set is exactly the two shipped plans", () => {
  assert.deepEqual(
    [...SIGNALS_PRO_PRODUCT_IDS].sort(),
    ["com.signalsapp.pro.lifetime", "com.signalsapp.pro.monthly"],
  );
});

// ── D. end to end through the real verifier seam ─────────────────────────────────────

function verifierWith(payload: JWSTransactionDecodedPayload) {
  const decoder: SignedTransactionDecoder = {
    verifyAndDecodeTransaction: async () => payload,
  };
  return new RealAppleEntitlementVerifier({
    environment: "Production",
    bundleId: SIGNALS_BUNDLE_ID,
    appAppleId: 1234567890,
    rootCertificates: [],
    enableOnlineChecks: true,
    decoder,
    nowMs: now,   // fixed test clock — the expiry check must not depend on wall time
  });
}

test("D1. the device-JWS path accepts an active monthly", async () => {
  const entitlement = await verifierWith(monthlyPayload()).verifySignedTransaction({
    signedTransactionInfo: "eyJhbGciOiJFUzI1NiJ9.FAKE.FAKE",
    expectedEnvironment: "Production",
  });
  assert.equal(entitlement.productType, "AUTO_RENEWABLE_SUBSCRIPTION");
});

test("D2. the device-JWS path rejects an expired monthly", async () => {
  await assert.rejects(
    verifierWith(monthlyPayload({ expiresDate: 1 })).verifySignedTransaction({
      signedTransactionInfo: "eyJhbGciOiJFUzI1NiJ9.FAKE.FAKE",
      expectedEnvironment: "Production",
    }),
    (error: unknown) =>
      error instanceof AppleEntitlementError && error.reason === "expired",
  );
});

// ── E. history-scan regression: dead transactions must not mask live ones ────────────

function decoderFor(map: Record<string, JWSTransactionDecodedPayload>): SignedTransactionDecoder {
  return {
    verifyAndDecodeTransaction: async (signed: string) => {
      const payload = map[signed];
      if (!payload) throw new Error(`no fixture for ${signed}`);
      return payload;
    },
  };
}

function fetcherReturning(pages: HistoryResponse[]): TransactionHistoryFetcher {
  let index = 0;
  return { getTransactionHistory: async () => pages[Math.min(index++, pages.length - 1)] };
}

function scanningClient(
  fetcher: TransactionHistoryFetcher,
  decoder: SignedTransactionDecoder,
) {
  return new AppleEntitlementClient({
    fetcher,
    decoder,
    environment: "Production",
    sleep: async () => {},
    now: () => NOW_MS,
    retryBaseMs: 1,
  });
}

test("E1. an expired monthly earlier in history does not hide a valid lifetime", async () => {
  const c = scanningClient(
    fetcherReturning([{ signedTransactions: ["dead-monthly", "live-lifetime"], hasMore: false }]),
    decoderFor({
      "dead-monthly": monthlyPayload({ expiresDate: AN_HOUR_AGO }),
      "live-lifetime": lifetimePayload(),
    }),
  );
  const entitlement = await c.getCurrentProEntitlement(ORIGINAL_TRANSACTION_ID);
  assert.equal(entitlement.productId, SIGNALS_PRO_PRODUCT_ID);
});

test("E2. an account whose only Signals product is an expired monthly fails as expired", async () => {
  const c = scanningClient(
    fetcherReturning([{ signedTransactions: ["dead-monthly"], hasMore: false }]),
    decoderFor({ "dead-monthly": monthlyPayload({ expiresDate: AN_HOUR_AGO }) }),
  );
  await assert.rejects(
    c.getCurrentProEntitlement(ORIGINAL_TRANSACTION_ID),
    (error: unknown) =>
      error instanceof AppleEntitlementError && error.reason === "expired",
  );
});

test("E3. an active monthly is accepted through the history path", async () => {
  const c = scanningClient(
    fetcherReturning([{ signedTransactions: ["live-monthly"], hasMore: false }]),
    decoderFor({ "live-monthly": monthlyPayload() }),
  );
  const entitlement = await c.getCurrentProEntitlement(ORIGINAL_TRANSACTION_ID);
  assert.equal(entitlement.productType, "AUTO_RENEWABLE_SUBSCRIPTION");
  assert.equal(entitlement.expiresDate, IN_ONE_HOUR);
});
