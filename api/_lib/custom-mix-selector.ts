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

import { createHash } from "node:crypto";

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
  const sources = new Set(selected.map((c) => pyStr(c.source).toLowerCase()));

  let diversity = 0;
  if (!categories.has(pyStr(candidate.category).toLowerCase())) diversity += NEW_CATEGORY_BONUS;
  if (!sources.has(pyStr(candidate.source).toLowerCase())) diversity += NEW_SOURCE_BONUS;

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

    const duplicate = findDuplicate(candidate, soFar, guard);
    const parts = rank(candidate, topics, soFar);

    const log = logs.get(pyStr(candidate.id));
    if (log) {
      log.baseScore = parts.base;
      log.topicAdjustment = parts.adjustment;
      log.finalScore = parts.final;
      log.selectionPhase = phase;
      log.rejectionReason = duplicate.duplicate ? duplicate.reason : null;
    }
    if (duplicate.duplicate) continue;
    picked.push(candidate);
  }
  return picked;
}

/**
 * Python `_initial_targets`.
 *
 * `divmod` splits the slots evenly; any remainder goes to the regions with the strongest
 * top-N candidates. The SHA-256 of `identity|region` breaks a strength tie deterministically
 * without falling back to alphabetical order, so "japan" is not permanently privileged over
 * "united_states" when both are equally strong.
 */
function initialTargets(
  regions: readonly string[],
  candidates: MixCandidate[],
  size: number,
  identity: string,
): Record<string, number> {
  const base = Math.floor(size / regions.length);
  const remainder = size % regions.length;

  const targets: Record<string, number> = {};
  for (const region of regions) targets[region] = base;
  if (remainder === 0) return targets;

  const strengths: Record<string, number> = {};
  for (const region of regions) {
    const scores = candidates
      .filter((c) => memberships(c)[region] === "primary")
      .map((c) => pyFloat(c.baseScore))
      .sort((a, b) => b - a)
      .slice(0, base + 1);
    strengths[region] = scores.reduce((total, value) => total + value, 0);
  }

  const ordered = [...regions].sort((a, b) => {
    if (strengths[a] !== strengths[b]) return strengths[b] - strengths[a]; // -strength
    const hashA = createHash("sha256").update(`${identity}|${a}`).digest("hex");
    const hashB = createHash("sha256").update(`${identity}|${b}`).digest("hex");
    if (hashA !== hashB) return compareStrings(hashA, hashB);
    return compareStrings(a, b);
  });

  for (const region of ordered.slice(0, remainder)) targets[region] += 1;
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
    const outcome = isEligible(candidate, date);
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
    );
    selected.push(...picked);
    for (let i = 0; i < picked.length; i += 1) assignedRegions.push(region);
  } else {
    const targets = initialTargets(selectedRegions, regionPool, size, identity);
    for (const region of selectedRegions) {
      const picked = pick(
        regionPool.filter((c) => memberships(c)[region] === "primary" && !isSelected(c)),
        targets[region],
        selectedTopics,
        selected,
        `regional_quota:${region}`,
        logs,
        guard,
      );
      selected.push(...picked);
      for (let i = 0; i < picked.length; i += 1) assignedRegions.push(region);
    }
    if (selected.length < size) {
      const picked = pick(
        regionPool.filter((c) => !isSelected(c)),
        size - selected.length,
        selectedTopics,
        selected,
        "regional_reallocation",
        logs,
        guard,
      );
      selected.push(...picked);
      for (const candidate of picked) {
        const regionMap = memberships(candidate);
        const region = selectedRegions.find((r) => regionMap[r] === "primary");
        if (region === undefined) {
          throw new MixSelectionError("reallocated candidate has no selected-region primary");
        }
        assignedRegions.push(region);
      }
    }
  }

  const regionalCount = selected.length;
  let fallbackSlots = 0;
  if (selected.length < size) {
    // Note the EMPTY topic tuple: a global fallback is not topic-boosted.
    const picked = pick(
      eligible.filter((c) => !isSelected(c)),
      size - selected.length,
      [],
      selected,
      "global_fallback",
      logs,
      guard,
    );
    selected.push(...picked);
    fallbackSlots = picked.length;
    for (let i = 0; i < picked.length; i += 1) assignedRegions.push("global_fallback");
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
