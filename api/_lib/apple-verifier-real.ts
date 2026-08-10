/**
 * Real Apple entitlement verifier backed by @apple/app-store-server-library@3.1.0.
 * (Phase 3B-3)
 *
 * Official APIs used:
 *   • SignedDataVerifier(appleRootCertificates: Buffer[], enableOnlineChecks: boolean,
 *                        environment: Environment, bundleId: string, appAppleId?: number)
 *   • SignedDataVerifier.verifyAndDecodeTransaction(signedTransactionInfo) →
 *     Promise<JWSTransactionDecodedPayload>
 *   • VerificationException / VerificationStatus
 *   • Environment, Type, InAppOwnershipType enums
 *
 * Trust rule: NOTHING the device sends is believed until
 * `verifyAndDecodeTransaction` returns. Every field checked below is read from the
 * VERIFIED payload, never from a client-supplied claim.
 *
 * There is no Production↔Sandbox fallback. A verifier is built for exactly one
 * environment; a transaction from the other one fails `unsupported_environment`.
 */

import {
  Environment,
  InAppOwnershipType,
  SignedDataVerifier,
  Type,
  VerificationException,
  VerificationStatus,
  type JWSTransactionDecodedPayload,
} from "@apple/app-store-server-library";

import {
  AppleVerificationError,
  type AppleEntitlementVerifier,
  type SignalsEnvironment,
  type VerifiedAppleEntitlement,
  type VerifySignedTransactionInput,
} from "./apple-verifier.js";

export const SIGNALS_BUNDLE_ID = "com.kyohei.Signals";
/** The lifetime non-consumable. Legacy name kept — pre-subscription callers use it. */
export const SIGNALS_PRO_PRODUCT_ID = "com.signalsapp.pro.lifetime";
/** The monthly auto-renewable subscription (1.0 ships both). */
export const SIGNALS_PRO_MONTHLY_PRODUCT_ID = "com.signalsapp.pro.monthly";
/** Every product that grants Signals Pro. Anything else is `wrong_product`. */
export const SIGNALS_PRO_PRODUCT_IDS: ReadonlySet<string> = new Set([
  SIGNALS_PRO_PRODUCT_ID,
  SIGNALS_PRO_MONTHLY_PRODUCT_ID,
]);

/**
 * Stable internal reasons. Extends the Phase 3B-1 codes with the specific mismatch
 * reasons this adapter can distinguish. Callers map these to API error envelopes; the
 * strings never contain Apple payloads.
 */
export type AppleEntitlementReason =
  | "invalid_proof"
  | "wrong_bundle"
  | "wrong_product"
  | "wrong_product_type"
  | "wrong_ownership"
  | "revoked"
  | "expired"
  | "unsupported_environment"
  | "verification_unavailable";

export class AppleEntitlementError extends Error {
  readonly reason: AppleEntitlementReason;

  constructor(reason: AppleEntitlementReason) {
    // The message IS the reason code — no transaction, JWS, payload or identifier.
    super(reason);
    this.name = "AppleEntitlementError";
    this.reason = reason;
  }
}

/** Minimal seam so tests can inject a decoder without a real signed fixture. */
export interface SignedTransactionDecoder {
  verifyAndDecodeTransaction(
    signedTransactionInfo: string,
  ): Promise<JWSTransactionDecodedPayload>;
}

export function toAppleEnvironment(environment: SignalsEnvironment): Environment {
  return environment === "Production" ? Environment.PRODUCTION : Environment.SANDBOX;
}

/** Translate the library's verification failures into our stable reasons. */
export function mapVerificationException(error: unknown): AppleEntitlementReason {
  if (error instanceof VerificationException) {
    switch (error.status) {
      case VerificationStatus.INVALID_APP_IDENTIFIER:
        return "wrong_bundle";
      case VerificationStatus.INVALID_ENVIRONMENT:
        return "unsupported_environment";
      case VerificationStatus.RETRYABLE_VERIFICATION_FAILURE:
        // Transient (e.g. an OCSP/CRL fetch failed) — not proof of a bad transaction.
        return "verification_unavailable";
      case VerificationStatus.VERIFICATION_FAILURE:
      case VerificationStatus.INVALID_CHAIN_LENGTH:
      case VerificationStatus.INVALID_CERTIFICATE:
      case VerificationStatus.FAILURE:
      default:
        return "invalid_proof";
    }
  }
  return "verification_unavailable";
}

/**
 * Apply the Signals entitlement rules to an ALREADY-VERIFIED payload.
 * Exported so the API-client adapter applies exactly the same rules to the
 * transactions it verifies out of Transaction History.
 */
export function assertSignalsEntitlement(
  payload: JWSTransactionDecodedPayload,
  expectedEnvironment: SignalsEnvironment,
  nowMs: () => number = () => Date.now(),
): VerifiedAppleEntitlement {
  if (payload.bundleId !== SIGNALS_BUNDLE_ID) {
    throw new AppleEntitlementError("wrong_bundle");
  }
  if (typeof payload.productId !== "string" || !SIGNALS_PRO_PRODUCT_IDS.has(payload.productId)) {
    // Unknown product IDs — including future SKUs not yet rolled out — grant nothing.
    throw new AppleEntitlementError("wrong_product");
  }
  const isMonthly = payload.productId === SIGNALS_PRO_MONTHLY_PRODUCT_ID;

  // The product's TYPE must match what that product ID is: the lifetime is a
  // non-consumable and the monthly is an auto-renewable subscription. A signed payload
  // claiming any other combination is not a Signals purchase.
  if (isMonthly) {
    if (payload.type !== Type.AUTO_RENEWABLE_SUBSCRIPTION) {
      throw new AppleEntitlementError("wrong_product_type");
    }
  } else if (payload.type !== Type.NON_CONSUMABLE) {
    throw new AppleEntitlementError("wrong_product_type");
  }
  if (payload.inAppOwnershipType !== InAppOwnershipType.PURCHASED) {
    // FAMILY_SHARED is a real ownership type but is not a purchase by this account.
    throw new AppleEntitlementError("wrong_ownership");
  }
  if (payload.revocationDate !== undefined && payload.revocationDate !== null) {
    // Refunds and revocations lock Pro for BOTH plans, exactly as before.
    throw new AppleEntitlementError("revoked");
  }
  // Expiry applies ONLY to the monthly subscription. The lifetime non-consumable never
  // expires and is deliberately exempt — its expiresDate, if Apple ever set one, is
  // ignored, so existing lifetime owners can never be locked out by this check.
  let expiresDate: number | undefined;
  if (isMonthly) {
    expiresDate = typeof payload.expiresDate === "number" ? payload.expiresDate : undefined;
    if (expiresDate === undefined || expiresDate <= nowMs()) {
      // A subscription with no verified expiry is treated as lapsed, not trusted open-ended.
      throw new AppleEntitlementError("expired");
    }
  }
  if (payload.environment !== expectedEnvironment) {
    throw new AppleEntitlementError("unsupported_environment");
  }
  const originalTransactionId = payload.originalTransactionId;
  if (typeof originalTransactionId !== "string" || originalTransactionId.length === 0) {
    throw new AppleEntitlementError("invalid_proof");
  }

  return {
    originalTransactionId,
    bundleId: SIGNALS_BUNDLE_ID,
    productId: payload.productId,
    environment: expectedEnvironment,
    ownershipType: "PURCHASED",
    productType: isMonthly ? "AUTO_RENEWABLE_SUBSCRIPTION" : "NON_CONSUMABLE",
    revoked: false,
    ...(isMonthly ? { expiresDate } : {}),
  };
}

export type RealAppleEntitlementVerifierOptions = {
  environment: SignalsEnvironment;
  bundleId: string;
  /** Required by Apple for Production; must be omitted for Sandbox. */
  appAppleId?: number;
  /** Verified DER roots from `loadAppleRootCertificates()`. */
  rootCertificates: Buffer[];
  /** Apple's certificate revocation/expiry online checks. */
  enableOnlineChecks: boolean;
  /** Test seam: inject a decoder instead of constructing SignedDataVerifier. */
  decoder?: SignedTransactionDecoder;
  /** Test seam: the clock used for the monthly expiry check. Defaults to Date.now. */
  nowMs?: () => number;
};

export class RealAppleEntitlementVerifier implements AppleEntitlementVerifier {
  private readonly decoder: SignedTransactionDecoder;
  private readonly environment: SignalsEnvironment;
  private readonly nowMs: () => number;

  constructor(options: RealAppleEntitlementVerifierOptions) {
    this.environment = options.environment;
    this.nowMs = options.nowMs ?? (() => Date.now());

    if (options.bundleId !== SIGNALS_BUNDLE_ID) {
      throw new AppleEntitlementError("wrong_bundle");
    }
    // Apple requires appAppleId for Production and omits it for Sandbox. Enforce both
    // directions so a misconfigured deployment cannot silently verify against the wrong
    // identity.
    if (options.environment === "Production" && options.appAppleId === undefined) {
      throw new AppleEntitlementError("verification_unavailable");
    }
    if (options.environment === "Sandbox" && options.appAppleId !== undefined) {
      throw new AppleEntitlementError("verification_unavailable");
    }

    if (options.decoder) {
      this.decoder = options.decoder;
    } else {
      if (options.rootCertificates.length === 0) {
        throw new AppleEntitlementError("verification_unavailable");
      }
      this.decoder = new SignedDataVerifier(
        options.rootCertificates,
        options.enableOnlineChecks,
        toAppleEnvironment(options.environment),
        options.bundleId,
        options.appAppleId,
      );
    }
  }

  async verifySignedTransaction(
    input: VerifySignedTransactionInput,
  ): Promise<VerifiedAppleEntitlement> {
    if (input.expectedEnvironment !== this.environment) {
      // This verifier serves exactly one environment — never fall back to the other.
      throw new AppleEntitlementError("unsupported_environment");
    }
    if (typeof input.signedTransactionInfo !== "string" || input.signedTransactionInfo.length === 0) {
      throw new AppleEntitlementError("invalid_proof");
    }

    let payload: JWSTransactionDecodedPayload;
    try {
      payload = await this.decoder.verifyAndDecodeTransaction(input.signedTransactionInfo);
    } catch (error) {
      // Only the mapped reason escapes — never the library's message or the JWS.
      throw new AppleEntitlementError(mapVerificationException(error));
    }

    return assertSignalsEntitlement(payload, this.environment, this.nowMs);
  }
}

/** Keeps the Phase 3B-1 error type usable by callers that only know the older codes. */
export function toLegacyVerificationError(
  error: AppleEntitlementError,
): AppleVerificationError {
  switch (error.reason) {
    case "unsupported_environment":
      return new AppleVerificationError("unsupported_environment");
    case "verification_unavailable":
      return new AppleVerificationError("verification_unavailable");
    default:
      return new AppleVerificationError("invalid_proof");
  }
}
