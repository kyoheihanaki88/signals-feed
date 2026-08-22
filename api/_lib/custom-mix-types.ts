/**
 * Custom Mix selector types. (Phase 3D-1)
 *
 * These mirror the Phase 2A Python contract in `pipeline/custom_mix_selector.py` exactly.
 * The Python implementation is the SPECIFICATION: where a name or a shape looks odd here,
 * it is because Python produces it, and cross-language parity outranks TypeScript taste.
 *
 * Nothing in this module reads a file, spawns a process or touches the environment.
 */

/**
 * Selection semantics version. v3 (2026-08-18): canonical (category) topic allowlist,
 * publisher-family caps (max 1 per family; UK families capped at 1 total while the US
 * is active), an absolute region boundary (no fallback ever crosses into an unselected
 * region), CBS US structured-region sourcing and a coverage-aware enrichment pool.
 * Each bump invalidates every previous mix identity, so no cache computed under older
 * semantics can ever be reused.
 */
export const SELECTOR_VERSION = 3;

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
 * checks. As of Phase 3D-1.5 that function IS ported —
 * `editorial-duplicate-guard.ts` / `editorial-story-identity.ts` — and
 * `productionEditorialDuplicateGuard` is the selector's default. It stays an injected seam
 * so tests can substitute a spy or a deliberately-firing stub.
 */
export type EditorialDuplicateGuard = (
  a: MixCandidate,
  b: MixCandidate,
) => { duplicate: boolean; reason?: string; rule?: string };

/**
 * TEST-ONLY no-op.
 *
 * This is NOT the production default and must never be passed by production code: it
 * disables the third duplicate rule entirely. It remains useful for isolating the identity
 * and URL gates in a test, and for demonstrating that the real guard changes nothing on the
 * golden fixture.
 */
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
