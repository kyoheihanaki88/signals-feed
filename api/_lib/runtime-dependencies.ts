/**
 * Runtime dependency construction — the composition root. (Phase 3C-1)
 *
 * Everything the two routes need is built HERE, from a validated `RuntimeConfig`, through
 * explicit factory functions. Nothing in this module runs at import time: no certificate
 * read, no Redis call, no Apple call, no `process.env` access. Importing this file is
 * inert; calling `createRuntimeDependencies()` is what constructs.
 *
 * Two properties this file exists to guarantee:
 *
 *   1. NO SILENT FAKE. A Production or Sandbox deployment always gets
 *      `RealAppleEntitlementVerifier` + `AppleEntitlementClient`. There is no fallback path
 *      that substitutes a stub when something is missing — a missing dependency throws.
 *      Fake verification lives behind `createDevelopmentDependencies()`, which refuses to
 *      run for any mode other than `development`.
 *
 *   2. LIVE STATE IS MANDATORY. `LiveAppleEntitlementVerifier` refuses to construct without
 *      a current-state source, so a Signals token can never be issued from a signed
 *      transaction alone. A refund issued after purchase is only visible in Apple's current
 *      state, and this is the seam that makes checking it structurally unavoidable.
 *
 * Test seams in `RuntimeTransports` are TRANSPORTS, not policy: injecting a decoder or an
 * HTTP fetcher swaps how bytes move, while every entitlement rule, retry, breaker,
 * namespace and fail-closed branch still executes exactly as it would in Production.
 */

import { randomUUID } from "node:crypto";

import { SignedDataVerifier } from "@apple/app-store-server-library";

import {
  AppleEntitlementClient,
  AppleServiceBreaker,
  createAppStoreClient,
  type Sleep,
  type TransactionHistoryFetcher,
} from "./apple-client.js";
import {
  loadAppleRootCertificates,
  type LoadAppleRootCertificatesOptions,
} from "./apple-root-certificates.js";
import {
  AppleEntitlementError,
  RealAppleEntitlementVerifier,
  toAppleEnvironment,
  type AppleEntitlementReason,
  type RealAppleEntitlementVerifierOptions,
  type SignedTransactionDecoder,
} from "./apple-verifier-real.js";
import {
  AppleVerificationError,
  type AppleEntitlementVerifier,
  type SignalsEnvironment,
  type VerifiedAppleEntitlement,
  type VerifySignedTransactionInput,
} from "./apple-verifier.js";
import { PersistentIdempotencyStore } from "./persistent-idempotency-store.js";
import { PersistentRateLimiter } from "./persistent-rate-limit.js";
import { PersistentRevocationStore } from "./persistent-revocation-store.js";
import type { RateLimiter } from "./rate-limit.js";
import {
  FakeRedisClient,
  UpstashRestClient,
  type FetchLike,
  type RedisClient,
} from "./redis-client.js";
import type { RevocationStore } from "./revocation-store.js";
import { isRealDeployment, type RuntimeConfig, type RuntimeMode } from "./runtime-config.js";
import { JsonSecurityLogger, type SecurityLogger } from "./security-logging.js";
import { SignalsTokenService, type Clock } from "./signals-token.js";
import { configureKeyDerivation } from "./subject-hash.js";

export class RuntimeCompositionError extends Error {
  readonly reason: string;

  constructor(reason: string) {
    // The reason is a short mechanical code — never a secret, a key or an identifier.
    super(`runtime composition failed: ${reason}`);
    this.name = "RuntimeCompositionError";
    this.reason = reason;
  }
}

/**
 * Carries a PRECISE entitlement reason while still being an `AppleVerificationError`, which
 * is the type the exchange route already understands.
 *
 * The shared `AppleVerificationErrorCode` enum has only three members, so mapping every
 * policy failure onto it would collapse "revoked" and "wrong product" into "invalid proof"
 * in both the API envelope and the security log. Widening that enum would mean editing a
 * module outside this phase's scope, so the adapter narrows the gap here instead: the base
 * class receives a valid coarse code (which is what decides 401 vs 503) and the precise
 * reason is surfaced as the reported code.
 */
export class PreciseAppleVerificationError extends AppleVerificationError {
  readonly reason: AppleEntitlementReason;

  constructor(reason: AppleEntitlementReason) {
    super(
      reason === "verification_unavailable"
        ? "verification_unavailable"
        : reason === "unsupported_environment"
          ? "unsupported_environment"
          : "invalid_proof",
    );
    this.name = "PreciseAppleVerificationError";
    this.reason = reason;
    // Deliberate: `code` is the field the route reports. Keeping the coarse value in the
    // base constructor preserves the 503-vs-401 decision; this replaces only the label.
    (this as { code: string }).code = reason;
  }
}

/** Normalize anything a verifier or client can throw into the route's error type. */
export function toVerificationError(error: unknown): AppleVerificationError {
  if (error instanceof AppleEntitlementError) {
    return new PreciseAppleVerificationError(error.reason);
  }
  if (error instanceof AppleVerificationError) return error;
  // An unknown failure is never evidence of a bad proof — treat it as unavailability.
  return new PreciseAppleVerificationError("verification_unavailable");
}

/** The current-state seam: Apple's Transaction History, or a stub in tests. */
export interface CurrentEntitlementSource {
  getCurrentProEntitlement(
    originalTransactionId: string,
  ): Promise<VerifiedAppleEntitlement>;
}

export type LiveAppleEntitlementVerifierOptions = {
  environment: SignalsEnvironment;
  /** Verifies the device's own signed transaction. */
  signedProofVerifier: AppleEntitlementVerifier;
  /** REQUIRED. Without it no token may be issued. */
  currentState: CurrentEntitlementSource;
  /** Durable denylist, consulted BEFORE Apple is contacted. */
  revocations: RevocationStore;
  deriveSubject: (
    originalTransactionId: string,
    environment: SignalsEnvironment,
  ) => string;
  /** Best-effort persistence of a revocation Apple has just confirmed. */
  onRevoked?: (subject: string) => Promise<void>;
};

/**
 * Composes "the device proved it" with "Apple still agrees".
 *
 * Order (matches the Phase 3C-1 contract):
 *   verify JWS → entitlement rules → originalTransactionId → persistent denylist →
 *   current-state lookup → confirm still active.
 *
 * A revoked entitlement is RETURNED with `revoked: true` rather than thrown, because the
 * exchange route already turns that flag into a precise `401 revoked`.
 */
export class LiveAppleEntitlementVerifier implements AppleEntitlementVerifier {
  private readonly options: LiveAppleEntitlementVerifierOptions;

  constructor(options: LiveAppleEntitlementVerifierOptions) {
    if (!options.signedProofVerifier) {
      throw new RuntimeCompositionError("signed_proof_verifier_required");
    }
    if (
      !options.currentState ||
      typeof options.currentState.getCurrentProEntitlement !== "function"
    ) {
      // The single most important guard in this file.
      throw new RuntimeCompositionError("live_current_state_required");
    }
    if (!options.revocations || typeof options.revocations.isRevoked !== "function") {
      throw new RuntimeCompositionError("revocation_store_required");
    }
    if (typeof options.deriveSubject !== "function") {
      throw new RuntimeCompositionError("subject_derivation_required");
    }
    this.options = options;
  }

  async verifySignedTransaction(
    input: VerifySignedTransactionInput,
  ): Promise<VerifiedAppleEntitlement> {
    if (input.expectedEnvironment !== this.options.environment) {
      throw new PreciseAppleVerificationError("unsupported_environment");
    }

    let proof: VerifiedAppleEntitlement;
    try {
      proof = await this.options.signedProofVerifier.verifySignedTransaction(input);
    } catch (error) {
      throw toVerificationError(error);
    }

    const subject = this.options.deriveSubject(
      proof.originalTransactionId,
      this.options.environment,
    );

    // Denylist first: a subject we already know is revoked never costs an Apple call.
    try {
      if (await this.options.revocations.isRevoked(subject)) {
        return { ...proof, revoked: true };
      }
    } catch {
      // "We could not check" is not "not revoked".
      throw new PreciseAppleVerificationError("verification_unavailable");
    }

    let live: VerifiedAppleEntitlement;
    try {
      live = await this.options.currentState.getCurrentProEntitlement(
        proof.originalTransactionId,
      );
    } catch (error) {
      if (error instanceof AppleEntitlementError && error.reason === "revoked") {
        // Remember it, so the next request short-circuits above. A write failure here
        // cannot make the outcome more permissive — the answer is already a denial.
        if (this.options.onRevoked) {
          try {
            await this.options.onRevoked(subject);
          } catch {
            /* durable record is best-effort; the denial stands either way */
          }
        }
        return { ...proof, revoked: true };
      }
      throw toVerificationError(error);
    }

    if (live.originalTransactionId !== proof.originalTransactionId) {
      // Apple answered about a different purchase than the device proved.
      throw new PreciseAppleVerificationError("invalid_proof");
    }
    if (live.environment !== this.options.environment) {
      throw new PreciseAppleVerificationError("unsupported_environment");
    }
    return live;
  }
}

export type RuntimeNamespaces = {
  /** Every persistent key this deployment writes begins with this. */
  root: string;
  /** Reserved for the Custom Mix pool. Not connected in Phase 3C-1. */
  pool: string;
};

export type RuntimeDependencies = {
  mode: RuntimeMode;
  environment: SignalsEnvironment;
  verifier: AppleEntitlementVerifier;
  tokens: SignalsTokenService;
  revocations: RevocationStore;
  idempotency: PersistentIdempotencyStore;
  ipLimiter: RateLimiter;
  subjectLimiter: RateLimiter;
  editionLimiter: RateLimiter;
  logger: SecurityLogger;
  clock: Clock;
  requestId: () => string;
  namespaces: RuntimeNamespaces;
  killSwitch: { customMixEnabled: boolean };
  /**
   * The exact parameters the Apple verifier was constructed with, minus key material.
   * Exposed so a deployment can be asserted (Production carries appAppleId, Sandbox does
   * not) without reaching into the verifier.
   */
  appleIdentity: {
    environment: SignalsEnvironment;
    bundleId: string;
    appAppleId?: number;
    enableOnlineChecks: boolean;
  };
};

/** Transport-level seams. None of these replace a policy object. */
export type RuntimeTransports = {
  clock?: Clock;
  logger?: SecurityLogger;
  requestId?: () => string;
  /** Passed to the Upstash REST client. */
  fetchImpl?: FetchLike;
  /** Override the vendored certificate directory. Tests only. */
  certificateDirectory?: LoadAppleRootCertificatesOptions["directory"];
  /** Pre-built Redis client. Tests inject `FakeRedisClient` so no real Redis is touched. */
  redisClient?: RedisClient;
  /** JWS decoding seam. Every entitlement RULE still runs unchanged. */
  appleDecoder?: SignedTransactionDecoder;
  /** Transaction History transport. Retry, breaker and re-verification still run. */
  transactionHistoryFetcher?: TransactionHistoryFetcher;
  /** Backoff sleep, so tests are instant. */
  sleep?: Sleep;
};

const HOUR_SECONDS = 60 * 60;

/**
 * The exact options handed to `RealAppleEntitlementVerifier`. Pure and deterministic, so a
 * deployment's Apple identity can be asserted without constructing anything.
 */
export function buildVerifierOptions(
  config: Extract<RuntimeConfig, { mode: "production" | "sandbox" }>,
  rootCertificates: Buffer[],
  decoder?: SignedTransactionDecoder,
): RealAppleEntitlementVerifierOptions {
  return {
    environment: config.apple.environment,
    bundleId: config.apple.bundleId,
    // Present for Production, ABSENT for Sandbox — Apple's verifier rejects the reverse,
    // and this is what stops a Sandbox deployment reusing a Production app identity.
    ...(config.apple.appAppleId === undefined
      ? {}
      : { appAppleId: config.apple.appAppleId }),
    rootCertificates,
    enableOnlineChecks: config.apple.enableOnlineChecks,
    ...(decoder === undefined ? {} : { decoder }),
  };
}

/** The exact options handed to the App Store Server API client. Pure and deterministic. */
export function buildAppleClientOptions(
  config: Extract<RuntimeConfig, { mode: "production" | "sandbox" }>,
): {
  signingKeyPem: string;
  keyId: string;
  issuerId: string;
  bundleId: string;
  environment: SignalsEnvironment;
} {
  return {
    signingKeyPem: config.apple.privateKeyPem,
    keyId: config.apple.keyId,
    issuerId: config.apple.issuerId,
    bundleId: config.apple.bundleId,
    environment: config.apple.environment,
  };
}

function buildTokenService(
  config: RuntimeConfig,
  clock: Clock,
): SignalsTokenService {
  const verificationKeys = [
    { kid: config.token.signingKid, publicKeyPem: config.token.currentPublicKeyPem },
  ];
  if (config.token.previousKid && config.token.previousPublicKeyPem) {
    // Rotation: tokens signed by the retired key stay verifiable until they expire.
    verificationKeys.push({
      kid: config.token.previousKid,
      publicKeyPem: config.token.previousPublicKeyPem,
    });
  }
  try {
    return new SignalsTokenService({
      signer: {
        kid: config.token.signingKid,
        privateKeyPem: config.token.signingPrivateKeyPem,
      },
      verificationKeys,
      hmacSecret: config.token.hmacSecret,
      clock,
    });
  } catch {
    // Never re-throw the original: a crypto error can echo the material it rejected.
    throw new RuntimeCompositionError("token_key_material_unusable");
  }
}

/**
 * Build every dependency for a REAL (Production or Sandbox) deployment.
 *
 * Throws `RuntimeCompositionError` when anything required is missing. There is no partial
 * or degraded result: either the whole stack is real, or nothing is returned.
 */
export function createRuntimeDependencies(
  config: RuntimeConfig,
  transports: RuntimeTransports = {},
): RuntimeDependencies {
  if (!isRealDeployment(config)) {
    // Development must be requested explicitly; it can never be reached by falling back.
    throw new RuntimeCompositionError("development_requires_explicit_factory");
  }
  if ("fakeVerifier" in (transports as Record<string, unknown>)) {
    throw new RuntimeCompositionError("fake_verifier_forbidden");
  }

  const clock: Clock = transports.clock ?? { nowMs: () => Date.now() };
  const logger = transports.logger ?? new JsonSecurityLogger();
  const requestId = transports.requestId ?? (() => randomUUID());

  // The pepper for every derived persistent key. One process serves one deployment, so a
  // single process-wide value is correct; it is set from the validated config only.
  configureKeyDerivation(config.token.hmacSecret);

  const redis: RedisClient =
    transports.redisClient ??
    new UpstashRestClient({
      restUrl: config.storage.restUrl,
      restToken: config.storage.restToken,
      ...(transports.fetchImpl === undefined ? {} : { fetchImpl: transports.fetchImpl }),
    });

  const namespace = config.storage.namespace;
  const namespaces: RuntimeNamespaces = {
    root: namespace,
    pool: config.storage.poolNamespace,
  };

  const ipLimiter = new PersistentRateLimiter({
    client: redis,
    namespace,
    bucket: "ip_exchange",
    limit: config.limits.exchangePerIpPerMinute,
    windowSeconds: 60,
  });
  const subjectLimiter = new PersistentRateLimiter({
    client: redis,
    namespace,
    bucket: "subject_exchange",
    limit: config.limits.exchangePerSubjectPerHour,
    windowSeconds: HOUR_SECONDS,
  });
  const editionLimiter = new PersistentRateLimiter({
    client: redis,
    namespace,
    bucket: "token_edition",
    limit: config.limits.editionPerTokenPerMinute,
    windowSeconds: 60,
  });

  const revocations = new PersistentRevocationStore({ client: redis, namespace });
  const idempotency = new PersistentIdempotencyStore({ client: redis, namespace });
  const tokens = buildTokenService(config, clock);

  // Vendored roots, hash-verified. Read HERE — never at module import.
  const rootCertificates = loadAppleRootCertificates(
    transports.certificateDirectory === undefined
      ? {}
      : { directory: transports.certificateDirectory },
  );

  // One decoder, shared by the device-JWS path and the history re-verification path, so
  // both trust exactly the same roots, environment and app identity.
  const decoder: SignedTransactionDecoder =
    transports.appleDecoder ??
    new SignedDataVerifier(
      rootCertificates,
      config.apple.enableOnlineChecks,
      toAppleEnvironment(config.apple.environment),
      config.apple.bundleId,
      config.apple.appAppleId,
    );

  const signedProofVerifier = new RealAppleEntitlementVerifier(
    buildVerifierOptions(config, rootCertificates, decoder),
  );

  const fetcher: TransactionHistoryFetcher =
    transports.transactionHistoryFetcher ??
    createAppStoreClient(buildAppleClientOptions(config));

  const currentState = new AppleEntitlementClient(
    {
      fetcher,
      decoder,
      environment: config.apple.environment,
      breakerFailureThreshold: config.limits.circuitBreakerFailureThreshold,
      breakerOpenMs: config.limits.circuitBreakerOpenMs,
      now: () => clock.nowMs(),
      ...(transports.sleep === undefined ? {} : { sleep: transports.sleep }),
    },
    new AppleServiceBreaker(
      config.limits.circuitBreakerFailureThreshold,
      config.limits.circuitBreakerOpenMs,
    ),
  );

  const verifier = new LiveAppleEntitlementVerifier({
    environment: config.apple.environment,
    signedProofVerifier,
    currentState,
    revocations,
    deriveSubject: (originalTransactionId, environment) =>
      tokens.deriveSubject(originalTransactionId, environment),
    onRevoked: (subject) => revocations.markRevoked(subject, clock.nowMs()),
  });

  return {
    mode: config.mode,
    environment: config.apple.environment,
    verifier,
    tokens,
    revocations,
    idempotency,
    ipLimiter,
    subjectLimiter,
    editionLimiter,
    logger,
    clock,
    requestId,
    namespaces,
    killSwitch: { customMixEnabled: config.customMix.enabled },
    appleIdentity: {
      environment: config.apple.environment,
      bundleId: config.apple.bundleId,
      ...(config.apple.appAppleId === undefined
        ? {}
        : { appAppleId: config.apple.appAppleId }),
      enableOnlineChecks: config.apple.enableOnlineChecks,
    },
  };
}

export type DevelopmentOverrides = Omit<
  RuntimeTransports,
  "appleDecoder" | "transactionHistoryFetcher" | "certificateDirectory"
> & {
  /** REQUIRED. Development never invents a verifier for you either. */
  fakeVerifier: AppleEntitlementVerifier;
  environment?: SignalsEnvironment;
};

/**
 * Build a dependency set for local development. Refuses any other mode, so mock behaviour
 * is opt-in and structurally impossible under Production or Sandbox.
 */
export function createDevelopmentDependencies(
  config: RuntimeConfig,
  overrides: DevelopmentOverrides,
): RuntimeDependencies {
  if (config.mode !== "development") {
    throw new RuntimeCompositionError("development_factory_requires_development_mode");
  }
  if (!overrides?.fakeVerifier) {
    throw new RuntimeCompositionError("development_requires_explicit_verifier");
  }

  const clock: Clock = overrides.clock ?? { nowMs: () => Date.now() };
  const logger = overrides.logger ?? new JsonSecurityLogger();
  const requestId = overrides.requestId ?? (() => randomUUID());
  const environment: SignalsEnvironment = overrides.environment ?? "Sandbox";

  configureKeyDerivation(config.token.hmacSecret);

  const redis: RedisClient = overrides.redisClient ?? new FakeRedisClient();
  const namespace = config.storage?.namespace ?? "signals:development:none";
  const namespaces: RuntimeNamespaces = {
    root: namespace,
    pool: config.storage?.poolNamespace ?? `${namespace}:pool`,
  };

  const tokens = buildTokenService(config, clock);
  const revocations = new PersistentRevocationStore({ client: redis, namespace });

  return {
    mode: config.mode,
    environment,
    verifier: overrides.fakeVerifier,
    tokens,
    revocations,
    idempotency: new PersistentIdempotencyStore({ client: redis, namespace }),
    ipLimiter: new PersistentRateLimiter({
      client: redis,
      namespace,
      bucket: "ip_exchange",
      limit: config.limits.exchangePerIpPerMinute,
      windowSeconds: 60,
    }),
    subjectLimiter: new PersistentRateLimiter({
      client: redis,
      namespace,
      bucket: "subject_exchange",
      limit: config.limits.exchangePerSubjectPerHour,
      windowSeconds: HOUR_SECONDS,
    }),
    editionLimiter: new PersistentRateLimiter({
      client: redis,
      namespace,
      bucket: "token_edition",
      limit: config.limits.editionPerTokenPerMinute,
      windowSeconds: 60,
    }),
    logger,
    clock,
    requestId,
    namespaces,
    killSwitch: { customMixEnabled: config.customMix.enabled },
    appleIdentity: {
      environment,
      bundleId: "development",
      enableOnlineChecks: false,
    },
  };
}
