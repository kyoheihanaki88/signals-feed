/**
 * Redis-backed idempotency claims. (Phase 3B-3)
 *
 * A client that retries a request (flaky network, cold start, user double-tap) must not
 * produce a second side effect. The caller claims a key first: the FIRST claim wins and
 * proceeds; later claims observe the stored outcome instead of redoing the work.
 *
 * Atomicity: `SET key NX EX ttl` — a single round trip that both tests and sets. Two
 * concurrent instances cannot both win.
 *
 * Stored payload is bounded, non-sensitive result METADATA only (status + a short
 * fingerprint of the request). Never a token, a JWS, an Apple identifier, or a response body.
 *
 * FAIL CLOSED: any Redis problem raises; the caller must not perform the side effect.
 */

import { RedisUnavailableError, type RedisClient } from "./redis-client.js";
import { deriveKeyComponent } from "./subject-hash.js";

export class IdempotencyStoreUnavailableError extends Error {
  constructor() {
    super("idempotency store unavailable");
    this.name = "IdempotencyStoreUnavailableError";
  }
}

/** The stored key was reused with a materially different request. */
export class IdempotencyConflictError extends Error {
  constructor() {
    super("idempotency key reused with a different request");
    this.name = "IdempotencyConflictError";
  }
}

export type IdempotencyRecord = {
  /** "in_progress" until completed, then the terminal outcome. */
  state: "in_progress" | "completed";
  /** Derived fingerprint of the request, to detect key reuse with different input. */
  requestFingerprint: string;
  createdAtMs: number;
  completedAtMs?: number;
  /** Bounded, non-sensitive outcome metadata (e.g. { status: 200 }). */
  result?: Record<string, string | number | boolean>;
};

export type ClaimOutcome =
  | { claimed: true }
  | { claimed: false; record: IdempotencyRecord };

export type PersistentIdempotencyStoreOptions = {
  client: RedisClient;
  namespace: string;
  /** Explicit expiry — required by contract, never unbounded. Default 24h. */
  ttlSeconds?: number;
  /** Cap on serialized result metadata, guarding against unbounded growth. */
  maxResultBytes?: number;
};

const DEFAULT_TTL_SECONDS = 24 * 60 * 60;
const DEFAULT_MAX_RESULT_BYTES = 512;

export class PersistentIdempotencyStore {
  private readonly ttlSeconds: number;
  private readonly maxResultBytes: number;

  constructor(private readonly options: PersistentIdempotencyStoreOptions) {
    if (!options.namespace) throw new Error("idempotency store requires a namespace");
    this.ttlSeconds = options.ttlSeconds ?? DEFAULT_TTL_SECONDS;
    this.maxResultBytes = options.maxResultBytes ?? DEFAULT_MAX_RESULT_BYTES;
    if (this.ttlSeconds < 1) throw new Error("idempotency ttl must be explicit and >= 1s");
  }

  private keyFor(idempotencyKey: string): string {
    return `${this.options.namespace}:idem:${deriveKeyComponent(idempotencyKey)}`;
  }

  /**
   * Attempt to claim the key. `claimed: true` means the caller owns the work.
   * `claimed: false` returns the existing record so the caller can replay its outcome.
   * Throws `IdempotencyConflictError` when the same key is reused for a different request.
   */
  async claim(
    idempotencyKey: string,
    requestPayload: string,
    nowMs: number,
  ): Promise<ClaimOutcome> {
    const key = this.keyFor(idempotencyKey);
    const fingerprint = deriveKeyComponent(requestPayload);
    const record: IdempotencyRecord = {
      state: "in_progress",
      requestFingerprint: fingerprint,
      createdAtMs: nowMs,
    };

    let setResult: string | null;
    try {
      setResult = await this.options.client.command<string | null>([
        "SET",
        key,
        JSON.stringify(record),
        "NX",
        "EX",
        this.ttlSeconds,
      ]);
    } catch (error) {
      if (error instanceof RedisUnavailableError) throw new IdempotencyStoreUnavailableError();
      throw new IdempotencyStoreUnavailableError();
    }

    if (setResult === "OK") return { claimed: true };

    const existing = await this.read(idempotencyKey);
    if (!existing) {
      // Raced with an expiry between SET NX and GET — safest is to deny this attempt.
      throw new IdempotencyStoreUnavailableError();
    }
    if (existing.requestFingerprint !== fingerprint) throw new IdempotencyConflictError();
    return { claimed: false, record: existing };
  }

  async read(idempotencyKey: string): Promise<IdempotencyRecord | null> {
    let raw: string | null;
    try {
      raw = await this.options.client.command<string | null>(["GET", this.keyFor(idempotencyKey)]);
    } catch (error) {
      if (error instanceof RedisUnavailableError) throw new IdempotencyStoreUnavailableError();
      throw new IdempotencyStoreUnavailableError();
    }
    if (raw === null || raw === undefined) return null;
    try {
      return JSON.parse(raw) as IdempotencyRecord;
    } catch {
      throw new IdempotencyStoreUnavailableError();
    }
  }

  /** Record the terminal outcome, preserving the original TTL window. */
  async complete(
    idempotencyKey: string,
    result: Record<string, string | number | boolean>,
    nowMs: number,
  ): Promise<void> {
    const existing = await this.read(idempotencyKey);
    if (!existing) throw new IdempotencyStoreUnavailableError();

    const serializedResult = JSON.stringify(result);
    if (Buffer.byteLength(serializedResult, "utf8") > this.maxResultBytes) {
      throw new Error("idempotency result metadata exceeds the permitted size");
    }

    const updated: IdempotencyRecord = {
      ...existing,
      state: "completed",
      completedAtMs: nowMs,
      result,
    };
    try {
      await this.options.client.command([
        "SET",
        this.keyFor(idempotencyKey),
        JSON.stringify(updated),
        "EX",
        this.ttlSeconds,
      ]);
    } catch (error) {
      if (error instanceof RedisUnavailableError) throw new IdempotencyStoreUnavailableError();
      throw new IdempotencyStoreUnavailableError();
    }
  }
}
