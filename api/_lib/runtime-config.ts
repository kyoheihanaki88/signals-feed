/**
 * Production runtime configuration model for the Custom Mix API. (Phase 3C-1)
 *
 * This is the SINGLE place that turns raw environment variables into the typed values the
 * composition root needs. It deliberately does NOT re-implement the Phase 3B-3 contract in
 * `env.ts`: that module already owns Apple credentials, Redis credentials, token key
 * material and the rate-limit/breaker variable names. This module DELEGATES to it and adds
 * the runtime-only layer on top:
 *
 *   • an explicit Production / Sandbox / Development mode
 *   • the Custom Mix invariants (selector version, story count, pool timezone, kill switch)
 *   • the token lifetime declaration (which must agree with the compiled constants)
 *   • real cryptographic parsing of every PEM, so malformed key material fails HERE rather
 *     than at first use
 *
 * Two hard rules run through the whole file:
 *   1. There is no default for any critical secret. A missing Production value fails at
 *      construction; nothing silently degrades.
 *   2. A thrown error names the VARIABLE and the problem. It never contains the value —
 *      not a key, not a token, not a URL, not an identifier.
 *
 * Nothing here reads the network, the filesystem, or `process.env` at import time, and
 * `process.env` is never mutated: `loadRuntimeConfig()` copies the map it is handed.
 */

import { createPrivateKey, createPublicKey } from "node:crypto";

import {
  EnvironmentContractError,
  loadApiConfig,
  type AppleEnvironment,
  type LimitsConfig,
  type RawEnv,
  type RedisConfig,
  type SignalsApiConfig,
  type TokenKeyConfig,
} from "./env.js";
import {
  SIGNALS_BUNDLE_ID,
  SIGNALS_PRO_PRODUCT_ID,
} from "./apple-verifier-real.js";
import {
  STORY_COUNT,
  SUPPORTED_SELECTOR_VERSION,
} from "./custom-mix-contract.js";
import {
  TOKEN_AUDIENCE,
  TOKEN_CLOCK_SKEW_SECONDS,
  TOKEN_ISSUER,
  TOKEN_TTL_SECONDS,
} from "./signals-token.js";

/** The runtime's own vocabulary. `development` is the runtime name for a preview deploy. */
export type RuntimeMode = "production" | "sandbox" | "development";

/** v1 pools are built on the New York editorial day; nothing else is supported. */
export const REQUIRED_POOL_TIMEZONE = "America/New_York";

export class RuntimeConfigError extends Error {
  readonly issues: readonly string[];

  constructor(issues: readonly string[]) {
    super(`invalid runtime configuration: ${issues.join("; ")}`);
    this.name = "RuntimeConfigError";
    this.issues = issues;
  }
}

export type RuntimeAppleConfig = {
  environment: AppleEnvironment;
  bundleId: string;
  productId: string;
  /** Required by Apple for Production; must be absent for Sandbox. */
  appAppleId?: number;
  issuerId: string;
  keyId: string;
  /** Normalized PKCS#8 PEM from the App Store Connect .p8. */
  privateKeyPem: string;
  /** Apple's online certificate revocation/expiry checks. */
  enableOnlineChecks: boolean;
};

export type RuntimeTokenConfig = TokenKeyConfig & {
  ttlSeconds: number;
  clockSkewSeconds: number;
};

export type RuntimeCustomMixConfig = {
  /** The kill switch. When false, both routes answer 503 custom_mix_disabled. */
  enabled: boolean;
  selectorVersion: number;
  storyCount: number;
  poolTimezone: string;
};

export type RuntimeStorageConfig = RedisConfig & {
  /**
   * Reserved namespace for the Custom Mix pool. Phase 3C-1 computes it so the separation
   * is provable, but NOTHING reads or writes it yet — the selector is not connected.
   */
  poolNamespace: string;
};

type RuntimeConfigBase = {
  token: RuntimeTokenConfig;
  customMix: RuntimeCustomMixConfig;
  limits: LimitsConfig;
};

export type RuntimeConfig =
  | (RuntimeConfigBase & {
      mode: "development";
      /** Development never holds Apple credentials — it cannot verify for real. */
      apple: null;
      storage: RuntimeStorageConfig | null;
    })
  | (RuntimeConfigBase & {
      mode: "production" | "sandbox";
      apple: RuntimeAppleConfig;
      storage: RuntimeStorageConfig;
    });

const MODE_ALIASES: Record<string, RuntimeMode> = {
  production: "production",
  sandbox: "sandbox",
  development: "development",
  // `preview` is the deploy name the Phase 3B-3 contract already uses for the same thing.
  preview: "development",
};

/** The env.ts deploy name for a runtime mode. */
function deployEnvFor(mode: RuntimeMode): "production" | "sandbox" | "preview" {
  return mode === "development" ? "preview" : mode;
}

function read(env: RawEnv, name: string): string | undefined {
  const raw = env[name];
  if (raw === undefined) return undefined;
  const trimmed = raw.trim();
  return trimmed.length === 0 ? undefined : trimmed;
}

function requireExact(
  env: RawEnv,
  name: string,
  expected: string,
  issues: string[],
): void {
  const value = read(env, name);
  if (value === undefined) {
    issues.push(`${name} is required`);
    return;
  }
  if (value !== expected) {
    // The expectation is a public constant, so naming it helps without leaking anything.
    issues.push(`${name} must be exactly "${expected}"`);
  }
}

function optionalExactInteger(
  env: RawEnv,
  name: string,
  expected: number,
  issues: string[],
): number {
  const value = read(env, name);
  if (value === undefined) return expected;
  if (!/^\d+$/.test(value)) {
    issues.push(`${name} must be a positive integer`);
    return expected;
  }
  if (Number.parseInt(value, 10) !== expected) {
    issues.push(
      `${name} must be ${expected} for selector v1 (a change requires explicit approval)`,
    );
  }
  return expected;
}

function parseBooleanFlag(
  env: RawEnv,
  name: string,
  issues: string[],
): boolean {
  const value = read(env, name);
  if (value === undefined) {
    issues.push(`${name} is required (true | false)`);
    return false;
  }
  if (value !== "true" && value !== "false") {
    issues.push(`${name} must be exactly "true" or "false"`);
    return false;
  }
  return value === "true";
}

/**
 * Confirm the PEM is a real ES256 (P-256) key. `env.ts` proves the PEM SHAPE; this proves
 * the key MATERIAL, so a value that merely looks like a PEM cannot reach the token signer
 * or Apple's client. Only the variable name escapes — the underlying error is discarded.
 */
function assertEs256Key(
  pem: string,
  name: string,
  kind: "private" | "public",
  issues: string[],
): void {
  if (pem.length === 0) return; // absence/shape is already reported by the caller
  try {
    const key =
      kind === "private" ? createPrivateKey(pem) : createPublicKey(pem);
    if (key.asymmetricKeyType !== "ec") {
      issues.push(`${name} must be an EC (P-256) key`);
      return;
    }
    const curve = key.asymmetricKeyDetails?.namedCurve;
    if (curve !== undefined && curve !== "prime256v1") {
      issues.push(`${name} must use the P-256 curve`);
    }
  } catch {
    // Deliberately swallowed: a crypto error can echo input. Report the variable only.
    issues.push(`${name} is not usable key material`);
  }
}

/** Delegate to the Phase 3B-3 contract, re-badging its issues as runtime issues. */
function parseBaseContract(source: RawEnv, mode: RuntimeMode): SignalsApiConfig {
  try {
    return loadApiConfig({ ...source, SIGNALS_DEPLOY_ENV: deployEnvFor(mode) });
  } catch (error) {
    if (error instanceof EnvironmentContractError) {
      throw new RuntimeConfigError(error.issues);
    }
    throw new RuntimeConfigError(["environment contract could not be parsed"]);
  }
}

function validateRestUrl(url: string, name: string, issues: string[]): void {
  if (url.length === 0) return;
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    issues.push(`${name} is not a valid URL`);
    return;
  }
  if (parsed.protocol !== "https:") issues.push(`${name} must use https`);
  if (!parsed.hostname) issues.push(`${name} must include a hostname`);
  if (parsed.search || parsed.hash) {
    issues.push(`${name} must not contain a query string or fragment`);
  }
}

/**
 * Parse and validate the full runtime contract.
 *
 * Throws `RuntimeConfigError` listing every problem found. The returned object is the ONLY
 * thing the dependency factories accept — no factory reads `process.env`.
 */
export function loadRuntimeConfig(env: RawEnv = process.env): RuntimeConfig {
  // Copy first: nothing downstream can mutate the caller's environment map.
  const source: RawEnv = { ...env };

  const declaredMode = read(source, "SIGNALS_DEPLOY_ENV");
  if (!declaredMode) {
    throw new RuntimeConfigError([
      "SIGNALS_DEPLOY_ENV is required (production | sandbox | development)",
    ]);
  }
  const mode = MODE_ALIASES[declaredMode.toLowerCase()];
  if (!mode) {
    throw new RuntimeConfigError([
      "SIGNALS_DEPLOY_ENV must be one of production | sandbox | development",
    ]);
  }

  const issues: string[] = [];

  // ── Custom Mix invariants ──────────────────────────────────────────────────────────
  const enabled = parseBooleanFlag(source, "CUSTOM_MIX_API_ENABLED", issues);
  const selectorVersion = optionalExactInteger(
    source,
    "CUSTOM_MIX_SELECTOR_VERSION",
    SUPPORTED_SELECTOR_VERSION,
    issues,
  );
  const storyCount = optionalExactInteger(
    source,
    "CUSTOM_MIX_STORY_COUNT",
    STORY_COUNT,
    issues,
  );
  if (read(source, "CUSTOM_MIX_SELECTOR_VERSION") === undefined) {
    issues.push("CUSTOM_MIX_SELECTOR_VERSION is required");
  }
  if (read(source, "CUSTOM_MIX_STORY_COUNT") === undefined) {
    issues.push("CUSTOM_MIX_STORY_COUNT is required");
  }
  requireExact(
    source,
    "CUSTOM_MIX_POOL_TIMEZONE",
    REQUIRED_POOL_TIMEZONE,
    issues,
  );

  // ── token lifetime: a declaration that must agree with the compiled service ─────────
  // `signals-token.ts` compiles TTL and skew as constants. Accepting a different declared
  // value would make the environment lie about what the service actually does.
  const ttlSeconds = optionalExactInteger(
    source,
    "SIGNALS_TOKEN_TTL_SECONDS",
    TOKEN_TTL_SECONDS,
    issues,
  );
  const clockSkewSeconds = optionalExactInteger(
    source,
    "SIGNALS_TOKEN_CLOCK_SKEW_SECONDS",
    TOKEN_CLOCK_SKEW_SECONDS,
    issues,
  );
  // Only NOW delegate to the Phase 3B-3 contract. Running the checks above first means a
  // deployment sees every problem in one pass instead of one per redeploy.
  let base: SignalsApiConfig;
  try {
    base = parseBaseContract(source, mode);
  } catch (error) {
    const baseIssues =
      error instanceof RuntimeConfigError
        ? error.issues
        : ["environment contract could not be parsed"];
    throw new RuntimeConfigError([...issues, ...baseIssues]);
  }

  if (base.token.issuer !== TOKEN_ISSUER) {
    issues.push(`SIGNALS_TOKEN_ISSUER must be exactly "${TOKEN_ISSUER}"`);
  }
  if (base.token.audience !== TOKEN_AUDIENCE) {
    issues.push(`SIGNALS_TOKEN_AUDIENCE must be exactly "${TOKEN_AUDIENCE}"`);
  }

  // ── real key material ──────────────────────────────────────────────────────────────
  assertEs256Key(
    base.token.signingPrivateKeyPem,
    "SIGNALS_TOKEN_SIGNING_KEY",
    "private",
    issues,
  );
  assertEs256Key(
    base.token.currentPublicKeyPem,
    "SIGNALS_TOKEN_PUBLIC_KEY",
    "public",
    issues,
  );
  if (base.token.previousPublicKeyPem) {
    assertEs256Key(
      base.token.previousPublicKeyPem,
      "SIGNALS_TOKEN_PREVIOUS_PUBLIC_KEY",
      "public",
      issues,
    );
  }

  const token: RuntimeTokenConfig = {
    ...base.token,
    ttlSeconds,
    clockSkewSeconds,
  };
  const customMix: RuntimeCustomMixConfig = {
    enabled,
    selectorVersion,
    storyCount,
    poolTimezone: REQUIRED_POOL_TIMEZONE,
  };

  if (base.redis) {
    validateRestUrl(base.redis.restUrl, "KV_REST_API_URL", issues);
  }

  // ── Development ────────────────────────────────────────────────────────────────────
  if (mode === "development") {
    // `loadApiConfig` already rejects Apple secrets in this mode. Product id is a
    // non-secret, but its presence still signals a Production config leaking downward.
    if (read(source, "APPLE_PRODUCT_ID") !== undefined) {
      issues.push("APPLE_PRODUCT_ID must not be set in Development");
    }
    if (read(source, "APPLE_APP_APPLE_ID") !== undefined) {
      issues.push("APPLE_APP_APPLE_ID must not be set in Development");
    }
    if (issues.length > 0) throw new RuntimeConfigError(issues);
    return {
      mode,
      apple: null,
      storage: base.redis
        ? { ...base.redis, poolNamespace: `${base.redis.namespace}:pool` }
        : null,
      token,
      customMix,
      limits: base.limits,
    };
  }

  // ── Production / Sandbox ───────────────────────────────────────────────────────────
  if (!base.apple || !base.redis) {
    // Unreachable for these modes (loadApiConfig throws first) but never assume it.
    throw new RuntimeConfigError([
      ...issues,
      "apple and storage configuration are required",
    ]);
  }

  if (base.apple.bundleId !== SIGNALS_BUNDLE_ID) {
    issues.push(`APPLE_BUNDLE_ID must be exactly "${SIGNALS_BUNDLE_ID}"`);
  }
  requireExact(source, "APPLE_PRODUCT_ID", SIGNALS_PRO_PRODUCT_ID, issues);
  assertEs256Key(
    base.apple.privateKeyPem,
    "APP_STORE_PRIVATE_KEY",
    "private",
    issues,
  );

  let appAppleId: number | undefined;
  if (base.apple.environment === "Production") {
    if (base.apple.appAppleId === undefined) {
      issues.push("APPLE_APP_APPLE_ID is required in Production");
    } else if (
      !Number.isSafeInteger(base.apple.appAppleId) ||
      base.apple.appAppleId <= 0
    ) {
      issues.push("APPLE_APP_APPLE_ID must be a positive integer");
    } else {
      appAppleId = base.apple.appAppleId;
    }
  } else if (read(source, "APPLE_APP_APPLE_ID") !== undefined) {
    // Apple's SignedDataVerifier rejects an appAppleId in Sandbox. Refusing it here means
    // a Production value can never be silently reused by a Sandbox deployment.
    issues.push("APPLE_APP_APPLE_ID must not be set when APPLE_ENVIRONMENT=Sandbox");
  }

  if (issues.length > 0) throw new RuntimeConfigError(issues);

  const apple: RuntimeAppleConfig = {
    environment: base.apple.environment,
    bundleId: base.apple.bundleId,
    productId: SIGNALS_PRO_PRODUCT_ID,
    ...(appAppleId === undefined ? {} : { appAppleId }),
    issuerId: base.apple.issuerId,
    keyId: base.apple.keyId,
    privateKeyPem: base.apple.privateKeyPem,
    // Online checks cost an outbound call per verification; they are meaningful in
    // Production and unreliable against Sandbox's test chain.
    enableOnlineChecks: base.apple.environment === "Production",
  };

  return {
    mode,
    apple,
    storage: { ...base.redis, poolNamespace: `${base.redis.namespace}:pool` },
    token,
    customMix,
    limits: base.limits,
  };
}

/** True only for a real Production or Sandbox deployment. */
export function isRealDeployment(
  config: RuntimeConfig,
): config is Extract<RuntimeConfig, { mode: "production" | "sandbox" }> {
  return config.mode === "production" || config.mode === "sandbox";
}
