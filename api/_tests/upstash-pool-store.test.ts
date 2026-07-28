/**
 * Phase 3D-3D — the concrete Upstash REST store.
 *
 * IMPORTANT: every test here injects a fake `fetch`. NO REAL PROVIDER IS CONTACTED, no
 * Upstash database exists, and passing this suite proves the CODE is correct — not that the
 * storage path works. The only thing that can prove that is the manual smoke test in
 * `UPSTASH_PROVISIONING.md`, run against a real database with real credentials.
 *
 * The byte-parity test (37) is the exception worth calling out: it runs the REAL Python
 * publisher, captures the exact bytes it would have sent, feeds them back through the REAL
 * TypeScript store inside a simulated Upstash envelope, and asserts the reader gets the
 * publisher's bytes back unchanged.
 */

import assert from "node:assert/strict";
import test from "node:test";
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
  UpstashStoreError,
  createUpstashCandidateSource,
  createUpstashPoolStore,
  mapUpstashReason,
  readMixPoolFromUpstash,
  resolveUpstashCredentials,
  type UpstashEnvSource,
} from "../_lib/upstash-pool-store.js";
import { mixPoolKey } from "../_lib/mix-pool-source.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const LIB_DIR = resolve(HERE, "..", "_lib");

function findPipelineDir(): string {
  let current = HERE;
  for (let depth = 0; depth < 8; depth += 1) {
    const candidate = join(current, "pipeline");
    if (existsSync(join(candidate, "mix_pool_publisher.py"))) return candidate;
    const parent = dirname(current);
    if (parent === current) break;
    current = parent;
  }
  return join(resolve(HERE, "..", ".."), "pipeline");
}
const PIPELINE_DIR = findPipelineDir();

const URL_VALUE = "https://fixture-not-a-real-database.upstash.io";
/** Obviously synthetic. Long enough that a leak test is meaningful. */
const TOKEN_VALUE = "FIXTURE-TOKEN-de6f9c1b4a2e47d0b8155c93aa07f2e1-NOT-REAL";
const GOOD_ENV: UpstashEnvSource = { KV_REST_API_URL: URL_VALUE, KV_REST_API_TOKEN: TOKEN_VALUE };

const DATE = "2026-07-27";
const KEY = mixPoolKey(DATE);

type Call = { url: string; init: RequestInit };

/** A fake `fetch` that records the call and replies with a caller-supplied response. */
function fakeFetch(reply: () => Response | Promise<Response>): {
  impl: typeof fetch;
  calls: Call[];
} {
  const calls: Call[] = [];
  const impl = (async (input: unknown, init?: RequestInit) => {
    calls.push({ url: String(input), init: init ?? {} });
    return await reply();
  }) as unknown as typeof fetch;
  return { impl, calls };
}

function envelope(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function storeOf(reply: () => Response | Promise<Response>, options = {}) {
  const fake = fakeFetch(reply);
  const created = createUpstashPoolStore(GOOD_ENV, { fetchImpl: fake.impl, ...options });
  assert.equal(created.ok, true);
  if (!created.ok) throw new Error("unreachable");
  return { store: created.store, calls: fake.calls };
}

// ── 1-4. configuration is validated and fails closed ──────────────────────────────────

test("1. a fully configured store is accepted", () => {
  const resolved = resolveUpstashCredentials(GOOD_ENV);
  assert.equal(resolved.ok, true);
  if (!resolved.ok) return;
  assert.equal(resolved.credentials.restUrl, URL_VALUE);
  assert.equal(resolved.credentials.restToken, TOKEN_VALUE);

  const created = createUpstashPoolStore(GOOD_ENV, { fetchImpl: fakeFetch(() => envelope({ result: null })).impl });
  assert.equal(created.ok, true);
});

test("2. a missing URL fails closed", () => {
  const created = createUpstashPoolStore({ KV_REST_API_TOKEN: TOKEN_VALUE });
  assert.equal(created.ok, false);
  if (created.ok) return;
  assert.equal(created.reason, "upstash_partial_configuration");
});

test("3. a missing token fails closed", () => {
  const created = createUpstashPoolStore({ KV_REST_API_URL: URL_VALUE });
  assert.equal(created.ok, false);
  if (created.ok) return;
  assert.equal(created.reason, "upstash_partial_configuration");
});

test("4. partial and absent configuration are distinguished, and both fail closed", () => {
  assert.deepEqual(createUpstashPoolStore({}), { ok: false, reason: "upstash_not_configured" });
  assert.deepEqual(createUpstashPoolStore({ KV_REST_API_URL: "  ", KV_REST_API_TOKEN: "  " }), {
    ok: false,
    reason: "upstash_not_configured",
  });
  // Whitespace is not a credential.
  assert.deepEqual(createUpstashPoolStore({ KV_REST_API_URL: URL_VALUE, KV_REST_API_TOKEN: "   " }), {
    ok: false,
    reason: "upstash_partial_configuration",
  });
});

test("4b. a plaintext or decorated URL is refused — a token never goes out in the clear", () => {
  for (const bad of [
    "http://fixture.upstash.io",
    "https://fixture.upstash.io?token=leak",
    "https://fixture.upstash.io#frag",
    "not-a-url",
    "ftp://fixture.upstash.io",
  ]) {
    const created = createUpstashPoolStore({ KV_REST_API_URL: bad, KV_REST_API_TOKEN: TOKEN_VALUE });
    assert.equal(created.ok, false, `accepted ${bad}`);
    if (!created.ok) assert.equal(created.reason, "upstash_insecure_url");
  }
});

// ── 5-6. the request itself ───────────────────────────────────────────────────────────

test("5. exactly the requested key is asked for, as data and not as a path", async () => {
  const { store, calls } = storeOf(() => envelope({ result: null }));
  await store.get(KEY, { timeoutMs: 500, maxBytes: 1_000 });

  assert.equal(calls.length, 1, "a read must be one round trip");
  assert.equal(calls[0].url, URL_VALUE, "the key must not be interpolated into the URL");
  assert.deepEqual(JSON.parse(String(calls[0].init.body)), ["GET", KEY]);
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.cache, "no-store");
  assert.equal(calls[0].init.redirect, "error");
});

test("6. the authorization header is a bearer token and nothing else is sent", async () => {
  const { store, calls } = storeOf(() => envelope({ result: null }));
  await store.get(KEY, { timeoutMs: 500, maxBytes: 1_000 });

  const headers = calls[0].init.headers as Record<string, string>;
  assert.equal(headers.authorization, `Bearer ${TOKEN_VALUE}`);
  assert.equal(headers["content-type"], "application/json");
  // No cookie, no api-key duplicate, no custom identity header.
  assert.deepEqual(Object.keys(headers).sort(), ["authorization", "content-type"]);
  // The token is in the header, never in the URL or the body.
  assert.ok(!calls[0].url.includes(TOKEN_VALUE));
  assert.ok(!String(calls[0].init.body).includes(TOKEN_VALUE));
});

// ── 7-8. success and absence ──────────────────────────────────────────────────────────

test("7. a stored string comes back byte-exact, including multi-byte characters", async () => {
  const stored = '{"date":"2026-07-27","headline":"東京 café — \\"pilot\\"","n":1}';
  const { store } = storeOf(() => envelope({ result: stored }));

  const bytes = await store.get(KEY, { timeoutMs: 500, maxBytes: 10_000 });
  assert.ok(bytes instanceof Uint8Array);
  assert.equal(new TextDecoder().decode(bytes!), stored);
  assert.deepEqual(Array.from(bytes!), Array.from(new TextEncoder().encode(stored)));
});

test("8. a missing key is null, which the reader turns into candidate_pool_missing", async () => {
  const { store } = storeOf(() => envelope({ result: null }));
  assert.equal(await store.get(KEY, { timeoutMs: 500, maxBytes: 10_000 }), null);

  assert.equal(mapUpstashReason("upstash_missing_key"), "candidate_pool_missing");

  const result = await readMixPoolFromUpstash(DATE, {
    env: GOOD_ENV,
    fetchImpl: fakeFetch(() => envelope({ result: null })).impl,
  });
  assert.deepEqual(result, { ok: false, reason: "candidate_pool_missing" });
});

// ── 9-11. transport failures map safely ───────────────────────────────────────────────

test("9. a timeout maps to upstash_timeout and then candidate_pool_timeout", async () => {
  const abort = () => {
    const error = new Error("aborted");
    error.name = "AbortError";
    return Promise.reject(error);
  };
  const impl = (async () => await abort()) as unknown as typeof fetch;

  const created = createUpstashPoolStore(GOOD_ENV, { fetchImpl: impl });
  assert.equal(created.ok, true);
  if (!created.ok) return;
  await assert.rejects(
    () => created.store.get(KEY, { timeoutMs: 5, maxBytes: 10 }),
    (error: unknown) => error instanceof UpstashStoreError && error.reason === "upstash_timeout",
  );

  assert.deepEqual(await readMixPoolFromUpstash(DATE, { env: GOOD_ENV, fetchImpl: impl }), {
    ok: false,
    reason: "candidate_pool_timeout",
  });
});

test("10. a provider 4xx maps safely and is never treated as a missing key", async () => {
  for (const status of [400, 401, 403, 404, 429]) {
    const { store } = storeOf(() => envelope({ error: "WRONGPASS invalid credential" }, status));
    await assert.rejects(
      () => store.get(KEY, { timeoutMs: 500, maxBytes: 10_000 }),
      (error: unknown) =>
        error instanceof UpstashStoreError && error.reason === "upstash_provider_error",
      `status ${status}`,
    );
  }
});

test("11. a provider 5xx maps safely", async () => {
  for (const status of [500, 502, 503]) {
    const { store } = storeOf(() => envelope({ error: "upstream unavailable" }, status));
    await assert.rejects(
      () => store.get(KEY, { timeoutMs: 500, maxBytes: 10_000 }),
      (error: unknown) =>
        error instanceof UpstashStoreError && error.reason === "upstash_provider_error",
    );
  }
});

// ── 12-14. the response envelope is validated ─────────────────────────────────────────

test("12. a malformed envelope is rejected", async () => {
  const bodies = ["not json", "[]", '"a string"', "123", "null", '{"unexpected":1}'];
  for (const body of bodies) {
    const { store } = storeOf(
      () => new Response(body, { status: 200, headers: { "content-type": "application/json" } }),
    );
    await assert.rejects(
      () => store.get(KEY, { timeoutMs: 500, maxBytes: 10_000 }),
      (error: unknown) =>
        error instanceof UpstashStoreError && error.reason === "upstash_invalid_response",
      `body ${body}`,
    );
  }
});

test("12b. a 200 carrying an error field is a provider error, not a valid read", async () => {
  const { store } = storeOf(() => envelope({ error: "ERR unknown command" }));
  await assert.rejects(
    () => store.get(KEY, { timeoutMs: 500, maxBytes: 10_000 }),
    (error: unknown) =>
      error instanceof UpstashStoreError && error.reason === "upstash_provider_error",
  );
});

test("13. a non-string stored value is rejected", async () => {
  for (const result of [42, true, { a: 1 }, [1, 2]]) {
    const { store } = storeOf(() => envelope({ result }));
    await assert.rejects(
      () => store.get(KEY, { timeoutMs: 500, maxBytes: 10_000 }),
      (error: unknown) =>
        error instanceof UpstashStoreError && error.reason === "upstash_non_string_value",
      `result ${JSON.stringify(result)}`,
    );
  }
});

test("14. an oversized value is refused, by declared length and by actual bytes", async () => {
  // Refused from the declared Content-Length, before the body is read.
  const huge = "x".repeat(4_000);
  const { store: byHeader } = storeOf(
    () =>
      new Response(JSON.stringify({ result: huge }), {
        status: 200,
        headers: { "content-type": "application/json", "content-length": String(10 ** 9) },
      }),
  );
  await assert.rejects(
    () => byHeader.get(KEY, { timeoutMs: 500, maxBytes: 100 }),
    (error: unknown) =>
      error instanceof UpstashStoreError && error.reason === "upstash_value_too_large",
  );

  // And refused on the decoded value even when the envelope fits under the stream cap.
  const { store: byValue } = storeOf(() => envelope({ result: "y".repeat(500) }));
  await assert.rejects(
    () => byValue.get(KEY, { timeoutMs: 500, maxBytes: 100 }),
    (error: unknown) =>
      error instanceof UpstashStoreError && error.reason === "upstash_value_too_large",
  );
});

// ── 15-16. nothing leaks ──────────────────────────────────────────────────────────────

test("15. provider prose never escapes the module", async () => {
  const prose = "WRONGPASS invalid or expired token for db 12345 at fixture.upstash.io";
  const { store } = storeOf(() => envelope({ error: prose }, 401));

  await store
    .get(KEY, { timeoutMs: 500, maxBytes: 10_000 })
    .then(
      () => assert.fail("expected a rejection"),
      (error: unknown) => {
        const text = `${String(error)} ${error instanceof Error ? error.stack ?? "" : ""}`;
        assert.ok(!text.includes("WRONGPASS"), "provider prose leaked");
        assert.ok(!text.includes("invalid or expired"), "provider prose leaked");
        assert.ok(!text.includes("12345"), "provider detail leaked");
      },
    );
});

test("16. credentials never appear in an error, a message or a stack", async () => {
  const cases: Array<() => Response> = [
    () => envelope({ error: `token ${TOKEN_VALUE} rejected` }, 401),
    () => envelope({ result: 7 }),
    () => new Response("not json", { status: 200 }),
  ];
  for (const reply of cases) {
    const { store } = storeOf(reply);
    await store.get(KEY, { timeoutMs: 500, maxBytes: 10_000 }).then(
      () => assert.fail("expected a rejection"),
      (error: unknown) => {
        const text = `${String(error)} ${error instanceof Error ? error.stack ?? "" : ""}`;
        assert.ok(!text.includes(TOKEN_VALUE), "the token leaked into an error");
        assert.ok(!text.includes(URL_VALUE), "the REST URL leaked into an error");
      },
    );
  }
  // And the neutral result carries a code only — no message field at all.
  const result = await readMixPoolFromUpstash(DATE, {
    env: GOOD_ENV,
    fetchImpl: fakeFetch(() => envelope({ error: TOKEN_VALUE }, 500)).impl,
  });
  assert.deepEqual(Object.keys(result).sort(), ["ok", "reason"]);
  assert.ok(!JSON.stringify(result).includes(TOKEN_VALUE));
});

// ── 17. determinism ───────────────────────────────────────────────────────────────────

test("17. repeated reads return identical bytes and cache nothing between calls", async () => {
  const stored = '{"a":"東京","b":[1,2,3]}';
  const { store, calls } = storeOf(() => envelope({ result: stored }));

  const first = await store.get(KEY, { timeoutMs: 500, maxBytes: 10_000 });
  const second = await store.get(KEY, { timeoutMs: 500, maxBytes: 10_000 });
  assert.deepEqual(Array.from(first!), Array.from(second!));
  // Two reads means two requests: a hidden cache would be a correctness hazard, because a
  // pool can be republished for the same date.
  assert.equal(calls.length, 2);
  assert.notEqual(first, second, "the same buffer instance was handed out twice");
});

// ── 18-20. boundaries ─────────────────────────────────────────────────────────────────

test("18. the module performs no filesystem access and imports no node builtins", () => {
  const source = readFileSync(join(LIB_DIR, "upstash-pool-store.ts"), "utf8");
  const code = source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
  for (const needle of ["node:fs", "readFileSync", "readFile", "node:child_process", "process.env"]) {
    assert.ok(!code.includes(needle), `the store references ${needle}`);
  }
});

test("19. the module contains no logging of any kind", () => {
  const source = readFileSync(join(LIB_DIR, "upstash-pool-store.ts"), "utf8");
  const code = source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
  assert.ok(!/console\s*\./.test(code), "the store logs");
  assert.ok(!/\bprocess\.std(out|err)\b/.test(code), "the store writes to a stream");
});

test("20. no route or runtime module imports the Upstash store", () => {
  const routes = ["edition.ts", "auth/exchange.ts"];
  const runtime = [
    "_lib/runtime-factory.ts",
    "_lib/runtime-dependencies.ts",
    "_lib/runtime-config.ts",
    "_lib/vercel-runtime.ts",
    "_lib/edition-orchestrator.ts",
  ];
  for (const relative of [...routes, ...runtime]) {
    const text = readFileSync(resolve(HERE, "..", relative), "utf8");
    const code = text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
    assert.ok(
      !/from\s+["'][^"']*\/upstash-pool-store\.js["']/.test(code),
      `${relative} imports the Upstash store`,
    );
  }
});

// ── the neutral candidate source, and the full validated read ─────────────────────────

test("the candidate source returns real candidates for a valid published pool", async () => {
  const published = publishViaPython();
  const observed: string[] = [];
  const source = createUpstashCandidateSource({
    env: GOOD_ENV,
    fetchImpl: fakeFetch(() => envelope({ result: published.body.toString("utf8") })).impl,
    now: () => Date.parse("2026-07-27T10:00:00Z"),
    onResult: (result) => observed.push(result.ok ? "ok" : result.reason),
  });

  const candidates = await source.loadCandidates(DATE);
  assert.ok(Array.isArray(candidates) && candidates.length > 0);
  assert.deepEqual(observed, ["ok"]);
});

test("an unconfigured deployment degrades to candidate_pool_not_configured, not a crash", async () => {
  const result = await readMixPoolFromUpstash(DATE, { env: {} });
  assert.deepEqual(result, { ok: false, reason: "candidate_pool_not_configured" });
});

// ── 37. Python publishes; TypeScript reads the same bytes ─────────────────────────────

/**
 * Runs the REAL Python publisher against a capturing store and returns exactly the bytes it
 * would have sent. Fails (never skips) if Python is unavailable — a skipped parity test is
 * indistinguishable from a passing one, and that is the failure mode this suite exists for.
 */
function publishViaPython(): { key: string; body: Buffer } {
  const script = `
import json, os, sys
sys.path.insert(0, os.environ["PIPELINE_DIR"])
import mix_pool, mix_pool_schema as S, mix_pool_publisher as P

src = json.load(open(os.path.join(os.environ["PIPELINE_DIR"], "fixtures",
                                  "mix_pool_scout_candidates.json"), encoding="utf-8"))
pool = mix_pool.build_mix_pool(src, "${DATE}", "2026-07-27T09:00:00Z", now="2026-07-27T09:00:00Z")
artifact = S.freeze_artifact(pool, source_input=src, source="offline-fixture",
                             reference_at="2026-07-27T09:00:00Z")

class Capture:
    def __init__(self): self.key = None; self.body = None; self.calls = 0
    def put(self, key, body, *, ttl_seconds):
        self.calls += 1
        self.key, self.body = key, body

store = Capture()
P.publish_mix_pool(artifact, store=store, date="${DATE}")
assert store.calls == 1, "the publisher must issue exactly one atomic write"
sys.stdout.write(json.dumps({"key": store.key, "bodyB64": __import__("base64").b64encode(store.body).decode()}))
`;
  const run = spawnSync("python3", ["-c", script], {
    encoding: "utf8",
    env: { ...process.env, PIPELINE_DIR: PIPELINE_DIR },
  });
  assert.equal(run.status, 0, `the Python publisher failed:\n${run.stderr}`);
  const parsed = JSON.parse(run.stdout) as { key: string; bodyB64: string };
  return { key: parsed.key, body: Buffer.from(parsed.bodyB64, "base64") };
}

test("37. bytes published by Python are returned byte-exact by the TypeScript store", async () => {
  const published = publishViaPython();

  // Simulate exactly what Upstash does with those bytes: store the UTF-8 text as a Redis
  // string, then hand it back inside a JSON envelope.
  const storedAsUpstashWould = published.body.toString("utf8");
  const { store, calls } = storeOf(() => envelope({ result: storedAsUpstashWould }));

  const retrieved = await store.get(published.key, { timeoutMs: 1_000, maxBytes: 2 ** 21 });
  assert.ok(retrieved !== null);

  assert.equal(retrieved!.byteLength, published.body.byteLength, "byte length changed in transit");
  assert.ok(Buffer.from(retrieved!).equals(published.body), "the bytes are not byte-exact");

  // The key the TypeScript reader asks for is the key the Python publisher wrote.
  assert.deepEqual(JSON.parse(String(calls[0].init.body)), ["GET", published.key]);
  assert.equal(published.key, mixPoolKey(DATE));
});

test("37b. the round-tripped bytes still pass full validation and identity re-derivation", async () => {
  const published = publishViaPython();
  const result = await readMixPoolFromUpstash(DATE, {
    env: GOOD_ENV,
    fetchImpl: fakeFetch(() => envelope({ result: published.body.toString("utf8") })).impl,
    now: () => Date.parse("2026-07-27T10:00:00Z"),
  });

  assert.equal(result.ok, true, result.ok ? "" : `read failed: ${result.reason}`);
  if (!result.ok) return;
  assert.equal(result.metadata.key, published.key);
  assert.equal(result.metadata.byteLength, published.body.byteLength);
  assert.ok(result.candidates.length > 0);
  // `readMixPool` re-derives `poolIdentity` from the candidates, so reaching `ok: true`
  // after a full transport round trip is the identity surviving the round trip.
});
