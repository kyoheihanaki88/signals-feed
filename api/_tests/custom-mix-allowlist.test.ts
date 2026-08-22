/**
 * Selector v3 — strict topic allowlist + fixed region priority + ABSOLUTE region
 * boundary. (2026-08-22; v2 2026-08-13)
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

import { publisherFamily, selectCustomMix } from "../_lib/custom-mix-selector.js";
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
  options: {
    topics: string[];
    regions: string[];
    score: number;
    category?: string;
    source?: string;
  },
): MixCandidate {
  const candidate = {
    id,
    headline: `Headline for ${id} with distinct wording ${id}`,
    summary: `A distinct summary for ${id} that shares no event with others.`,
    source: options.source ?? `SOURCE-${id}`,
    category: options.category ?? "WORLD",
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
  // Exactly one culture story exists in japan; v3's region boundary keeps the world
  // culture story out of a japan-only mix, so the result is 1/5 — never off-topic or
  // off-region filler.
  const result = select(ALL, ["japan"], ["culture"]);
  assert.deepEqual([...result.selectedIds], ["jp-culture"]);
  assert.equal(result.metadata.shortage, true);
  assert.equal(result.metadata.unfilledSlots, 4);
});

// ── region priority ───────────────────────────────────────────────────────────────────

test("the US beats world when both are selected (at least 3, UK-capped)", () => {
  // v2.1: this fixture's world pool is BBC/Guardian heavy, so the UK cap can shrink the
  // world share further — freed slots flow BACK to the US, never the other way.
  const result = select(ALL, ["united_states", "world"]);
  const mix = result.metadata.finalRegionMix;
  assert.ok((mix["united_states"] ?? 0) >= 3);
  assert.equal(Object.values(mix).reduce((a, b) => a + b, 0), 5);
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
    make("uk-science", { topics: ["science"], regions: ["world"], score: 99,
                         source: "BBC News (Science & Environment)" }),
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
    make("uk-science", { topics: ["science"], regions: ["world"], score: 99,
                         category: "SCIENCE", source: "BBC News (Science & Environment)" }),
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

// ── v2.1: publisher-family caps and canonical topics ──────────────────────────────────

test("a SCIENCE-category story never survives Science OFF, even with a tech tag", () => {
  const pool = [
    make("sci-tech-tagged", { topics: ["science", "tech"], regions: ["united_states"],
                              score: 99, category: "SCIENCE" }),
    make("tech-sci-tagged", { topics: ["tech", "science"], regions: ["united_states"],
                              score: 70, category: "TECH" }),
    make("us-a", { topics: [], regions: ["united_states"], score: 60 }),
    make("us-b", { topics: ["business"], regions: ["united_states"], score: 59,
                   category: "BUSINESS" }),
    make("world-a", { topics: ["health"], regions: ["world"], score: 58,
                      category: "HEALTH" }),
  ];
  const result = select(pool, ["united_states", "world"],
                        ["tech", "business", "health", "climate", "culture", "ai"]);
  assert.ok(!result.selectedIds.includes("sci-tech-tagged"));
  assert.ok(result.selectedIds.includes("tech-sci-tagged"));
});

test("one story per publisher family while distinct alternatives exist", () => {
  const pool = [
    make("bbc-1", { topics: [], regions: ["world"], score: 90, source: "BBC News (World)" }),
    make("bbc-2", { topics: ["health"], regions: ["world"], score: 89,
                    category: "HEALTH", source: "BBC News (Health)" }),
    make("npr-1", { topics: [], regions: ["world"], score: 60, source: "NPR (World)" }),
    make("aj-1", { topics: [], regions: ["world"], score: 59, source: "Al Jazeera" }),
    make("cbs-1", { topics: [], regions: ["world"], score: 58, source: "CBS News (U.S.)" }),
    make("verge-1", { topics: ["tech"], regions: ["world"], score: 57,
                      category: "TECH", source: "The Verge (Tech)" }),
  ];
  const result = select(pool, ["world"]);
  assert.equal(result.selectedIds.length, 5);
  assert.ok(!result.selectedIds.includes("bbc-2"),
            "two section feeds of one publisher may contribute only one story");
});

test("a UK flood cannot take more than one slot while the US is active", () => {
  const pool = [
    ...[0, 1, 2, 3].map((i) =>
      make(`bbc-${i}`, { topics: [], regions: ["world"], score: 99 - i,
                         source: "BBC News (World)" })),
    ...[0, 1, 2, 3].map((i) =>
      make(`guardian-${i}`, { topics: [], regions: ["world"], score: 95 - i,
                              source: "The Guardian (World)" })),
    make("us-a", { topics: [], regions: ["united_states"], score: 50,
                   source: "CBS News (U.S.)" }),
    make("us-b", { topics: [], regions: ["united_states"], score: 49,
                   source: "NPR (World)" }),
    make("us-c", { topics: ["tech"], regions: ["united_states"], score: 48,
                   category: "TECH", source: "The Verge" }),
    make("world-aj", { topics: [], regions: ["world"], score: 40, source: "Al Jazeera" }),
  ];
  const result = select(pool, ["united_states", "world"]);
  assert.equal(result.selectedIds.length, 5);
  const chosen = pool.filter((c) => result.selectedIds.includes(String(c.id)));
  const uk = chosen.filter((c) =>
    ["bbc", "guardian", "ft"].includes(publisherFamily(c.source))).length;
  const us = chosen.filter((c) => String(c.id).startsWith("us-")).length;
  assert.ok(uk <= 1, `UK families took ${uk} slots`);
  assert.ok(us >= 3);
});

test("the relaxed last-resort fallback reaches five without breaking UK or topic rules", () => {
  // Only two publisher families exist: the strict pass stalls at 2, the relaxed pass
  // fills to five — but a science story stays out (Science OFF) and the UK cap holds.
  const pool = [
    ...[0, 1, 2, 3].map((i) =>
      make(`npr-${i}`, { topics: [], regions: ["world"], score: 90 - i,
                         source: "NPR (World)" })),
    ...[0, 1].map((i) =>
      make(`aj-${i}`, { topics: [], regions: ["world"], score: 80 - i,
                        source: "Al Jazeera" })),
    make("sci", { topics: ["science"], regions: ["world"], score: 99,
                  category: "SCIENCE", source: "NPR (World)" }),
  ];
  const result = select(pool, ["world"],
                        ["tech", "business", "health", "climate", "culture", "ai"]);
  assert.equal(result.selectedIds.length, 5);
  assert.ok(!result.selectedIds.includes("sci"));
  const relaxed = result.candidateLogs.filter(
    (log) => log.selectionPhase === "global_fallback_relaxed" &&
             result.selectedIds.includes(String(log.id)));
  assert.ok(relaxed.length >= 1, "the relaxed pass provided the missing stories");
});

// ── v3: the region boundary is absolute ───────────────────────────────────────────────

function usPool(n: number, startScore = 80): MixCandidate[] {
  const cycle = ["tech", "business", "health", "climate", "culture"];
  return Array.from({ length: n }, (_, i) =>
    make(`us-${i}`, { topics: [cycle[i % 5]], regions: ["united_states"],
                      score: startScore - i }));
}

function worldPool(n: number, startScore = 79): MixCandidate[] {
  const cycle = ["climate", "health", "culture", "business", "tech"];
  return Array.from({ length: n }, (_, i) =>
    make(`world-${i}`, { topics: [cycle[i % 5]], regions: ["world"],
                         score: startScore - i }));
}

function reasonOf(result: ReturnType<typeof selectCustomMix>, id: string): string | null {
  return result.candidateLogs.find((l) => l.id === id)?.rejectionReason ?? null;
}

test("v3: a US-only mix never fills from world general articles", () => {
  const pool = [
    ...usPool(2),
    ...[0, 1, 2].map((i) =>
      make(`world-gen-${i}`, { topics: [], regions: ["world"], score: 95 - i })),
  ];
  const result = select(pool, ["united_states"]);
  assert.deepEqual([...result.selectedIds].sort(), ["us-0", "us-1"]);
  assert.equal(result.metadata.shortage, true);
  assert.equal(result.metadata.unfilledSlots, 3);
  assert.equal(result.metadata.fallbackSlots, 0);
  assert.ok(!("world" in result.metadata.finalRegionMix));
  for (const i of [0, 1, 2]) {
    assert.equal(reasonOf(result, `world-gen-${i}`),
                 "not primary for any selected region");
  }
});

test("v3: a world-only mix never fills from US general articles", () => {
  const pool = [
    ...worldPool(2),
    ...[0, 1, 2].map((i) =>
      make(`us-gen-${i}`, { topics: [], regions: ["united_states"], score: 95 - i,
                            source: "CBS News (U.S.)" })),
  ];
  const result = select(pool, ["world"]);
  assert.deepEqual([...result.selectedIds].sort(), ["world-0", "world-1"]);
  assert.equal(result.metadata.shortage, true);
  assert.equal(result.metadata.fallbackSlots, 0);
  assert.ok(!("united_states" in result.metadata.finalRegionMix));
  for (const i of [0, 1, 2]) {
    assert.equal(reasonOf(result, `us-gen-${i}`),
                 "not primary for any selected region");
  }
});

test("v3: a US general article is eligible only when the US is selected", () => {
  const usGeneral = make("cbs-us-gen", { topics: [], regions: ["united_states"],
                                         score: 99, source: "CBS News (U.S.)" });
  const withoutUs = select([...worldPool(5), usGeneral], ["world"]);
  assert.ok(!withoutUs.selectedIds.includes("cbs-us-gen"));
  const withUs = select([...worldPool(5), usGeneral], ["united_states", "world"]);
  assert.ok(withUs.selectedIds.includes("cbs-us-gen"));
});

test("v3: SCIENCE-category stories stay OFF across regions, even with sub-tags", () => {
  const pool = [
    ...usPool(5),
    make("us-science-tech", { topics: ["science", "tech"], regions: ["united_states"],
                              score: 99, category: "SCIENCE" }),
    make("world-science-tech", { topics: ["science", "tech"], regions: ["world"],
                                 score: 98, category: "SCIENCE" }),
  ];
  const result = select(pool, ["united_states", "world"],
                        ["tech", "business", "health", "climate", "culture"]);
  assert.ok(!result.selectedIds.includes("us-science-tech"));
  assert.ok(!result.selectedIds.includes("world-science-tech"));
  assert.equal(reasonOf(result, "us-science-tech"),
               "topic not selected (strict allowlist)");
  assert.equal(reasonOf(result, "world-science-tech"),
               "topic not selected (strict allowlist)");
});

test("v3: the US keeps at least 3 slots whenever its pool suffices", () => {
  const result = select([...usPool(5, 60), ...worldPool(5, 95)],
                        ["united_states", "world"]);
  const mix = result.metadata.finalRegionMix;
  assert.ok((mix["united_states"] ?? 0) >= 3);
  assert.equal(result.selectedIds.length, 5);
  assert.equal(Object.values(mix).reduce((a, b) => a + b, 0), 5);
});

test("v3: the UK cap survives even the relaxed fallback pass", () => {
  const pool = [
    ...usPool(2),
    ...[0, 1, 2].map((i) =>
      make(`bbc-${i}`, { topics: ["climate"], regions: ["world"], score: 90 - i,
                         source: `BBC News (Section ${i})` })),
    make("guardian-0", { topics: ["health"], regions: ["world"], score: 85,
                         source: "The Guardian" }),
  ];
  const result = select(pool, ["united_states", "world"]);
  const chosen = pool.filter((c) => result.selectedIds.includes(String(c.id)));
  const uk = chosen.filter((c) =>
    ["bbc", "guardian", "ft"].includes(publisherFamily(c.source))).length;
  assert.ok(uk <= 1, `UK families took ${uk} slots`);
  assert.equal(result.selectedIds.length, 3);
  assert.equal(result.metadata.shortage, true);
});

test("v3: a successful mix is always exactly five unique stories", () => {
  const dup = make("us-dup", { topics: ["tech"], regions: ["united_states"], score: 99 });
  (dup as { underlyingStoryIdentity: string }).underlyingStoryIdentity = "story-us-0";
  const pool = [...usPool(6), ...worldPool(3), dup];
  const result = select(pool, ["united_states", "world"]);
  assert.equal(result.selectedIds.length, 5);
  assert.equal(new Set(result.selectedIds).size, 5);
  const lookup = new Map(pool.map((c) => [String(c.id), c]));
  const identities = result.selectedIds.map(
    (id) => lookup.get(id)!.underlyingStoryIdentity);
  assert.equal(new Set(identities).size, identities.length);
  assert.equal(
    Object.values(result.metadata.finalRegionMix).reduce((a, b) => a + b, 0), 5);
  assert.equal(result.metadata.shortage, false);
});

// ── cache identity ────────────────────────────────────────────────────────────────────

test("the v3 identity and version invalidate every earlier cache entry", () => {
  const result = select(ALL, ["japan"]);
  assert.equal(SELECTOR_VERSION, 3);
  assert.equal(result.metadata.selectorVersion, 3);
  assert.ok(result.metadata.mixIdentity.includes("|selector=3|"));
  assert.ok(!result.metadata.mixIdentity.includes("|selector=2|"));
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
      selectorVersion: 3 as const,
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

test("end to end: exactly 5, Science 0, US at least 3, UK at most 1 (required test 13)", async () => {
  // A realistic day: strong UK world coverage, a real US pool (CBS + NPR + Verge),
  // and Science switched OFF. The contract below is the validated form of the body the
  // iOS client posts to /api/edition.
  const pool = [
    ...[0, 1, 2].map((i) =>
      make(`bbc-${i}`, { topics: [], regions: ["world"], score: 99 - i,
                         source: "BBC News (World)" })),
    ...[0, 1].map((i) =>
      make(`guardian-${i}`, { topics: [], regions: ["world"], score: 96 - i,
                              source: "The Guardian (World)" })),
    make("us-cbs", { topics: [], regions: ["united_states"], score: 60,
                     source: "CBS News (U.S.)" }),
    make("us-npr", { topics: [], regions: ["united_states"], score: 59,
                     source: "NPR (World)" }),
    make("us-verge", { topics: ["tech"], regions: ["united_states"], score: 58,
                       category: "TECH", source: "The Verge" }),
    make("us-sci", { topics: ["science"], regions: ["united_states"], score: 95,
                     category: "SCIENCE", source: "Science News" }),
    make("world-aj", { topics: [], regions: ["world"], score: 40, source: "Al Jazeera" }),
  ];
  const enriched = pool.map(
    (c) => ({ selector: structuredClone(c) }) as unknown as EnrichedCandidate,
  );
  const orchestrator = createEditionOrchestrator({
    candidates: {
      async loadCandidates() {
        return { candidates: pool.map((c) => structuredClone(c)), enriched };
      },
    },
    customMixEnabled: true,
  });
  const outcome = await orchestrator({
    contract: {
      date: DATE,
      active: {
        mode: "custom" as const,
        regions: ["united_states" as const, "world" as const],
        topics: ["business" as const, "tech" as const, "health" as const,
                 "climate" as const, "culture" as const, "ai" as const],
      },
      pending: null,
      selectorVersion: 3 as const,
      storyCount: 5 as const,
    },
  });

  assert.equal(outcome.path, "custom_mix_pro", "the mix must be served, not a fallback");
  const selection = outcome.selection!;
  assert.equal(selection.selectedIds.length, 5, "exactly five, never fewer");
  const chosen = pool.filter((c) => selection.selectedIds.includes(String(c.id)));
  assert.equal(chosen.filter((c) => c.category === "SCIENCE").length, 0, "Science OFF → 0");
  assert.ok(!selection.selectedIds.includes("us-sci"));
  const us = chosen.filter((c) =>
    (c.regionMemberships ?? []).some((m) => (m.id ?? m.region) === "united_states")).length;
  const uk = chosen.filter((c) =>
    ["bbc", "guardian", "ft"].includes(publisherFamily(c.source))).length;
  assert.ok(us >= 3, `US stories: ${us}`);
  assert.ok(uk <= 1, `UK-family stories: ${uk}`);
  assert.equal(outcome.selected?.length, 5, "the enriched rows the feed will render");
});
