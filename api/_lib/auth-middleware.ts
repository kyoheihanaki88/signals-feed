import type { SignalsEnvironment } from "./apple-verifier.js";
import type { RateLimiter } from "./rate-limit.js";
import type { RevocationStore } from "./revocation-store.js";
import {
  SignalsTokenError,
  type SignalsTokenClaims,
  type SignalsTokenService,
} from "./signals-token.js";

export class AuthenticationError extends Error {
  constructor(
    readonly status: number,
    readonly code:
      | "missing_token"
      | "invalid_token"
      | "expired_token"
      | "wrong_scope"
      | "wrong_environment"
      | "revoked"
      | "rate_limited"
      | "verification_unavailable",
    readonly retryAfterSeconds?: number,
  ) {
    super(code);
    this.name = "AuthenticationError";
  }
}

export type AuthenticateEditionInput = {
  authorization: string | null;
  expectedEnvironment: SignalsEnvironment;
  nowMs: number;
};

export type EditionAuthenticatorDependencies = {
  tokens: SignalsTokenService;
  revocations: RevocationStore;
  limiter: RateLimiter;
};

export async function authenticateEdition(
  input: AuthenticateEditionInput,
  dependencies: EditionAuthenticatorDependencies,
): Promise<SignalsTokenClaims> {
  if (!input.authorization?.startsWith("Bearer ")) {
    throw new AuthenticationError(401, "missing_token");
  }
  const token = input.authorization.slice("Bearer ".length).trim();
  if (!token) {
    throw new AuthenticationError(401, "missing_token");
  }

  let claims: SignalsTokenClaims;
  try {
    claims = dependencies.tokens.verify({
      token,
      expectedEnvironment: input.expectedEnvironment,
    });
  } catch (error) {
    if (error instanceof SignalsTokenError) {
      if (error.code === "expired_token") {
        throw new AuthenticationError(401, "expired_token");
      }
      if (error.code === "wrong_scope") {
        throw new AuthenticationError(403, "wrong_scope");
      }
      if (error.code === "wrong_environment") {
        throw new AuthenticationError(403, "wrong_environment");
      }
    }
    throw new AuthenticationError(401, "invalid_token");
  }

  try {
    if (await dependencies.revocations.isRevoked(claims.sub)) {
      throw new AuthenticationError(401, "revoked");
    }
    const decision = await dependencies.limiter.consume(
      claims.jti,
      input.nowMs,
    );
    if (!decision.allowed) {
      throw new AuthenticationError(
        429,
        "rate_limited",
        decision.retryAfterSeconds,
      );
    }
  } catch (error) {
    if (error instanceof AuthenticationError) {
      throw error;
    }
    throw new AuthenticationError(503, "verification_unavailable");
  }
  return claims;
}
