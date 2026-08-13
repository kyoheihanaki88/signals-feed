/**
 * Selector v2 — strict topic allowlist + fixed region priority. (2026-08-13)
 *
 * The user's Custom Mix settings are a CONTRACT, not a scoring hint:
 *   • a topic the user switched OFF is never selected — not by ranking, not by fallback;
 *   • rather than fill five slots with a violating story, the mix ships short (fail
 *     closed) and the orchestrator's 5-story requirement falls back to standard;
 *   • regions fill in the fixed priority united_states > japan > world, the US keeps a
 *     3-slot minimum when selected and its pool suffices, and a UK story competes only
 *     as a world story;
 *   • the request contract iOS sends (active regions/topics) is exactly what the
 *     selection honours — verified end-to-end through the orchestrator seam;
 *   • the v2 identity can never collide with (or reuse) a v1 cache entry.
 *
 * These are the same assertions Python's CustomMixSelectorV2Tests makes, restated
 * against the TypeScript port; cross-language agreement is proven by the parity suite.
 */

import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { selectCustomMix } from "../_lib/custom-mix-selector.js";
import { SELECTOR_VERSION, type MixCandidate } from "../_lib/custom-mix-types.js";
import { createEditionOrchestrator } from "../_lib/edition-orchestrator.js";
import type { MixCandidateBundle } from "../_lib/edition-orchestrator.js";
import type { EnrichedCandidate } from "../_lib/editorial-mix-pool-schema.js";

const FIXTURE_DIR = resolve(dirname(fileURLToPath(import.meta.url)), "..", "_fixtures");
const DATE = "2026-07-27";

function loadCandidates(): MixCandidate[] {
  const raw = readFileSync(join(FIXTURE_DIR, "custom_mix_candidates.json"), "utf8");
  return (JSON.parse(raw) as { candidates: MixCandidate[] }).candidates;
}

const ALL = loadCandidates();
const BY_ID = new Map(ALL.map((c) => [c.id, c]));

function topicsOf(id: string): Set<string> {
  const candidate = BY_ID.get(id) ?? MADE.get(id);
  return new Set(((candidate?.topics ?? []) as unknown[]).map((t) => String(t).toLowerCase()));
}

/** A fully-eligible synthetic candidate (mirrors the Python test helper `make`). */
const MADE = new Map<string, MixCandidate>();
function make(
  id: string,
  options: { topics: string[]; regions: string[]; score: number },
): MixCandidate {
  const candidate = {
    id,
    headline: `Headline for ${id} with distinct wording ${id}`,
    summary: `A distinct summary for ${id} that shares no event with others.`,
    source: `SOURCE-${id}`,
    category: "WORLD",
    url: `https://example.com/${id}`,
    publishedAt: "2026-07-27T06:00:00Z",
    baseScore: options.score,
    topics: options.topics,
    underlyingStoryIdentity: `story-${id}`,
    regionMemberships: options.regions.map((r) => ({ id: r, strength: "primary" })),
  } as unknown as MixCandidate;
  MADE.set(id, candidate);
  return candidate;
}

function select(
  candidates: MixCandidate[],
  regions: unknown[],
  topics: unknown[] = [],
): ReturnType<typeof selectCustomMix> {
  return selectCustomMix({
    candidates: candidates.map((c) => structuredClone(c)),
    date: DATE,
    regions,
    topics,
  });
}

// ── strict allowlist ──────────────────────────────────────────────────────────────────

test("science OFF selects no science-only story", () => {
  const result = select(ALL, ["japan"], ["tech", "business", "health", "climate", "culture"]);
  for (const id of result.selectedIds) {
    assert.ok(
      [...topicsOf(id)].some((t) =>
        ["tech", "business", "health", "climate", "culture"].includes(t)),
      `${id} violates the allowlist`,
    );
  }
  assert.ok(!result.selectedIds.includes("jp-quake-a"));
  assert.ok(!result.selectedIds.includes("jp-quake-b"));
});

test("science OFF survives fallback — nothing resurrects an OFF topic", () => {
  const pool = ALL.filter((c) =>
    ["jp-culture", "jp-quake-a", "us-science", "world-culture", "global-quake-duplicate"]
      .includes(String(c.id)));
  const result = select(pool, ["japan"], ["culture"]);
  for (const id of result.selectedIds) {
    assert.ok(topicsOf(id).has("culture"), `${id} is off-topic`);
  }
  assert.ok(!result.selectedIds.includes("us-science"));
  assert.ok(!result.selectedIds.includes("jp-quake-a"));
  assert.equal(result.metadata.shortage, true);
});

test("an off-topic candidate is logged with the allowlist rejection", () => {
  const result = select(ALL, ["japan"], ["culture"]);
  const log = result.candidateLogs.find((l) => l.id === "jp-quake-a");
  assert.equal(log?.rejectionReason, "topic not selected (strict allowlist)");
});

test("too few on-topic stories fail closed instead of filling with violations", () => {
  // Exactly one culture story in japan and one in world exist in the fixture.
  const result = select(ALL, ["japan"], ["culture"]);
  assert.deepEqual([...result.selectedIds].sort(), ["jp-culture", "world-culture"]);
  assert.equal(result.metadata.shortage, true);
  assert.equal(result.metadata.unfilledSlots, 3);
});

// ── region priority ───────────────────────────────────────────────────────────────────

test("the US beats world when both are selected (3/2)", () => {
  const result = select(ALL, ["united_states", "world"]);
  assert.equal(result.metadata.finalRegionMix["united_states"], 3);
  assert.equal(result.metadata.finalRegionMix["world"], 2);
});

test("US/japan/world selected: the US keeps a minimum of 3 of 5", () => {
  const result = select(ALL, ["united_states", "japan", "world"]);
  const mix = result.metadata.finalRegionMix;
  assert.ok((mix["united_states"] ?? 0) >= 3);
  assert.equal(mix["japan"], 1);
  assert.equal(mix["world"], 1);
});

test("a UK story is a world story and cannot displace a US slot", () => {
  const pool = [
    make("uk-science", { topics: ["science"], regions: ["world"], score: 99 }),
    make("us-a", { topics: ["tech"], regions: ["united_states"], score: 70 }),
    make("us-b", { topics: ["business"], regions: ["united_states"], score: 69 }),
    make("us-c", { topics: ["health"], regions: ["united_states"], score: 68 }),
    make("world-a", { topics: ["climate"], regions: ["world"], score: 67 }),
  ];
  const result = select(pool, ["united_states", "world"]);
  assert.equal(result.metadata.finalRegionMix["united_states"], 3);
  assert.ok(result.selectedIds.includes("uk-science")); // as a world story only
});

test("a UK science story loses entirely when science is OFF", () => {
  const pool = [
    make("uk-science", { topics: ["science"], regions: ["world"], score: 99 }),
    make("us-a", { topics: ["tech"], regions: ["united_states"], score: 70 }),
    make("us-b", { topics: ["business"], regions: ["united_states"], score: 69 }),
    make("us-c", { topics: ["health"], regions: ["united_states"], score: 68 }),
    make("world-a", { topics: ["climate"], regions: ["world"], score: 67 }),
  ];
  const result = select(pool, ["united_states", "world"],
                        ["tech", "business", "health", "climate"]);
  assert.ok(!result.selectedIds.includes("uk-science"));
  assert.deepEqual([...result.selectedIds].sort(), ["us-a", "us-b", "us-c", "world-a"]);
});

// ── cache identity ────────────────────────────────────────────────────────────────────

test("the v2 identity and version invalidate every v1 cache entry", () => {
  const result = select(ALL, ["japan"]);
  assert.equal(SELECTOR_VERSION, 2);
  assert.equal(result.metadata.selectorVersion, 2);
  assert.ok(result.metadata.mixIdentity.includes("|selector=2|"));
  assert.ok(!result.metadata.mixIdentity.includes("|selector=1|"));
});

// ── the iOS contract is what the selection honours ────────────────────────────────────

test("the active regions/topics iOS sends are exactly what the backend selects from", async () => {
  // The contract object below is the VALIDATED form of the body the iOS client posts to
  // /api/edition. Everything the orchestrator selects must sit inside it.
  const enriched = ALL.map(
    (c) => ({ selector: structuredClone(c) }) as unknown as EnrichedCandidate,
  );
  const bundle: MixCandidateBundle = {
    candidates: ALL.map((c) => structuredClone(c)),
    enriched,
  };
  const orchestrator = createEditionOrchestrator({
    candidates: { async loadCandidates() { return bundle; } },
    customMixEnabled: true,
  });

  const active = {
    mode: "custom" as const,
    regions: ["japan" as const, "united_states" as const],
    topics: ["business" as const, "health" as const, "tech" as const],
  };
  const outcome = await orchestrator({
    contract: {
      date: DATE,
      active,
      pending: null,
      selectorVersion: 2 as const,
      storyCount: 5 as const,
    },
  });

  assert.equal(outcome.path, "custom_mix_pro");
  assert.ok(outcome.selection);
  const selection = outcome.selection!;
  assert.equal(selection.selectedIds.length, 5);
  for (const id of selection.selectedIds) {
    const candidate = BY_ID.get(id)!;
    const topics = topicsOf(id);
    assert.ok(
      active.topics.some((t) => topics.has(t)),
      `${id} is outside the topics iOS sent`,
    );
    const regions = (candidate.regionMemberships ?? [])
      .filter((m) => m.strength === "primary")
      .map((m) => m.region ?? m.id);
    assert.ok(
      regions.some((r) => (active.regions as string[]).includes(String(r))) ||
        selection.candidateLogs.find((l) => l.id === id)?.selectionPhase === "global_fallback",
      `${id} is outside the regions iOS sent and was not an explicit fallback`,
    );
  }
  // US priority holds through the real orchestrator seam too.
  assert.ok((selection.metadata.finalRegionMix["united_states"] ?? 0) >= 3);
});
