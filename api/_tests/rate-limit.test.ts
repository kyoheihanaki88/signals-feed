import assert from "node:assert/strict";
import test from "node:test";
import {
  InMemorySlidingWindowRateLimiter,
  type RateLimiter,
} from "../_lib/rate-limit.js";
import { authenticateEdition } from "../_lib/auth-middleware.js";
import {
  createDependencies,
  createTokenFixture,
} from "./test-helpers.js";

test("burst/sustained window and retry-after are deterministic", async () => {
  const limiter = new InMemorySlidingWindowRateLimiter(2, 1_000);
  assert.deepEqual(await limiter.consume("a", 0), { allowed: true });
  assert.deepEqual(await limiter.consume("a", 1), { allowed: true });
  assert.deepEqual(await limiter.consume("a", 2), {
    allowed: false,
    retryAfterSeconds: 1,
  });
  assert.deepEqual(await limiter.consume("a", 1_001), { allowed: true });
});

test("different limiter keys are isolated", async () => {
  const limiter = new InMemorySlidingWindowRateLimiter(1, 60_000);
  assert.deepEqual(await limiter.consume("a", 0), { allowed: true });
  assert.deepEqual(await limiter.consume("b", 0), { allowed: true });
  assert.equal((await limiter.consume("a", 1)).allowed, false);
});

test("limiter unavailable fails closed in edition auth", async () => {
  const fixture = createTokenFixture();
  const dependencies = createDependencies();
  const token = fixture.tokens.issue({
    subject: "subject",
    environment: "Production",
  }).accessToken;
  const unavailable: RateLimiter = {
    async consume() {
      throw new Error("store down");
    },
  };
  await assert.rejects(
    () =>
      authenticateEdition(
        {
          authorization: `Bearer ${token}`,
          expectedEnvironment: "Production",
          nowMs: fixture.clock.nowMs(),
        },
        {
          tokens: fixture.tokens,
          revocations: dependencies.revocations,
          limiter: unavailable,
        },
      ),
    (error: unknown) =>
      error instanceof Error && error.message === "verification_unavailable",
  );
});
