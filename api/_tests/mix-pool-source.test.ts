/**
 * Phase 3D-3B — candidate-pool retrieval boundary.
 *
 * IMPORTANT: these are CONTRACT tests against a fake in-memory store. They prove the
 * boundary behaves correctly; they do NOT prove production readiness, because no storage
 * provider is provisioned. The fake exists only to exercise the contract.
 *
 * The artifact under test is produced by the REAL Python publisher path, so the bytes the
 * reader parses are the bytes the publisher would send.
 */

import assert from "node:assert/strict";
import test from "node:test";
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
  MAX_POOL_BYTES,
  MIX_POOL_KEY_NAMESPACE,
  MIX_POOL_KEY_VERSION,
  createMixPoolCandidateSource,
  isValidUtcDate,
  mixPoolKey,
  readMixPool,
  type MixPoolReadResult,
  type PoolObjectStore,
} from "../_lib/mix-pool-source.js";
import { mixPoolIdentity, type JsonValue } from "../_lib/mix-pool-schema.js";

const HERE = dirname(fileURLToPath(import.meta.url));

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
const DATE = "2026-07-27";
const GENERATED_AT_MS = Date.parse("2026-07-27T09:00:00Z");

/**
 * Runs the REAL Python publisher against a capturing store and returns exactly the bytes
 * it would have sent, plus its safe result metadata. Fails (never skips) without Python.
 */
function publishViaPython(): { key: string; body: Buffer; result: Record<string, JsonValue> } {
  const script = `
import json, os, sys
sys.path.insert(0, os.environ["PIPELINE_DIR"])
import mix_pool, mix_pool_schema as S, mix_pool_publisher as P

src = json.load(open(os.path.join(os.environ["PIPELINE_DIR"], "fixtures",
                                  "mix_pool_scout_candidates.json"), encoding="utf-8"))
pool = mix_pool.build_mix_pool(src, "2026-07-27", "2026-07-27T09:00:00Z",
                               now="2026-07-27T09:00:00Z")
art = S.freeze_artifact(pool, source_input=src, source="offline-fixture",
                        reference_at="2026-07-27T09:00:00Z")

captured = {}
class Capture:
    def put(self, key, body, *, ttl_seconds):
        captured["key"] = key
        captured["body"] = body.hex()
        captured["ttl"] = ttl_seconds

result = P.publish_mix_pool(art, store=Capture())
sys.stdout.write(json.dumps({"captured": captured, "result": result}))
`;
  const proc = spawnSync("python3", ["-c", script], {
    encoding: "utf8",
    env: { ...process.env, PIPELINE_DIR },
    maxBuffer: 64 * 1_024 * 1_024,
  });
  assert.ok(existsSync(join(PIPELINE_DIR, "mix_pool_publisher.py")), "publisher not found");
  assert.equal(proc.status, 0, `python publisher failed: ${(proc.stderr || "").slice(-500)}`);
  const parsed = JSON.parse(proc.stdout) as {
    captured: { key: string; body: string; ttl: number };
    result: Record<string, JsonValue>;
  };
  return {
    key: parsed.captured.key,
    body: Buffer.from(parsed.captured.body, "hex"),
    result: parsed.result,
  };
}

const published = publishViaPython();

/** A fake store. Contract-test only — explicitly not a production provider. */
function fakeStore(entries: Record<string, Uint8Array>): PoolObjectStore {
  return {
    async get(key) {
      return entries[key] ?? null;
    },
  };
}

function storeWith(body: Uint8Array, date = DATE): PoolObjectStore {
  return fakeStore({ [mixPoolKey(date)]: body });
}

function corrupt(mutate: (a: Record<string, JsonValue>) => void): Uint8Array {
  const artifact = JSON.parse(published.body.toString("utf8")) as Record<string, JsonValue>;
  mutate(artifact);
  return Buffer.from(JSON.stringify(artifact), "utf8");
}

async function read(store: PoolObjectStore | null, date = DATE): Promise<MixPoolReadResult> {
  return readMixPool(date, { store, now: () => GENERATED_AT_MS + 60_000 });
}

// ── key and date contract ─────────────────────────────────────────────────────────────

test("1+6. the key is namespaced, versioned and date-keyed", () => {
  assert.equal(mixPoolKey(DATE), `${MIX_POOL_KEY_NAMESPACE}:${MIX_POOL_KEY_VERSION}:${DATE}`);
  assert.equal(mixPoolKey(DATE), "signals:mix-pool:v1:2026-07-27");
  assert.equal(mixPoolKey(DATE), published.key, "TypeScript and Python keys diverged");
});

test("2. the key never depends on the local timezone", () => {
  const original = process.env.TZ;
  try {
    process.env.TZ = "Pacific/Kiritimati"; // UTC+14
    const east = mixPoolKey(DATE);
    process.env.TZ = "Pacific/Niue"; // UTC-11
    assert.equal(mixPoolKey(DATE), east);
  } finally {
    if (original === undefined) delete process.env.TZ;
    else process.env.TZ = original;
  }
});

test("4. a malformed or impossible date is rejected", () => {
  for (const bad of ["2026-7-27", "26-07-27", "2026-13-01", "2026-02-30", "", "today"]) {
    assert.equal(isValidUtcDate(bad), false, `${bad} was accepted`);
  }
  assert.throws(() => mixPoolKey("2026-7-27"));
  assert.equal(isValidUtcDate("2026-02-28"), true);
});

test("5. a missing date never silently falls back to another day", async () => {
  const store = storeWith(published.body, DATE);
  const other = await read(store, "2026-07-26");
  assert.equal(other.ok, false);
  if (!other.ok) assert.equal(other.reason, "candidate_pool_missing");
});

// ── consumer: the happy path ──────────────────────────────────────────────────────────

test("21+22+40. a valid artifact is retrieved by exact date, in artifact order", async () => {
  const result = await read(storeWith(published.body));
  assert.equal(result.ok, true, result.ok ? "" : result.reason);
  if (!result.ok) return;

  const artifact = JSON.parse(published.body.toString("utf8")) as {
    candidates: { id: string }[];
  };
  assert.deepEqual(
    result.candidates.map((c) => c.id),
    artifact.candidates.map((c) => c.id),
    "candidate order changed",
  );
  assert.equal(result.metadata.date, DATE);
  assert.equal(result.metadata.key, "signals:mix-pool:v1:2026-07-27");
  assert.equal(result.metadata.candidateCount, result.candidates.length);
  assert.equal(result.metadata.byteLength, published.body.byteLength);
  assert.equal(result.metadata.poolIdentityPrefix.length, 12);
});

test("41. returned candidates are frozen and cannot mutate the source", async () => {
  const result = await read(storeWith(published.body));
  assert.equal(result.ok, true);
  if (!result.ok) return;
  assert.ok(Object.isFrozen(result.candidates[0]));
  assert.throws(() => {
    (result.candidates[0] as { id: string }).id = "hacked";
  }, TypeError);

  const again = await read(storeWith(published.body));
  assert.equal(again.ok, true);
  if (again.ok) assert.notEqual(again.candidates[0].id, "hacked");
});

test("53. repeated retrieval of identical bytes yields identical candidates", async () => {
  const a = await read(storeWith(published.body));
  const b = await read(storeWith(published.body));
  assert.equal(a.ok && b.ok, true);
  if (a.ok && b.ok) {
    assert.equal(JSON.stringify(a.candidates), JSON.stringify(b.candidates));
    assert.deepEqual(a.metadata, b.metadata);
  }
});

// ── consumer: every failure maps to a stable reason code ──────────────────────────────

test("23-28. transport and payload failures map to safe codes", async () => {
  assert.equal((await read(fakeStore({}))).ok, false);
  assert.equal(((await read(fakeStore({}))) as { reason: string }).reason, "candidate_pool_missing");

  const timeout: PoolObjectStore = {
    async get() {
      const error = new Error("aborted");
      error.name = "AbortError";
      throw error;
    },
  };
  assert.equal(((await read(timeout)) as { reason: string }).reason, "candidate_pool_timeout");

  const boom: PoolObjectStore = {
    async get() {
      throw new Error("ECONNREFUSED https://secret.upstash.io token=abc");
    },
  };
  const failed = (await read(boom)) as { reason: string };
  assert.equal(failed.reason, "candidate_pool_provider_error");
  assert.ok(!JSON.stringify(failed).includes("upstash"), "provider detail leaked");
  assert.ok(!JSON.stringify(failed).includes("token"), "credential leaked");

  assert.equal(
    ((await read(storeWith(new Uint8Array(0)))) as { reason: string }).reason,
    "candidate_pool_empty",
  );
  assert.equal(
    ((await read(storeWith(Buffer.alloc(MAX_POOL_BYTES + 1, 0x20)))) as { reason: string }).reason,
    "candidate_pool_too_large",
  );
  assert.equal(
    ((await read(storeWith(Buffer.from("{not json", "utf8")))) as { reason: string }).reason,
    "candidate_pool_invalid_json",
  );
});

test("29-36. schema, version, integrity and identity failures map to safe codes", async () => {
  const cases: [string, (a: Record<string, JsonValue>) => void, string][] = [
    ["unsupported schema", (a) => { a.schemaVersion = 99; }, "candidate_pool_version_incompatible"],
    ["unsupported selector", (a) => { a.selectorVersion = 99; }, "candidate_pool_version_incompatible"],
    ["bad generator", (a) => {
      (a.provenance as Record<string, JsonValue>).generatorVersion = 99;
    }, "candidate_pool_version_incompatible"],
    ["count mismatch", (a) => { a.candidateCount = 99; }, "candidate_pool_schema_invalid"],
    ["duplicate id", (a) => {
      const c = a.candidates as { id: string }[];
      c[1].id = c[0].id;
    }, "candidate_pool_identity_mismatch"],
    ["duplicate url", (a) => {
      const c = a.candidates as { url: string }[];
      c[1].url = c[0].url;
    }, "candidate_pool_identity_mismatch"],
    ["invalid numeric", (a) => {
      (a.candidates as Record<string, JsonValue>[])[0].baseScore = 7.1234567;
    }, "candidate_pool_identity_mismatch"],
    ["identity mismatch", (a) => { a.poolIdentity = "0".repeat(64); }, "candidate_pool_identity_mismatch"],
  ];
  for (const [label, mutate, expected] of cases) {
    const result = (await read(storeWith(corrupt(mutate)))) as { ok: boolean; reason: string };
    assert.equal(result.ok, false, `${label} was accepted`);
    assert.equal(result.reason, expected, `${label} produced ${result.reason}`);
  }
});

test("37. an artifact whose own date differs from the key is rejected", async () => {
  // Mutating `date` alone keeps poolIdentity valid (it covers candidates only), so this
  // proves the date check is independent of the identity check.
  const body = corrupt((a) => { a.date = "2026-07-26"; });
  const store = fakeStore({ [mixPoolKey(DATE)]: body });
  const result = (await read(store)) as { ok: boolean; reason: string };
  assert.equal(result.ok, false);
  assert.equal(result.reason, "candidate_pool_date_mismatch");
});

test("38-39. freshness bounds are enforced in UTC", async () => {
  const store = storeWith(published.body);

  const stale = await readMixPool(DATE, {
    store,
    now: () => GENERATED_AT_MS + 9 * 24 * 60 * 60 * 1_000,
  });
  assert.equal(stale.ok, false);
  if (!stale.ok) assert.equal(stale.reason, "candidate_pool_stale");

  const future = await readMixPool(DATE, {
    store,
    now: () => GENERATED_AT_MS - 60 * 60 * 1_000,
  });
  assert.equal(future.ok, false);
  if (!future.ok) assert.equal(future.reason, "candidate_pool_future_timestamp");

  // A small skew inside the ceiling is fine.
  const skewed = await readMixPool(DATE, { store, now: () => GENERATED_AT_MS - 60_000 });
  assert.equal(skewed.ok, true);
});

// ── provider configuration ────────────────────────────────────────────────────────────

test("45-47. an unconfigured provider fails closed and leaks nothing", async () => {
  const result = (await read(null)) as { ok: boolean; reason: string };
  assert.equal(result.ok, false);
  assert.equal(result.reason, "candidate_pool_not_configured");
  assert.deepEqual(Object.keys(result).sort(), ["ok", "reason"]);
});

test("44. no reason code carries a provider message", async () => {
  const noisy: PoolObjectStore = {
    async get() {
      throw new Error("HTTP 500 from https://us1-x.upstash.io: Bearer AX...redacted");
    },
  };
  const result = await read(noisy);
  const serialized = JSON.stringify(result);
  for (const secret of ["upstash", "Bearer", "HTTP 500", "https://"]) {
    assert.ok(!serialized.includes(secret), `leaked: ${secret}`);
  }
});

// ── integration boundary ──────────────────────────────────────────────────────────────

test("49. the adapter satisfies the MixCandidateSource shape in isolation", async () => {
  const observed: string[] = [];
  const source = createMixPoolCandidateSource({
    store: storeWith(published.body),
    now: () => GENERATED_AT_MS + 60_000,
    onResult: (r) => observed.push(r.ok ? "ok" : r.reason),
  });

  const candidates = await source.loadCandidates(DATE);
  assert.ok(candidates && candidates.length > 0);
  assert.deepEqual(observed, ["ok"]);

  const missing = await source.loadCandidates("2026-07-26");
  assert.equal(missing, null, "a failure must degrade to null, not throw");
  assert.deepEqual(observed, ["ok", "candidate_pool_missing"]);
});

test("50+51. production stays disconnected and does not import the adapter", () => {
  for (const relative of ["edition.ts", "_lib/edition-orchestrator.ts", "_lib/runtime-factory.ts", "_lib/vercel-runtime.ts"]) {
    const source = readFileSync(join(HERE, "..", relative), "utf8");
    assert.ok(!source.includes("mix-pool-source"), `${relative} imports the adapter`);
    assert.ok(!source.includes("mix-pool-schema"), `${relative} imports the pool schema`);
  }
  assert.ok(readFileSync(join(HERE, "..", "edition.ts"), "utf8").includes("selector_not_connected"));
});

test("42+43. the module reads no file, no environment and logs nothing", () => {
  const source = readFileSync(join(HERE, "..", "_lib", "mix-pool-source.ts"), "utf8")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .split("\n")
    .filter((line) => !line.trim().startsWith("//") && !line.trim().startsWith("*"))
    .join("\n");
  for (const needle of ["readFileSync", "readFile", "child_process", "spawn", "process.env", "console."]) {
    assert.ok(!source.includes(needle), `mix-pool-source.ts references ${needle}`);
  }
});

// ── live Python parity ────────────────────────────────────────────────────────────────

test("54+55. the exact bytes Python publishes are accepted by TypeScript", async () => {
  const result = await read(storeWith(published.body));
  assert.equal(result.ok, true, result.ok ? "" : result.reason);

  const artifact = JSON.parse(published.body.toString("utf8")) as Record<string, JsonValue>;
  assert.equal(
    mixPoolIdentity(artifact.candidates as JsonValue[]),
    artifact.poolIdentity,
    "TypeScript re-derived a different poolIdentity from the published bytes",
  );
  assert.equal(artifact.poolIdentity, "38d9c03d43bd5e94eb0205387b363d9dde795283bbac277d8ec1847d45806a3d");
});

test("14. the publisher returns safe metadata only — no candidate content", () => {
  const keys = Object.keys(published.result).sort();
  assert.deepEqual(keys, [
    "byteLength", "candidateCount", "date", "key", "poolIdentityPrefix",
    "schemaVersion", "selectorVersion", "status", "ttlSeconds",
  ]);
  const serialized = JSON.stringify(published.result);
  for (const forbidden of ["headline", "summary", "http", "candidates", "url"]) {
    assert.ok(!serialized.toLowerCase().includes(forbidden), `publisher metadata leaked ${forbidden}`);
  }
  assert.equal(published.result.status, "published");
  assert.equal(String(published.result.poolIdentityPrefix).length, 12);
});

test("11. the published body is byte-identical to the Python serializer output", () => {
  const script = `
import json, os, sys
sys.path.insert(0, os.environ["PIPELINE_DIR"])
import mix_pool, mix_pool_schema as S
src = json.load(open(os.path.join(os.environ["PIPELINE_DIR"], "fixtures",
                                  "mix_pool_scout_candidates.json"), encoding="utf-8"))
pool = mix_pool.build_mix_pool(src, "2026-07-27", "2026-07-27T09:00:00Z",
                               now="2026-07-27T09:00:00Z")
art = S.freeze_artifact(pool, source_input=src, source="offline-fixture",
                        reference_at="2026-07-27T09:00:00Z")
sys.stdout.write(S.serialize(art).hex())
`;
  const proc = spawnSync("python3", ["-c", script], {
    encoding: "utf8",
    env: { ...process.env, PIPELINE_DIR },
    maxBuffer: 64 * 1_024 * 1_024,
  });
  assert.equal(proc.status, 0, (proc.stderr || "").slice(-400));
  assert.equal(published.body.toString("hex"), proc.stdout.trim());
});
