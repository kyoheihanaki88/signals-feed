/**
 * Phase 3D-3C.1 — Editorial Mix Pool schema, feed adapter, and live Python parity.
 *
 * Proves the two things this phase exists to settle: an enriched candidate can be stored
 * without edition-level contamination, and five of them become the exact `SignalsFeed` the
 * iOS client already decodes — with no request-time generation and no Swift model change.
 */

import assert from "node:assert/strict";
import test from "node:test";
import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync, realpathSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
  EDITION_LEVEL_FIELDS,
  EDITORIAL_ARTIFACT_TYPE,
  MINIMUM_PUBLISHABLE_POOL_SIZE,
  TARGET_POOL_SIZE,
  canonicalEditorialBytes,
  editorialPoolIdentityOf,
  extractSelectorCandidates,
  selectorPoolIdentityOf,
  validateEditorialMixPool,
  type EnrichedCandidate,
} from "../_lib/editorial-mix-pool-schema.js";
import {
  EditionAssemblyError,
  FEED_FOCUS,
  FEED_VERSION,
  OPTIONAL_FEED_SIGNAL_FIELDS,
  REQUIRED_FEED_SIGNAL_FIELDS,
  assembleSignalsFeed,
} from "../_lib/editorial-mix-feed.js";
import { mixPoolIdentity, type JsonValue } from "../_lib/mix-pool-schema.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURE_DIR = resolve(HERE, "..", "_fixtures");
function findPipelineDir(): string {
  let current = HERE;
  for (let depth = 0; depth < 8; depth += 1) {
    const candidate = join(current, "pipeline");
    if (existsSync(join(candidate, "editorial_mix_pool_schema.py"))) return candidate;
    const parent = dirname(current);
    if (parent === current) break;
    current = parent;
  }
  return join(resolve(HERE, "..", ".."), "pipeline");
}
const PIPELINE_DIR = findPipelineDir();

/** `signals-ios` sits beside `signals-feed` in both the Mac and sandbox layouts. */
function findSwiftModel(): string | null {
  const relative = join("signals-ios", "Signals", "Models", "SignalsFeed.swift");
  const roots = new Set<string>([resolve(HERE, "..", "..")]);
  try {
    // Resolve through any symlink so a compiled-output run still finds the real tree.
    roots.add(dirname(realpathSync(PIPELINE_DIR)));
  } catch {
    /* the pipeline directory is located separately below */
  }
  for (const root of roots) {
    let current = root;
    for (let depth = 0; depth < 4; depth += 1) {
      const candidate = join(dirname(current), relative);
      if (existsSync(candidate)) return candidate;
      const parent = dirname(current);
      if (parent === current) break;
      current = parent;
    }
  }
  return null;
}



const RAW_POOL_IDENTITY = "38d9c03d43bd5e94eb0205387b363d9dde795283bbac277d8ec1847d45806a3d";
const DATE = "2026-07-27";

const artifact = JSON.parse(
  readFileSync(join(FIXTURE_DIR, "editorial_mix_pool.json"), "utf8"),
) as Record<string, JsonValue>;

function clone(): Record<string, JsonValue> {
  return JSON.parse(JSON.stringify(artifact)) as Record<string, JsonValue>;
}

function candidates(): EnrichedCandidate[] {
  return (clone().candidates as unknown) as EnrichedCandidate[];
}

function five(): EnrichedCandidate[] {
  return candidates().slice(0, 5);
}

function broken(mutate: (a: Record<string, JsonValue>) => void): string[] {
  const copy = clone();
  mutate(copy);
  return validateEditorialMixPool(copy).errors;
}

// ── artifact schema ───────────────────────────────────────────────────────────────────

test("1. the committed enriched fixture validates", () => {
  const result = validateEditorialMixPool(artifact);
  assert.equal(result.valid, true, result.errors.join("; ").slice(0, 400));
  assert.equal(artifact.artifactType, EDITORIAL_ARTIFACT_TYPE);
  assert.ok((artifact.candidates as unknown[]).length >= 5);
});

test("2-6. envelope discriminators and counts are enforced", () => {
  assert.ok(broken((a) => { a.artifactType = "raw-mix-pool"; }).some((e) => e.includes("artifactType")));
  assert.ok(broken((a) => { a.schemaVersion = 99; }).some((e) => e.includes("schemaVersion")));
  assert.ok(broken((a) => { a.selectorVersion = 99; }).some((e) => e.includes("selectorVersion")));
  assert.ok(broken((a) => { a.editorialVersion = 99; }).some((e) => e.includes("editorialVersion")));
  assert.ok(broken((a) => { a.candidateCount = 99; }).some((e) => e.includes("candidateCount")));
  assert.ok(broken((a) => { a.extra = 1; }).some((e) => e.includes("unsupported top-level")));
});

test("7-8. duplicate candidate id and url are rejected", () => {
  assert.ok(broken((a) => {
    const c = a.candidates as { selector: { id: string } }[];
    c[1].selector.id = c[0].selector.id;
  }).some((e) => e.includes("duplicate candidate id")));

  assert.ok(broken((a) => {
    const c = a.candidates as { selector: { url: string } }[];
    c[1].selector.url = c[0].selector.url;
  }).some((e) => e.includes("duplicate candidate url")));
});

test("9-17. missing or invalid story-level data is rejected", () => {
  const cases: [string, (a: Record<string, JsonValue>) => void, string][] = [
    ["missing selector", (a) => { delete (a.candidates as Record<string, JsonValue>[])[0].selector; }, "selector"],
    ["missing editorial", (a) => { delete (a.candidates as Record<string, JsonValue>[])[0].editorial; }, "editorial"],
    ["empty keyTakeaways", (a) => { ed(a).keyTakeaways = []; }, "keyTakeaways"],
    ["empty takeaway", (a) => { (ed(a).keyTakeaways as string[])[0] = "   "; }, "keyTakeaways[0]"],
    ["missing whyItMatters", (a) => { ed(a).whyItMatters = ""; }, "whyItMatters"],
    ["invalid readTime", (a) => { ed(a).readTime = 0; }, "readTime"],
    ["fractional readTime", (a) => { ed(a).readTime = 3.5; }, "readTime"],
    ["http imageURL", (a) => { ed(a).imageURL = "http://example.com/a.jpg"; }, "imageURL"],
    ["local image path", (a) => { ed(a).imageURL = "file:///Users/me/a.jpg"; }, "imageURL"],
    ["invalid originalURL", (a) => { ed(a).originalURL = "not-a-url"; }, "originalURL"],
    ["non-string audioURL", (a) => { ed(a).audioURL = 0; }, "audioURL"],
    ["category mismatch", (a) => { ed(a).category = "CULTURE"; }, "category"],
  ];
  for (const [label, mutate, needle] of cases) {
    const errors = broken(mutate);
    assert.ok(errors.length > 0, `${label} was accepted`);
    assert.ok(errors.some((e) => e.includes(needle)), `${label}: ${errors.slice(0, 2).join("; ")}`);
  }
});

function ed(a: Record<string, JsonValue>): Record<string, JsonValue> {
  return (a.candidates as Record<string, JsonValue>[])[0].editorial as Record<string, JsonValue>;
}

test("18. provider, prompt and body fields are forbidden anywhere", () => {
  for (const key of ["prompt", "rawBody", "articleBody", "sourceText", "providerResponse", "apiKey"]) {
    const errors = broken((a) => {
      (a.candidates as Record<string, JsonValue>[])[0].editorial = {
        ...(ed(a) as object),
        [key]: "x",
      } as JsonValue;
    });
    assert.ok(errors.length > 0, `${key} was accepted`);
  }
});

test("19-21. edition-level fields can never be stored on a candidate", () => {
  for (const field of EDITION_LEVEL_FIELDS) {
    for (const block of ["selector", "editorial"] as const) {
      const errors = broken((a) => {
        const candidate = (a.candidates as Record<string, JsonValue>[])[0];
        (candidate[block] as Record<string, JsonValue>)[field] = 1 as JsonValue;
      });
      assert.ok(
        errors.some((e) => e.includes(`must not carry ${field}`)),
        `${block}.${field} was accepted: ${errors.slice(0, 2).join("; ")}`,
      );
    }
  }
  // …and none is present in the committed fixture.
  for (const candidate of artifact.candidates as Record<string, Record<string, JsonValue>>[]) {
    for (const field of EDITION_LEVEL_FIELDS) {
      assert.ok(!(field in candidate.selector), `fixture selector carries ${field}`);
      assert.ok(!(field in candidate.editorial), `fixture editorial carries ${field}`);
    }
  }
});

test("23-24. raw selector numeric contract and taxonomy are still enforced", () => {
  assert.ok(broken((a) => {
    (a.candidates as Record<string, Record<string, JsonValue>>[])[0].selector.baseScore = 7.1234567;
  }).some((e) => e.startsWith("selector:")), "raw numeric contract not delegated");

  assert.ok(broken((a) => {
    (a.candidates as Record<string, Record<string, JsonValue>>[])[0].selector.category = "NOPE";
  }).some((e) => e.startsWith("selector:")), "raw taxonomy not delegated");
});

// ── identity design ───────────────────────────────────────────────────────────────────

test("25. extracted selector candidates reproduce the RAW pool identity", () => {
  assert.equal(selectorPoolIdentityOf(artifact), RAW_POOL_IDENTITY);
  assert.equal(artifact.selectorPoolIdentity, RAW_POOL_IDENTITY);
  assert.equal(mixPoolIdentity(extractSelectorCandidates(artifact)), RAW_POOL_IDENTITY);
});

test("26. an editorial change moves ONLY the editorial identity", () => {
  const copy = clone();
  ed(copy).headline = "A different headline entirely";

  assert.equal(selectorPoolIdentityOf(copy), RAW_POOL_IDENTITY, "selector identity moved");
  assert.notEqual(
    editorialPoolIdentityOf(copy.candidates as JsonValue[]),
    artifact.editorialPoolIdentity,
    "editorial identity did not move",
  );
});

test("27. a selector change moves BOTH identities", () => {
  const copy = clone();
  (copy.candidates as Record<string, Record<string, JsonValue>>[])[0].selector.baseScore = 3;

  assert.notEqual(selectorPoolIdentityOf(copy), RAW_POOL_IDENTITY);
  assert.notEqual(
    editorialPoolIdentityOf(copy.candidates as JsonValue[]),
    artifact.editorialPoolIdentity,
  );
});

test("28+33. identity ordering is explicit and hashing is stable", () => {
  const shuffled = [...(artifact.candidates as JsonValue[])].reverse();
  assert.equal(editorialPoolIdentityOf(shuffled), artifact.editorialPoolIdentity);
  assert.equal(mixPoolIdentity(extractSelectorCandidates(artifact)), RAW_POOL_IDENTITY);
  assert.equal(
    canonicalEditorialBytes(artifact).toString("hex"),
    canonicalEditorialBytes(artifact).toString("hex"),
  );
});

test("the two identities are distinct values", () => {
  assert.notEqual(artifact.selectorPoolIdentity, artifact.editorialPoolIdentity);
});

// ── feed adapter ──────────────────────────────────────────────────────────────────────

test("34-44. five candidates become the exact SignalsFeed shape", () => {
  const feed = assembleSignalsFeed(DATE, five());

  assert.equal(feed.date, DATE);
  assert.equal(feed.focus, FEED_FOCUS);
  assert.equal(feed.focus, "MIXED");
  assert.equal(feed.version, FEED_VERSION);
  assert.equal(feed.version, 1);
  assert.equal(feed.signals.length, 5);
  assert.deepEqual(Object.keys(feed).sort(), ["date", "focus", "signals", "version"]);

  assert.deepEqual(feed.signals.map((s) => s.number), [1, 2, 3, 4, 5]);
  assert.deepEqual(feed.signals.map((s) => s.importance), [1, 2, 3, 4, 5]);
  assert.deepEqual(feed.signals.map((s) => s.lead), [true, false, false, false, false]);
  assert.equal(feed.signals.filter((s) => s.lead).length, 1);

  // Selector order is preserved verbatim.
  assert.deepEqual(
    feed.signals.map((s) => s.headline),
    five().map((c) => c.editorial.headline),
  );
});

test("35-36. anything other than exactly five is rejected", () => {
  for (const count of [0, 1, 4, 6, 10]) {
    assert.throws(
      () => assembleSignalsFeed(DATE, candidates().slice(0, count)),
      EditionAssemblyError,
      `${count} candidates were accepted`,
    );
  }
});

test("45-47. a malformed date or duplicate selection is rejected", () => {
  for (const bad of ["2026-7-27", "26-07-27", "2026-13-01", "2026-02-30", ""]) {
    assert.throws(() => assembleSignalsFeed(bad, five()), EditionAssemblyError, bad);
  }

  const duped = five();
  duped[1] = JSON.parse(JSON.stringify(duped[0])) as EnrichedCandidate;
  assert.throws(() => assembleSignalsFeed(DATE, duped), EditionAssemblyError);
});

test("48. every non-optional FeedSignal field is present and correctly typed", () => {
  const feed = assembleSignalsFeed(DATE, five());
  for (const signal of feed.signals) {
    for (const field of REQUIRED_FEED_SIGNAL_FIELDS) {
      assert.ok(field in signal, `missing required field ${field}`);
      assert.notEqual((signal as Record<string, unknown>)[field], undefined);
    }
    assert.equal(typeof signal.number, "number");
    assert.equal(typeof signal.lead, "boolean");
    assert.equal(typeof signal.readTime, "number");
    assert.ok(Number.isSafeInteger(signal.readTime));
    assert.ok(Array.isArray(signal.keyTakeaways) && signal.keyTakeaways.length > 0);
    assert.ok(signal.imageURL.startsWith("https://"));
    assert.ok(signal.originalURL.startsWith("https://"));
    assert.equal(typeof signal.audioURL, "string");
  }
});

test("49-50. no selector internals or artifact identities leak into the feed", () => {
  const feed = assembleSignalsFeed(DATE, five());
  const serialized = JSON.stringify(feed);
  for (const leak of [
    "baseScore", "underlyingStoryIdentity", "topicFingerprint", "regionMemberships",
    "sourceReliability", "selectorPoolIdentity", "editorialPoolIdentity", "quality",
    "eligible", "poolIdentity", "selector",
  ]) {
    assert.ok(!serialized.includes(leak), `${leak} leaked into the feed`);
  }
  const allowed = new Set<string>([...REQUIRED_FEED_SIGNAL_FIELDS, ...OPTIONAL_FEED_SIGNAL_FIELDS]);
  for (const signal of feed.signals) {
    for (const key of Object.keys(signal)) {
      assert.ok(allowed.has(key), `unexpected feed field: ${key}`);
    }
  }
});

test("51-52. inputs are not mutated and repeated calls are byte-equivalent", () => {
  const input = five();
  const snapshot = JSON.stringify(input);
  const first = assembleSignalsFeed(DATE, input);
  const second = assembleSignalsFeed(DATE, input);

  assert.equal(JSON.stringify(input), snapshot, "the adapter mutated its input");
  assert.equal(JSON.stringify(first), JSON.stringify(second));
  assert.ok(Object.isFrozen(first), "the feed should be immutable");
  assert.ok(Object.isFrozen(first.signals[0]));
});

// ── client compatibility ──────────────────────────────────────────────────────────────

test("53+56+58. the transformed feed matches the live static feed's structure exactly", () => {
  const feed = assembleSignalsFeed(DATE, five());
  const live = JSON.parse(readFileSync(resolve(HERE, "..", "..", "latest.json"), "utf8")) as {
    signals: Record<string, unknown>[];
  };

  // Every field we emit exists in the live feed — no invented keys.
  const liveKeys = new Set(Object.keys(live.signals[0]));
  for (const key of Object.keys(feed.signals[0])) {
    assert.ok(liveKeys.has(key), `field ${key} is not present in the live feed`);
  }
  // Every field the live feed marks required is present in ours.
  for (const field of REQUIRED_FEED_SIGNAL_FIELDS) {
    assert.ok(liveKeys.has(field) && field in feed.signals[0]);
  }
  // v1 compatibility: the live feed carries "" for audioURL, and so do we.
  assert.equal(live.signals[0].audioURL, "");
  assert.equal(feed.signals[0].audioURL, "");
});

test("54-55+57. the real Swift model accepts this shape (structural verification)", () => {
  // Swift cannot be compiled in this environment, so the model's own Codable declarations
  // are parsed and every non-optional property is checked against the emitted feed.
  const modelPath = findSwiftModel();
  assert.ok(modelPath, "SignalsFeed.swift not found beside the feed repository");
  const swift = readFileSync(modelPath, "utf8");
  const feed = assembleSignalsFeed(DATE, five());

  const block = (name: string): string =>
    swift.slice(swift.indexOf(`struct ${name}`), swift.indexOf("}", swift.indexOf(`struct ${name}`)));

  const parse = (source: string): { name: string; optional: boolean }[] =>
    [...source.matchAll(/let\s+(\w+)\s*:\s*([^\n/]+)/g)].map((m) => ({
      name: m[1],
      optional: m[2].trim().endsWith("?"),
    }));

  const feedFields = parse(block("SignalsFeed"));
  for (const field of feedFields.filter((f) => !f.optional)) {
    assert.ok(field.name in feed, `SignalsFeed requires ${field.name}`);
  }

  const signalFields = parse(block("FeedSignal"));
  const required = signalFields.filter((f) => !f.optional).map((f) => f.name);
  assert.ok(required.length >= 10, `parsed only ${required.length} required fields`);
  for (const name of required) {
    assert.ok(name in feed.signals[0], `FeedSignal requires ${name}, which we do not emit`);
  }
  // Listen and localized must be OPTIONAL for omission to be legal.
  for (const name of ["listen", "localized"]) {
    const field = signalFields.find((f) => f.name === name);
    assert.ok(field?.optional, `${name} must be optional for Custom Mix to omit it`);
    assert.ok(!(name in feed.signals[0]));
  }
});

// ── live Python parity ────────────────────────────────────────────────────────────────

type PythonReply = {
  canonicalHex: string;
  selectorPoolIdentity: string;
  editorialPoolIdentity: string;
  valid: boolean;
  rejections: Record<string, boolean>;
};

function runPython(): PythonReply {
  const script = `
import json, os, sys
sys.path.insert(0, os.environ["PIPELINE_DIR"])
import mix_pool_schema as S, editorial_mix_pool_schema as E
art = json.loads(os.environ["ARTIFACT"])

def mutated(fn):
    copy = json.loads(json.dumps(art))
    fn(copy)
    return E.validate_editorial_mix_pool(copy)["valid"]

def stored_number(a): a["candidates"][0]["editorial"]["number"] = 1
def dup_id(a): a["candidates"][1]["selector"]["id"] = a["candidates"][0]["selector"]["id"]
def bad_type(a): a["artifactType"] = "raw-mix-pool"
def bad_read(a): a["candidates"][0]["editorial"]["readTime"] = 0
def empty_take(a): a["candidates"][0]["editorial"]["keyTakeaways"] = []
def bad_numeric(a): a["candidates"][0]["selector"]["baseScore"] = 7.1234567

sys.stdout.write(json.dumps({
  "canonicalHex": S.canonical_bytes(art).hex(),
  "selectorPoolIdentity": E.selector_pool_identity(art),
  "editorialPoolIdentity": E.editorial_pool_identity(art["candidates"]),
  "valid": E.validate_editorial_mix_pool(art)["valid"],
  "rejections": {
    "storedNumber": mutated(stored_number),
    "duplicateId": mutated(dup_id),
    "artifactType": mutated(bad_type),
    "readTime": mutated(bad_read),
    "emptyTakeaways": mutated(empty_take),
    "rawNumeric": mutated(bad_numeric),
  },
}))
`;
  const proc = spawnSync("python3", ["-c", script], {
    encoding: "utf8",
    env: { ...process.env, PIPELINE_DIR, ARTIFACT: JSON.stringify(artifact) },
    maxBuffer: 64 * 1_024 * 1_024,
  });
  assert.ok(existsSync(join(PIPELINE_DIR, "editorial_mix_pool_schema.py")), "python schema missing");
  assert.equal(proc.status, 0, `python failed: ${(proc.stderr || "").slice(-600)}`);
  return JSON.parse(proc.stdout) as PythonReply;
}

const python = runPython();

test("29+32. canonical bytes match Python exactly, including Unicode and escaping", () => {
  const actual = canonicalEditorialBytes(artifact).toString("hex");
  if (actual !== python.canonicalHex) {
    let i = 0;
    while (i < Math.min(actual.length, python.canonicalHex.length) && actual[i] === python.canonicalHex[i]) i += 1;
    assert.fail(`canonical bytes diverge at byte ${Math.floor(i / 2)}`);
  }
  assert.equal(actual, python.canonicalHex);
  // The fixture deliberately carries Unicode and escaping.
  const text = canonicalEditorialBytes(artifact).toString("utf8");
  assert.ok(text.includes("東京"), "fixture lost its Unicode coverage");
  assert.ok(text.includes('\\"'), "fixture lost its quote-escaping coverage");
});

test("30-31. both identities match live Python", () => {
  assert.equal(selectorPoolIdentityOf(artifact), python.selectorPoolIdentity);
  assert.equal(editorialPoolIdentityOf(artifact.candidates as JsonValue[]), python.editorialPoolIdentity);
  assert.equal(python.selectorPoolIdentity, RAW_POOL_IDENTITY);
  assert.equal(
    createHash("sha256").update(canonicalEditorialBytes(artifact)).digest("hex").length,
    64,
  );
});

test("38-39. Python and TypeScript accept and reject the same artifacts", () => {
  assert.equal(python.valid, true);
  assert.equal(validateEditorialMixPool(artifact).valid, true);

  const actual: Record<string, boolean> = {
    storedNumber: validateEditorialMixPool(
      mutate((a) => { ed(a).number = 1 as JsonValue; }),
    ).valid,
    duplicateId: validateEditorialMixPool(
      mutate((a) => {
        const c = a.candidates as { selector: { id: string } }[];
        c[1].selector.id = c[0].selector.id;
      }),
    ).valid,
    artifactType: validateEditorialMixPool(mutate((a) => { a.artifactType = "raw-mix-pool"; })).valid,
    readTime: validateEditorialMixPool(mutate((a) => { ed(a).readTime = 0; })).valid,
    emptyTakeaways: validateEditorialMixPool(mutate((a) => { ed(a).keyTakeaways = []; })).valid,
    rawNumeric: validateEditorialMixPool(
      mutate((a) => {
        (a.candidates as Record<string, Record<string, JsonValue>>[])[0].selector.baseScore = 7.1234567;
      }),
    ).valid,
  };
  assert.deepEqual(actual, python.rejections, "accept/reject decisions diverged from Python");
  for (const [name, valid] of Object.entries(actual)) {
    assert.equal(valid, false, `${name} should be rejected in both languages`);
  }
});

function mutate(fn: (a: Record<string, JsonValue>) => void): Record<string, JsonValue> {
  const copy = clone();
  fn(copy);
  return copy;
}

// ── boundaries ────────────────────────────────────────────────────────────────────────

test("61-64. the adapter reaches the client only through the edition route", () => {
  // Phase 3E-1: `edition.ts` DOES import the adapter now — that is the connection. What
  // must stay true is that nothing else does, so feed assembly has exactly one caller.
  const route = readFileSync(join(HERE, "..", "edition.ts"), "utf8");
  assert.ok(route.includes("editorial-mix-feed.js"));
  for (const name of ["auth/exchange.ts", "_lib/vercel-runtime.ts", "_lib/custom-mix-selector.ts"]) {
    assert.ok(!readFileSync(join(HERE, "..", name), "utf8").includes("editorial-mix-feed.js"), name);
  }
});

test("the adapter and schema touch no file, network, environment or clock", () => {
  for (const relative of ["editorial-mix-pool-schema.ts", "editorial-mix-feed.ts"]) {
    const source = readFileSync(join(HERE, "..", "_lib", relative), "utf8")
      .replace(/\/\*[\s\S]*?\*\//g, "")
      .split("\n")
      .filter((line) => !line.trim().startsWith("//") && !line.trim().startsWith("*"))
      .join("\n");
    for (const needle of [
      "readFileSync", "child_process", "spawn", "process.env", "fetch(", "console.",
      "Date.now", "Math.random",
    ]) {
      assert.ok(!source.includes(needle), `${relative} references ${needle}`);
    }
  }
});

test("the future pipeline targets are recorded beside the contract", () => {
  assert.equal(TARGET_POOL_SIZE, 20);
  assert.equal(MINIMUM_PUBLISHABLE_POOL_SIZE, 15);
});
