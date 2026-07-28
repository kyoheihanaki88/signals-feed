/**
 * Phase 3D-1.5 — editorial duplicate guard.
 *
 * Covers required tests B (matched rule), C (reason strings), D (positive cases),
 * E (negative cases), G (roundup containment), H (unrelated-family protection),
 * I (brand-only protection), J (product/non-product protection), O (reversed ordering)
 * and P (repeated runs).
 *
 * The reason strings are compared verbatim: they are part of the contract Python and
 * TypeScript share, not decoration.
 */

import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
  duplicateStory,
  familyRelated,
  productionEditorialDuplicateGuard,
  sameUnderlyingStory,
} from "../_lib/editorial-duplicate-guard.js";
import { storyIdentity } from "../_lib/editorial-story-identity.js";

const FIXTURE_DIR = resolve(dirname(fileURLToPath(import.meta.url)), "..", "_fixtures");

type Pair = {
  id: string;
  intent: string;
  a: { title: string; snippet: string };
  b: { title: string; snippet: string };
};

const pairs = (
  JSON.parse(
    readFileSync(join(FIXTURE_DIR, "editorial_duplicate_cases.json"), "utf8"),
  ) as { pairs: Pair[] }
).pairs;

function pair(id: string): Pair {
  const found = pairs.find((p) => p.id === id);
  assert.ok(found, `fixture is missing pair ${id}`);
  return found;
}

function decide(id: string) {
  const p = pair(id);
  return duplicateStory(
    { title: p.a.title, snippet: p.a.snippet },
    { title: p.b.title, snippet: p.b.snippet },
  );
}

// ── B. matched rules ──────────────────────────────────────────────────────────────────

test("B. each fixture pair resolves to its expected rule", () => {
  const expected: Record<string, string> = {
    A_unpacked_roundup_vs_zfold_handson: "same-product-family",
    C_launch_vs_semiconductor_earnings: "not-product-story",
    D_apple_iphone_roundup_vs_macbook_handson: "distinct-product-families",
    E_broad_apple_roundup_naming_macbook: "roundup-covers-family",
    I_same_launch_event_broad_and_individual: "same-launch-event",
    K_both_identities_empty: "no-identity",
    P_different_brand_same_generic_terms: "different-brand",
    U_roundup_contains_broad: "roundup-contains-broad",
    V_brand_product_news_no_line: "brand-product-news-no-line",
  };
  for (const [id, rule] of Object.entries(expected)) {
    assert.equal(decide(id).matchedRule, rule, `${id} matched the wrong rule`);
  }
});

test("B2. all nine rules are reachable from the fixture", () => {
  const seen = new Set(pairs.map((p) => decide(p.id).matchedRule));
  assert.deepEqual([...seen].sort(), [
    "brand-product-news-no-line",
    "different-brand",
    "distinct-product-families",
    "no-identity",
    "not-product-story",
    "roundup-contains-broad",
    "roundup-covers-family",
    "same-launch-event",
    "same-product-family",
  ]);
});

test("B3. the specific rules run BEFORE any generic roundup containment", () => {
  // If roundup containment ran first, D would be a duplicate. It must not be.
  assert.equal(decide("D_apple_iphone_roundup_vs_macbook_handson").duplicate, false);
  assert.equal(
    decide("D_apple_iphone_roundup_vs_macbook_handson").matchedRule,
    "distinct-product-families",
  );
});

// ── C. reason strings ─────────────────────────────────────────────────────────────────

test("C. reason strings match Python verbatim", () => {
  // The reason names `fa or fb` — the FIRST side's family. Here the roundup resolves to
  // the broad "galaxy" and the hands-on to "galaxy z fold"; they are hierarchically
  // related, and Python reports the left-hand one.
  assert.equal(
    decide("A_unpacked_roundup_vs_zfold_handson").reason,
    "same brand (samsung) + same product family (galaxy)",
  );
  assert.equal(
    decide("I_same_launch_event_broad_and_individual").reason,
    "same brand (samsung) + same launch event (unpacked)",
  );
  assert.equal(
    decide("E_broad_apple_roundup_naming_macbook").reason,
    "same brand (apple) + roundup covers that product line (mac)",
  );
  assert.equal(
    decide("U_roundup_contains_broad").reason,
    "same brand (sony) + event roundup and a story with no distinct product line",
  );
  assert.equal(
    decide("V_brand_product_news_no_line").reason,
    "same brand (sony) product news with no distinguishing product line",
  );
});

test("C2. a non-duplicate carries an EMPTY reason, never a explanatory string", () => {
  for (const id of [
    "C_launch_vs_semiconductor_earnings",
    "D_apple_iphone_roundup_vs_macbook_handson",
    "K_both_identities_empty",
    "P_different_brand_same_generic_terms",
  ]) {
    const decision = decide(id);
    assert.equal(decision.duplicate, false);
    assert.equal(decision.reason, "");
  }
});

// ── D / E. positive and negative cases ────────────────────────────────────────────────

test("D. every fixture pair marked 'duplicate' is a duplicate", () => {
  const positives = pairs.filter((p) => p.intent === "duplicate");
  assert.ok(positives.length >= 7, `only ${positives.length} positive cases`);
  for (const p of positives) {
    assert.equal(decide(p.id).duplicate, true, `${p.id} should be a duplicate`);
  }
});

test("E. every fixture pair marked 'not-duplicate' is not a duplicate", () => {
  const negatives = pairs.filter((p) => p.intent === "not-duplicate");
  assert.ok(negatives.length >= 8, `only ${negatives.length} negative cases`);
  for (const p of negatives) {
    assert.equal(decide(p.id).duplicate, false, `${p.id} should NOT be a duplicate`);
  }
});

// ── G. roundup containment ────────────────────────────────────────────────────────────

test("G. a roundup absorbs a story only when it NAMES that product line", () => {
  // Names the MacBook → duplicate.
  assert.equal(decide("E_broad_apple_roundup_naming_macbook").duplicate, true);
  // Does not name it → not duplicate.
  assert.equal(decide("D_apple_iphone_roundup_vs_macbook_handson").duplicate, false);
});

test("G2. a roundup and a hands-on about the SAME line are one story", () => {
  assert.equal(decide("H_same_family_roundup_and_handson").duplicate, true);
});

// ── H. unrelated-family protection ────────────────────────────────────────────────────

test("H. two unrelated lines of one brand stay separate, even with a recap headline", () => {
  assert.equal(decide("F_same_brand_unrelated_families_with_recap").duplicate, false);
  assert.equal(decide("G_galaxy_watch_recap_vs_zfold_handson").duplicate, false);
  assert.equal(decide("O_covered_family_specificity_pruning").duplicate, false);
});

test("H2. the family hierarchy still merges a parent and its child", () => {
  assert.equal(decide("N_parent_child_family_hierarchy").duplicate, true);
  assert.equal(familyRelated("galaxy", "galaxy z fold"), true);
  assert.equal(familyRelated("galaxy z fold", "galaxy"), true);
  assert.equal(familyRelated("galaxy watch", "galaxy z fold"), false);
  assert.equal(familyRelated("iphone", "mac"), false);
  assert.equal(familyRelated(null, "mac"), false);
  assert.equal(familyRelated(null, null), true);
});

// ── I. brand-only protection ──────────────────────────────────────────────────────────

test("I. a shared brand alone is never enough", () => {
  assert.equal(decide("J_brand_only_match").duplicate, false);
  assert.equal(decide("J_brand_only_match").matchedRule, "not-product-story");
});

test("I2. different brands are rejected before anything else is considered", () => {
  assert.equal(decide("P_different_brand_same_generic_terms").matchedRule, "different-brand");
});

// ── J. product / non-product protection ───────────────────────────────────────────────

test("J. a launch and a non-product story about one brand stay independent", () => {
  assert.equal(decide("C_launch_vs_semiconductor_earnings").duplicate, false);
  assert.equal(decide("L_product_story_vs_legal_story").duplicate, false);
  assert.equal(decide("R_same_event_materially_distinct_followup").duplicate, false);
});

// ── O. ordering ───────────────────────────────────────────────────────────────────────

test("O. reversing the pair yields the same decision for every fixture case", () => {
  for (const p of pairs) {
    const forward = duplicateStory(
      { title: p.a.title, snippet: p.a.snippet },
      { title: p.b.title, snippet: p.b.snippet },
    );
    const reversed = duplicateStory(
      { title: p.b.title, snippet: p.b.snippet },
      { title: p.a.title, snippet: p.a.snippet },
    );
    assert.equal(reversed.duplicate, forward.duplicate, `${p.id}: duplicate flag flipped`);
    assert.equal(reversed.matchedRule, forward.matchedRule, `${p.id}: rule changed`);
  }
});

// ── K. empty identity ─────────────────────────────────────────────────────────────────

test("K. a null identity on either side short-circuits to no-identity", () => {
  const real = storyIdentity({ title: "Samsung launches the Galaxy Z Fold 8 phone" });
  assert.deepEqual(sameUnderlyingStory(null, null), {
    duplicate: false,
    reason: "",
    matchedRule: "no-identity",
  });
  assert.deepEqual(sameUnderlyingStory(real, null), {
    duplicate: false,
    reason: "",
    matchedRule: "no-identity",
  });
  assert.deepEqual(sameUnderlyingStory(null, real), {
    duplicate: false,
    reason: "",
    matchedRule: "no-identity",
  });
});

// ── P. determinism ────────────────────────────────────────────────────────────────────

test("P. repeated evaluation is byte-equivalent", () => {
  const first = pairs.map((p) => decide(p.id));
  const second = pairs.map((p) => decide(p.id));
  assert.equal(JSON.stringify(first), JSON.stringify(second));
});

// ── the selector-facing adapter ───────────────────────────────────────────────────────

test("the production guard adapts the selector's candidate shape", () => {
  const decision = productionEditorialDuplicateGuard(
    {
      headline: "Samsung Galaxy Unpacked 2026: The 6 biggest announcements",
      summary: "Everything Samsung showed, including the Z Fold 8.",
    },
    {
      headline: "Samsung's wider Z Fold 8 feels just right",
      summary: "An hour with the new foldable.",
    },
  );
  assert.equal(decision.duplicate, true);
  assert.equal(decision.rule, "same-product-family");
  assert.equal(decision.reason, "same brand (samsung) + same product family (galaxy z fold)");
});

test("the production guard tolerates missing headline and summary", () => {
  assert.deepEqual(productionEditorialDuplicateGuard({}, {}), {
    duplicate: false,
    reason: "",
    rule: "no-identity",
  });
});
