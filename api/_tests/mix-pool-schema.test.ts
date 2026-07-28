/**
 * Phase 3D-3A.2 — the TypeScript Mix Pool contract.
 *
 * Covers the numeric domain, canonical serialization and the full artifact validator.
 * Byte-level and hash-level agreement with live Python is proven separately in
 * `mix-pool-parity.test.ts`.
 */

import assert from "node:assert/strict";
import test from "node:test";
import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
  BOOLEAN_FIELD_PATHS,
  CANDIDATE_OPTIONAL,
  CANDIDATE_REQUIRED,
  INTEGRAL_FIELD_RANGES,
  MixPoolNumericError,
  NUMERIC_FIELD_KINDS,
  SCORE_MAX,
  SCORE_MIN,
  canonicalMixPoolBytes,
  deepSortKeys,
  fractionalDigits,
  mixPoolArtifactHash,
  mixPoolIdentity,
  normalizeScore,
  parseMixPoolArtifact,
  serializeMixPoolArtifact,
  validateIntegral,
  validateMixPoolArtifact,
  type JsonValue,
} from "../_lib/mix-pool-schema.js";

const HERE = dirname(fileURLToPath(import.meta.url));

function findPipelineDir(): string {
  let current = HERE;
  for (let depth = 0; depth < 8; depth += 1) {
    const candidate = join(current, "pipeline");
    if (existsSync(join(candidate, "mix_pool_schema.py"))) return candidate;
    const parent = dirname(current);
    if (parent === current) break;
    current = parent;
  }
  return join(resolve(HERE, "..", ".."), "pipeline");
}
export const PIPELINE_DIR = findPipelineDir();

export const BASELINE = {
  poolIdentity: "38d9c03d43bd5e94eb0205387b363d9dde795283bbac277d8ec1847d45806a3d",
  canonicalSha256: "41f9bb608da21a1aed737c21fdb5f50fad3c69aea6593468e93d8c49615a626b",
  canonicalLength: 5499,
  serializeSha256: "2ea69a392d31833c0b9f5680bbf189204e1564590304c19df56052a63ca14464",
};

/** Builds the pinned real artifact by running the REAL Python producer. Never writes. */
export function buildRealArtifact(): Record<string, JsonValue> {
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
sys.stdout.write(json.dumps(art))
`;
  const proc = spawnSync("python3", ["-c", script], {
    encoding: "utf8",
    env: { ...process.env, PIPELINE_DIR },
    maxBuffer: 64 * 1_024 * 1_024,
  });
  assert.equal(proc.status, 0, `python producer failed: ${(proc.stderr || "").slice(-400)}`);
  return JSON.parse(proc.stdout) as Record<string, JsonValue>;
}

const artifact = buildRealArtifact();

function rejects(value: unknown, name: string): string {
  try {
    normalizeScore(value);
  } catch (error) {
    assert.ok(error instanceof MixPoolNumericError, `${name}: wrong error type`);
    return (error as Error).message;
  }
  assert.fail(`${name}: ${String(value)} was accepted`);
}

// ── numeric contract: baseScore ───────────────────────────────────────────────────────

test("1-2. fractional scores up to six decimals are accepted", () => {
  assert.equal(normalizeScore(11.753704), 11.753704);
  assert.equal(normalizeScore(5.744444), 5.744444);
  assert.equal(normalizeScore(7.5), 7.5);
});

test("3. more than six decimals is rejected", () => {
  rejects(7.1234567, "over-precision");
});

test("4-5. integral and negative integral scores serialize as integers", () => {
  assert.equal(JSON.stringify(normalizeScore(7)), "7");
  assert.equal(JSON.stringify(normalizeScore(-7)), "-7");
  assert.equal(JSON.stringify(normalizeScore(17)), "17");
});

test("6-7. negative and positive zero both canonicalize to 0", () => {
  assert.equal(JSON.stringify(normalizeScore(-0)), "0");
  assert.equal(JSON.stringify(normalizeScore(0)), "0");
  assert.ok(!Object.is(normalizeScore(-0), -0), "negative zero survived");
});

test("8-10. the exponent-safe floor is enforced", () => {
  assert.equal(JSON.stringify(normalizeScore(0.0001)), "0.0001");
  assert.equal(JSON.stringify(normalizeScore(-0.0001)), "-0.0001");
  rejects(0.00001, "0.00001");
  rejects(0.000001, "0.000001");
});

test("11-14. range bounds are inclusive", () => {
  rejects(SCORE_MIN - 1, "below range");
  rejects(SCORE_MAX + 1, "above range");
  assert.equal(normalizeScore(SCORE_MIN), SCORE_MIN);
  assert.equal(normalizeScore(SCORE_MAX), SCORE_MAX);
});

test("15-19. non-finite, boolean and string inputs are rejected", () => {
  rejects(Number.NaN, "NaN");
  rejects(Number.POSITIVE_INFINITY, "+Infinity");
  rejects(Number.NEGATIVE_INFINITY, "-Infinity");
  rejects(true, "boolean");
  rejects("7.5", "numeric string");
  rejects(null, "null");
  rejects(undefined, "undefined");
});

test("20. the error carries a safe field path and no payload", () => {
  let message = "";
  try {
    normalizeScore(7.1234567, "candidates[3].baseScore");
  } catch (error) {
    message = (error as Error).message;
  }
  assert.match(message, /candidates\[3\]\.baseScore/);
  assert.ok(!message.includes("http"), "a URL leaked into the error");
});

test("21-23. canonical output never carries exponent, -0 or a trailing .0", () => {
  const bytes = canonicalMixPoolBytes(artifact).toString("utf8");
  const scores = (artifact.candidates as JsonValue[]).map((c) =>
    JSON.stringify((c as { baseScore: number }).baseScore),
  );
  for (const rendered of scores) {
    assert.ok(!rendered.toLowerCase().includes("e"), `exponent in ${rendered}`);
    assert.notEqual(rendered, "-0");
    assert.ok(!rendered.endsWith(".0"), `trailing .0 in ${rendered}`);
  }
  assert.ok(!bytes.includes(":-0,"), "negative zero in canonical bytes");
});

test("the decimal-digit rule matches Python's Decimal(str(value)) analogue", () => {
  assert.equal(fractionalDigits(7), 0);
  assert.equal(fractionalDigits(7.5), 1);
  assert.equal(fractionalDigits(7.123456), 6);
  assert.equal(fractionalDigits(7.1234567), 7);
  assert.equal(fractionalDigits(0.0001), 4);
  assert.equal(fractionalDigits(0.1 + 0.2), 17); // 0.30000000000000004 — noise is visible
  assert.equal(fractionalDigits(1e-7), -1); // exponent form
});

// ── integral fields ───────────────────────────────────────────────────────────────────

for (const [field, bounds] of Object.entries(INTEGRAL_FIELD_RANGES)) {
  test(`24-31. ${field}: the integral contract holds`, () => {
    let errors: string[] = [];
    validateIntegral(bounds[0], field, `$.${field}`, errors);
    assert.deepEqual(errors, [], "a valid integer was rejected");

    for (const [value, label] of [
      [1.5, "fractional"],
      [Number.MAX_SAFE_INTEGER + 2, "unsafe integer"],
      [bounds[0] - 1, "below minimum"],
      [bounds[1] + 1, "above maximum"],
      [true, "boolean"],
      ["1", "numeric string"],
      [Number.NaN, "NaN"],
    ] as [unknown, string][]) {
      errors = [];
      validateIntegral(value, field, `$.${field}`, errors);
      assert.equal(errors.length, 1, `${label} was accepted for ${field}`);
      assert.match(errors[0], new RegExp(`\\$\\.${field}`), "field path missing");
    }
  });
}

// ── canonical JSON ────────────────────────────────────────────────────────────────────

test("32-33. key insertion order never affects the bytes, at any depth", () => {
  const a: JsonValue = { b: { z: 1, a: 2 }, a: [{ y: 1, x: 2 }] };
  const b: JsonValue = { a: [{ x: 2, y: 1 }], b: { a: 2, z: 1 } };
  assert.equal(canonicalMixPoolBytes(a).toString("hex"), canonicalMixPoolBytes(b).toString("hex"));
  assert.equal(canonicalMixPoolBytes(a).toString("utf8"), '{"a":[{"x":2,"y":1}],"b":{"a":2,"z":1}}');
});

test("34. array order is preserved, never sorted", () => {
  assert.equal(canonicalMixPoolBytes([3, 1, 2]).toString("utf8"), "[3,1,2]");
  assert.equal(canonicalMixPoolBytes(["b", "a"]).toString("utf8"), '["b","a"]');
});

test("35-38. unicode, escaping and separators", () => {
  assert.equal(canonicalMixPoolBytes({ t: "café 日本" }).toString("utf8"), '{"t":"café 日本"}');
  assert.equal(canonicalMixPoolBytes({ q: 'a"b\\c' }).toString("utf8"), '{"q":"a\\"b\\\\c"}');
  assert.equal(canonicalMixPoolBytes({ c: "a\nb\tc" }).toString("utf8"), '{"c":"a\\nb\\tc"}');
  assert.equal(canonicalMixPoolBytes({ c: "" }).toString("utf8"), '{"c":"\\u0001"}');
  assert.equal(canonicalMixPoolBytes({ a: 1, b: 2 }).toString("utf8"), '{"a":1,"b":2}');
  assert.equal(canonicalMixPoolBytes({ n: null, t: true }).toString("utf8"), '{"n":null,"t":true}');
});

test("39-41. serialization is repeatable, idempotent and locale-independent", () => {
  const first = canonicalMixPoolBytes(artifact);
  assert.equal(first.toString("hex"), canonicalMixPoolBytes(artifact).toString("hex"));
  const resorted = deepSortKeys(artifact as JsonValue);
  assert.equal(canonicalMixPoolBytes(resorted).toString("hex"), first.toString("hex"));
  // A locale-sensitive implementation would differ under a comma decimal separator.
  assert.equal(canonicalMixPoolBytes({ v: 1234.5 }).toString("utf8"), '{"v":1234.5}');
});

test("42-43. pool identity sorts candidates by id; the artifact array is untouched", () => {
  const candidates = artifact.candidates as JsonValue[];
  const shuffled = [...candidates].reverse();
  assert.equal(mixPoolIdentity(shuffled), mixPoolIdentity(candidates));

  // The artifact's own array order is NOT changed by canonicalization.
  const ids = candidates.map((c) => (c as { id: string }).id);
  const roundTripped = JSON.parse(canonicalMixPoolBytes(artifact).toString("utf8")) as {
    candidates: { id: string }[];
  };
  assert.deepEqual(roundTripped.candidates.map((c) => c.id), ids);
});

// ── schema validation ─────────────────────────────────────────────────────────────────

function broken(mutate: (a: Record<string, JsonValue>) => void): ReturnType<typeof validateMixPoolArtifact> {
  const copy = JSON.parse(JSON.stringify(artifact)) as Record<string, JsonValue>;
  mutate(copy);
  return validateMixPoolArtifact(copy);
}

test("44. the real artifact validates", () => {
  const result = validateMixPoolArtifact(artifact);
  assert.equal(result.valid, true, result.errors.join("; ").slice(0, 300));
});

test("45-57. every existing validation rule still rejects", () => {
  const cases: [string, (a: Record<string, JsonValue>) => void][] = [
    ["duplicate id", (a) => { (a.candidates as { id: string }[])[1].id = (a.candidates as { id: string }[])[0].id; }],
    ["duplicate url", (a) => { (a.candidates as { url: string }[])[1].url = (a.candidates as { url: string }[])[0].url; }],
    ["candidateCount mismatch", (a) => { a.candidateCount = 99; }],
    ["schemaVersion", (a) => { a.schemaVersion = 99; }],
    ["selectorVersion", (a) => { a.selectorVersion = 99; }],
    ["generatorVersion", (a) => { (a.provenance as Record<string, JsonValue>).generatorVersion = 99; }],
    ["forbidden key", (a) => { (a.candidates as Record<string, JsonValue>[])[0].rawBody = "x"; }],
    ["local path", (a) => { (a.provenance as Record<string, JsonValue>).source = "/Users/me/pool.json"; }],
    ["noncanonical category", (a) => { (a.candidates as Record<string, JsonValue>[])[0].category = "NOPE"; }],
    ["noncanonical topic", (a) => { (a.candidates as Record<string, JsonValue>[])[0].topics = ["sports"]; }],
    ["missing evidence", (a) => {
      const m = (a.candidates as Record<string, JsonValue>[])[0].regionMemberships as Record<string, JsonValue>[];
      m[0].strength = "primary"; m[0].evidence = [];
    }],
    ["boolean field", (a) => { (a.candidates as Record<string, JsonValue>[])[0].eligible = 1; }],
    ["empty headline", (a) => { (a.candidates as Record<string, JsonValue>[])[0].headline = "  "; }],
    ["quality shape", (a) => { (a.candidates as Record<string, JsonValue>[])[0].quality = "nope"; }],
    ["quality integral", (a) => {
      ((a.candidates as Record<string, JsonValue>[])[0].quality as Record<string, JsonValue>).clusterSize = 1.5;
    }],
    ["unsupported top key", (a) => { a.extra = 1; }],
    ["non-canonical 7.0 score", (a) => {
      (a.candidates as Record<string, JsonValue>[])[0].baseScore = 7.1234567;
    }],
  ];
  for (const [label, mutate] of cases) {
    const result = broken(mutate);
    assert.equal(result.valid, false, `${label} was accepted`);
    assert.ok(result.errors.length > 0);
  }
});

test("58. every permitted numeric field has an explicit contract entry", () => {
  // The residual Python-side risk from 3D-3A.1, enforced here as a shared invariant:
  // a new numeric field cannot enter the hash without being declared.
  const declared = new Set(Object.keys(NUMERIC_FIELD_KINDS));
  const candidate = (artifact.candidates as Record<string, JsonValue>[])[0];

  const numericPaths: string[] = [];
  for (const [key, value] of Object.entries(artifact)) {
    if (typeof value === "number") numericPaths.push(`$.${key}`);
  }
  for (const [key, value] of Object.entries(candidate)) {
    if (typeof value === "number") numericPaths.push(`$.candidates[].${key}`);
  }
  for (const [key, value] of Object.entries(candidate.quality as Record<string, JsonValue>)) {
    if (typeof value === "number") numericPaths.push(`$.candidates[].quality.${key}`);
  }
  for (const [key, value] of Object.entries(artifact.provenance as Record<string, JsonValue>)) {
    if (typeof value === "number") numericPaths.push(`$.provenance.${key}`);
  }

  for (const path of numericPaths) {
    assert.ok(declared.has(path), `undeclared numeric field reaching the hash: ${path}`);
  }
  assert.ok(numericPaths.length >= 6, `only found ${numericPaths.length} numeric fields`);

  // Boolean paths must never be treated as integral.
  for (const path of BOOLEAN_FIELD_PATHS) {
    assert.ok(!declared.has(path), `${path} is a boolean but is declared numeric`);
  }
  // The candidate key lists agree with Python.
  assert.equal(CANDIDATE_REQUIRED.length, 13);
  assert.deepEqual([...CANDIDATE_OPTIONAL], ["quality", "eligible"]);
});

test("parseMixPoolArtifact rejects malformed JSON without throwing", () => {
  const bad = parseMixPoolArtifact("{not json");
  assert.equal(bad.ok, false);
  if (!bad.ok) assert.deepEqual(bad.errors, ["artifact is not valid JSON"]);

  const good = parseMixPoolArtifact(JSON.stringify(artifact));
  assert.equal(good.ok, true);
});

// ── the four pinned baselines ─────────────────────────────────────────────────────────

test("59-62. all four pinned baseline values are reproduced in TypeScript", () => {
  const canonical = canonicalMixPoolBytes(artifact);
  assert.equal(mixPoolIdentity(artifact.candidates as JsonValue[]), BASELINE.poolIdentity);
  assert.equal(mixPoolArtifactHash(artifact), BASELINE.canonicalSha256);
  assert.equal(canonical.length, BASELINE.canonicalLength);
  assert.equal(
    createHash("sha256").update(serializeMixPoolArtifact(artifact)).digest("hex"),
    BASELINE.serializeSha256,
  );
});

test("poolIdentity and the artifact hash are different operations", () => {
  assert.notEqual(
    mixPoolIdentity(artifact.candidates as JsonValue[]),
    mixPoolArtifactHash(artifact),
  );
});

test("the module reads no file, opens no socket and logs nothing", () => {
  const source = readFileSync(join(HERE, "..", "_lib", "mix-pool-schema.ts"), "utf8")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .split("\n")
    .filter((line) => !line.trim().startsWith("//") && !line.trim().startsWith("*"))
    .join("\n");
  for (const needle of [
    "readFileSync", "readFile", "child_process", "spawn", "process.env",
    "fetch(", "console.", "latest.json", "editions/", "mix-pool-source",
  ]) {
    assert.ok(!source.includes(needle), `mix-pool-schema.ts references ${needle}`);
  }
});
