/**
 * Phase 3D-3A.2 — live Python ⇄ TypeScript byte and hash parity for the Mix Pool contract.
 *
 * This suite compares ACTUAL BYTES, not parsed objects. Every case is run through both
 * implementations in the same process invocation and compared as hex, so a single differing
 * byte fails with an offset rather than passing a structural equality check.
 *
 * Per the existing live-parity policy, an unavailable Python FAILS the suite rather than
 * skipping it — a parity test that silently skips proves nothing.
 */

import assert from "node:assert/strict";
import test from "node:test";
import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
  MixPoolNumericError,
  canonicalMixPoolBytes,
  mixPoolArtifactHash,
  mixPoolIdentity,
  normalizeScore,
  serializeMixPoolArtifact,
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
const PIPELINE_DIR = findPipelineDir();

const BASELINE = {
  poolIdentity: "38d9c03d43bd5e94eb0205387b363d9dde795283bbac277d8ec1847d45806a3d",
  canonicalSha256: "41f9bb608da21a1aed737c21fdb5f50fad3c69aea6593468e93d8c49615a626b",
  canonicalLength: 5499,
  serializeSha256: "2ea69a392d31833c0b9f5680bbf189204e1564590304c19df56052a63ca14464",
};

/** Values canonicalized on BOTH sides and compared byte-for-byte. */
const CANONICAL_CASES: [string, JsonValue][] = [
  ["integers", { v: 7 }],
  ["negative integer", { v: -7 }],
  ["fractional", { v: 7.5 }],
  ["six decimals", { v: 11.753704 }],
  ["exponent-floor value", { v: 0.0001 }],
  ["negative exponent-floor", { v: -0.0001 }],
  ["zero", { v: 0 }],
  ["range bounds", { lo: -1000, hi: 1000 }],
  ["nested sorting", { b: { z: 1, a: 2 }, a: [{ y: 1, x: 2 }] }],
  ["array order preserved", { a: [3, 1, 2] }],
  ["unicode", { t: "café 日本 🇯🇵 Ω" }],
  ["quotes and backslashes", { q: 'a"b\\c/d' }],
  ["control characters", { c: "a\nb\tc\rdef" }],
  ["null and booleans", { n: null, t: true, f: false }],
  ["empty containers", { o: {}, a: [] }],
  ["deep nesting", { a: { b: { c: { d: [1, { e: "x" }] } } } }],
];

/** Scores each implementation must accept or reject identically. */
const SCORE_CASES: [string, number | boolean | string | null][] = [
  ["ordinary fractional", 11.753704],
  ["six decimals", 5.744444],
  ["seven decimals", 7.1234567],
  ["integral", 7],
  ["negative integral", -7],
  ["zero", 0],
  ["exponent floor", 0.0001],
  ["below floor", 0.00001],
  ["far below floor", 0.000001],
  ["min bound", -1000],
  ["max bound", 1000],
  ["below range", -1000.5],
  ["above range", 1000.5],
  ["boolean", true],
  ["string", "7.5"],
  ["null", null],
];

type PythonReply = {
  artifact: Record<string, JsonValue>;
  canonicalHex: string;
  canonicalLen: number;
  serializeHex: string;
  poolIdentity: string;
  canonicalCases: Record<string, string>;
  scoreCases: Record<string, { accepted: boolean; rendered: string | null }>;
  rejections: Record<string, boolean>;
};

/**
 * One Python invocation returning every comparison payload. Structured JSON on stdout, no
 * repository writes, no story content logged.
 */
function runPython(): PythonReply {
  const script = `
import json, os, sys
sys.path.insert(0, os.environ["PIPELINE_DIR"])
import mix_pool, mix_pool_schema as S

fixture = os.path.join(os.environ["PIPELINE_DIR"], "fixtures", "mix_pool_scout_candidates.json")
src = json.load(open(fixture, encoding="utf-8"))
pool = mix_pool.build_mix_pool(src, "2026-07-27", "2026-07-27T09:00:00Z",
                               now="2026-07-27T09:00:00Z")
art = S.freeze_artifact(pool, source_input=src, source="offline-fixture",
                        reference_at="2026-07-27T09:00:00Z")

canonical_cases = {}
for name, value in json.loads(os.environ["CANONICAL_CASES"]).items():
    canonical_cases[name] = S.canonical_bytes(value).hex()

score_cases = {}
for name, value in json.loads(os.environ["SCORE_CASES"]).items():
    try:
        normalized = S.normalize_score(value)
        score_cases[name] = {"accepted": True, "rendered": json.dumps(normalized)}
    except S.MixPoolNumericError:
        score_cases[name] = {"accepted": False, "rendered": None}
    except Exception:
        score_cases[name] = {"accepted": False, "rendered": None}

def mutated(fn):
    copy = json.loads(json.dumps(art))
    fn(copy)
    return S.validate_artifact(copy)["valid"]

def dup_id(a): a["candidates"][1]["id"] = a["candidates"][0]["id"]
def dup_url(a): a["candidates"][1]["url"] = a["candidates"][0]["url"]
def bad_schema(a): a["schemaVersion"] = 99
def bad_selector(a): a["selectorVersion"] = 99
def bad_count(a): a["candidateCount"] = 99
def over_precision(a): a["candidates"][0]["baseScore"] = 7.1234567
def forbidden(a): a["candidates"][0]["rawBody"] = "x"

rejections = {
    "valid": S.validate_artifact(art)["valid"],
    "duplicateId": mutated(dup_id),
    "duplicateUrl": mutated(dup_url),
    "schemaVersion": mutated(bad_schema),
    "selectorVersion": mutated(bad_selector),
    "candidateCount": mutated(bad_count),
    "overPrecision": mutated(over_precision),
    "forbiddenKey": mutated(forbidden),
}

sys.stdout.write(json.dumps({
    "artifact": art,
    "canonicalHex": S.canonical_bytes(art).hex(),
    "canonicalLen": len(S.canonical_bytes(art)),
    "serializeHex": S.serialize(art).hex(),
    "poolIdentity": S.pool_identity(art["candidates"]),
    "canonicalCases": canonical_cases,
    "scoreCases": score_cases,
    "rejections": rejections,
}))
`;
  const proc = spawnSync("python3", ["-c", script], {
    encoding: "utf8",
    env: {
      ...process.env,
      PIPELINE_DIR,
      CANONICAL_CASES: JSON.stringify(Object.fromEntries(CANONICAL_CASES)),
      SCORE_CASES: JSON.stringify(Object.fromEntries(SCORE_CASES)),
    },
    maxBuffer: 64 * 1_024 * 1_024,
  });
  // Fail, never skip: a parity suite that skips proves nothing.
  assert.ok(
    existsSync(join(PIPELINE_DIR, "mix_pool_schema.py")),
    `pipeline/mix_pool_schema.py not found at ${PIPELINE_DIR}`,
  );
  assert.equal(proc.status, 0, `python failed: ${(proc.stderr || "").slice(-600)}`);
  return JSON.parse(proc.stdout) as PythonReply;
}

const python = runPython();

/** Report the first differing byte rather than just "not equal". */
function assertHexEqual(actual: string, expected: string, label: string): void {
  if (actual === expected) return;
  let index = 0;
  while (index < Math.min(actual.length, expected.length) && actual[index] === expected[index]) {
    index += 1;
  }
  const byteOffset = Math.floor(index / 2);
  const window = (hex: string): string =>
    Buffer.from(hex.slice(Math.max(0, index - 40), index + 40), "hex").toString("utf8");
  assert.fail(
    `${label}: first divergence at byte ${byteOffset}\n` +
      `  typescript: …${window(actual)}…\n  python    : …${window(expected)}…`,
  );
}

// ── canonical byte parity ─────────────────────────────────────────────────────────────

test("canonical bytes match Python exactly for every representative value", () => {
  for (const [name, value] of CANONICAL_CASES) {
    const actual = canonicalMixPoolBytes(value).toString("hex");
    const expected = python.canonicalCases[name];
    assert.ok(expected, `python produced no result for ${name}`);
    assertHexEqual(actual, expected, `canonical bytes [${name}]`);
  }
  assert.equal(Object.keys(python.canonicalCases).length, CANONICAL_CASES.length);
});

test("the full real artifact canonicalizes to identical bytes", () => {
  const actual = canonicalMixPoolBytes(python.artifact);
  assertHexEqual(actual.toString("hex"), python.canonicalHex, "full artifact canonical bytes");
  assert.equal(actual.length, python.canonicalLen);
});

test("the indent-2 serialization matches Python byte for byte", () => {
  assertHexEqual(
    serializeMixPoolArtifact(python.artifact).toString("hex"),
    python.serializeHex,
    "serialize(artifact)",
  );
});

// ── hash parity ───────────────────────────────────────────────────────────────────────

test("poolIdentity matches live Python and the pinned baseline", () => {
  const actual = mixPoolIdentity(python.artifact.candidates as JsonValue[]);
  assert.equal(actual, python.poolIdentity, "TypeScript poolIdentity differs from Python");
  assert.equal(actual, BASELINE.poolIdentity);
});

test("all four pinned baselines hold across both languages", () => {
  const canonical = canonicalMixPoolBytes(python.artifact);
  const serialized = serializeMixPoolArtifact(python.artifact);

  assert.equal(mixPoolIdentity(python.artifact.candidates as JsonValue[]), BASELINE.poolIdentity);
  assert.equal(mixPoolArtifactHash(python.artifact), BASELINE.canonicalSha256);
  assert.equal(canonical.length, BASELINE.canonicalLength);
  assert.equal(createHash("sha256").update(serialized).digest("hex"), BASELINE.serializeSha256);

  // …and Python independently agrees on the same four.
  assert.equal(python.poolIdentity, BASELINE.poolIdentity);
  assert.equal(
    createHash("sha256").update(Buffer.from(python.canonicalHex, "hex")).digest("hex"),
    BASELINE.canonicalSha256,
  );
  assert.equal(python.canonicalLen, BASELINE.canonicalLength);
  assert.equal(
    createHash("sha256").update(Buffer.from(python.serializeHex, "hex")).digest("hex"),
    BASELINE.serializeSha256,
  );
});

// ── accept / reject parity ────────────────────────────────────────────────────────────

test("every score is accepted or rejected identically, with identical rendering", () => {
  for (const [name, value] of SCORE_CASES) {
    const expected = python.scoreCases[name];
    assert.ok(expected, `python produced no result for score case ${name}`);

    let accepted = true;
    let rendered: string | null = null;
    try {
      rendered = JSON.stringify(normalizeScore(value));
    } catch (error) {
      assert.ok(error instanceof MixPoolNumericError, `${name}: wrong error type`);
      accepted = false;
    }

    assert.equal(
      accepted,
      expected.accepted,
      `${name}: TypeScript ${accepted ? "accepted" : "rejected"} but Python ` +
        `${expected.accepted ? "accepted" : "rejected"} (${JSON.stringify(value)})`,
    );
    if (accepted) {
      assert.equal(rendered, expected.rendered, `${name}: canonical rendering differs`);
    }
  }
  assert.equal(Object.keys(python.scoreCases).length, SCORE_CASES.length);
});

test("artifact validation decisions agree with live Python", () => {
  const mutate = (fn: (a: Record<string, JsonValue>) => void): boolean => {
    const copy = JSON.parse(JSON.stringify(python.artifact)) as Record<string, JsonValue>;
    fn(copy);
    return validateMixPoolArtifact(copy).valid;
  };
  const actual: Record<string, boolean> = {
    valid: validateMixPoolArtifact(python.artifact).valid,
    duplicateId: mutate((a) => {
      const c = a.candidates as { id: string }[];
      c[1].id = c[0].id;
    }),
    duplicateUrl: mutate((a) => {
      const c = a.candidates as { url: string }[];
      c[1].url = c[0].url;
    }),
    schemaVersion: mutate((a) => { a.schemaVersion = 99; }),
    selectorVersion: mutate((a) => { a.selectorVersion = 99; }),
    candidateCount: mutate((a) => { a.candidateCount = 99; }),
    overPrecision: mutate((a) => {
      (a.candidates as Record<string, JsonValue>[])[0].baseScore = 7.1234567;
    }),
    forbiddenKey: mutate((a) => {
      (a.candidates as Record<string, JsonValue>[])[0].rawBody = "x";
    }),
  };

  assert.deepEqual(actual, python.rejections, "validation decisions diverged from Python");
  assert.equal(actual.valid, true, "the real artifact must validate in both languages");
  for (const [name, valid] of Object.entries(actual)) {
    if (name === "valid") continue;
    assert.equal(valid, false, `${name} should have been rejected`);
  }
});
