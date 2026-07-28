import {
  createHmac,
  createPrivateKey,
  createPublicKey,
  createSign,
  createVerify,
  randomBytes,
  type KeyObject,
} from "node:crypto";
import type { SignalsEnvironment } from "./apple-verifier.js";

export const TOKEN_ISSUER = "signals-auth";
export const TOKEN_AUDIENCE = "signals-custom-mix";
export const TOKEN_SCOPE = "custom_mix";
export const PRO_PRODUCT_ID = "com.signalsapp.pro.lifetime";
export const TOKEN_TTL_SECONDS = 15 * 60;
export const TOKEN_CLOCK_SKEW_SECONDS = 60;

export type Clock = { nowMs(): number };

export type SignalsTokenClaims = {
  iss: typeof TOKEN_ISSUER;
  aud: typeof TOKEN_AUDIENCE;
  sub: string;
  scope: [typeof TOKEN_SCOPE];
  product: typeof PRO_PRODUCT_ID;
  environment: SignalsEnvironment;
  iat: number;
  nbf: number;
  exp: number;
  jti: string;
};

export type SignalsTokenSigner = {
  kid: string;
  privateKeyPem: string;
};

export type SignalsTokenVerifierKey = {
  kid: string;
  publicKeyPem: string;
};

export type IssueSignalsTokenInput = {
  subject: string;
  environment: SignalsEnvironment;
};

export type VerifySignalsTokenInput = {
  token: string;
  expectedEnvironment: SignalsEnvironment;
};

export type SignalsTokenServiceOptions = {
  signer: SignalsTokenSigner;
  verificationKeys: SignalsTokenVerifierKey[];
  hmacSecret: string;
  clock: Clock;
  randomJti?: () => string;
};

export class SignalsTokenError extends Error {
  readonly code:
    | "invalid_token"
    | "expired_token"
    | "wrong_issuer"
    | "wrong_audience"
    | "wrong_scope"
    | "wrong_product"
    | "wrong_environment";

  constructor(code: SignalsTokenError["code"]) {
    super(code);
    this.name = "SignalsTokenError";
    this.code = code;
  }
}

function encodeJson(value: unknown): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function decodeJson(value: string): unknown {
  return JSON.parse(Buffer.from(value, "base64url").toString("utf8"));
}

function signPayload(input: string, privateKey: KeyObject): string {
  const signer = createSign("SHA256");
  signer.update(input);
  signer.end();
  return signer
    .sign({ key: privateKey, dsaEncoding: "ieee-p1363" })
    .toString("base64url");
}

function verifyPayload(
  input: string,
  signature: string,
  publicKey: KeyObject,
): boolean {
  const verifier = createVerify("SHA256");
  verifier.update(input);
  verifier.end();
  return verifier.verify(
    { key: publicKey, dsaEncoding: "ieee-p1363" },
    Buffer.from(signature, "base64url"),
  );
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export class SignalsTokenService {
  private readonly signingKey: KeyObject;
  private readonly verificationKeys: Map<string, KeyObject>;
  private readonly randomJti: () => string;

  constructor(private readonly options: SignalsTokenServiceOptions) {
    if (Buffer.byteLength(options.hmacSecret, "utf8") < 32) {
      throw new Error("HMAC secret must be at least 32 bytes");
    }
    this.signingKey = createPrivateKey(options.signer.privateKeyPem);
    this.verificationKeys = new Map(
      options.verificationKeys.map((item) => [
        item.kid,
        createPublicKey(item.publicKeyPem),
      ]),
    );
    this.randomJti =
      options.randomJti ?? (() => randomBytes(16).toString("base64url"));
  }

  deriveSubject(
    originalTransactionId: string,
    environment: SignalsEnvironment,
  ): string {
    if (!originalTransactionId) {
      throw new Error("original transaction ID is required");
    }
    return createHmac("sha256", this.options.hmacSecret)
      .update(`${environment}:${originalTransactionId}`, "utf8")
      .digest("base64url");
  }

  issue(input: IssueSignalsTokenInput): {
    accessToken: string;
    claims: SignalsTokenClaims;
  } {
    const now = Math.floor(this.options.clock.nowMs() / 1_000);
    const claims: SignalsTokenClaims = {
      iss: TOKEN_ISSUER,
      aud: TOKEN_AUDIENCE,
      sub: input.subject,
      scope: [TOKEN_SCOPE],
      product: PRO_PRODUCT_ID,
      environment: input.environment,
      iat: now,
      nbf: now,
      exp: now + TOKEN_TTL_SECONDS,
      jti: this.randomJti(),
    };
    const header = { alg: "ES256", typ: "JWT", kid: this.options.signer.kid };
    const signingInput = `${encodeJson(header)}.${encodeJson(claims)}`;
    const signature = signPayload(signingInput, this.signingKey);
    return { accessToken: `${signingInput}.${signature}`, claims };
  }

  verify(input: VerifySignalsTokenInput): SignalsTokenClaims {
    const parts = input.token.split(".");
    if (parts.length !== 3 || parts.some((part) => !part)) {
      throw new SignalsTokenError("invalid_token");
    }

    let header: unknown;
    let payload: unknown;
    try {
      header = decodeJson(parts[0]);
      payload = decodeJson(parts[1]);
    } catch {
      throw new SignalsTokenError("invalid_token");
    }
    if (
      !isObject(header) ||
      header.alg !== "ES256" ||
      typeof header.kid !== "string"
    ) {
      throw new SignalsTokenError("invalid_token");
    }
    const publicKey = this.verificationKeys.get(header.kid);
    if (
      !publicKey ||
      !verifyPayload(`${parts[0]}.${parts[1]}`, parts[2], publicKey)
    ) {
      throw new SignalsTokenError("invalid_token");
    }
    if (!isObject(payload)) {
      throw new SignalsTokenError("invalid_token");
    }

    const requiredStrings = [
      "iss",
      "aud",
      "sub",
      "product",
      "environment",
      "jti",
    ] as const;
    if (
      requiredStrings.some((key) => typeof payload[key] !== "string") ||
      !Array.isArray(payload.scope) ||
      payload.scope.some((scope) => typeof scope !== "string") ||
      !["iat", "nbf", "exp"].every(
        (key) => typeof payload[key] === "number",
      )
    ) {
      throw new SignalsTokenError("invalid_token");
    }

    if (payload.iss !== TOKEN_ISSUER) {
      throw new SignalsTokenError("wrong_issuer");
    }
    if (payload.aud !== TOKEN_AUDIENCE) {
      throw new SignalsTokenError("wrong_audience");
    }
    if (
      payload.scope.length !== 1 ||
      payload.scope[0] !== TOKEN_SCOPE
    ) {
      throw new SignalsTokenError("wrong_scope");
    }
    if (payload.product !== PRO_PRODUCT_ID) {
      throw new SignalsTokenError("wrong_product");
    }
    if (payload.environment !== input.expectedEnvironment) {
      throw new SignalsTokenError("wrong_environment");
    }

    const now = Math.floor(this.options.clock.nowMs() / 1_000);
    if (
      (payload.nbf as number) >
      now + TOKEN_CLOCK_SKEW_SECONDS
    ) {
      throw new SignalsTokenError("invalid_token");
    }
    if (
      (payload.exp as number) <
      now - TOKEN_CLOCK_SKEW_SECONDS
    ) {
      throw new SignalsTokenError("expired_token");
    }

    return payload as SignalsTokenClaims;
  }
}
