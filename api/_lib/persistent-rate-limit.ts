/**
 * Redis-backed fixed-window rate limiter. (Phase 3B-3)
 *
 * Replaces the in-memory limiter for Production/Sandbox, where process memory is useless
 * (serverless instances are ephemeral and horizontally scaled) and unsafe (a cold start
 * would silently reset every limit).
 *
 * Atomicity: INCR + EXPIRE are issued as ONE pipeline. INCR creates the counter on first
 * use and the EXPIRE bounds it, so a window can never grow unbounded and a race between
 * two instances still yields a single shared count.
 *
 * FAIL CLOSED: any Redis problem raises `RateLimiterUnavailableError`. Callers must map
 * that to a denial, never to an allow.
 */

import type { RateLimitDecision, RateLimiter } from "./rate-limit.js";
import { RateLimiterUnavailableError } from "./rate-limit.js";
import { RedisUnavailableError, type RedisClient } from "./redis-client.js";
import { deriveKeyComponent } from "./subject-hash.js";

export type PersistentRateLimiterOptions = {
  client: RedisClient;
  /** Deployment namespace from the env contract, e.g. "signals:production:production". */
  namespace: string;
  /** Bucket name, e.g. "ip_exchange" — becomes part of the key. */
  bucket: string;
  limit: number;
  windowSeconds: number;
};

export class PersistentRateLimiter implements RateLimiter {
  constructor(private readonly options: PersistentRateLimiterOptions) {
    if (options.limit < 1 || options.windowSeconds < 1) {
      throw new Error("invalid rate-limit configuration");
    }
    if (!options.namespace || !options.bucket) {
      throw new Error("rate limiter requires namespace and bucket");
    }
  }

  /**
   * Key shape: <namespace>:rl:<bucket>:<window index>:<derived key>
   *
   * The caller-supplied key is one-way derived, so a client IP or a subject identifier
   * never appears verbatim in Redis. The window index makes the key self-rotating.
   */
  private keyFor(rawKey: string, nowMs: number): string {
    const window = Math.floor(nowMs / (this.options.windowSeconds * 1_000));
    const derived = deriveKeyComponent(rawKey);
    return `${this.options.namespace}:rl:${this.options.bucket}:${window}:${derived}`;
  }

  async consume(key: string, nowMs: number): Promise<RateLimitDecision> {
    const redisKey = this.keyFor(key, nowMs);
    let count: number;
    let ttlMs: number;

    try {
      const [incremented, , pttl] = await this.options.client.pipeline<unknown>([
        ["INCR", redisKey],
        ["EXPIRE", redisKey, this.options.windowSeconds],
        ["PTTL", redisKey],
      ]);
      count = Number(incremented);
      ttlMs = Number(pttl);
      if (!Number.isFinite(count) || count < 1) {
        throw new RedisUnavailableError("unexpected counter value");
      }
    } catch (error) {
      // Fail closed: an unavailable limiter must not become an open door.
      if (error instanceof RedisUnavailableError) throw new RateLimiterUnavailableError();
      throw new RateLimiterUnavailableError();
    }

    if (count > this.options.limit) {
      const remainingMs =
        Number.isFinite(ttlMs) && ttlMs > 0 ? ttlMs : this.options.windowSeconds * 1_000;
      return {
        allowed: false,
        retryAfterSeconds: Math.max(1, Math.ceil(remainingMs / 1_000)),
      };
    }
    return { allowed: true };
  }
}
