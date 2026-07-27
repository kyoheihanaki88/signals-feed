export type RateLimitDecision =
  | { allowed: true }
  | { allowed: false; retryAfterSeconds: number };

export interface RateLimiter {
  consume(key: string, nowMs: number): Promise<RateLimitDecision>;
}

export class RateLimiterUnavailableError extends Error {
  constructor() {
    super("rate limiter unavailable");
    this.name = "RateLimiterUnavailableError";
  }
}

export class InMemorySlidingWindowRateLimiter implements RateLimiter {
  private readonly events = new Map<string, number[]>();

  constructor(
    private readonly limit: number,
    private readonly windowMs: number,
  ) {
    if (limit < 1 || windowMs < 1) {
      throw new Error("invalid rate-limit configuration");
    }
  }

  async consume(key: string, nowMs: number): Promise<RateLimitDecision> {
    const cutoff = nowMs - this.windowMs;
    const recent = (this.events.get(key) ?? []).filter(
      (timestamp) => timestamp > cutoff,
    );

    if (recent.length >= this.limit) {
      const retryAfterMs = Math.max(1, recent[0] + this.windowMs - nowMs);
      this.events.set(key, recent);
      return {
        allowed: false,
        retryAfterSeconds: Math.max(1, Math.ceil(retryAfterMs / 1_000)),
      };
    }

    recent.push(nowMs);
    this.events.set(key, recent);
    return { allowed: true };
  }
}
