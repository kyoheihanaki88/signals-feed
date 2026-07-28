export type SignalsEnvironment = "Production" | "Sandbox";

export type VerifiedAppleEntitlement = {
  originalTransactionId: string;
  bundleId: string;
  productId: string;
  environment: SignalsEnvironment;
  ownershipType: "PURCHASED";
  productType: "NON_CONSUMABLE";
  revoked: boolean;
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
