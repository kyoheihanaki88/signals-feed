/**
 * Strict environment contract for the Custom Mix API. (Phase 3B-3)
 *
 * Parsed ONCE through this module. Every consumer takes the resulting typed config;
 * nothing else in the API reads `process.env` for security-relevant values.
 *
 * Hard rules enforced here:
 *   • Production and Sandbox are SEPARATE deployments with separate Apple credentials,
 *     Redis namespaces and signing keys. A missing or contradictory value fails startup
 *     rather than silently degrading.
 *   • Preview NEVER receives Production/Sandbox secrets and NEVER performs real Apple
 *     verification — it is fake-verifier-only by construction.
 *   • Secret VALUES are never included in thrown errors or logs; errors name the variable
 *     and the problem only.
 */

export type DeployEnvironment = "production" | "sandbox" | "preview";
export type AppleEnvironment = "Production" | "Sandbox";

export class EnvironmentContractError extends Error {
  readonly issues: readonly string[];

  constructor(issues: readonly string[]) {
    super(`invalid environment configuration: ${issues.join("; ")}`);
    this.name = "EnvironmentContractError";
    this.issues = issues;
  }
}

export type RedisConfig = {
  restUrl: string;
  restToken: string;
  /** Namespace prefix for every key this deployment writes. */
  namespace: string;
};

export type AppleCredentials = {
  environment: AppleEnvironment;
  bundleId: string;
  /** Required only when environment === "Production". */
  appAppleId?: number;
  issuerId: string;
  keyId: string;
  privateKeyPem: string;
};

export type TokenKeyConfig = {
  signingKid: string;
  signingPrivateKeyPem: string;
  currentPublicKeyPem: string;
  previousKid?: string;
  previousPublicKeyPem?: string;
  hmacSecret: string;
  issuer: string;
  audience: string;
};

export type LimitsConfig = {
  exchangePerIpPerMinute: number;
  exchangePerSubjectPerHour: number;
  editionPerTokenPerMinute: number;
  circuitBreakerFailureThreshold: number;
  circuitBreakerOpenMs: number;
};

export type SignalsApiConfig =
  | {
      deployEnvironment: "preview";
      /** Preview is fake-only: there are no Apple credentials at all. */
      apple: null;
      redis: RedisConfig | null;
      token: TokenKeyConfig;
      limits: LimitsConfig;
    }
  | {
      deployEnvironment: "production" | "sandbox";
      apple: AppleCredentials;
      redis: RedisConfig;
      token: TokenKeyConfig;
      limits: LimitsConfig;
    };

export type RawEnv = Record<string, string | undefined>;

const APPLE_SECRET_VARS = [
  "APP_STORE_ISSUER_ID",
  "APP_STORE_KEY_ID",
  "APP_STORE_PRIVATE_KEY",
] as const;

const DEFAULT_LIMITS: LimitsConfig = {
  exchangePerIpPerMinute: 10,
  exchangePerSubjectPerHour: 30,
  editionPerTokenPerMinute: 60,
  circuitBreakerFailureThreshold: 5,
  circuitBreakerOpenMs: 30_000,
};

/** Trim surrounding whitespace; treat an all-whitespace value as absent. */
function read(env: RawEnv, name: string): string | undefined {
  const raw = env[name];
  if (raw === undefined) return undefined;
  const trimmed = raw.trim();
  return trimmed.length === 0 ? undefined : trimmed;
}

function present(env: RawEnv, name: string): boolean {
  return read(env, name) !== undefined;
}

/**
 * Normalize a PEM that may arrive with literal "\n" sequences (the common way to put a
 * multi-line key into a single-line dashboard field) or with CRLF line endings.
 * Only whitespace/newline shape is touched — never the key material.
 */
export function normalizePem(value: string): string {
  return value
    .replace(/\\r\\n/g, "\n")
    .replace(/\\n/g, "\n")
    .replace(/\r\n/g, "\n")
    .trim();
}

function pemLooksValid(pem: string, label: string): boolean {
  const header = `-----BEGIN ${label}-----`;
  const footer = `-----END ${label}-----`;
  if (!pem.startsWith(header) || !pem.endsWith(footer)) return false;
  const body = pem.slice(header.length, pem.length - footer.length).trim();
  if (body.length === 0) return false;
  return /^[A-Za-z0-9+/=\s]+$/.test(body);
}

function readPositiveInt(
  env: RawEnv,
  name: string,
  fallback: number,
  issues: string[],
): number {
  const raw = read(env, name);
  if (raw === undefined) return fallback;
  if (!/^\d+$/.test(raw)) {
    issues.push(`${name} must be a positive integer`);
    return fallback;
  }
  const value = Number.parseInt(raw, 10);
  if (value < 1) {
    issues.push(`${name} must be >= 1`);
    return fallback;
  }
  return value;
}

function parseTokenConfig(env: RawEnv, issues: string[]): TokenKeyConfig {
  const signingKid = read(env, "SIGNALS_TOKEN_SIGNING_KID");
  const signingKeyRaw = read(env, "SIGNALS_TOKEN_SIGNING_KEY");
  const currentPublicRaw = read(env, "SIGNALS_TOKEN_PUBLIC_KEY");
  const previousKid = read(env, "SIGNALS_TOKEN_PREVIOUS_KID");
  const previousPublicRaw = read(env, "SIGNALS_TOKEN_PREVIOUS_PUBLIC_KEY");
  const hmacSecret = read(env, "SIGNALS_TOKEN_HMAC_SECRET");

  if (!signingKid) issues.push("SIGNALS_TOKEN_SIGNING_KID is required");
  if (!signingKeyRaw) issues.push("SIGNALS_TOKEN_SIGNING_KEY is required");
  if (!currentPublicRaw) issues.push("SIGNALS_TOKEN_PUBLIC_KEY is required");
  if (!hmacSecret) issues.push("SIGNALS_TOKEN_HMAC_SECRET is required");
  else if (hmacSecret.length < 32) {
    issues.push("SIGNALS_TOKEN_HMAC_SECRET must be at least 32 characters");
  }

  const signingPrivateKeyPem = signingKeyRaw ? normalizePem(signingKeyRaw) : "";
  if (signingKeyRaw && !pemLooksValid(signingPrivateKeyPem, "PRIVATE KEY")) {
    issues.push("SIGNALS_TOKEN_SIGNING_KEY is not a valid PKCS#8 PEM private key");
  }
  const currentPublicKeyPem = currentPublicRaw ? normalizePem(currentPublicRaw) : "";
  if (currentPublicRaw && !pemLooksValid(currentPublicKeyPem, "PUBLIC KEY")) {
    issues.push("SIGNALS_TOKEN_PUBLIC_KEY is not a valid PEM public key");
  }

  let previousPublicKeyPem: string | undefined;
  if (previousKid || previousPublicRaw) {
    // The previous key is optional, but it is a PAIR: one half alone is ambiguous.
    if (!previousKid || !previousPublicRaw) {
      issues.push(
        "SIGNALS_TOKEN_PREVIOUS_KID and SIGNALS_TOKEN_PREVIOUS_PUBLIC_KEY must be set together",
      );
    } else {
      previousPublicKeyPem = normalizePem(previousPublicRaw);
      if (!pemLooksValid(previousPublicKeyPem, "PUBLIC KEY")) {
        issues.push("SIGNALS_TOKEN_PREVIOUS_PUBLIC_KEY is not a valid PEM public key");
      }
      if (previousKid === signingKid) {
        issues.push("SIGNALS_TOKEN_PREVIOUS_KID must differ from SIGNALS_TOKEN_SIGNING_KID");
      }
    }
  }

  return {
    signingKid: signingKid ?? "",
    signingPrivateKeyPem,
    currentPublicKeyPem,
    previousKid: previousKid && previousPublicKeyPem ? previousKid : undefined,
    previousPublicKeyPem,
    hmacSecret: hmacSecret ?? "",
    issuer: read(env, "SIGNALS_TOKEN_ISSUER") ?? "signals-auth",
    audience: read(env, "SIGNALS_TOKEN_AUDIENCE") ?? "signals-custom-mix",
  };
}

function parseRedis(
  env: RawEnv,
  deployEnvironment: DeployEnvironment,
  appleEnvironment: AppleEnvironment | null,
  issues: string[],
): RedisConfig | null {
  const restUrl = read(env, "KV_REST_API_URL");
  const restToken = read(env, "KV_REST_API_TOKEN");

  if (!restUrl && !restToken) {
    if (deployEnvironment === "preview") return null; // preview may run store-less
    issues.push("KV_REST_API_URL and KV_REST_API_TOKEN are required");
    return null;
  }
  if (!restUrl) issues.push("KV_REST_API_URL is required when KV_REST_API_TOKEN is set");
  if (!restToken) issues.push("KV_REST_API_TOKEN is required when KV_REST_API_URL is set");
  if (restUrl && !/^https:\/\/[^\s]+$/.test(restUrl)) {
    issues.push("KV_REST_API_URL must be an https URL");
  }

  // Keys are namespaced by deployment so Production, Sandbox and Preview can never
  // read or clobber each other even if they were ever pointed at one database.
  const namespace = `signals:${deployEnvironment}:${(appleEnvironment ?? "none").toLowerCase()}`;
  return { restUrl: restUrl ?? "", restToken: restToken ?? "", namespace };
}

function parseApple(
  env: RawEnv,
  deployEnvironment: "production" | "sandbox",
  issues: string[],
): AppleCredentials | null {
  const declared = read(env, "APPLE_ENVIRONMENT");
  if (!declared) {
    issues.push("APPLE_ENVIRONMENT is required");
    return null;
  }
  if (declared !== "Production" && declared !== "Sandbox") {
    issues.push('APPLE_ENVIRONMENT must be exactly "Production" or "Sandbox"');
    return null;
  }
  // The deployment and the Apple environment must agree — a Production deployment
  // pointed at Sandbox (or vice versa) is a contradiction, not a configuration.
  const expected: AppleEnvironment =
    deployEnvironment === "production" ? "Production" : "Sandbox";
  if (declared !== expected) {
    issues.push(
      `APPLE_ENVIRONMENT (${declared}) contradicts SIGNALS_DEPLOY_ENV (${deployEnvironment})`,
    );
  }

  const bundleId = read(env, "APPLE_BUNDLE_ID");
  if (!bundleId) issues.push("APPLE_BUNDLE_ID is required");

  const issuerId = read(env, "APP_STORE_ISSUER_ID");
  if (!issuerId) issues.push("APP_STORE_ISSUER_ID is required");
  const keyId = read(env, "APP_STORE_KEY_ID");
  if (!keyId) issues.push("APP_STORE_KEY_ID is required");

  const privateKeyRaw = read(env, "APP_STORE_PRIVATE_KEY");
  let privateKeyPem = "";
  if (!privateKeyRaw) {
    issues.push("APP_STORE_PRIVATE_KEY is required");
  } else {
    privateKeyPem = normalizePem(privateKeyRaw);
    if (!pemLooksValid(privateKeyPem, "PRIVATE KEY")) {
      issues.push("APP_STORE_PRIVATE_KEY is not a valid PKCS#8 PEM private key");
    }
  }

  let appAppleId: number | undefined;
  const appAppleIdRaw = read(env, "APPLE_APP_APPLE_ID");
  if (declared === "Production") {
    if (!appAppleIdRaw) {
      issues.push("APPLE_APP_APPLE_ID is required when APPLE_ENVIRONMENT=Production");
    } else if (!/^\d+$/.test(appAppleIdRaw)) {
      issues.push("APPLE_APP_APPLE_ID must be a numeric App Store app id");
    } else {
      appAppleId = Number.parseInt(appAppleIdRaw, 10);
    }
  }

  return {
    environment: declared,
    bundleId: bundleId ?? "",
    appAppleId,
    issuerId: issuerId ?? "",
    keyId: keyId ?? "",
    privateKeyPem,
  };
}

/**
 * Parse and validate the whole contract. Throws `EnvironmentContractError` listing every
 * problem found (never a secret value). Call once at module init.
 */
export function loadApiConfig(env: RawEnv = process.env): SignalsApiConfig {
  const issues: string[] = [];

  const deployRaw = read(env, "SIGNALS_DEPLOY_ENV");
  if (!deployRaw) {
    throw new EnvironmentContractError([
      "SIGNALS_DEPLOY_ENV is required (production | sandbox | preview)",
    ]);
  }
  const deployEnvironment = deployRaw.toLowerCase() as DeployEnvironment;
  if (!["production", "sandbox", "preview"].includes(deployEnvironment)) {
    throw new EnvironmentContractError([
      "SIGNALS_DEPLOY_ENV must be one of production | sandbox | preview",
    ]);
  }

  const token = parseTokenConfig(env, issues);
  const limits: LimitsConfig = {
    exchangePerIpPerMinute: readPositiveInt(
      env, "SIGNALS_RATE_EXCHANGE_IP_PER_MIN", DEFAULT_LIMITS.exchangePerIpPerMinute, issues),
    exchangePerSubjectPerHour: readPositiveInt(
      env, "SIGNALS_RATE_EXCHANGE_SUBJECT_PER_HOUR", DEFAULT_LIMITS.exchangePerSubjectPerHour, issues),
    editionPerTokenPerMinute: readPositiveInt(
      env, "SIGNALS_RATE_EDITION_TOKEN_PER_MIN", DEFAULT_LIMITS.editionPerTokenPerMinute, issues),
    circuitBreakerFailureThreshold: readPositiveInt(
      env, "SIGNALS_APPLE_BREAKER_THRESHOLD", DEFAULT_LIMITS.circuitBreakerFailureThreshold, issues),
    circuitBreakerOpenMs: readPositiveInt(
      env, "SIGNALS_APPLE_BREAKER_OPEN_MS", DEFAULT_LIMITS.circuitBreakerOpenMs, issues),
  };

  if (deployEnvironment === "preview") {
    // Preview must be incapable of touching real Apple credentials. Their mere presence
    // is a misconfiguration (a leaked secret in a lower environment), so fail closed.
    for (const name of APPLE_SECRET_VARS) {
      if (present(env, name)) {
        issues.push(`${name} must not be set in Preview (fake Apple verification only)`);
      }
    }
    if (present(env, "APPLE_ENVIRONMENT")) {
      issues.push("APPLE_ENVIRONMENT must not be set in Preview");
    }
    const redis = parseRedis(env, deployEnvironment, null, issues);
    if (issues.length > 0) throw new EnvironmentContractError(issues);
    return { deployEnvironment, apple: null, redis, token, limits };
  }

  const apple = parseApple(env, deployEnvironment, issues);
  const redis = parseRedis(env, deployEnvironment, apple?.environment ?? null, issues);
  if (issues.length > 0 || !apple || !redis) {
    throw new EnvironmentContractError(
      issues.length > 0 ? issues : ["apple and redis configuration are required"],
    );
  }
  return { deployEnvironment, apple, redis, token, limits };
}

/** True when this deployment must use the fake Apple verifier (Preview only). */
export function usesFakeAppleVerification(config: SignalsApiConfig): boolean {
  return config.deployEnvironment === "preview";
}
