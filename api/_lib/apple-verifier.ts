export type SignalsEnvironment = "Production" | "Sandbox";

export type VerifiedAppleEntitlement = {
  originalTransactionId: string;
  bundleId: string;
  productId: string;
  environment: SignalsEnvironment;
  ownershipType: "PURCHASED";
  /** Lifetime is a non-consumable; the monthly plan is an auto-renewable subscription. */
  productType: "NON_CONSUMABLE" | "AUTO_RENEWABLE_SUBSCRIPTION";
  revoked: boolean;
  /** Present only for the monthly subscription: the verified expiry (ms since epoch),
   *  already checked to be in the future at verification time. */
  expiresDate?: number;
};

export type VerifySignedTransactionInput = {
  signedTransactionInfo: string;
  expectedEnvironment: SignalsEnvironment;
};

export interface AppleEntitlementVerifier {
  verifySignedTransaction(
    input: VerifySignedTransactionInput,
  ): Promise<VerifiedAppleEntitlement>;
}

export type AppleVerificationErrorCode =
  | "invalid_proof"
  | "unsupported_environment"
  | "verification_unavailable";

export class AppleVerificationError extends Error {
  readonly code: AppleVerificationErrorCode;

  constructor(code: AppleVerificationErrorCode) {
    super(code);
    this.name = "AppleVerificationError";
    this.code = code;
  }
}

export class UnavailableAppleEntitlementVerifier
  implements AppleEntitlementVerifier
{
  async verifySignedTransaction(): Promise<VerifiedAppleEntitlement> {
    throw new AppleVerificationError("verification_unavailable");
  }
}
