/**
 * Custom Mix selector types. (Phase 3D-1)
 *
 * These mirror the Phase 2A Python contract in `pipeline/custom_mix_selector.py` exactly.
 * The Python implementation is the SPECIFICATION: where a name or a shape looks odd here,
 * it is because Python produces it, and cross-language parity outranks TypeScript taste.
 *
 * Nothing in this module reads a file, spawns a process or touches the environment.
 */

export const SELECTOR_VERSION = 1;

export const SUPPORTED_REGIONS = ["japan", "united_states", "world"] as const;
export const SUPPORTED_TOPICS = [
  "ai",
  "business",
  "climate",
  "culture",
  "health",
  "science",
  "tech",
] as const;

export type Region = (typeof SUPPORTED_REGIONS)[number];
export type Topic = (typeof SUPPORTED_TOPICS)[number];

/** Python: `classify_region` returns exactly these strengths. */
export type RegionStrength = "primary" | "incidental" | "none";

export type RegionMembership = {
  region?: string;
  /** Python accepts `id` as an alias for `region`. */
  id?: string;
  strength?: string;
  evidence?: string[];
};

/**
 * The Phase 2 candidate shape. Optional fields are optional in Python too — the selector
 * defaults them rather than rejecting, and the eligibility rules decide what survives.
 */
export type MixCandidate = {
  id: string;
  headline?: string;
  summary?: string;
  source?: string;
  url?: string;
  publishedAt?: string;
  category?: string;
  topics?: string[];
  regionMemberships?: RegionMembership[];
  baseScore?: number;
  sourceReliability?: string;
  topicFingerprint?: string[];
  underlyingStoryIdentity?: string;
  /** Python: an explicit `false` short-circuits eligibility. */
  eligible?: boolean;
};

/** One row of `candidateLogs`. Field order is irrelevant; the value set is not. */
export type CandidateLog = {
  id: string;
  baseScore: number;
  topicAdjustment: number;
  regionEligibility: string[];
  finalScore: number | null;
  selectionPhase: string | null;
  rejectionReason: string | null;
};

export type MixMetadata = {
  selectedRegions: string[];
  selectedTopics: string[];
  requestedRegionCount: number;
  candidatePoolTotal: number;
  qualifyingRegionCandidates: number;
  regionalCandidatesAfterDedup: number;
  selectedRegionStories: number;
  fallbackSlots: number;
  fallbackReason: string | null;
  selectorVersion: number;
  mixIdentity: string;
  finalRegionMix: Record<string, number>;
  shortage: boolean;
  unfilledSlots: number;
};

export type MixSelectionResult = {
  selectedIds: string[];
  metadata: MixMetadata;
  candidateLogs: CandidateLog[];
};

/**
 * The third duplicate check.
 *
 * Python's `_duplicate` consults `editorial.duplicate_story` after the identity and URL
 * checks. That function is the whole Phase-2.6 editorial guard — brand and product-family
 * regexes, roundup containment, launch-event rules — and its behaviour is NOT encoded in
 * the candidate contract, so it cannot be ported from the fixture.
 *
 * It is therefore an explicit injected seam. Measured against the golden fixture, Python
 * calls it 392 times across the eight golden scenarios and it returns `true` ZERO times.
 * `custom-mix-selector-parity.test.ts` asserts that the TypeScript port reaches the seam
 * the SAME number of times, scenario by scenario, which is what makes the enforcement order
 * verifiable even though the rule never fires on this data.
 *
 * So `noEditorialDuplicateGuard` is provably equivalent ON THIS FIXTURE and provably
 * nothing beyond it. It is NOT a port of the editorial guard and is NOT production-complete.
 * A real implementation must be supplied and tested before this selector is connected to
 * `/api/edition`.
 */
export type EditorialDuplicateGuard = (
  a: MixCandidate,
  b: MixCandidate,
) => { duplicate: boolean; reason?: string; rule?: string };

/** The documented no-op. Named so it can never be mistaken for a ported implementation. */
export const noEditorialDuplicateGuard: EditorialDuplicateGuard = () => ({
  duplicate: false,
});

export type SelectCustomMixOptions = {
  candidates: MixCandidate[];
  date: string;
  regions: unknown[];
  topics?: unknown[];
  size?: number;
  selectorVersion?: number;
  editorialDuplicateGuard?: EditorialDuplicateGuard;
};

export class UnsupportedMixValue extends Error {
  constructor(message: string) {
    super(message);
    this.name = "UnsupportedMixValue";
  }
}

/** Python raises a bare `ValueError` here, not `UnsupportedMixValue`. */
export class MixSelectionError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "MixSelectionError";
  }
}
