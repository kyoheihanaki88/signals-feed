/**
 * Editorial duplicate-story guard. (Phase 3D-1.5)
 *
 * A faithful port of `same_underlying_story()` and `duplicate_story()` from
 * `pipeline/editorial.py`, including the reason strings verbatim — the parity tests compare
 * them character for character, so they are API, not prose.
 *
 * RULE ORDER (first match wins). The ordering is the whole design: the specific evidence
 * rules run BEFORE any generic roundup containment, so a roundup headline can never merge
 * two materially different product lines of one brand.
 *
 *   1. no-identity                 → not duplicate
 *   2. different-brand             → not duplicate
 *   3. not-product-story           → not duplicate  (a launch and an earnings story stay split)
 *   4. same-launch-event           → DUPLICATE      (strongest same-event evidence)
 *   5. same-product-family         → DUPLICATE      (equal or hierarchical: galaxy ⊃ galaxy z fold)
 *   6. roundup-covers-family       → DUPLICATE      (the roundup NAMES the other line)
 *   7. distinct-product-families   → not duplicate  (iphone vs mac — before any generic fallback)
 *   8. roundup-contains-broad      → DUPLICATE      (roundup + a side with no identified line)
 *   9. brand-product-news-no-line  → DUPLICATE      (neither side names a line)
 *
 * Rules 6 and 7 are the pair that fixed the reviewed false positive: an Apple iPhone
 * roundup plus a MacBook hands-on must stay separate, so "distinct product families" is
 * checked before the generic roundup fallback.
 *
 * No file, network, subprocess, environment or clock access.
 */

import {
  storyIdentity,
  type StoryIdentity,
  type StoryIdentityInput,
} from "./editorial-story-identity.js";

export type DuplicateDecision = {
  duplicate: boolean;
  reason: string;
  matchedRule: string;
};

/**
 * `_family_related`: two families describe the same line when they are equal, or when one
 * is a hierarchical prefix of the other ("galaxy" covers "galaxy z fold").
 */
export function familyRelated(a: string | null, b: string | null): boolean {
  if (a === b) return true;
  if (!a || !b) return false;
  return a.startsWith(`${b} `) || b.startsWith(`${a} `);
}

/** `same_underlying_story`. */
export function sameUnderlyingStory(
  idA: StoryIdentity | null,
  idB: StoryIdentity | null,
): DuplicateDecision {
  if (!idA || !idB) return { duplicate: false, reason: "", matchedRule: "no-identity" };
  if (idA.brand !== idB.brand) {
    return { duplicate: false, reason: "", matchedRule: "different-brand" };
  }
  if (!(idA.isProductStory && idB.isProductStory)) {
    return { duplicate: false, reason: "", matchedRule: "not-product-story" };
  }

  const brand = idA.brand;
  const fa = idA.productFamily;
  const fb = idB.productFamily;

  if (idA.launchEvent && idA.launchEvent === idB.launchEvent) {
    return {
      duplicate: true,
      reason: `same brand (${brand}) + same launch event (${idA.launchEvent})`,
      matchedRule: "same-launch-event",
    };
  }

  if ((fa || fb) && familyRelated(fa, fb)) {
    return {
      duplicate: true,
      reason: `same brand (${brand}) + same product family (${fa || fb})`,
      matchedRule: "same-product-family",
    };
  }

  // A roundup absorbs a specific story ONLY when it actually names that product line.
  // "Apple's iPhone 18 event: the 5 biggest announcements" does not cover a MacBook story.
  for (const [roundId, otherFam] of [
    [idA, fb],
    [idB, fa],
  ] as [StoryIdentity, string | null][]) {
    if (
      roundId.isRoundup &&
      otherFam &&
      roundId.coveredFamilies.some((family) => familyRelated(otherFam, family))
    ) {
      return {
        duplicate: true,
        reason: `same brand (${brand}) + roundup covers that product line (${otherFam})`,
        matchedRule: "roundup-covers-family",
      };
    }
  }

  if (fa && fb) {
    // Both lines identified and materially unrelated — genuinely distinct news value.
    return { duplicate: false, reason: "", matchedRule: "distinct-product-families" };
  }

  if (idA.isRoundup || idB.isRoundup) {
    return {
      duplicate: true,
      reason: `same brand (${brand}) + event roundup and a story with no distinct product line`,
      matchedRule: "roundup-contains-broad",
    };
  }

  if (!fa && !fb) {
    return {
      duplicate: true,
      reason: `same brand (${brand}) product news with no distinguishing product line`,
      matchedRule: "brand-product-news-no-line",
    };
  }

  return { duplicate: false, reason: "", matchedRule: "distinct-product-families" };
}

/** `duplicate_story`: the convenience wrapper over two raw stories. */
export function duplicateStory(
  a: StoryIdentityInput,
  b: StoryIdentityInput,
  nowMs?: number,
): DuplicateDecision {
  return sameUnderlyingStory(storyIdentity(a, nowMs), storyIdentity(b, nowMs));
}

/**
 * The shape the Custom Mix selector passes in. Declared structurally rather than importing
 * `MixCandidate`, so the editorial guard stays independent of the selector's types.
 */
export type EditorialGuardCandidate = {
  headline?: string;
  summary?: string;
};

/**
 * The PRODUCTION guard for the Custom Mix selector.
 *
 * Mirrors Python's `_duplicate`, which calls
 * `duplicate_story(candidate.headline, candidate.summary, other.headline, other.summary)` —
 * argument order included, since the rule order is not symmetric in general.
 */
export function productionEditorialDuplicateGuard(
  candidate: EditorialGuardCandidate,
  other: EditorialGuardCandidate,
): { duplicate: boolean; reason?: string; rule?: string } {
  const decision = duplicateStory(
    { title: candidate.headline, snippet: candidate.summary },
    { title: other.headline, snippet: other.summary },
  );
  return {
    duplicate: decision.duplicate,
    reason: decision.reason,
    rule: decision.matchedRule,
  };
}
