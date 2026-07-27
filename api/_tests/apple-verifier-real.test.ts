import assert from "node:assert/strict";
import test from "node:test";
import {
  Environment,
  InAppOwnershipType,
  Type,
  VerificationException,
  VerificationStatus,
  type JWSTransactionDecodedPayload,
} from "@apple/app-store-server-library";
import {
  AppleEntitlementError,
  RealAppleEntitlementVerifier,
  SIGNALS_BUNDLE_ID,
  SIGNALS_PRO_PRODUCT_ID,
  assertSignalsEntitlement,
  mapVerificationException,
  type SignedTransactionDecoder,
} from "../_lib/apple-verifier-real.js";

// A stand-in signed blob. Never a real JWS — the decoder is injected, so no signature
// material or Apple credential is needed anywhere in these tests.
const FAKE_JWS = "eyJhbGciOiJFUzI1NiJ9.FAKE-DEVICE-PAYLOAD.FAKE-SIGNATURE";
const ORIGINAL_TRANSACTION_ID = "2000000123456789";

function validPayload(
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

function decoderReturning(payload: JWSTransactionDecodedPayload): SignedTransactionDecoder {
  return { verifyAndDecodeTransaction: async () => payload };
}

function decoderThrowing(error: unknown): SignedTransactionDecoder {
  return {
    verifyAndDecodeTransaction: async () => {
      throw error;
    },
  };
}

function productionVerifier(decoder: SignedTransactionDecoder) {
  return new RealAppleEntitlementVerifier({
    environment: "Production",
    bundleId: SIGNALS_BUNDLE_ID,
    appAppleId: 1234567890,
    rootCertificates: [],
    enableOnlineChecks: true,
    decoder,
  });
}

// ── G. Production requires appAppleId ────────────────────────────────────────────────

test("G. Production without appAppleId is refused at construction", () => {
  assert.throws(
    () =>
      new RealAppleEntitlementVerifier({
        environment: "Production",
        bundleId: SIGNALS_BUNDLE_ID,
        rootCertificates: [],
        enableOnlineChecks: true,
        decoder: decoderReturning(validPayload()),
      }),
    (error: unknown) =>
      error instanceof AppleEntitlementError && error.reason === "verification_unavailable",
  );
});

test("G2. Sandbox with an appAppleId is refused at construction", () => {
  assert.throws(
    () =>
      new RealAppleEntitlementVerifier({
        environment: "Sandbox",
        bundleId: SIGNALS_BUNDLE_ID,
        appAppleId: 1234567890,
        rootCertificates: [],
        enableOnlineChecks: true,
        decoder: decoderReturning(validPayload()),
      }),
    AppleEntitlementError,
  );
});

test("G3. a foreign bundle id is refused at construction", () => {
  assert.throws(
    () =>
      new RealAppleEntitlementVerifier({
        environment: "Sandbox",
        bundleId: "com.someone.else",
        rootCertificates: [],
        enableOnlineChecks: true,
        decoder: decoderReturning(validPayload()),
      }),
    (error: unknown) => error instanceof AppleEntitlementError && error.reason === "wrong_bundle",
  );
});

// ── H. environment isolation ─────────────────────────────────────────────────────────

test("H. a Sandbox transaction is rejected by a Production verifier", async () => {
  const verifier = productionVerifier(
    decoderReturning(validPayload({ environment: Environment.SANDBOX })),
  );
  await assert.rejects(
    () =>
      verifier.verifySignedTransaction({
        signedTransactionInfo: FAKE_JWS,
        expectedEnvironment: "Production",
      }),
    (error: unknown) =>
      error instanceof AppleEntitlementError && error.reason === "unsupported_environment",
  );
});

test("H2. a mismatched expectedEnvironment never falls back", async () => {
  const verifier = productionVerifier(decoderReturning(validPayload()));
  await assert.rejects(
    () =>
      verifier.verifySignedTransaction({
        signedTransactionInfo: FAKE_JWS,
        expectedEnvironment: "Sandbox",
      }),
    (error: unknown) =>
      error instanceof AppleEntitlementError && error.reason === "unsupported_environment",
  );
});

// ── I–N. field rules ─────────────────────────────────────────────────────────────────

test("I. a wrong bundle id in the verified payload is rejected", async () => {
  const verifier = productionVerifier(
    decoderReturning(validPayload({ bundleId: "com.attacker.app" })),
  );
  await assert.rejects(
    () =>
      verifier.verifySignedTransaction({
        signedTransactionInfo: FAKE_JWS,
        expectedEnvironment: "Production",
      }),
    (error: unknown) => error instanceof AppleEntitlementError && error.reason === "wrong_bundle",
  );
});

test("J. a wrong product id is rejected", async () => {
  const verifier = productionVerifier(
    decoderReturning(validPayload({ productId: "com.signalsapp.something.else" })),
  );
  await assert.rejects(
    () =>
      verifier.verifySignedTransaction({
        signedTransactionInfo: FAKE_JWS,
        expectedEnvironment: "Production",
      }),
    (error: unknown) => error instanceof AppleEntitlementError && error.reason === "wrong_product",
  );
});

test("K. a wrong product type is rejected", async () => {
  const verifier = productionVerifier(
    decoderReturning(validPayload({ type: Type.AUTO_RENEWABLE_SUBSCRIPTION })),
  );
  await assert.rejects(
    () =>
      verifier.verifySignedTransaction({
        signedTransactionInfo: FAKE_JWS,
        expectedEnvironment: "Production",
      }),
    (error: unknown) =>
      error instanceof AppleEntitlementError && error.reason === "wrong_product_type",
  );
});

test("L. FAMILY_SHARED ownership is rejected", async () => {
  const verifier = productionVerifier(
    decoderReturning(validPayload({ inAppOwnershipType: InAppOwnershipType.FAMILY_SHARED })),
  );
  await assert.rejects(
    () =>
      verifier.verifySignedTransaction({
        signedTransactionInfo: FAKE_JWS,
        expectedEnvironment: "Production",
      }),
    (error: unknown) => error instanceof AppleEntitlementError && error.reason === "wrong_ownership",
  );
});

test("M. a revoked transaction is rejected", async () => {
  const verifier = productionVerifier(
    decoderReturning(validPayload({ revocationDate: 1_760_000_000_000 })),
  );
  await assert.rejects(
    () =>
      verifier.verifySignedTransaction({
        signedTransactionInfo: FAKE_JWS,
        expectedEnvironment: "Production",
      }),
    (error: unknown) => error instanceof AppleEntitlementError && error.reason === "revoked",
  );
});

test("N. a missing originalTransactionId is rejected", async () => {
  const verifier = productionVerifier(
    decoderReturning(validPayload({ originalTransactionId: undefined })),
  );
  await assert.rejects(
    () =>
      verifier.verifySignedTransaction({
        signedTransactionInfo: FAKE_JWS,
        expectedEnvironment: "Production",
      }),
    (error: unknown) => error instanceof AppleEntitlementError && error.reason === "invalid_proof",
  );
});

// ── O. the happy path ────────────────────────────────────────────────────────────────

test("O. a valid PURCHASED non-consumable is accepted", async () => {
  const verifier = productionVerifier(decoderReturning(validPayload()));
  const entitlement = await verifier.verifySignedTransaction({
    signedTransactionInfo: FAKE_JWS,
    expectedEnvironment: "Production",
  });
  assert.deepEqual(entitlement, {
    originalTransactionId: ORIGINAL_TRANSACTION_ID,
    bundleId: SIGNALS_BUNDLE_ID,
    productId: SIGNALS_PRO_PRODUCT_ID,
    environment: "Production",
    ownershipType: "PURCHASED",
    productType: "NON_CONSUMABLE",
    revoked: false,
  });
});

test("O2. Sandbox accepts a Sandbox transaction", async () => {
  const verifier = new RealAppleEntitlementVerifier({
    environment: "Sandbox",
    bundleId: SIGNALS_BUNDLE_ID,
    rootCertificates: [],
    enableOnlineChecks: false,
    decoder: decoderReturning(validPayload({ environment: Environment.SANDBOX })),
  });
  const entitlement = await verifier.verifySignedTransaction({
    signedTransactionInfo: FAKE_JWS,
    expectedEnvironment: "Sandbox",
  });
  assert.equal(entitlement.environment, "Sandbox");
});

test("O3. an empty signed transaction is rejected before any decoding", async () => {
  let called = false;
  const verifier = productionVerifier({
    verifyAndDecodeTransaction: async () => {
      called = true;
      return validPayload();
    },
  });
  await assert.rejects(
    () =>
      verifier.verifySignedTransaction({
        signedTransactionInfo: "",
        expectedEnvironment: "Production",
      }),
    (error: unknown) => error instanceof AppleEntitlementError && error.reason === "invalid_proof",
  );
  assert.equal(called, false, "unverified input must never reach the decoder path");
});

// ── verification-exception mapping ───────────────────────────────────────────────────

test("library verification failures map to stable reasons", () => {
  assert.equal(
    mapVerificationException(new VerificationException(VerificationStatus.INVALID_APP_IDENTIFIER)),
    "wrong_bundle",
  );
  assert.equal(
    mapVerificationException(new VerificationException(VerificationStatus.INVALID_ENVIRONMENT)),
    "unsupported_environment",
  );
  assert.equal(
    mapVerificationException(
      new VerificationException(VerificationStatus.RETRYABLE_VERIFICATION_FAILURE),
    ),
    "verification_unavailable",
  );
  assert.equal(
    mapVerificationException(new VerificationException(VerificationStatus.INVALID_CERTIFICATE)),
    "invalid_proof",
  );
  assert.equal(
    mapVerificationException(new VerificationException(VerificationStatus.VERIFICATION_FAILURE)),
    "invalid_proof",
  );
  assert.equal(mapVerificationException(new Error("something else")), "verification_unavailable");
});

test("an invalid signature surfaces as invalid_proof", async () => {
  const verifier = productionVerifier(
    decoderThrowing(new VerificationException(VerificationStatus.VERIFICATION_FAILURE)),
  );
  await assert.rejects(
    () =>
      verifier.verifySignedTransaction({
        signedTransactionInfo: FAKE_JWS,
        expectedEnvironment: "Production",
      }),
    (error: unknown) => error instanceof AppleEntitlementError && error.reason === "invalid_proof",
  );
});

// ── P / V / W / X. redaction ─────────────────────────────────────────────────────────

test("P/V/W/X. verifier errors contain no JWS, transaction id or Apple payload", async () => {
  const payload = validPayload({ bundleId: "com.attacker.app" });
  const verifier = productionVerifier(decoderReturning(payload));
  try {
    await verifier.verifySignedTransaction({
      signedTransactionInfo: FAKE_JWS,
      expectedEnvironment: "Production",
    });
    assert.fail("expected a rejection");
  } catch (error) {
    assert.ok(error instanceof AppleEntitlementError);
    const serialized = `${error.message} ${error.name} ${error.stack ?? ""} ${JSON.stringify(error)}`;
    assert.ok(!serialized.includes(FAKE_JWS), "raw JWS leaked");
    assert.ok(!serialized.includes(ORIGINAL_TRANSACTION_ID), "transaction id leaked");
    assert.ok(!serialized.includes("com.attacker.app"), "Apple payload leaked");
    assert.equal(error.message, "wrong_bundle", "message is the stable reason only");
  }
});

test("P2. an underlying library error message never escapes", async () => {
  const secretish = new VerificationException(
    VerificationStatus.VERIFICATION_FAILURE,
    new Error(`chain failed for ${ORIGINAL_TRANSACTION_ID}`),
  );
  const verifier = productionVerifier(decoderThrowing(secretish));
  try {
    await verifier.verifySignedTransaction({
      signedTransactionInfo: FAKE_JWS,
      expectedEnvironment: "Production",
    });
    assert.fail("expected a rejection");
  } catch (error) {
    assert.ok(error instanceof AppleEntitlementError);
    assert.ok(!`${error.message}${error.stack ?? ""}`.includes(ORIGINAL_TRANSACTION_ID));
  }
});

// ── shared rule helper ───────────────────────────────────────────────────────────────

test("assertSignalsEntitlement enforces the same rules for history transactions", () => {
  assert.throws(
    () => assertSignalsEntitlement(validPayload({ revocationDate: 1 }), "Production"),
    (error: unknown) => error instanceof AppleEntitlementError && error.reason === "revoked",
  );
  const ok = assertSignalsEntitlement(validPayload(), "Production");
  assert.equal(ok.revoked, false);
});
