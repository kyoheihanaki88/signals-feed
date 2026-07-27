/**
 * Redis-backed entitlement revocation state. (Phase 3B-3)
 *
 * Durable answer to "is this subject's Pro entitlement still good?" so a refund observed
 * once (via Apple verification or, later, App Store Server Notifications) keeps applying
 * to every subsequent request and every serverless instance.
 *
 * Privacy: the subject is one-way derived before it becomes part of a key, so no raw
 * Apple transaction identifier is ever written to Redis — as a key or as a value.
 *
 * FAIL CLOSED: if the state cannot be read, `isRevoked` throws. Callers must deny.
 * "We could not check" must never be treated as "not revoked".
 */

import type { RevocationStore } from "./revocation-store.js";
import { RedisUnavailableError, type RedisClient } from "./redis-client.js";
import { deriveKeyComponent } from "./subject-hash.js";

export class RevocationStoreUnavailableError extends Error {
  constructor() {
    super("revocation state unavailable");
    this.name = "RevocationStoreUnavailableError";
  }
}

export type EntitlementStatus = "active" | "revoked";

export type EntitlementRecord = {
  status: EntitlementStatus;
  /** Epoch ms when this state was recorded. */
  updatedAtMs: number;
  /** Epoch ms Apple reported the revocation, when known. */
  revokedAtMs?: number;
};

export type PersistentRevocationStoreOptions = {
  client: RedisClient;
  namespace: string;
  /** Retention for a record; refreshed on every write. Default 400 days. */
  ttlSeconds?: number;
};

const DEFAULT_TTL_SECONDS = 400 * 24 * 60 * 60;

export class PersistentRevocationStore implements RevocationStore {
  private readonly ttlSeconds: number;

  constructor(private readonly options: PersistentRevocationStoreOptions) {
    if (!options.namespace) throw new Error("revocation store requires a namespace");
    this.ttlSeconds = options.ttlSeconds ?? DEFAULT_TTL_SECONDS;
    if (this.ttlSeconds < 1) throw new Error("invalid revocation ttl");
  }

  private keyFor(subject: string): string {
    return `${this.options.namespace}:ent:${deriveKeyComponent(subject)}`;
  }

  /** True when the subject is known-revoked. Throws when the state cannot be read. */
  async isRevoked(subject: string): Promise<boolean> {
    const record = await this.read(subject);
    return record?.status === "revoked";
  }

  async read(subject: string): Promise<EntitlementRecord | null> {
    let raw: string | null;
    try {
      raw = await this.options.client.command<string | null>(["GET", this.keyFor(subject)]);
    } catch (error) {
      if (error instanceof RedisUnavailableError) throw new RevocationStoreUnavailableError();
      throw new RevocationStoreUnavailableError();
    }
    if (raw === null || raw === undefined) return null;
    try {
      const parsed = JSON.parse(raw) as EntitlementRecord;
      if (parsed.status !== "active" && parsed.status !== "revoked") {
        // A corrupt record is not evidence of a good entitlement.
        throw new RevocationStoreUnavailableError();
      }
      return parsed;
    } catch (error) {
      if (error instanceof RevocationStoreUnavailableError) throw error;
      throw new RevocationStoreUnavailableError();
    }
  }

  async setStatus(
    subject: string,
    status: EntitlementStatus,
    nowMs: number,
    revokedAtMs?: number,
  ): Promise<void> {
    const record: EntitlementRecord = { status, updatedAtMs: nowMs };
    if (status === "revoked" && revokedAtMs !== undefined) record.revokedAtMs = revokedAtMs;
    try {
      await this.options.client.command([
        "SET",
        this.keyFor(subject),
        JSON.stringify(record),
        "EX",
        this.ttlSeconds,
      ]);
    } catch (error) {
      if (error instanceof RedisUnavailableError) throw new RevocationStoreUnavailableError();
      throw new RevocationStoreUnavailableError();
    }
  }

  async markRevoked(subject: string, nowMs: number, revokedAtMs?: number): Promise<void> {
    await this.setStatus(subject, "revoked", nowMs, revokedAtMs);
  }

  async markActive(subject: string, nowMs: number): Promise<void> {
    await this.setStatus(subject, "active", nowMs);
  }
}
