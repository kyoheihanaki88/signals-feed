/**
 * Phase 3D-1 — selector behaviour.
 *
 * Covers required tests C through P. These are the SAME assertions Python's
 * `test_custom_mix_selector.py` makes, restated against the TypeScript port, plus the
 * eligibility and duplicate-ordering cases the port could plausibly get wrong.
 *
 * The candidate fixture is read from `api/_fixtures/`, never from `pipeline/` — production
 * code must not depend on the Python tree, and neither should the unit tests that guard it.
 */

import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
  canonicalUrl,
  isEligible,
  selectCustomMix,
  topicAdjustment,
} from "../_lib/custom-mix-selector.js";
import { productionEditorialDuplicateGuard } from "../_lib/editorial-duplicate-guard.js";
import {
  MixSelectionError,
  UnsupportedMixValue,
  noEditorialDuplicateGuard,
  type EditorialDuplicateGuard,
  type MixCandidate,
} from "../_lib/custom-mix-types.js";

const FIXTURE_DIR = resolve(dirname(fileURLToPath(import.meta.url)), "..", "_fixtures");
const DATE = "2026-07-27";

function loadCandidates(): MixCandidate[] {
  const raw = readFileSync(join(FIXTURE_DIR, "custom_mix_candidates.json"), "utf8");
  return (JSON.parse(raw) as { candidates: MixCandidate[] }).candidates;
}

const ALL = loadCandidates();
const BY_ID = new Map(ALL.map((c) => [c.id, c]));

function subset(ids: string[]): MixCandidate[] {
  return ALL.filter((c) => ids.includes(c.id)).map((c) => structuredClone(c));
}

function select(
  candidates: MixCandidate[] = ALL.map((c) => structuredClone(c)),
  regions: unknown[] = ["japan"],
  topics: unknown[] = [],
): ReturnType<typeof selectCustomMix> {
  return selectCustomMix({ candidates, date: DATE, regions, topics });
}

function isPrimary(candidate: MixCandidate, region: string): boolean {
  return (candidate.regionMemberships ?? []).some(
    (m) => (m.region ?? m.id) === region && m.strength === "primary",
  );
}

function logFor(result: ReturnType<typeof selectCustomMix>, id: string) {
  const log = result.candidateLogs.find((row) => row.id === id);
  assert.ok(log, `no log for ${id}`);
  return log;
}

// ── C. Japan 5/5 ──────────────────────────────────────────────────────────────────────

test("C. Japan-only selects five Japan-primary stories with no fallback", () => {
  const result = select();
  assert.equal(result.selectedIds.length, 5);
  for (const id of result.selectedIds) {
    assert.ok(isPrimary(BY_ID.get(id) as MixCandidate, "japan"), `${id} is not Japan-primary`);
  }
  assert.equal(result.metadata.selectedRegionStories, 5);
  assert.equal(result.metadata.fallbackSlots, 0);
  assert.equal(result.metadata.fallbackReason, null);
  assert.equal(result.metadata.shortage, false);
  assert.equal(result.metadata.unfilledSlots, 0);
  assert.deepEqual(result.metadata.finalRegionMix, { japan: 5 });
});

test("C2. a Japan-primary candidate pool can never yield zero regional stories", () => {
  assert.ok(select().metadata.selectedRegionStories > 0);
});

test("C3. incidental and company-false-positive candidates are not region-eligible", () => {
  const result = select();
  for (const id of ["false-positive-sony", "incidental-japan"]) {
    assert.ok(!result.selectedIds.includes(id), `${id} was selected`);
    assert.deepEqual(logFor(result, id).regionEligibility, []);
    assert.equal(logFor(result, id).selectionPhase, "outside_scope");
    assert.equal(logFor(result, id).rejectionReason, "not primary for any selected region");
  }
});

// ── D. shortage fallback ──────────────────────────────────────────────────────────────

test("D. three Japan stories are topped up by two global fallbacks", () => {
  const pool = subset([
    "jp-tech-robot",
    "jp-business",
    "jp-health",
    "world-climate",
    "world-health",
    "us-tech",
  ]);
  const result = select(pool);
  const japanCount = result.selectedIds.filter((id) =>
    isPrimary(BY_ID.get(id) as MixCandidate, "japan"),
  ).length;

  assert.equal(japanCount, 3);
  assert.equal(result.metadata.selectedRegionStories, 3);
  assert.equal(result.metadata.fallbackSlots, 2);
  assert.equal(result.metadata.fallbackReason, "insufficient qualifying regional candidates");
  assert.equal(result.metadata.finalRegionMix.global_fallback, 2);
  assert.equal(result.metadata.shortage, false);
});

test("D2. the regional phase is exhausted before any fallback is considered", () => {
  const result = select(
    subset(["jp-tech-robot", "jp-business", "jp-health", "world-climate", "world-health", "us-tech"]),
  );
  // Every Japan story appears before every fallback.
  const phases = result.selectedIds.map((id) => logFor(result, id).selectionPhase);
  assert.deepEqual(phases, [
    "regional_primary",
    "regional_primary",
    "regional_primary",
    "global_fallback",
    "global_fallback",
  ]);
});

// ── E. duplicate elimination ──────────────────────────────────────────────────────────

test("E. two candidates for one underlying story never both survive", () => {
  const result = select();
  const quakes = ["jp-quake-a", "jp-quake-b"].filter((id) => result.selectedIds.includes(id));
  assert.ok(quakes.length <= 1, `both quake stories selected: ${quakes.join(", ")}`);
});

test("E2. the duplicate is rejected by underlyingStoryIdentity, and says so", () => {
  const pool = subset([
    "jp-quake-a",
    "jp-business",
    "jp-health",
    "global-quake-duplicate",
    "world-health",
    "world-culture",
  ]);
  const result = select(pool);
  assert.ok(result.selectedIds.includes("jp-quake-a"));
  assert.ok(!result.selectedIds.includes("global-quake-duplicate"));
  assert.match(
    logFor(result, "global-quake-duplicate").rejectionReason ?? "",
    /^duplicate underlyingStoryIdentity=jp-coastal-warning$/,
  );
});

test("E3. a duplicate canonical URL is caught even with a different story identity", () => {
  const [first] = subset(["jp-business"]);
  const twin = structuredClone(first);
  twin.id = "jp-business-twin";
  twin.underlyingStoryIdentity = "some-other-identity";
  twin.url = `${first.url}/`; // trailing slash only — same canonical URL
  twin.baseScore = 1;

  const result = select([first, twin, ...subset(["jp-health", "jp-culture"])]);
  assert.ok(result.selectedIds.includes("jp-business"));
  assert.ok(!result.selectedIds.includes("jp-business-twin"));
  assert.equal(logFor(result, "jp-business-twin").rejectionReason, "duplicate canonical URL");
});

test("E4. canonicalUrl matches Python's urlsplit/urlunsplit normalisation", () => {
  assert.equal(canonicalUrl("https://Example.COM/a/b/"), "https://example.com/a/b");
  assert.equal(canonicalUrl("HTTPS://example.com/a?x=1#frag"), "https://example.com/a");
  assert.equal(canonicalUrl("https://example.com/"), "https://example.com");
  assert.equal(canonicalUrl("https://example.com"), "https://example.com");
  assert.equal(canonicalUrl(""), "");
});

// ── F. diversity ──────────────────────────────────────────────────────────────────────

test("F. the soft diversity bonus keeps at least three categories in the mix", () => {
  const result = select();
  const categories = new Set(
    result.selectedIds.map((id) => (BY_ID.get(id) as MixCandidate).category),
  );
  assert.ok(categories.size >= 3, `only ${categories.size} categories: ${[...categories]}`);
});

test("F2. the bonus is soft — a large score gap still wins", () => {
  // Two TECH stories from one source: the bonus (max 1.0) cannot overturn a 5-point gap.
  const pool = subset(["jp-tech-robot", "jp-tech-chips", "jp-business", "jp-health", "jp-culture"]);
  const result = select(pool);
  assert.deepEqual(result.selectedIds.slice(0, 2), ["jp-tech-robot", "jp-tech-chips"]);
});

// ── G. Japan + US ─────────────────────────────────────────────────────────────────────

test("G. Japan and the US are balanced 3/2 with both represented", () => {
  const result = select(undefined, ["japan", "united_states"]);
  const mix = result.metadata.finalRegionMix;
  assert.equal((mix.japan ?? 0) + (mix.united_states ?? 0), 5);
  assert.deepEqual([mix.japan ?? 0, mix.united_states ?? 0].sort(), [2, 3]);
  assert.equal(result.metadata.fallbackSlots, 0);
});

test("G2. the two-region allocation is deterministic across repeated runs", () => {
  const first = select(undefined, ["japan", "united_states"]);
  const second = select(undefined, ["united_states", "japan"]);
  assert.deepEqual(first.selectedIds, second.selectedIds);
  assert.deepEqual(first.metadata.finalRegionMix, second.metadata.finalRegionMix);
});

// ── H / I. Japan + Tech ordering ──────────────────────────────────────────────────────

test("H. Japan Tech comes first, then other Japan", () => {
  const result = select(undefined, ["japan"], ["tech"]);
  assert.deepEqual(result.selectedIds.slice(0, 2), ["jp-tech-robot", "jp-tech-chips"]);
  for (const id of result.selectedIds) {
    assert.ok(isPrimary(BY_ID.get(id) as MixCandidate, "japan"));
  }
});

test("I. a higher-scoring GLOBAL tech story never displaces a qualifying Japan story", () => {
  const result = select(undefined, ["japan"], ["tech"]);
  // us-tech scores 96 — higher than every Japan candidate — and is still excluded.
  assert.ok(!result.selectedIds.includes("us-tech"));
  assert.equal((BY_ID.get("us-tech") as MixCandidate).baseScore, 96);
  assert.equal(logFor(result, "us-tech").selectionPhase, "outside_scope");
});

test("I2. the topic adjustment is +10 per matching topic", () => {
  const robot = BY_ID.get("jp-tech-robot") as MixCandidate; // topics: tech, health
  assert.equal(topicAdjustment(robot, []), 0);
  assert.equal(topicAdjustment(robot, ["tech"]), 10);
  assert.equal(topicAdjustment(robot, ["tech", "health"]), 20);
  assert.equal(topicAdjustment(robot, ["business"]), 0);
});

// ── J. fallback duplicate ─────────────────────────────────────────────────────────────

test("J. a fallback candidate duplicating a regional pick is rejected", () => {
  const pool = subset([
    "jp-quake-a",
    "jp-business",
    "jp-health",
    "global-quake-duplicate",
    "world-health",
    "world-culture",
  ]);
  const result = select(pool);
  assert.ok(result.selectedIds.includes("jp-quake-a"));
  assert.ok(!result.selectedIds.includes("global-quake-duplicate"));
  assert.equal(result.metadata.fallbackSlots, 2);
  assert.deepEqual(result.selectedIds, [
    "jp-quake-a",
    "jp-business",
    "jp-health",
    "world-health",
    "world-culture",
  ]);
});

// ── K. shortage metadata ──────────────────────────────────────────────────────────────

test("K. fewer than five eligible candidates reports shortage metadata", () => {
  const pool = subset([
    "jp-quake-a",
    "global-quake-duplicate",
    "world-health",
    "false-positive-sony",
  ]);
  const result = select(pool);
  assert.equal(result.metadata.shortage, true);
  assert.ok(result.metadata.unfilledSlots > 0);
  assert.equal(result.metadata.fallbackReason, "insufficient total eligible candidates");

  const identities = result.selectedIds.map(
    (id) => (BY_ID.get(id) as MixCandidate).underlyingStoryIdentity,
  );
  assert.equal(new Set(identities).size, identities.length, "a duplicate story survived");
});

// ── L / M. determinism ────────────────────────────────────────────────────────────────

test("L. repeated execution is byte-equivalent", () => {
  const first = select();
  const second = select(ALL.map((c) => structuredClone(c)));
  assert.equal(JSON.stringify(first), JSON.stringify(second));
});

test("M. reversing the candidate input changes nothing", () => {
  const forward = select();
  const reversed = select([...ALL].reverse().map((c) => structuredClone(c)));
  assert.equal(JSON.stringify(forward), JSON.stringify(reversed));
});

test("M2. shuffling the candidate input changes nothing", () => {
  // A fixed, deterministic permutation — no random source in a test.
  const shuffled = ALL.map((c, i) => ({ c, k: (i * 7 + 3) % ALL.length }))
    .sort((a, b) => a.k - b.k)
    .map(({ c }) => structuredClone(c));
  assert.equal(JSON.stringify(select(shuffled)), JSON.stringify(select()));
});

// ── N. unsupported input ──────────────────────────────────────────────────────────────

test("N. an unsupported region or topic fails the whole selection", () => {
  assert.throws(() => select(undefined, ["mars"]), UnsupportedMixValue);
  assert.throws(() => select(undefined, ["japan"], ["sports"]), UnsupportedMixValue);
});

test("N2. an empty region list is rejected", () => {
  assert.throws(
    () => select(undefined, []),
    (error: unknown) =>
      error instanceof MixSelectionError && /at least one region is required/.test(error.message),
  );
});

test("N3. a candidate without regionMemberships fails loudly rather than being classified", () => {
  const orphan = structuredClone(BY_ID.get("jp-business") as MixCandidate);
  delete orphan.regionMemberships;
  assert.throws(
    () => select([orphan]),
    (error: unknown) =>
      error instanceof MixSelectionError && /has no regionMemberships/.test(error.message),
  );
});

// ── O / P. eligibility ────────────────────────────────────────────────────────────────

test("O. a stale candidate is rejected with the freshness reason", () => {
  const stale = structuredClone(BY_ID.get("jp-health") as MixCandidate);
  stale.id = "stale";
  stale.url = "https://example.com/stale";
  stale.underlyingStoryIdentity = "stale";
  stale.publishedAt = "2026-07-20T00:00:00Z";

  const result = select([stale, ...subset(["world-health"])]);
  assert.equal(logFor(result, "stale").rejectionReason, "outside 72-hour freshness window");
  assert.ok(!result.selectedIds.includes("stale"));
});

test("O2. a far-future candidate is rejected by the same window", () => {
  const future = structuredClone(BY_ID.get("jp-health") as MixCandidate);
  future.id = "future";
  future.url = "https://example.com/future";
  future.underlyingStoryIdentity = "future";
  future.publishedAt = "2026-07-30T00:00:00Z";
  // The window is 72h backward and 24h forward; beyond that is not today's news.
  const outcome = isEligible(future, DATE);
  assert.equal(outcome.eligible, false);
  assert.equal(outcome.reason, "outside 72-hour freshness window");
});

test("P. a low-reliability candidate is rejected", () => {
  const low = structuredClone(BY_ID.get("jp-business") as MixCandidate);
  low.id = "low-source";
  low.url = "https://example.com/low-source";
  low.underlyingStoryIdentity = "low-source";
  low.sourceReliability = "low";

  const result = select([low, ...subset(["world-health"])]);
  assert.equal(logFor(result, "low-source").rejectionReason, "low source reliability");
  assert.ok(!result.selectedIds.includes("low-source"));
});

test("P2. eligibility rules fire in Python's order, first failure winning", () => {
  const base = structuredClone(BY_ID.get("jp-business") as MixCandidate);

  assert.deepEqual(isEligible({ ...base, eligible: false, sourceReliability: "low" }, DATE), {
    eligible: false,
    reason: "candidate marked ineligible",
  });
  assert.deepEqual(isEligible({ ...base, sourceReliability: "low", headline: "" }, DATE), {
    eligible: false,
    reason: "low source reliability",
  });
  assert.deepEqual(isEligible({ ...base, headline: "", url: "ftp://x" }, DATE), {
    eligible: false,
    reason: "missing required field",
  });
  assert.deepEqual(isEligible({ ...base, url: "ftp://x", publishedAt: "nope" }, DATE), {
    eligible: false,
    reason: "invalid article URL",
  });
  assert.deepEqual(isEligible({ ...base, publishedAt: "nope" }, DATE), {
    eligible: false,
    reason: "invalid publishedAt",
  });
  assert.equal(isEligible(base, DATE).eligible, true);
});

test("P3. a naive timestamp is read as UTC, not as local time", () => {
  const naive = structuredClone(BY_ID.get("jp-business") as MixCandidate);
  naive.publishedAt = "2026-07-27T08:20:00";
  assert.equal(isEligible(naive, DATE).eligible, true);
});

// ── the editorial duplicate seam ──────────────────────────────────────────────────────

test("the editorial guard seam is consulted, and never fires on this fixture", () => {
  let calls = 0;
  let fired = 0;
  const spy: EditorialDuplicateGuard = (a, b) => {
    calls += 1;
    const decision = productionEditorialDuplicateGuard(a, b);
    if (decision.duplicate) fired += 1;
    return decision;
  };
  const withSpy = selectCustomMix({
    candidates: ALL.map((c) => structuredClone(c)),
    date: DATE,
    regions: ["japan"],
    editorialDuplicateGuard: spy,
  });
  assert.ok(calls > 0, "the seam was never consulted");
  assert.equal(fired, 0, "the real guard fires on the base fixture — goldens would change");
  // The default (which IS the real guard) produces exactly the same result.
  assert.equal(JSON.stringify(withSpy), JSON.stringify(select()));
});

test("the production default is the REAL editorial guard, not the test-only no-op", () => {
  // Proven behaviourally: a pair the real guard catches is caught WITHOUT injection.
  const roundup = structuredClone(BY_ID.get("jp-business") as MixCandidate);
  roundup.id = "samsung-roundup";
  roundup.headline = "Samsung Galaxy Unpacked 2026: The 6 biggest announcements";
  roundup.summary = "Everything Samsung showed, including the new Z Fold 8.";
  roundup.url = "https://example.com/samsung-roundup";
  roundup.underlyingStoryIdentity = "samsung-roundup";
  roundup.baseScore = 95;

  const handsOn = structuredClone(roundup);
  handsOn.id = "samsung-handson";
  handsOn.headline = "Samsung's wider Z Fold 8 feels just right";
  handsOn.summary = "An hour with the new foldable.";
  handsOn.url = "https://example.com/samsung-handson";
  handsOn.underlyingStoryIdentity = "samsung-handson";
  handsOn.baseScore = 94;

  const withDefault = select([roundup, handsOn, ...subset(["jp-health", "jp-culture"])]);
  assert.ok(withDefault.selectedIds.includes("samsung-roundup"));
  assert.ok(
    !withDefault.selectedIds.includes("samsung-handson"),
    "the default guard let an editorial duplicate through",
  );

  // And the test-only no-op lets it through, confirming the difference is the guard.
  const withNoOp = selectCustomMix({
    candidates: [roundup, handsOn, ...subset(["jp-health", "jp-culture"])].map((c) =>
      structuredClone(c),
    ),
    date: DATE,
    regions: ["japan"],
    editorialDuplicateGuard: noEditorialDuplicateGuard,
  });
  assert.ok(withNoOp.selectedIds.includes("samsung-handson"));
});

test("an editorial guard that DOES fire changes the outcome and reports its rule", () => {
  const guard: EditorialDuplicateGuard = (a, b) =>
    a.id === "jp-tech-chips" && b.id === "jp-tech-robot"
      ? { duplicate: true, reason: "same launch event", rule: "same-launch-event" }
      : { duplicate: false };

  const result = selectCustomMix({
    candidates: ALL.map((c) => structuredClone(c)),
    date: DATE,
    regions: ["japan"],
    editorialDuplicateGuard: guard,
  });
  assert.ok(!result.selectedIds.includes("jp-tech-chips"));
  assert.equal(
    logFor(result, "jp-tech-chips").rejectionReason,
    "existing duplicate guard: same-launch-event: same launch event",
  );
});
