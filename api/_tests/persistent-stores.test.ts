import assert from "node:assert/strict";
import test from "node:test";
import { FakeRedisClient, RedisUnavailableError, UpstashRestClient } from "../_lib/redis-client.js";
import { PersistentRateLimiter } from "../_lib/persistent-rate-limit.js";
import {
  PersistentRevocationStore,
  RevocationStoreUnavailableError,
} from "../_lib/persistent-revocation-store.js";
import {
  IdempotencyConflictError,
  IdempotencyStoreUnavailableError,
  PersistentIdempotencyStore,
} from "../_lib/persistent-idempotency-store.js";
import { RateLimiterUnavailableError } from "../_lib/rate-limit.js";
import { configureKeyDerivation, deriveKeyComponent } from "../_lib/subject-hash.js";

configureKeyDerivation("test-pepper-value-that-is-long-enough-32+");

const NAMESPACE = "signals:production:production";
// Stand-in for a real Apple identifier; must never appear in keys or values.
const RAW_SUBJECT = "2000000999888777";

// ── rate limiter ─────────────────────────────────────────────────────────────────────

test("rate limiter allows up to the limit then denies with Retry-After", async () => {
  const client = new FakeRedisClient();
  const limiter = new PersistentRateLimiter({
    client, namespace: NAMESPACE, bucket: "ip_exchange", limit: 3, windowSeconds: 60,
  });
  client.nowMs = 0;

  for (let i = 0; i < 3; i += 1) {
    assert.deepEqual(await limiter.consume("1.2.3.4", 0), { allowed: true });
  }
  const denied = await limiter.consume("1.2.3.4", 0);
  assert.equal(denied.allowed, false);
  assert.ok(!denied.allowed && denied.retryAfterSeconds >= 1);
  assert.ok(!denied.allowed && denied.retryAfterSeconds <= 60);
});

test("rate limiter counts distinct keys independently", async () => {
  const client = new FakeRedisClient();
  const limiter = new PersistentRateLimiter({
    client, namespace: NAMESPACE, bucket: "ip_exchange", limit: 1, windowSeconds: 60,
  });
  assert.deepEqual(await limiter.consume("a", 0), { allowed: true });
  assert.deepEqual(await limiter.consume("b", 0), { allowed: true });
  assert.equal((await limiter.consume("a", 0)).allowed, false);
});

test("rate limiter window rolls over and sets an expiry", async () => {
  const client = new FakeRedisClient();
  const limiter = new PersistentRateLimiter({
    client, namespace: NAMESPACE, bucket: "ip_exchange", limit: 1, windowSeconds: 60,
  });
  assert.deepEqual(await limiter.consume("ip", 0), { allowed: true });
  assert.equal((await limiter.consume("ip", 30_000)).allowed, false, "same window");
  assert.deepEqual(await limiter.consume("ip", 60_000), { allowed: true }, "next window");
  // Bounded retention: every counter carries a TTL.
  for (const key of client.keys()) {
    const ttl = await client.command<number>(["PTTL", key]);
    assert.ok(ttl > 0, `key without expiry: ${key}`);
  }
});

test("rate limiter namespaces keys and never stores the raw identifier", async () => {
  const client = new FakeRedisClient();
  const limiter = new PersistentRateLimiter({
    client, namespace: NAMESPACE, bucket: "subject_exchange", limit: 5, windowSeconds: 60,
  });
  await limiter.consume(RAW_SUBJECT, 0);
  const keys = client.keys();
  assert.equal(keys.length, 1);
  assert.ok(keys[0].startsWith(`${NAMESPACE}:rl:subject_exchange:`));
  assert.ok(!keys[0].includes(RAW_SUBJECT), "raw identifier leaked into the key");
  assert.ok(keys[0].includes(deriveKeyComponent(RAW_SUBJECT)));
});

test("Production and Sandbox namespaces cannot collide", async () => {
  const client = new FakeRedisClient();
  const production = new PersistentRateLimiter({
    client, namespace: "signals:production:production", bucket: "b", limit: 1, windowSeconds: 60,
  });
  const sandbox = new PersistentRateLimiter({
    client, namespace: "signals:sandbox:sandbox", bucket: "b", limit: 1, windowSeconds: 60,
  });
  assert.deepEqual(await production.consume("same", 0), { allowed: true });
  assert.deepEqual(await sandbox.consume("same", 0), { allowed: true }, "separate counters");
});

test("rate limiter FAILS CLOSED when Redis is unavailable", async () => {
  const client = new FakeRedisClient();
  const limiter = new PersistentRateLimiter({
    client, namespace: NAMESPACE, bucket: "ip_exchange", limit: 10, windowSeconds: 60,
  });
  client.failNextOperations = 1;
  await assert.rejects(() => limiter.consume("ip", 0), RateLimiterUnavailableError);
});

// ── revocation store ─────────────────────────────────────────────────────────────────

test("revocation store reports active/revoked and preserves timestamps", async () => {
  const client = new FakeRedisClient();
  const store = new PersistentRevocationStore({ client, namespace: NAMESPACE });

  assert.equal(await store.isRevoked(RAW_SUBJECT), false, "unknown subject is not revoked");
  await store.markActive(RAW_SUBJECT, 1_000);
  assert.equal(await store.isRevoked(RAW_SUBJECT), false);

  await store.markRevoked(RAW_SUBJECT, 2_000, 1_900);
  assert.equal(await store.isRevoked(RAW_SUBJECT), true);
  const record = await store.read(RAW_SUBJECT);
  assert.equal(record?.status, "revoked");
  assert.equal(record?.updatedAtMs, 2_000);
  assert.equal(record?.revokedAtMs, 1_900);
});

test("revocation state survives and is reinstatable", async () => {
  const client = new FakeRedisClient();
  const store = new PersistentRevocationStore({ client, namespace: NAMESPACE });
  await store.markRevoked(RAW_SUBJECT, 1_000);
  assert.equal(await store.isRevoked(RAW_SUBJECT), true);
  await store.markActive(RAW_SUBJECT, 2_000);
  assert.equal(await store.isRevoked(RAW_SUBJECT), false);
});

test("revocation keys and values never contain the raw identifier", async () => {
  const client = new FakeRedisClient();
  const store = new PersistentRevocationStore({ client, namespace: NAMESPACE });
  await store.markRevoked(RAW_SUBJECT, 1_000, 900);
  const keys = client.keys();
  assert.equal(keys.length, 1);
  assert.ok(keys[0].startsWith(`${NAMESPACE}:ent:`));
  assert.ok(!keys[0].includes(RAW_SUBJECT));
  assert.ok(!(client.peek(keys[0]) ?? "").includes(RAW_SUBJECT));
});

test("revocation store FAILS CLOSED when the state cannot be read", async () => {
  const client = new FakeRedisClient();
  const store = new PersistentRevocationStore({ client, namespace: NAMESPACE });
  client.failNextOperations = 1;
  await assert.rejects(() => store.isRevoked(RAW_SUBJECT), RevocationStoreUnavailableError);
});

test("a corrupt revocation record fails closed rather than reading as active", async () => {
  const client = new FakeRedisClient();
  const store = new PersistentRevocationStore({ client, namespace: NAMESPACE });
  await store.markActive(RAW_SUBJECT, 1_000);
  const key = client.keys()[0];
  await client.command(["SET", key, "{not json"]);
  await assert.rejects(() => store.isRevoked(RAW_SUBJECT), RevocationStoreUnavailableError);
});

test("revocation records carry an explicit bounded TTL", async () => {
  const client = new FakeRedisClient();
  const store = new PersistentRevocationStore({ client, namespace: NAMESPACE, ttlSeconds: 60 });
  await store.markRevoked(RAW_SUBJECT, 0);
  const key = client.keys()[0];
  assert.ok((await client.command<number>(["PTTL", key])) > 0);
  client.nowMs = 61_000;
  assert.equal(await store.read(RAW_SUBJECT), null, "record expired");
});

// ── idempotency store ────────────────────────────────────────────────────────────────

test("first claim wins and a second claim observes the record", async () => {
  const client = new FakeRedisClient();
  const store = new PersistentIdempotencyStore({ client, namespace: NAMESPACE });

  const first = await store.claim("req-1", "payload", 1_000);
  assert.equal(first.claimed, true);

  const second = await store.claim("req-1", "payload", 1_100);
  assert.equal(second.claimed, false);
  assert.ok(!second.claimed && second.record.state === "in_progress");
});

test("completing a claim stores bounded metadata and replays it", async () => {
  const client = new FakeRedisClient();
  const store = new PersistentIdempotencyStore({ client, namespace: NAMESPACE });
  await store.claim("req-2", "payload", 1_000);
  await store.complete("req-2", { status: 200 }, 1_500);

  const replay = await store.claim("req-2", "payload", 2_000);
  assert.equal(replay.claimed, false);
  assert.ok(!replay.claimed && replay.record.state === "completed");
  assert.ok(!replay.claimed && replay.record.result?.status === 200);
  assert.ok(!replay.claimed && replay.record.completedAtMs === 1_500);
});

test("reusing a key with a different request is a conflict", async () => {
  const client = new FakeRedisClient();
  const store = new PersistentIdempotencyStore({ client, namespace: NAMESPACE });
  await store.claim("req-3", "payload-a", 1_000);
  await assert.rejects(
    () => store.claim("req-3", "payload-b", 1_100),
    IdempotencyConflictError,
  );
});

test("oversized result metadata is rejected", async () => {
  const client = new FakeRedisClient();
  const store = new PersistentIdempotencyStore({
    client, namespace: NAMESPACE, maxResultBytes: 32,
  });
  await store.claim("req-4", "payload", 1_000);
  await assert.rejects(
    () => store.complete("req-4", { blob: "x".repeat(200) }, 1_100),
    /exceeds the permitted size/,
  );
});

test("idempotency claims expire on an explicit TTL", async () => {
  const client = new FakeRedisClient();
  const store = new PersistentIdempotencyStore({ client, namespace: NAMESPACE, ttlSeconds: 60 });
  await store.claim("req-5", "payload", 0);
  const key = client.keys()[0];
  assert.ok((await client.command<number>(["PTTL", key])) > 0);
  client.nowMs = 61_000;
  assert.equal(await store.read("req-5"), null);
  assert.equal((await store.claim("req-5", "payload", 61_000)).claimed, true, "reclaimable");
});

test("idempotency keys never contain the raw request key", async () => {
  const client = new FakeRedisClient();
  const store = new PersistentIdempotencyStore({ client, namespace: NAMESPACE });
  await store.claim(RAW_SUBJECT, "payload", 0);
  const key = client.keys()[0];
  assert.ok(key.startsWith(`${NAMESPACE}:idem:`));
  assert.ok(!key.includes(RAW_SUBJECT));
});

test("idempotency store FAILS CLOSED when Redis is unavailable", async () => {
  const client = new FakeRedisClient();
  const store = new PersistentIdempotencyStore({ client, namespace: NAMESPACE });
  client.failNextOperations = 1;
  await assert.rejects(
    () => store.claim("req-6", "payload", 0),
    IdempotencyStoreUnavailableError,
  );
});

// ── REST client transport ────────────────────────────────────────────────────────────

test("REST client fails closed on non-2xx, transport error and malformed body", async () => {
  const base = { restUrl: "https://example.upstash.io", restToken: "dummy" };

  const http500 = new UpstashRestClient({
    ...base,
    fetchImpl: async () => ({ ok: false, status: 500, text: async () => "" }),
  });
  await assert.rejects(() => http500.command(["GET", "k"]), RedisUnavailableError);

  const transport = new UpstashRestClient({
    ...base,
    fetchImpl: async () => {
      throw new Error("socket hang up");
    },
  });
  await assert.rejects(() => transport.command(["GET", "k"]), RedisUnavailableError);

  const malformed = new UpstashRestClient({
    ...base,
    fetchImpl: async () => ({ ok: true, status: 200, text: async () => "<html>" }),
  });
  await assert.rejects(() => malformed.command(["GET", "k"]), RedisUnavailableError);

  const commandError = new UpstashRestClient({
    ...base,
    fetchImpl: async () => ({
      ok: true, status: 200, text: async () => JSON.stringify({ error: "WRONGTYPE" }),
    }),
  });
  await assert.rejects(() => commandError.command(["GET", "k"]), RedisUnavailableError);
});

test("REST client returns the parsed result and never logs the token", async () => {
  const seen: string[] = [];
  const client = new UpstashRestClient({
    restUrl: "https://example.upstash.io/",
    restToken: "super-secret-token",
    fetchImpl: async (url, init) => {
      seen.push(url, JSON.stringify(init?.body ?? ""));
      return { ok: true, status: 200, text: async () => JSON.stringify({ result: "OK" }) };
    },
  });
  assert.equal(await client.command(["SET", "k", "v"]), "OK");
  assert.ok(!seen.join(" ").includes("super-secret-token"), "token must stay in the header only");
});

test("a Redis-unavailable error message carries no key, value or endpoint", () => {
  const error = new RedisUnavailableError("http 500");
  assert.ok(!error.message.includes("upstash.io"));
  assert.ok(!error.message.includes("Bearer"));
});
