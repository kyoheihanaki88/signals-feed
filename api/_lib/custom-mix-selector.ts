/**
 * Deterministic Custom Mix selector. (Phase 3D-1)
 *
 * A behaviour-preserving port of `pipeline/custom_mix_selector.py`. The Python module is
 * the specification: this file reproduces its ordering, thresholds, tie-breaks, logs and
 * metadata rather than improving on them, because the two must agree exactly.
 *
 * The shape of the algorithm:
 *   1. Normalise the mix and derive the identity string.
 *   2. Walk candidates in ID order, deciding eligibility and recording one log per
 *      candidate — the log is written even for rejects, so every decision is explainable.
 *   3. Fill regional slots. One region takes all five; several split by `divmod` with the
 *      remainder broken by regional strength, then by a hash of the identity so the answer
 *      is stable but not alphabetically biased.
 *   4. Reallocate inside the requested regions, then fall back globally.
 *   5. Re-check duplicates over the finished list, independently of the phases that built
 *      it, and refuse to return a mix that fails.
 *
 * Determinism comes from three places: candidates are processed in sorted ID order, every
 * ranking tie is broken by ID, and no clock, random source or ambient state is consulted.
 *
 * Not connected to `/api/edition`. No file, network, subprocess or environment access, at
 * import time or after.
 */

import { mixIdentity, normalizeMix, normalizeRegions } from "./custom-mix-identity.js";
import { productionEditorialDuplicateGuard } from "./editorial-duplicate-guard.js";
import {
  MixSelectionError,
  SELECTOR_VERSION,
  UnsupportedMixValue,
  type CandidateLog,
  type EditorialDuplicateGuard,
  type MixCandidate,
  type MixSelectionResult,
  type SelectCustomMixOptions,
} from "./custom-mix-types.js";

export const TOPIC_ADJUSTMENT = 10.0;
export const NEW_CATEGORY_BONUS = 0.6;
export const NEW_SOURCE_BONUS = 0.4;
const FRESHNESS_MAX_MS = 72 * 60 * 60 * 1_000;
const FRESHNESS_MIN_MS = -24 * 60 * 60 * 1_000;

// ── selector v2 (2026-08-13) — mirrors pipeline/custom_mix_selector.py exactly ─────────
// Selected topics are a STRICT ALLOWLIST (fallback included; ship short rather than
// violate the user's settings), and regions fill in fixed priority order with a 3-slot
// US minimum when the US is selected alongside others. A UK story carries only `world`
// membership, so it competes for world slots and can never displace a US slot.
export const REGION_PRIORITY = ["united_states", "japan", "world"] as const;
export const US_MIN_QUOTA = 3;

// ── canonical topic rule (v2.1, 2026-08-18) — mirrors the Python selector exactly ──────
// What an article IS is its CATEGORY. A category-typed article is eligible only when
// EVERY canonical topic is selected (Science OFF removes every SCIENCE article, even a
// tech-tagged one); general news (WORLD / JAPAN / OTHER) is region coverage and stays
// topic-eligible. This map mirrors pipeline/mix_pool._CATEGORY_TOPICS.
const CANONICAL_TOPICS_BY_CATEGORY: Record<string, readonly string[]> = {
  AI: ["ai", "tech"],
  BUSINESS: ["business"],
  CLIMATE: ["climate"],
  CULTURE: ["culture"],
  ECONOMY: ["business"],
  FINANCE: ["business"],
  HEALTH: ["health"],
  SCIENCE: ["science"],
  TECH: ["tech"],
};

// ── publisher families (v2.1) — mirrors the Python selector exactly ────────────────────
// Section feeds are ONE publisher; the family is derived from `source` at selection
// time so the pool artifact schema is untouched. Caps: max 1 story per family, and at
// most 1 UK-family story in total while the United States is active.
const PUBLISHER_FAMILY_ALIASES: Record<string, string> = {
  "bbc news": "bbc",
  bbc: "bbc",
  "the guardian": "guardian",
  guardian: "guardian",
  "financial times": "ft",
  "the verge": "verge",
  npr: "npr",
  "al jazeera": "al-jazeera",
  "cbs news": "cbs",
};
const UK_PUBLISHER_FAMILIES = new Set(["bbc", "guardian", "ft"]);
const SECTION_SUFFIX_RE = /\s*\(.*?\)\s*$/;

export function publisherFamily(source: unknown): string {
  const base = pyStr(source).replace(SECTION_SUFFIX_RE, "").trim().toLowerCase();
  return PUBLISHER_FAMILY_ALIASES[base] ?? base;
}

function priorityOrder(regions: readonly string[]): string[] {
  return REGION_PRIORITY.filter((r) => regions.includes(r));
}

function canonicalTopics(candidate: MixCandidate): readonly string[] {
  return CANONICAL_TOPICS_BY_CATEGORY[pyStr(candidate.category).toUpperCase()] ?? [];
}

/** Python `_topic_allowed` (v2.1): canonical-topic subset rule; empty selection = no filter. */
function topicAllowed(candidate: MixCandidate, topics: readonly string[]): boolean {
  if (topics.length === 0) return true;
  return canonicalTopics(candidate).every((topic) => topics.includes(topic));
}

/** Python `_family_violation`: the publisher-family caps, applied in every phase. */
function familyViolation(
  candidate: MixCandidate,
  chosen: MixCandidate[],
  regions: readonly string[],
  relaxFamily = false,
): string | null {
  // "Max 1 per family" is a PRINCIPLE, not a suicide pact: the LAST fallback pass may
  // relax the generic cap when five stories are otherwise unreachable. The UK cap is
  // never relaxed. Mirrors Python `_family_violation` exactly.
  const family = publisherFamily(candidate.source);
  if (regions.includes("united_states") && UK_PUBLISHER_FAMILIES.has(family)) {
    if (chosen.some((c) => UK_PUBLISHER_FAMILIES.has(publisherFamily(c.source)))) {
      return "UK publisher cap reached (max 1 while United States is active)";
    }
  }
  if (!relaxFamily) {
    if (chosen.some((c) => publisherFamily(c.source) === family)) {
      return `publisher family '${family}' already selected (max 1 per family)`;
    }
  }
  return null;
}

/**
 * Python's `str(d.get(key, ""))`. An ABSENT key yields the fallback; a key present with a
 * null value yields `"None"`, because that is what `str(None)` produces. The distinction
 * matters: it is the difference between "no category" and "category: None".
 */
function pyStr(value: unknown, fallbackWhenAbsent = ""): string {
  if (value === undefined) return fallbackWhenAbsent;
  if (value === null) return "None";
  return String(value);
}

/** Python's `float(d.get(key, 0))`. */
function pyFloat(value: unknown): number {
  if (value === undefined) return 0;
  return Number(value);
}

/** Python string ordering for the ASCII identifiers this selector deals in. */
function compareStrings(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0;
}

/**
 * Python `_memberships`.
 *
 * Python falls back to `classify_regions(candidate)` when `regionMemberships` is absent.
 * That classifier is NOT part of the candidate contract, so rather than approximate it
 * this port refuses: an absent membership list is a caller error, loudly.
 */
export function memberships(candidate: MixCandidate): Record<string, string> {
  const rows = candidate.regionMemberships;
  if (rows === undefined || rows === null) {
    throw new MixSelectionError(
      `candidate ${pyStr(candidate.id)} has no regionMemberships; ` +
        "classification is not part of the ported contract",
    );
  }
  const result: Record<string, string> = {};
  for (const row of rows) {
    const region = row.region || row.id;
    if (!region) continue;
    let canonical: string;
    try {
      canonical = normalizeRegions([region])[0];
    } catch (error) {
      if (error instanceof UnsupportedMixValue) continue; // Python swallows ValueError
      throw error;
    }
    result[canonical] = pyStr(row.strength, "none");
  }
  return result;
}

/**
 * Python `_canonical_url` via `urlsplit`/`urlunsplit`: lowercase scheme and netloc, strip
 * trailing slashes from the path, drop query and fragment.
 *
 * Split by hand rather than with `URL`, because `URL` also normalises percent-encoding and
 * resolves dot segments — transformations Python does not perform, which would show up as
 * a parity failure on an unusual path.
 */
export function canonicalUrl(url: unknown): string {
  const raw = pyStr(url);
  const match = /^([A-Za-z][A-Za-z0-9+.-]*):(.*)$/.exec(raw);
  if (!match) return raw.split(/[?#]/)[0].replace(/\/+$/, "");

  const scheme = match[1].toLowerCase();
  let rest = match[2];
  let netloc = "";
  if (rest.startsWith("//")) {
    rest = rest.slice(2);
    const boundary = rest.search(/[/?#]/);
    netloc = (boundary === -1 ? rest : rest.slice(0, boundary)).toLowerCase();
    rest = boundary === -1 ? "" : rest.slice(boundary);
  }
  const path = rest.split(/[?#]/)[0].replace(/\/+$/, "");

  // urlunsplit((scheme, netloc, path, "", ""))
  if (netloc) return `${scheme}://${netloc}${path}`;
  return path ? `${scheme}:${path}` : `${scheme}:`;
}

/**
 * Python `datetime.fromisoformat(...)` semantics for the timestamps this selector sees.
 * A value with no timezone designator is treated as UTC, matching Python's explicit
 * `replace(tzinfo=utc)` — JavaScript would otherwise read it as local time.
 */
function parseIsoUtc(value: string): number {
  const text = value.trim().replace(/Z$/i, "+00:00");
  const hasZone = /[+-]\d{2}:?\d{2}$/.test(text);
  const parsed = Date.parse(hasZone ? text : `${text}Z`);
  return parsed;
}

export type EligibilityOutcome = { eligible: boolean; reason: string };

/** Python `_eligible`, in the same order — the first failure wins. */
export function isEligible(
  candidate: MixCandidate,
  editionDate: string,
): EligibilityOutcome {
  if (candidate.eligible === false) {
    return { eligible: false, reason: "candidate marked ineligible" };
  }
  if (pyStr(candidate.sourceReliability).toLowerCase() === "low") {
    return { eligible: false, reason: "low source reliability" };
  }
  if (!candidate.id || !candidate.headline || !candidate.source) {
    return { eligible: false, reason: "missing required field" };
  }
  const url = pyStr(candidate.url);
  if (!url.startsWith("https://") && !url.startsWith("http://")) {
    return { eligible: false, reason: "invalid article URL" };
  }

  const published = parseIsoUtc(pyStr(candidate.publishedAt));
  if (!Number.isFinite(published)) {
    return { eligible: false, reason: "invalid publishedAt" };
  }
  const editionDay = Date.parse(`${pyStr(editionDate).trim()}T23:59:59Z`);
  if (!Number.isFinite(editionDay)) {
    return { eligible: false, reason: "invalid publishedAt" };
  }
  const age = editionDay - published;
  if (age > FRESHNESS_MAX_MS || age < FRESHNESS_MIN_MS) {
    return { eligible: false, reason: "outside 72-hour freshness window" };
  }
  return { eligible: true, reason: "" };
}

type DuplicateOutcome = { duplicate: boolean; reason: string };

/** Python `_duplicate`: identity, then canonical URL, then the editorial guard. */
function findDuplicate(
  candidate: MixCandidate,
  selected: MixCandidate[],
  guard: EditorialDuplicateGuard,
): DuplicateOutcome {
  const identity = candidate.underlyingStoryIdentity;
  const url = canonicalUrl(candidate.url);

  for (const other of selected) {
    if (identity && identity === other.underlyingStoryIdentity) {
      return { duplicate: true, reason: `duplicate underlyingStoryIdentity=${identity}` };
    }
    if (url && url === canonicalUrl(other.url)) {
      return { duplicate: true, reason: "duplicate canonical URL" };
    }
    const editorial = guard(candidate, other);
    if (editorial.duplicate) {
      return {
        duplicate: true,
        reason: `existing duplicate guard: ${editorial.rule ?? ""}: ${editorial.reason ?? ""}`,
      };
    }
  }
  return { duplicate: false, reason: "" };
}

/** Python `_topic_adjustment`. */
export function topicAdjustment(
  candidate: MixCandidate,
  topics: readonly string[],
): number {
  const candidateTopics = new Set((candidate.topics ?? []).map((t) => pyStr(t).toLowerCase()));
  let overlap = 0;
  for (const topic of topics) if (candidateTopics.has(topic)) overlap += 1;
  return TOPIC_ADJUSTMENT * overlap;
}

type RankParts = { sortKey: [number, string]; base: number; adjustment: number; final: number };

/** Python `_rank`. The diversity bonus is measured against what is ALREADY selected. */
function rank(
  candidate: MixCandidate,
  topics: readonly string[],
  selected: MixCandidate[],
): RankParts {
  const base = pyFloat(candidate.baseScore);
  const adjustment = topicAdjustment(candidate, topics);

  const categories = new Set(selected.map((c) => pyStr(c.category).toLowerCase()));
  // v2.1: the source-diversity bonus is measured per publisher FAMILY, so a second
  // section feed of the same publisher can never look like a new source.
  const families = new Set(selected.map((c) => publisherFamily(c.source)));

  let diversity = 0;
  if (!categories.has(pyStr(candidate.category).toLowerCase())) diversity += NEW_CATEGORY_BONUS;
  if (!families.has(publisherFamily(candidate.source))) diversity += NEW_SOURCE_BONUS;

  const final = base + adjustment + diversity;
  return { sortKey: [-final, pyStr(candidate.id)], base, adjustment, final };
}

function compareRankKeys(a: [number, string], b: [number, string]): number {
  if (a[0] !== b[0]) return a[0] < b[0] ? -1 : 1;
  return compareStrings(a[1], b[1]);
}

/**
 * Python `_pick`.
 *
 * Re-ranks on every iteration, because the diversity bonus depends on what has been picked
 * so far. A duplicate is CONSUMED rather than skipped — it leaves `remaining` and is logged
 * with its rejection reason — which is what stops a duplicate from being reconsidered.
 */
function pick(
  pool: MixCandidate[],
  count: number,
  topics: readonly string[],
  selected: MixCandidate[],
  phase: string,
  logs: Map<string, CandidateLog>,
  guard: EditorialDuplicateGuard,
  regions: readonly string[] = [],
  relaxFamily = false,
): MixCandidate[] {
  const picked: MixCandidate[] = [];
  const remaining = [...pool];

  while (remaining.length > 0 && picked.length < count) {
    const soFar = [...selected, ...picked];
    const ranked = remaining
      .map((candidate) => ({ key: rank(candidate, topics, soFar).sortKey, candidate }))
      .sort((a, b) => compareRankKeys(a.key, b.key));

    const candidate = ranked[0].candidate;
    remaining.splice(remaining.indexOf(candidate), 1);

    let rejection = findDuplicate(candidate, soFar, guard);
    if (!rejection.duplicate) {
      // Publisher-family caps run in EVERY phase, exactly like the duplicate guard:
      // a capped candidate is consumed and logged, never reconsidered.
      const familyReason = familyViolation(candidate, soFar, regions, relaxFamily);
      if (familyReason) rejection = { duplicate: true, reason: familyReason };
    }
    const parts = rank(candidate, topics, soFar);

    const log = logs.get(pyStr(candidate.id));
    if (log) {
      log.baseScore = parts.base;
      log.topicAdjustment = parts.adjustment;
      log.finalScore = parts.final;
      log.selectionPhase = phase;
      log.rejectionReason = rejection.duplicate ? rejection.reason : null;
    }
    if (rejection.duplicate) continue;
    picked.push(candidate);
  }
  return picked;
}

/**
 * Python `_initial_targets` (v2).
 *
 * Fixed priority united_states > japan > world. With the US selected alongside other
 * regions it takes US_MIN_QUOTA slots up front; the rest split evenly over the remaining
 * regions with any remainder awarded in priority order. Without the US, slots split
 * evenly with the remainder in priority order. The `candidates`/`identity` parameters
 * are kept for call-site parity with the Python signature.
 */
function initialTargets(
  regions: readonly string[],
  _candidates: MixCandidate[],
  size: number,
  _identity: string,
): Record<string, number> {
  const ordered = priorityOrder(regions);
  const targets: Record<string, number> = {};
  for (const region of regions) targets[region] = 0;

  if (ordered.includes("united_states") && ordered.length > 1) {
    const us = Math.min(US_MIN_QUOTA, size);
    targets["united_states"] = us;
    const others = ordered.filter((r) => r !== "united_states");
    const base = Math.floor((size - us) / others.length);
    const remainder = (size - us) % others.length;
    for (const region of others) targets[region] = base;
    for (const region of others.slice(0, remainder)) targets[region] += 1;
  } else {
    const base = Math.floor(size / ordered.length);
    const remainder = size % ordered.length;
    for (const region of ordered) targets[region] = base;
    for (const region of ordered.slice(0, remainder)) targets[region] += 1;
  }
  return targets;
}

/** Python `_dedup_count`. Deliberately separate from selection: it writes no logs. */
function dedupCount(pool: MixCandidate[], guard: EditorialDuplicateGuard): number {
  const unique: MixCandidate[] = [];
  const ordered = [...pool].sort((a, b) => {
    const scoreA = pyFloat(a.baseScore);
    const scoreB = pyFloat(b.baseScore);
    if (scoreA !== scoreB) return scoreB - scoreA;
    return compareStrings(pyStr(a.id), pyStr(b.id));
  });
  for (const candidate of ordered) {
    if (!findDuplicate(candidate, unique, guard).duplicate) unique.push(candidate);
  }
  return unique.length;
}

/** Python `select_custom_mix`. */
export function selectCustomMix(options: SelectCustomMixOptions): MixSelectionResult {
  const {
    candidates,
    date,
    regions,
    topics = [],
    size = 5,
    selectorVersion = SELECTOR_VERSION,
    // The REAL ported editorial guard is the default. A caller must opt in explicitly to
    // anything weaker — `noEditorialDuplicateGuard` is test-only.
    editorialDuplicateGuard: guard = productionEditorialDuplicateGuard,
  } = options;

  const normalized = normalizeMix(regions, topics);
  const selectedRegions = normalized.regions;
  const selectedTopics = normalized.topics;
  if (selectedRegions.length === 0) {
    throw new MixSelectionError("at least one region is required");
  }
  const identity = mixIdentity(date, selectedRegions, selectedTopics, selectorVersion, size);

  // ── one log per candidate, in ID order ────────────────────────────────────────────
  const logs = new Map<string, CandidateLog>();
  const eligible: MixCandidate[] = [];
  const orderedCandidates = [...candidates].sort((a, b) =>
    compareStrings(pyStr(a.id), pyStr(b.id)),
  );

  for (const candidate of orderedCandidates) {
    let outcome = isEligible(candidate, date);
    // v2 strict topic allowlist: an unselected topic is ineligible for EVERY phase,
    // fallback included — checked after the base checks so their reasons win.
    if (outcome.eligible && !topicAllowed(candidate, selectedTopics)) {
      outcome = { eligible: false, reason: "topic not selected (strict allowlist)" };
    }
    const regionMap = memberships(candidate);
    const regionEligibility = selectedRegions.filter((r) => regionMap[r] === "primary");
    logs.set(pyStr(candidate.id), {
      id: pyStr(candidate.id),
      baseScore: pyFloat(candidate.baseScore),
      topicAdjustment: topicAdjustment(candidate, selectedTopics),
      regionEligibility,
      finalScore: null,
      selectionPhase: null,
      rejectionReason: outcome.eligible ? null : outcome.reason,
    });
    if (outcome.eligible) eligible.push(candidate);
  }

  const regionPool = eligible.filter((c) => {
    const regionMap = memberships(c);
    return selectedRegions.some((r) => regionMap[r] === "primary");
  });

  const selected: MixCandidate[] = [];
  const assignedRegions: string[] = [];
  const isSelected = (c: MixCandidate): boolean => selected.includes(c);

  if (selectedRegions.length === 1) {
    const region = selectedRegions[0];
    const picked = pick(
      regionPool.filter((c) => memberships(c)[region] === "primary"),
      size,
      selectedTopics,
      selected,
      "regional_primary",
      logs,
      guard,
      selectedRegions,
    );
    selected.push(...picked);
    for (let i = 0; i < picked.length; i += 1) assignedRegions.push(region);
  } else {
    const targets = initialTargets(selectedRegions, regionPool, size, identity);
    // Quotas fill in PRIORITY order (US first), so the US takes its slots before any
    // lower-priority region can consume a story that also has US membership.
    for (const region of priorityOrder(selectedRegions)) {
      const picked = pick(
        regionPool.filter((c) => memberships(c)[region] === "primary" && !isSelected(c)),
        targets[region],
        selectedTopics,
        selected,
        `regional_quota:${region}`,
        logs,
        guard,
        selectedRegions,
      );
      selected.push(...picked);
      for (let i = 0; i < picked.length; i += 1) assignedRegions.push(region);
    }
    // Deterministic reallocation, still inside the selected-region scope, and still in
    // priority order: an unmet quota is refilled from the US pool first, then japan,
    // then world — a deep US pool grows the US share, never the other way around.
    if (selected.length < size) {
      for (const region of priorityOrder(selectedRegions)) {
        if (selected.length >= size) break;
        const picked = pick(
          regionPool.filter((c) => memberships(c)[region] === "primary" && !isSelected(c)),
          size - selected.length,
          selectedTopics,
          selected,
          `regional_reallocation:${region}`,
          logs,
          guard,
          selectedRegions,
        );
        selected.push(...picked);
        for (let i = 0; i < picked.length; i += 1) assignedRegions.push(region);
      }
    }
  }

  const regionalCount = selected.length;
  let fallbackSlots = 0;
  if (selected.length < size) {
    // v3: the REGION BOUNDARY is absolute — every fallback pass draws only from
    // candidates primary in a SELECTED region. Note the EMPTY topic tuple: a global
    // fallback is not topic-boosted.
    const picked = pick(
      regionPool.filter((c) => !isSelected(c)),
      size - selected.length,
      [],
      selected,
      "global_fallback",
      logs,
      guard,
      selectedRegions,
    );
    selected.push(...picked);
    fallbackSlots = picked.length;
    // LAST RESORT (v2.1): five stories beat the per-family principle. The UK cap, the
    // topic allowlist (applied at eligibility) and every duplicate guard stay enforced.
    if (selected.length < size) {
      const relaxed = pick(
        regionPool.filter((c) => !isSelected(c)),
        size - selected.length,
        [],
        selected,
        "global_fallback_relaxed",
        logs,
        guard,
        selectedRegions,
        true,
      );
      selected.push(...relaxed);
      fallbackSlots += relaxed.length;
    }
    for (let i = 0; i < fallbackSlots; i += 1) assignedRegions.push("global_fallback");
  }

  // A final guard, independent of the phases that produced the list.
  for (let i = 0; i < selected.length; i += 1) {
    const outcome = findDuplicate(selected[i], selected.slice(0, i), guard);
    if (outcome.duplicate) {
      throw new MixSelectionError(
        `final duplicate guard failed: ${pyStr(selected[i].id)}: ${outcome.reason}`,
      );
    }
  }

  const qualifying = regionPool.length;
  const regionalAfterDedup = dedupCount(regionPool, guard);
  const shortage = selected.length < size;
  const fallbackReason = shortage
    ? "insufficient total eligible candidates"
    : fallbackSlots
      ? "insufficient qualifying regional candidates"
      : null;

  const finalRegionMix: Record<string, number> = {};
  for (const region of assignedRegions) {
    finalRegionMix[region] = (finalRegionMix[region] ?? 0) + 1;
  }

  for (const candidate of selected) {
    const log = logs.get(pyStr(candidate.id));
    if (log && log.selectionPhase === null) log.selectionPhase = "selected";
  }
  for (const candidate of eligible) {
    const log = logs.get(pyStr(candidate.id));
    if (!log) continue;
    if (log.finalScore === null) {
      log.finalScore = pyFloat(candidate.baseScore) + log.topicAdjustment;
    }
    if (log.selectionPhase === null) {
      if (log.regionEligibility.length === 0) {
        log.selectionPhase = "outside_scope";
        log.rejectionReason = "not primary for any selected region";
      } else {
        log.selectionPhase = "regional_primary";
        log.rejectionReason = "lower deterministic rank after requested slots filled";
      }
    }
  }

  const candidateLogs = [...logs.keys()]
    .sort(compareStrings)
    .map((key) => logs.get(key) as CandidateLog);

  return {
    selectedIds: selected.map((c) => pyStr(c.id)),
    metadata: {
      selectedRegions: [...selectedRegions],
      selectedTopics: [...selectedTopics],
      requestedRegionCount: size,
      candidatePoolTotal: candidates.length,
      qualifyingRegionCandidates: qualifying,
      regionalCandidatesAfterDedup: regionalAfterDedup,
      selectedRegionStories: regionalCount,
      fallbackSlots,
      fallbackReason,
      selectorVersion,
      mixIdentity: identity,
      finalRegionMix,
      shortage,
      unfilledSlots: Math.max(0, size - selected.length),
    },
    candidateLogs,
  };
}
