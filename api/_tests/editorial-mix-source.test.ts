/**
 * Phase 3E-1 — the Editorial Mix Pool retrieval boundary.
 *
 * `editorial-mix-source.ts` is the ONLY module that turns stored bytes into candidates the
 * selector may see. Everything downstream — the orchestrator, the feed adapter, the route —
 * trusts whatever this module returns, so every rejection it is responsible for is proven
 * here rather than assumed.
 *
 * NO NETWORK. The store is a fake that returns bytes from memory. That is the point: the
 * contract under test is "given these bytes, what does the reader do", and a real provider
 * would only make the answer less observable.
 *
 * The base artifact is the REAL fixture produced by the Python publisher path, grown to a
 * publishable size and re-signed with the schema's own identity functions — so the bytes
 * parsed here are shaped exactly like the bytes production stores.
 */

import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  EDITORIAL_KEY_NAMESPACE,
  EDITORIAL_KEY_VERSION,
  createEditorialCandidateSource,
  editorialMixPoolKey,
  readEditorialMixPool,
} from "../_lib/editorial-mix-source.js";
import {
  MINIMUM_PUBLISHABLE_POOL_SIZE,
  TARGET_POOL_SIZE,
  editorialPoolIdentityOf,
  selectorPoolIdentityOf,
} from "../_lib/editorial-mix-pool-schema.js";
import type { JsonValue } from "../_lib/mix-pool-schema.js";
import type { PoolObjectStore } from "../_lib/mix-pool-source.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const DATE = "2026-07-27";
const NOW_MS = Date.parse("2026-07-27T10:00:00Z");
const now = () => NOW_MS;

type Artifact = Record<string, JsonValue>;

const FIXTURE = JSON.parse(
  readFileSync(join(HERE, "..", "_fixtures", "editorial_mix_pool.json"), "utf8"),
) as Artifact;

/**
 * Grow the 6-candidate fixture to a publishable pool, then RE-DERIVE both identities so the
 * artifact is internally consistent. Every clone gets a unique id, url, headline and story
 * identity — otherwise the duplicate guard, not the reader, would be under test.
 */
function poolOf(count: number, overrides: Partial<Artifact> = {}): Artifact {
  const base = (FIXTURE.candidates as JsonValue[]) as Record<string, Record<string, JsonValue>>[];
  const candidates: JsonValue[] = [];
  for (let index = 0; index < count; index += 1) {
    const seed = base[index % base.length];
    const clone = JSON.parse(JSON.stringify(seed)) as Record<string, Record<string, JsonValue>>;
    const suffix = `-c${index}`;
    clone.selector.id = `${String(clone.selector.id)}${suffix}`;
    clone.selector.url = `https://example.test/story${suffix}`;
    clone.selector.headline = `${String(clone.selector.headline)} ${index}`;
    clone.selector.underlyingStoryIdentity = `story${suffix}`;
    clone.selector.topicFingerprint = `fingerprint${suffix}`;
    clone.editorial.headline = `${String(clone.editorial.headline)} ${index}`;
    clone.editorial.originalURL = `https://example.test/story${suffix}`;
    clone.editorial.imageURL = `https://images.example.test/story${suffix}.jpg`;
    candidates.push(clone as unknown as JsonValue);
  }

  const artifact: Artifact = {
    ...FIXTURE,
    date: DATE,
    generatedAt: "2026-07-27T09:00:00Z",
    candidates,
    candidateCount: count,
    ...overrides,
  };
  // Sign LAST, so an override that changes content is still consistently signed unless the
  // test deliberately re-breaks it afterwards.
  artifact.selectorPoolIdentity = selectorPoolIdentityOf(artifact);
  artifact.editorialPoolIdentity = editorialPoolIdentityOf(
    artifact.candidates as JsonValue[],
  );
  return artifact;
}

const VALID = poolOf(MINIMUM_PUBLISHABLE_POOL_SIZE);

function bytesOf(artifact: Artifact): Uint8Array {
  return new TextEncoder().encode(JSON.stringify(artifact));
}

/** Records every key requested, so "which key did it ask for" is directly assertable. */
function storeOf(
  entries: Record<string, Uint8Array | null>,
  behaviour?: { throws?: Error },
): PoolObjectStore & { reads: string[] } {
  const reads: string[] = [];
  return {
    reads,
    async get(key: string) {
      reads.push(key);
      if (behaviour?.throws) throw behaviour.throws;
      return entries[key] ?? null;
    },
  } as PoolObjectStore & { reads: string[] };
}

function storeWith(artifact: Artifact, date = DATE): PoolObjectStore & { reads: string[] } {
  return storeOf({ [editorialMixPoolKey(date)]: bytesOf(artifact) });
}

async function read(
  store: PoolObjectStore | null,
  date = DATE,
  extra: Record<string, unknown> = {},
) {
  return readEditorialMixPool(date, { store, now, ...extra });
}

async function reasonOf(store: PoolObjectStore | null, date = DATE): Promise<string> {
  const result = await read(store, date);
  return result.ok ? "ok" : result.reason;
}

// ── 1. key construction ───────────────────────────────────────────────────────────────

test("1. the key is the exact namespace the Python publisher writes", () => {
  assert.equal(editorialMixPoolKey("2026-07-27"), "signals:editorial-mix-pool:v1:2026-07-27");
  assert.equal(EDITORIAL_KEY_NAMESPACE, "signals:editorial-mix-pool");
  assert.equal(EDITORIAL_KEY_VERSION, "v1");
});

test("1b. the key carries the date and NOTHING that identifies a user", () => {
  // Two users with the same preferences must land on the same key; that is what makes the
  // pool shareable and the selection deterministic. The key is therefore EXACTLY three
  // fixed segments plus the date — no subject, no token, no preference material.
  const key = editorialMixPoolKey(DATE);
  assert.deepEqual(key.split(":"), ["signals", "editorial-mix-pool", "v1", DATE]);
  for (const identifying of ["subject", "token", "user", "japan", "tech", "sub_"]) {
    assert.ok(!key.includes(identifying), `the key carries ${identifying}`);
  }
});

test("1c. a malformed date can never become a key", () => {
  for (const bad of ["", "2026-7-27", "27-07-2026", "2026-07-27T00:00:00Z", "../../etc", "*"]) {
    assert.throws(() => editorialMixPoolKey(bad), /YYYY-MM-DD/);
  }
});

test("1d. the reader asks for exactly one key, and it is the requested date's", async () => {
  const store = storeWith(VALID);
  await read(store);
  assert.deepEqual(store.reads, ["signals:editorial-mix-pool:v1:2026-07-27"]);
});

// ── 2. the happy path ─────────────────────────────────────────────────────────────────

test("2. a valid payload yields selector rows, enriched rows and safe metadata", async () => {
  const result = await read(storeWith(VALID));
  assert.equal(result.ok, true);
  if (!result.ok) return;

  assert.equal(result.candidates.length, MINIMUM_PUBLISHABLE_POOL_SIZE);
  assert.equal(result.enriched.length, MINIMUM_PUBLISHABLE_POOL_SIZE);
  assert.equal(result.metadata.date, DATE);
  assert.equal(result.metadata.candidateCount, MINIMUM_PUBLISHABLE_POOL_SIZE);
  assert.equal(result.metadata.schemaVersion, 1);
  // Identities are exposed as PREFIXES only: a whole identity is a content fingerprint.
  assert.equal(result.metadata.selectorPoolIdentityPrefix.length, 12);
  assert.equal(result.metadata.editorialPoolIdentityPrefix.length, 12);
});

test("2b. selector rows keep stored order and pair with their enriched row", async () => {
  const result = await read(storeWith(VALID));
  assert.equal(result.ok, true);
  if (!result.ok) return;
  const stored = VALID.candidates as unknown as { selector: { id: string } }[];
  assert.deepEqual(
    result.candidates.map((candidate) => candidate.id),
    stored.map((entry) => entry.selector.id),
  );
  assert.deepEqual(
    result.enriched.map((entry) => entry.selector.id),
    stored.map((entry) => entry.selector.id),
  );
});

test("2c. a full-size pool is accepted at the upper bound", async () => {
  assert.equal(await reasonOf(storeWith(poolOf(TARGET_POOL_SIZE))), "ok");
});

// ── 3. the artifact must prove its own date ───────────────────────────────────────────

test("3. an artifact stored under the right key but carrying another date is refused", async () => {
  const wrongDate = poolOf(MINIMUM_PUBLISHABLE_POOL_SIZE, { date: "2026-07-26" });
  const store = storeOf({ [editorialMixPoolKey(DATE)]: bytesOf(wrongDate) });
  assert.equal(await reasonOf(store), "candidate_pool_date_mismatch");
});

test("3b. yesterday's pool is never silently served for today", async () => {
  // Only yesterday's key exists. The reader must NOT fall back to it.
  const store = storeOf({
    [editorialMixPoolKey("2026-07-26")]: bytesOf(poolOf(15, { date: "2026-07-26" })),
  });
  assert.equal(await reasonOf(store, DATE), "candidate_pool_missing");
  assert.deepEqual(store.reads, ["signals:editorial-mix-pool:v1:2026-07-27"]);
});

test("3c. an invalid requested date is rejected before any read", async () => {
  const store = storeWith(VALID);
  assert.equal(await reasonOf(store, "2026-02-30"), "candidate_pool_invalid_date");
  assert.deepEqual(store.reads, [], "an invalid date still reached the store");
});

// ── 4. freshness ──────────────────────────────────────────────────────────────────────

test("4. a stale payload is refused", async () => {
  const stale = poolOf(15, { generatedAt: "2026-07-15T09:00:00Z" });
  assert.equal(await reasonOf(storeWith(stale)), "candidate_pool_stale");
});

test("4b. a payload generated in the future beyond the skew ceiling is refused", async () => {
  const future = poolOf(15, { generatedAt: "2026-07-27T23:59:00Z" });
  assert.equal(await reasonOf(storeWith(future)), "candidate_pool_future_timestamp");
});

test("4c. a small forward skew is tolerated, because clocks disagree", async () => {
  const skewed = poolOf(15, { generatedAt: "2026-07-27T10:00:30Z" });
  assert.equal(await reasonOf(storeWith(skewed)), "ok");
});

test("4d. an unparseable generatedAt is a schema failure, not an accepted pool", async () => {
  const broken = poolOf(15, { generatedAt: "not-a-timestamp" });
  assert.notEqual(await reasonOf(storeWith(broken)), "ok");
});

// ── 5. schema ─────────────────────────────────────────────────────────────────────────

test("5. malformed JSON is refused", async () => {
  const store = storeOf({
    [editorialMixPoolKey(DATE)]: new TextEncoder().encode("{not json"),
  });
  assert.equal(await reasonOf(store), "candidate_pool_invalid_json");
});

test("5b. bytes that are not valid UTF-8 are refused", async () => {
  const store = storeOf({ [editorialMixPoolKey(DATE)]: Uint8Array.from([0xff, 0xfe, 0xfd]) });
  assert.equal(await reasonOf(store), "candidate_pool_invalid_json");
});

test("5c. a structurally wrong artifact is refused", async () => {
  for (const broken of [
    { ...VALID, candidates: "not-an-array" },
    { ...VALID, artifactType: "something-else" },
    { ...VALID, schemaVersion: 99 },
  ]) {
    assert.notEqual(await reasonOf(storeWith(broken as Artifact)), "ok");
  }
});

test("5d. a candidate missing required editorial copy is refused", async () => {
  const broken = JSON.parse(JSON.stringify(VALID)) as Artifact;
  const rows = broken.candidates as unknown as Record<string, Record<string, JsonValue>>[];
  delete rows[3].editorial.summary;
  broken.editorialPoolIdentity = editorialPoolIdentityOf(broken.candidates as JsonValue[]);
  assert.equal(await reasonOf(storeWith(broken)), "candidate_pool_schema_invalid");
});

test("5e. an empty stored value is refused", async () => {
  const store = storeOf({ [editorialMixPoolKey(DATE)]: new Uint8Array(0) });
  assert.equal(await reasonOf(store), "candidate_pool_empty");
});

// ── 6. the count contract ─────────────────────────────────────────────────────────────

test("6. a pool below the publishable minimum is refused", async () => {
  const thin = poolOf(MINIMUM_PUBLISHABLE_POOL_SIZE - 1);
  assert.notEqual(await reasonOf(storeWith(thin)), "ok");
});

test("6b. a pool above the target size is refused", async () => {
  const fat = poolOf(TARGET_POOL_SIZE + 1);
  assert.notEqual(await reasonOf(storeWith(fat)), "ok");
});

test("6c. a declared count that disagrees with the candidates is refused", async () => {
  const lying = poolOf(15);
  lying.candidateCount = 15 - 1;
  lying.selectorPoolIdentity = selectorPoolIdentityOf(lying);
  assert.equal(await reasonOf(storeWith(lying)), "candidate_pool_schema_invalid");
});

// ── 7. both identities ────────────────────────────────────────────────────────────────

test("7. a tampered selector identity is refused", async () => {
  const tampered = poolOf(15);
  tampered.selectorPoolIdentity = "0".repeat(64);
  assert.equal(await reasonOf(storeWith(tampered)), "candidate_pool_identity_mismatch");
});

test("7b. a tampered editorial identity is refused", async () => {
  const tampered = poolOf(15);
  tampered.editorialPoolIdentity = "0".repeat(64);
  assert.equal(await reasonOf(storeWith(tampered)), "candidate_pool_identity_mismatch");
});

test("7c. editing a HEADLINE without re-signing is caught", async () => {
  // The editorial identity covers copy the selector never sees. Without it, an edited
  // headline would pass every selector-side check and reach the reader unnoticed.
  const edited = poolOf(15);
  const rows = edited.candidates as unknown as Record<string, Record<string, JsonValue>>[];
  rows[0].editorial.headline = "SUBSTITUTED HEADLINE";
  assert.equal(await reasonOf(storeWith(edited)), "candidate_pool_identity_mismatch");
});

/**
 * DOCUMENTED LIMIT, not an oversight. `editorialPoolIdentityOf` sorts by selector id before
 * hashing, and `selectorPoolIdentityOf` does the same, so both identities are deliberately
 * order-INDEPENDENT: they answer "is this the same SET of stories", not "in what order were
 * they stored". A reordered pool therefore verifies cleanly.
 *
 * That is safe here because the reader hands the rows to `selectCustomMix`, whose output is
 * a deterministic function of the candidate SET, and because the store is private and
 * written only by the publisher. It is recorded as a test so the property is a decision
 * rather than an accident.
 */
test("7d. identity covers the candidate SET, not its stored order", async () => {
  const reordered = poolOf(15);
  const rows = reordered.candidates as JsonValue[];
  [rows[0], rows[1]] = [rows[1], rows[0]];
  // Re-derived identities still match: the set is unchanged.
  assert.equal(selectorPoolIdentityOf(reordered), reordered.selectorPoolIdentity);
  assert.equal(editorialPoolIdentityOf(rows), reordered.editorialPoolIdentity);
  assert.equal(await reasonOf(storeWith(reordered)), "ok");

  // And the reader passes the rows through in STORED order, without re-sorting them.
  const result = await read(storeWith(reordered));
  assert.equal(result.ok, true);
  if (!result.ok) return;
  assert.deepEqual(
    result.candidates.map((candidate) => candidate.id),
    (rows as unknown as { selector: { id: string } }[]).map((row) => row.selector.id),
  );
});

test("7e. ADDING or REMOVING a candidate is caught", async () => {
  const removed = poolOf(16);
  (removed.candidates as JsonValue[]).pop();
  // candidateCount is now stale too, but identity alone must already reject it.
  assert.notEqual(await reasonOf(storeWith(removed)), "ok");
});

// ── 8. absence and provider failure ───────────────────────────────────────────────────

test("8. a missing key is reported as missing, not as an error", async () => {
  assert.equal(await reasonOf(storeOf({})), "candidate_pool_missing");
});

test("8b. no store at all is reported as not configured", async () => {
  // This is the Preview deployment's shape: no read-only credentials present.
  assert.equal(await reasonOf(null), "candidate_pool_not_configured");
});

test("8c. a timeout is classified as a timeout", async () => {
  const abort = Object.assign(new Error("aborted"), { name: "AbortError" });
  assert.equal(await reasonOf(storeOf({}, { throws: abort })), "candidate_pool_timeout");
});

test("8d. an auth failure and a provider outage both map to one neutral code", async () => {
  for (const message of [
    "WRONGPASS invalid or missing auth token",
    "NOPERM this user has no permissions to run the 'get' command",
    "502 Bad Gateway from https://real-db.upstash.io",
  ]) {
    assert.equal(
      await reasonOf(storeOf({}, { throws: new Error(message) })),
      "candidate_pool_provider_error",
    );
  }
});

test("8e. a provider message never survives into the result", async () => {
  const leaky = new Error("NOPERM token TOKEN-abc123 at https://real-db.upstash.io");
  const result = await read(storeOf({}, { throws: leaky }));
  const text = JSON.stringify(result);
  for (const secret of ["NOPERM", "TOKEN-abc123", "upstash.io"]) {
    assert.ok(!text.includes(secret), `the result leaked ${secret}`);
  }
});

test("8f. an oversized payload is refused rather than parsed", async () => {
  const store = storeOf({ [editorialMixPoolKey(DATE)]: bytesOf(VALID) });
  assert.equal(await reasonOf(null), "candidate_pool_not_configured");
  const result = await read(store, DATE, { maxBytes: 16 });
  assert.equal(result.ok, false);
  if (!result.ok) assert.equal(result.reason, "candidate_pool_too_large");
});

// ── 9. the stored pool cannot be mutated ──────────────────────────────────────────────

test("9. returned candidates are deep-frozen", async () => {
  const result = await read(storeWith(VALID));
  assert.equal(result.ok, true);
  if (!result.ok) return;

  assert.ok(Object.isFrozen(result.candidates[0]));
  assert.ok(Object.isFrozen(result.enriched[0]));
  assert.ok(Object.isFrozen(result.enriched[0].editorial));
  assert.ok(Object.isFrozen(result.enriched[0].editorial.keyTakeaways));
});

test("9b. a downstream write throws instead of silently corrupting the pool", async () => {
  const result = await read(storeWith(VALID));
  assert.equal(result.ok, true);
  if (!result.ok) return;
  // Strict mode: a write to a frozen object throws rather than failing silently.
  assert.throws(() => {
    (result.enriched[0].editorial as { headline: string }).headline = "REWRITTEN";
  });
  assert.throws(() => {
    (result.candidates as unknown as unknown[]).push({});
  });
});

test("9c. two reads of the same bytes are independent objects", async () => {
  const store = storeWith(VALID);
  const first = await read(store);
  const second = await read(store);
  assert.equal(first.ok && second.ok, true);
  if (!first.ok || !second.ok) return;
  assert.notEqual(first.candidates, second.candidates);
  assert.deepEqual(
    first.candidates.map((candidate) => candidate.id),
    second.candidates.map((candidate) => candidate.id),
  );
});

// ── 10. the candidate source adapter ──────────────────────────────────────────────────

test("10. the source returns a bundle on success and null on every failure", async () => {
  const good = createEditorialCandidateSource({ store: storeWith(VALID), now });
  const bundle = await good.loadCandidates(DATE);
  assert.ok(bundle);
  assert.equal(bundle.candidates.length, MINIMUM_PUBLISHABLE_POOL_SIZE);
  assert.equal(bundle.enriched.length, MINIMUM_PUBLISHABLE_POOL_SIZE);

  const empty = createEditorialCandidateSource({ store: storeOf({}), now });
  assert.equal(await empty.loadCandidates(DATE), null);

  const unconfigured = createEditorialCandidateSource({ store: null, now });
  assert.equal(await unconfigured.loadCandidates(DATE), null);
});

test("10b. the caller can observe the precise reason without the module logging", async () => {
  const seen: string[] = [];
  const source = createEditorialCandidateSource({
    store: storeOf({}),
    now,
    onResult: (result) => seen.push(result.ok ? "ok" : result.reason),
  });
  await source.loadCandidates(DATE);
  assert.deepEqual(seen, ["candidate_pool_missing"]);
});

// ── 11. read-only by construction ─────────────────────────────────────────────────────

test("11. the module names no write credential and issues no mutating command", () => {
  const source = readFileSync(join(HERE, "..", "_lib", "editorial-mix-source.ts"), "utf8")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/^\s*\/\/.*$/gm, "");
  for (const forbidden of [
    "KV_REST_API_WRITE_TOKEN",
    "process.env",
    ".put(",
    ".set(",
    ".del(",
    "delete_key",
    "console.",
    "readFileSync",
  ]) {
    assert.ok(!source.includes(forbidden), `the reader references ${forbidden}`);
  }
});

test("11b. the store contract the reader depends on exposes only `get`", async () => {
  // If the reader ever needed more than `get`, this would stop compiling — and a
  // read-only credential would stop being sufficient.
  const store = storeWith(VALID);
  assert.deepEqual(
    Object.keys(store).filter((key) => typeof (store as never)[key] === "function"),
    ["get"],
  );
});

test("11c. the reader is pure: same bytes and same clock give the same answer", async () => {
  const first = await read(storeWith(VALID));
  const second = await read(storeWith(VALID));
  assert.deepEqual(JSON.stringify(first), JSON.stringify(second));
});
