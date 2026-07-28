/**
 * Phase 3D-1.5 — cross-language parity for the editorial duplicate guard.
 *
 * Covers required tests L (live Python parity over all pair fixtures), N (the
 * editorial-only duplicate selector case) and Q (no API connection introduced).
 *
 * Same two-layer design as the selector parity suite: committed Python-generated goldens
 * are checked on every run, and when `python3` is available the Python implementation is
 * executed and compared directly, which also proves the goldens have not drifted. No test
 * ever rewrites a golden file.
 */

import assert from "node:assert/strict";
import test from "node:test";
import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { sameUnderlyingStory } from "../_lib/editorial-duplicate-guard.js";
import { storyIdentity } from "../_lib/editorial-story-identity.js";
import { selectCustomMix } from "../_lib/custom-mix-selector.js";
import type { MixCandidate, MixSelectionResult } from "../_lib/custom-mix-types.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURE_DIR = resolve(HERE, "..", "_fixtures");

function findPipelineDir(): string {
  let current = HERE;
  for (let depth = 0; depth < 8; depth += 1) {
    const candidate = join(current, "pipeline");
    if (existsSync(join(candidate, "editorial.py"))) return candidate;
    const parent = dirname(current);
    if (parent === current) break;
    current = parent;
  }
  return join(resolve(HERE, "..", ".."), "pipeline");
}
const PIPELINE_DIR = findPipelineDir();

type Side = { title: string; snippet: string };
type Pair = { id: string; intent: string; a: Side; b: Side };
type Decision = { duplicate: boolean; reason: string; matchedRule: string };
type GoldenPair = {
  id: string;
  identityA: unknown;
  identityB: unknown;
  forward: Decision;
  reversed: Decision;
};

const casesBytes = readFileSync(join(FIXTURE_DIR, "editorial_duplicate_cases.json"));
const cases = JSON.parse(casesBytes.toString("utf8")) as { pairs: Pair[] };
const golden = JSON.parse(
  readFileSync(join(FIXTURE_DIR, "editorial_duplicate_golden_results.json"), "utf8"),
) as { casesSha256: string; pairs: GoldenPair[] };

const selectorCaseBytes = readFileSync(
  join(FIXTURE_DIR, "custom_mix_editorial_duplicate_case.json"),
);
const selectorCase = JSON.parse(selectorCaseBytes.toString("utf8")) as {
  date: string;
  candidates: MixCandidate[];
};
const selectorGolden = JSON.parse(
  readFileSync(join(FIXTURE_DIR, "custom_mix_editorial_duplicate_golden.json"), "utf8"),
) as {
  fixtureSha256: string;
  date: string;
  input: { regions: string[]; topics: string[]; size: number; selectorVersion: number };
  result: MixSelectionResult;
};

function normalize(value: unknown): unknown {
  return JSON.parse(JSON.stringify(value));
}

function evaluate(p: Pair): { identityA: unknown; identityB: unknown; forward: Decision; reversed: Decision } {
  const ia = storyIdentity(p.a);
  const ib = storyIdentity(p.b);
  return {
    identityA: ia,
    identityB: ib,
    forward: sameUnderlyingStory(ia, ib),
    reversed: sameUnderlyingStory(ib, ia),
  };
}

// ── fixture integrity ─────────────────────────────────────────────────────────────────

test("the golden results were generated from THIS pair fixture", () => {
  const hash = createHash("sha256").update(casesBytes).digest("hex");
  assert.equal(hash, golden.casesSha256, "the pair fixture and its goldens have diverged");
  assert.equal(golden.pairs.length, cases.pairs.length);
});

test("the selector goldens were generated from THIS selector fixture", () => {
  const hash = createHash("sha256").update(selectorCaseBytes).digest("hex");
  assert.equal(hash, selectorGolden.fixtureSha256);
});

test("no golden file records a wall-clock timestamp", () => {
  for (const serialized of [JSON.stringify(golden), JSON.stringify(selectorGolden)]) {
    assert.ok(!/"(generatedAt|timestamp|createdAt|ranAt)"/.test(serialized));
  }
});

// ── committed-golden parity ───────────────────────────────────────────────────────────

test("every pair matches the committed Python golden exactly, in both directions", () => {
  for (const p of cases.pairs) {
    const expected = golden.pairs.find((g) => g.id === p.id);
    assert.ok(expected, `no golden for ${p.id}`);
    const actual = normalize(evaluate(p)) as GoldenPair;

    assert.deepEqual(actual.identityA, expected.identityA, `${p.id}: identity A`);
    assert.deepEqual(actual.identityB, expected.identityB, `${p.id}: identity B`);
    assert.deepEqual(actual.forward, expected.forward, `${p.id}: forward decision`);
    assert.deepEqual(actual.reversed, expected.reversed, `${p.id}: reversed decision`);
  }
});

// ── L. live Python parity ─────────────────────────────────────────────────────────────

function runPythonPairs():
  | { ok: true; pairs: Record<string, GoldenPair> }
  | { ok: false; reason: string } {
  if (!existsSync(join(PIPELINE_DIR, "editorial.py"))) {
    return { ok: false, reason: "pipeline/editorial.py is not present" };
  }
  const script = `
import json, os, sys
sys.path.insert(0, os.environ["PIPELINE_DIR"])
from editorial import story_identity, same_underlying_story

pairs = json.loads(os.environ["PAIRS"])

def ident(side):
    i = story_identity(side.get("title", ""), side.get("snippet", ""))
    if i is None:
        return None
    return {"brand": i["brand"], "productFamily": i["product_family"],
            "coveredFamilies": sorted(i["covered_families"]), "launchEvent": i["event"],
            "eventFamily": i["event_family"], "isRoundup": bool(i["is_roundup"]),
            "isProductStory": bool(i["is_product_story"])}

out = {}
for p in pairs:
    ia = story_identity(p["a"].get("title", ""), p["a"].get("snippet", ""))
    ib = story_identity(p["b"].get("title", ""), p["b"].get("snippet", ""))
    d, r, rule = same_underlying_story(ia, ib)
    rd, rr, rrule = same_underlying_story(ib, ia)
    out[p["id"]] = {"id": p["id"], "identityA": ident(p["a"]), "identityB": ident(p["b"]),
                    "forward": {"duplicate": bool(d), "reason": r, "matchedRule": rule},
                    "reversed": {"duplicate": bool(rd), "reason": rr, "matchedRule": rrule}}
sys.stdout.write(json.dumps(out))
`;
  const proc = spawnSync("python3", ["-c", script], {
    encoding: "utf8",
    env: { ...process.env, PIPELINE_DIR, PAIRS: JSON.stringify(cases.pairs) },
    maxBuffer: 32 * 1_024 * 1_024,
  });
  if (proc.error) return { ok: false, reason: `python3 unavailable: ${proc.error.message}` };
  if (proc.status !== 0) {
    return { ok: false, reason: `python exited ${proc.status}: ${(proc.stderr || "").slice(-400)}` };
  }
  return { ok: true, pairs: JSON.parse(proc.stdout) as Record<string, GoldenPair> };
}

test("L. the LIVE Python guard and the TypeScript port agree on every pair", (t) => {
  const python = runPythonPairs();
  if (!python.ok) {
    t.skip(`live Python comparison skipped — ${python.reason}`);
    return;
  }

  for (const p of cases.pairs) {
    const expected: GoldenPair | undefined = python.pairs[p.id];
    assert.ok(expected, `python returned no result for ${p.id}`);
    const actual = normalize(evaluate(p)) as GoldenPair;

    assert.deepEqual(actual.identityA, expected.identityA, `${p.id}: identity A vs live Python`);
    assert.deepEqual(actual.identityB, expected.identityB, `${p.id}: identity B vs live Python`);
    assert.deepEqual(actual.forward, expected.forward, `${p.id}: forward vs live Python`);
    assert.deepEqual(actual.reversed, expected.reversed, `${p.id}: reversed vs live Python`);

    // …and the committed golden still describes what Python does today.
    const committed = golden.pairs.find((g) => g.id === p.id);
    assert.deepEqual(
      { identityA: expected.identityA, identityB: expected.identityB, forward: expected.forward, reversed: expected.reversed },
      { identityA: committed?.identityA, identityB: committed?.identityB, forward: committed?.forward, reversed: committed?.reversed },
      `${p.id}: the committed golden has drifted from live Python`,
    );
  }
});

// ── N. the editorial-only duplicate selector case ─────────────────────────────────────

test("N. the selector rejects a duplicate that ONLY the editorial guard can see", () => {
  const roundup = selectorCase.candidates.find((c) => c.id === "jp-samsung-roundup");
  const handsOn = selectorCase.candidates.find((c) => c.id === "jp-samsung-handson");
  assert.ok(roundup && handsOn);
  // The first two duplicate gates must both PASS these two, or the case proves nothing.
  assert.notEqual(roundup.underlyingStoryIdentity, handsOn.underlyingStoryIdentity);
  assert.notEqual(roundup.url, handsOn.url);

  const result = selectCustomMix({
    candidates: selectorCase.candidates.map((c) => structuredClone(c)),
    date: selectorCase.date,
    regions: selectorGolden.input.regions,
    topics: selectorGolden.input.topics,
    size: selectorGolden.input.size,
    selectorVersion: selectorGolden.input.selectorVersion,
  });

  assert.ok(result.selectedIds.includes("jp-samsung-roundup"));
  assert.ok(!result.selectedIds.includes("jp-samsung-handson"));

  const log = result.candidateLogs.find((row) => row.id === "jp-samsung-handson");
  assert.equal(
    log?.rejectionReason,
    "existing duplicate guard: same-product-family: " +
      "same brand (samsung) + same product family (galaxy z fold)",
  );

  // Full parity with the Python-generated golden for this scenario.
  assert.deepEqual(normalize(result), selectorGolden.result);
});

test("N2. with the guard disabled, BOTH Samsung stories survive — the case is load-bearing", () => {
  const result = selectCustomMix({
    candidates: selectorCase.candidates.map((c) => structuredClone(c)),
    date: selectorCase.date,
    regions: ["japan"],
    editorialDuplicateGuard: () => ({ duplicate: false }),
  });
  assert.ok(result.selectedIds.includes("jp-samsung-roundup"));
  assert.ok(
    result.selectedIds.includes("jp-samsung-handson"),
    "the no-op guard should let the duplicate through; otherwise this case proves nothing",
  );
});

test("N3. the LIVE Python selector rejects the same candidate for the same reason", (t) => {
  if (!existsSync(join(PIPELINE_DIR, "custom_mix_selector.py"))) {
    t.skip("live Python comparison skipped — pipeline/custom_mix_selector.py is not present");
    return;
  }
  const script = `
import json, os, sys
sys.path.insert(0, os.environ["PIPELINE_DIR"])
from custom_mix_selector import select_custom_mix
payload = json.loads(os.environ["PAYLOAD"])
result = select_custom_mix(payload["candidates"], payload["date"], payload["regions"],
                           payload["topics"], payload["size"], payload["selectorVersion"])
sys.stdout.write(json.dumps(result))
`;
  const proc = spawnSync("python3", ["-c", script], {
    encoding: "utf8",
    env: {
      ...process.env,
      PIPELINE_DIR,
      PAYLOAD: JSON.stringify({
        candidates: selectorCase.candidates,
        date: selectorCase.date,
        ...selectorGolden.input,
      }),
    },
    maxBuffer: 32 * 1_024 * 1_024,
  });
  if (proc.error || proc.status !== 0) {
    t.skip(`live Python comparison skipped — python3 unavailable (${proc.status})`);
    return;
  }

  const pythonResult = JSON.parse(proc.stdout) as MixSelectionResult;
  const tsResult = selectCustomMix({
    candidates: selectorCase.candidates.map((c) => structuredClone(c)),
    date: selectorCase.date,
    regions: selectorGolden.input.regions,
    topics: selectorGolden.input.topics,
    size: selectorGolden.input.size,
    selectorVersion: selectorGolden.input.selectorVersion,
  });

  assert.deepEqual(normalize(tsResult), normalize(pythonResult));
  assert.ok(!pythonResult.selectedIds.includes("jp-samsung-handson"));
});

// ── Q. boundaries ─────────────────────────────────────────────────────────────────────

function stripComments(source: string): string {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .split("\n")
    .filter((line) => {
      const trimmed = line.trim();
      return !trimmed.startsWith("//") && !trimmed.startsWith("*");
    })
    .join("\n");
}

test("Q. no editorial production module reads a file, spawns a process or logs", () => {
  for (const relative of ["editorial-story-identity.ts", "editorial-duplicate-guard.ts"]) {
    const source = stripComments(readFileSync(join(HERE, "..", "_lib", relative), "utf8"));
    for (const needle of [
      "readFileSync",
      "readFile",
      "child_process",
      "spawn",
      "execFile",
      "process.env",
      "pipeline/",
      "latest.json",
      "editions/",
      "mix-pool",
      "fetch(",
      "console.",
    ]) {
      assert.ok(!source.includes(needle), `${relative} references ${needle}`);
    }
  }
});

test("Q2. the selector still is not wired into any API route", () => {
  for (const relative of ["edition.ts", "_lib/runtime-factory.ts", "_lib/vercel-runtime.ts"]) {
    const source = readFileSync(join(HERE, "..", relative), "utf8");
    for (const needle of ["custom-mix-selector", "editorial-duplicate-guard", "editorial-story-identity"]) {
      assert.ok(!source.includes(needle), `${relative} imports ${needle}`);
    }
  }
  assert.ok(readFileSync(join(HERE, "..", "edition.ts"), "utf8").includes("selector_not_connected"));
});

test("Q3. the selector's default guard is the REAL editorial guard, not the no-op", () => {
  const source = readFileSync(join(HERE, "..", "_lib", "custom-mix-selector.ts"), "utf8");
  assert.ok(
    /editorialDuplicateGuard: guard = productionEditorialDuplicateGuard/.test(source),
    "the production selector no longer defaults to the real guard",
  );
  assert.ok(!source.includes("= noEditorialDuplicateGuard"));
});
