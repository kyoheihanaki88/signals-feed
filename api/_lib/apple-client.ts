/**
 * App Store Server API adapter — current-entitlement check. (Phase 3B-3)
 *
 * Official APIs used from @apple/app-store-server-library@3.1.0:
 *   • AppStoreServerAPIClient(signingKey, keyId, issuerId, bundleId, environment)
 *   • getTransactionHistory(anyTransactionId, revision, TransactionHistoryRequest,
 *                           GetTransactionHistoryVersion.V2) → Promise<HistoryResponse>
 *   • HistoryResponse { revision?, hasMore?, signedTransactions?[] }
 *   • APIException { httpStatusCode, apiError, errorMessage }
 *   • Environment, ProductType, Order enums
 *
 * After the device's own JWS is verified, this asks Apple what it currently believes:
 * a refund issued after purchase only shows up here. EVERY transaction Apple returns is
 * re-verified through `SignedDataVerifier` before a single field is read — the history
 * response is treated as untrusted transport.
 *
 * No process.env is read here and no credential is touched at import time: everything is
 * injected by the caller from the validated environment contract.
 */

import {
  APIException,
  AppStoreServerAPIClient,
  Environment,
  GetTransactionHistoryVersion,
  Order,
  ProductType,
  type HistoryResponse,
  type JWSTransactionDecodedPayload,
} from "@apple/app-store-server-library";

import {
  AppleEntitlementError,
  assertSignalsEntitlement,
  mapVerificationException,
  SIGNALS_PRO_PRODUCT_ID,
  toAppleEnvironment,
  type AppleEntitlementReason,
  type SignedTransactionDecoder,
} from "./apple-verifier-real.js";
import type { SignalsEnvironment, VerifiedAppleEntitlement } from "./apple-verifier.js";

/** The transport seam: the real client, or a stub in tests. */
export interface TransactionHistoryFetcher {
  getTransactionHistory(
    anyTransactionId: string,
    revision: string | null,
    request: {
      productIds?: string[];
      productTypes?: ProductType[];
      sort?: Order;
      revoked?: boolean;
    },
    version?: GetTransactionHistoryVersion,
  ): Promise<HistoryResponse>;
}

export type Sleep = (ms: number) => Promise<void>;
export type Now = () => number;

export type AppleClientOptions = {
  fetcher: TransactionHistoryFetcher;
  decoder: SignedTransactionDecoder;
  environment: SignalsEnvironment;
  /** Total wall-clock budget across all attempts and pages. */
  totalBudgetMs?: number;
  maxRetries?: number;
  /** Base backoff; multiplied per attempt. Injectable so tests are instant. */
  retryBaseMs?: number;
  maxPages?: number;
  /** Consecutive Apple failures before the breaker opens. */
  breakerFailureThreshold?: number;
  breakerOpenMs?: number;
  sleep?: Sleep;
  now?: Now;
};

const DEFAULTS = {
  totalBudgetMs: 8_000,
  maxRetries: 2,
  retryBaseMs: 200,
  maxPages: 10,
  breakerFailureThreshold: 5,
  breakerOpenMs: 30_000,
};

const RETRYABLE_STATUS = new Set([429, 500, 502, 503, 504]);

/** Shared breaker state — deliberately module-level so all instances agree. */
type BreakerState = { consecutiveFailures: number; openedAtMs: number | null };

export class AppleServiceBreaker {
  private state: BreakerState = { consecutiveFailures: 0, openedAtMs: null };

  constructor(
    private readonly threshold: number,
    private readonly openMs: number,
  ) {}

  isOpen(nowMs: number): boolean {
    if (this.state.openedAtMs === null) return false;
    if (nowMs - this.state.openedAtMs >= this.openMs) {
      // Half-open: let the next call through; success closes it, failure re-opens.
      this.state.openedAtMs = null;
      this.state.consecutiveFailures = 0;
      return false;
    }
    return true;
  }

  recordSuccess(): void {
    this.state = { consecutiveFailures: 0, openedAtMs: null };
  }

  recordFailure(nowMs: number): void {
    this.state.consecutiveFailures += 1;
    if (this.state.consecutiveFailures >= this.threshold) {
      this.state.openedAtMs = nowMs;
    }
  }

  /** Test helper. */
  reset(): void {
    this.state = { consecutiveFailures: 0, openedAtMs: null };
  }
}

/**
 * Build the official client. Credentials are passed in — never read from the
 * environment here.
 */
export function createAppStoreClient(options: {
  signingKeyPem: string;
  keyId: string;
  issuerId: string;
  bundleId: string;
  environment: SignalsEnvironment;
}): AppStoreServerAPIClient {
  return new AppStoreServerAPIClient(
    options.signingKeyPem,
    options.keyId,
    options.issuerId,
    options.bundleId,
    toAppleEnvironment(options.environment) as Environment,
  );
}

function isRetryable(error: unknown): boolean {
  if (error instanceof APIException) return RETRYABLE_STATUS.has(error.httpStatusCode);
  // A transport/network error carries no HTTP status — retry it.
  return !(error instanceof AppleEntitlementError);
}

/** Retry-After, in ms, when Apple supplied one. */
function retryAfterMs(error: unknown): number | null {
  const header = (error as { retryAfter?: unknown; headers?: { get?(name: string): string | null } })
    ?.headers?.get?.("retry-after");
  const value = header ?? (error as { retryAfter?: string | number }).retryAfter;
  if (value === undefined || value === null) return null;
  const seconds = typeof value === "number" ? value : Number.parseInt(String(value), 10);
  return Number.isFinite(seconds) && seconds >= 0 ? seconds * 1_000 : null;
}

export class AppleEntitlementClient {
  private readonly o: Required<Omit<AppleClientOptions, "fetcher" | "decoder" | "environment">> &
    Pick<AppleClientOptions, "fetcher" | "decoder" | "environment">;
  private readonly breaker: AppleServiceBreaker;

  constructor(options: AppleClientOptions, breaker?: AppleServiceBreaker) {
    this.o = {
      ...DEFAULTS,
      sleep: options.sleep ?? ((ms) => new Promise((r) => setTimeout(r, ms))),
      now: options.now ?? (() => Date.now()),
      ...options,
    } as typeof this.o;
    this.breaker =
      breaker ??
      new AppleServiceBreaker(this.o.breakerFailureThreshold, this.o.breakerOpenMs);
  }

  /**
   * Is the Lifetime Pro non-consumable currently valid for this originalTransactionId?
   * Resolves with the verified entitlement, or throws `AppleEntitlementError`.
   */
  async getCurrentProEntitlement(
    originalTransactionId: string,
  ): Promise<VerifiedAppleEntitlement> {
    const startedAt = this.o.now();
    if (this.breaker.isOpen(startedAt)) {
      throw new AppleEntitlementError("verification_unavailable");
    }

    let revision: string | null = null;
    let pages = 0;
    let sawMatchingProduct = false;

    try {
      while (pages < this.o.maxPages) {
        const response = await this.fetchPageWithRetry(originalTransactionId, revision, startedAt);
        pages += 1;

        const signed = response.signedTransactions ?? [];
        for (const signedTransaction of signed) {
          let payload: JWSTransactionDecodedPayload;
          try {
            // Apple's own response is NOT trusted until its signature verifies.
            payload = await this.o.decoder!.verifyAndDecodeTransaction(signedTransaction);
          } catch (error) {
            throw new AppleEntitlementError(mapVerificationException(error));
          }
          if (payload.productId !== SIGNALS_PRO_PRODUCT_ID) continue;
          sawMatchingProduct = true;

          // Reuses the exact same entitlement rules as the device-JWS path, so a
          // refunded or family-shared transaction is rejected identically here.
          const entitlement = assertSignalsEntitlement(payload, this.o.environment!);
          this.breaker.recordSuccess();
          return entitlement;
        }

        if (!response.hasMore || !response.revision) break;
        revision = response.revision;
      }
    } catch (error) {
      if (error instanceof AppleEntitlementError) {
        // A definitive answer (revoked / wrong product) is NOT a service failure.
        if (error.reason === "verification_unavailable") this.breaker.recordFailure(this.o.now());
        throw error;
      }
      this.breaker.recordFailure(this.o.now());
      throw new AppleEntitlementError("verification_unavailable");
    }

    this.breaker.recordSuccess();
    // Apple answered, but this account holds no valid Pro purchase.
    throw new AppleEntitlementError(sawMatchingProduct ? "revoked" : "wrong_product");
  }

  private async fetchPageWithRetry(
    originalTransactionId: string,
    revision: string | null,
    startedAt: number,
  ): Promise<HistoryResponse> {
    let attempt = 0;
    let lastReason: AppleEntitlementReason = "verification_unavailable";

    while (attempt <= this.o.maxRetries) {
      if (this.o.now() - startedAt > this.o.totalBudgetMs) {
        throw new AppleEntitlementError("verification_unavailable");
      }
      try {
        const response = await this.o.fetcher!.getTransactionHistory(
          originalTransactionId,
          revision,
          {
            productIds: [SIGNALS_PRO_PRODUCT_ID],
            productTypes: [ProductType.NON_CONSUMABLE],
            sort: Order.DESCENDING,
          },
          GetTransactionHistoryVersion.V2,
        );
        if (typeof response !== "object" || response === null) {
          throw new AppleEntitlementError("verification_unavailable");
        }
        return response;
      } catch (error) {
        if (error instanceof AppleEntitlementError) throw error;
        if (!isRetryable(error)) {
          // Deterministic client/validation failure — retrying cannot change it.
          lastReason = "verification_unavailable";
          break;
        }
        attempt += 1;
        if (attempt > this.o.maxRetries) break;

        const waitMs = retryAfterMs(error) ?? this.o.retryBaseMs * attempt;
        const elapsed = this.o.now() - startedAt;
        if (elapsed + waitMs > this.o.totalBudgetMs) break; // would blow the budget
        await this.o.sleep!(waitMs);
      }
    }
    throw new AppleEntitlementError(lastReason);
  }
}
