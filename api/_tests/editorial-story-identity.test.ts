/**
 * Phase 3D-1.5 — editorial story identity.
 *
 * Covers required tests A (identity extraction), F (specificity pruning) and K (empty
 * identity). Python's `story_identity()` is the specification.
 */

import assert from "node:assert/strict";
import test from "node:test";

import {
  eventClassification,
  isConsumerLaunch,
  isFresh,
  storyIdentity,
} from "../_lib/editorial-story-identity.js";

function identity(title: string, snippet = "") {
  return storyIdentity({ title, snippet });
}

// ── A. brand extraction ───────────────────────────────────────────────────────────────

test("A. a recognised brand is extracted; an unrecognised one yields NO identity", () => {
  assert.equal(identity("Samsung launches a new phone")?.brand, "samsung");
  assert.equal(identity("The new iPhone 18 arrives")?.brand, "apple");
  assert.equal(identity("Google Pixel 11 review")?.brand, "google");
  assert.equal(identity("Sony unveils a camera")?.brand, "sony");
  // Unknown maker → null. The guard must never invent an identity.
  assert.equal(identity("Fairphone launches a repairable smartphone"), null);
  assert.equal(identity("Lenders tighten mortgage rules"), null);
});

test("A2. brand order is first-match-wins, as in Python", () => {
  // "apple" precedes "google" in the table, so a story naming both resolves to apple.
  assert.equal(identity("Apple and Google settle a dispute")?.brand, "apple");
});

test("A3. product families resolve most-specific-first", () => {
  assert.equal(identity("Samsung Galaxy Z Fold 8 review")?.productFamily, "galaxy z fold");
  assert.equal(identity("Samsung Galaxy Z Flip 8 review")?.productFamily, "galaxy z flip");
  assert.equal(identity("Samsung Galaxy S26 review")?.productFamily, "galaxy s");
  assert.equal(identity("Samsung Galaxy Watch 8 review")?.productFamily, "galaxy watch");
  assert.equal(identity("Samsung Galaxy Buds 4 review")?.productFamily, "galaxy buds");
  assert.equal(identity("Samsung Galaxy range refresh")?.productFamily, "galaxy");
  assert.equal(identity("The new MacBook Pro review")?.productFamily, "mac");
  assert.equal(identity("The new iPad Pro review")?.productFamily, "ipad");
});

test("A4. named launch events are extracted", () => {
  assert.equal(identity("Samsung Galaxy Unpacked 2026")?.launchEvent, "unpacked");
  assert.equal(identity("Apple WWDC keynote highlights")?.launchEvent, "wwdc");
  assert.equal(identity("Google made by Google event")?.launchEvent, "made by google");
  assert.equal(identity("Sony at CES this year")?.launchEvent, "trade show");
  assert.equal(identity("Samsung Galaxy S26 review")?.launchEvent, null);
});

test("A5. roundup detection reads the TITLE only, never the snippet", () => {
  assert.equal(identity("Samsung Galaxy Unpacked: the biggest announcements")?.isRoundup, true);
  assert.equal(identity("Samsung Galaxy Z Fold 8 recap")?.isRoundup, true);
  // The same word in the snippet must not make it a roundup.
  assert.equal(
    identity("Samsung's Z Fold 8 feels right", "Everything announced at the event")?.isRoundup,
    false,
  );
});

// ── F. specificity pruning ────────────────────────────────────────────────────────────

test("F. a broad parent family is pruned when a specific descendant also matched", () => {
  // "Galaxy Watch 8" matches BOTH "galaxy watch" and the broad "galaxy". Keeping the
  // parent would make every Samsung roundup appear to cover every Galaxy line.
  const watch = identity("Samsung Galaxy Watch 8 recap");
  assert.deepEqual(watch?.coveredFamilies, ["galaxy watch"]);
  assert.ok(!watch?.coveredFamilies.includes("galaxy"));

  const fold = identity("Samsung Galaxy Z Fold 8 hands on");
  assert.deepEqual(fold?.coveredFamilies, ["galaxy z fold"]);
});

test("F2. a genuinely broad story keeps the parent family", () => {
  assert.deepEqual(identity("Samsung's Galaxy line gets a refresh")?.coveredFamilies, ["galaxy"]);
});

test("F3. a roundup naming several lines covers all of them, sorted", () => {
  const roundup = identity(
    "Apple event recap: everything announced",
    "From the iPhone 18 to the new MacBook Pro and the iPad refresh.",
  );
  assert.deepEqual(roundup?.coveredFamilies, ["ipad", "iphone", "mac"]);
});

// ── consumer-launch gate ──────────────────────────────────────────────────────────────

test("the consumer-launch gate requires a launch verb in the TITLE", () => {
  assert.equal(isConsumerLaunch({ title: "Samsung launches a new phone" }), true);
  // A verb only in the snippet does not qualify.
  assert.equal(
    isConsumerLaunch({ title: "Samsung's quiet year", snippet: "It announced a new phone" }),
    false,
  );
});

test("rumours, leaks, deals, benchmarks and hands-on never qualify as launches", () => {
  for (const title of [
    "Samsung reportedly launches a new phone",
    "Leaked: Samsung launches a new phone",
    "Samsung launches a new phone, rumour says",
    "Samsung launches a new phone deals roundup",
    "Hands-on: Samsung launches a new phone",
    "Samsung launches a new phone benchmark results",
  ]) {
    assert.equal(isConsumerLaunch({ title }), false, `"${title}" qualified`);
  }
});

test("accessory refreshes are not platform-level launches", () => {
  assert.equal(isConsumerLaunch({ title: "Apple launches new iPhone cases" }), false);
  assert.equal(isConsumerLaunch({ title: "Apple launches new iPhone chargers" }), false);
});

test("a low-reliability source can never produce a launch", () => {
  assert.equal(isConsumerLaunch({ title: "Samsung launches a new phone" }), true);
  assert.equal(
    isConsumerLaunch({ title: "Samsung launches a new phone", reliability: "low" }),
    false,
  );
});

test("an unknown timestamp is treated as fresh — missing metadata is neutral", () => {
  assert.equal(isFresh(undefined), true);
  assert.equal(isFresh(null), true);
  assert.equal(isFresh("not-a-date"), true);
  assert.equal(isFresh("2026-07-27T00:00:00Z", Date.parse("2026-07-27T06:00:00Z")), true);
  assert.equal(isFresh("2026-07-01T00:00:00Z", Date.parse("2026-07-27T06:00:00Z")), false);
});

// ── event families ────────────────────────────────────────────────────────────────────

test("event families are classified in Python's order, launch first", () => {
  assert.equal(
    eventClassification({ title: "Samsung launches a new phone" }).eventFamily,
    "consumer_launch",
  );
  assert.equal(
    eventClassification({ title: "Samsung quarterly profit rises" }).eventFamily,
    "earnings",
  );
  // "shares" hits the markets pattern; the earnings pattern needs "earnings" or an
  // explicit "quarterly results" form. Verified against Python, which returns markets.
  assert.equal(
    eventClassification({ title: "Samsung shares slide after results" }).eventFamily,
    "markets",
  );
  assert.equal(
    eventClassification({ title: "Samsung quarterly revenue beats forecasts" }).eventFamily,
    "earnings",
  );
  assert.equal(eventClassification({ title: "A quiet week in Tokyo" }).eventFamily, "other");
});

test("a crisis story is never classified as a science discovery", () => {
  const crisis = eventClassification({
    title: "Her body was discovered near the river",
    snippet: "Police confirmed the death overnight.",
  });
  assert.notEqual(crisis.eventFamily, "science_discovery");
});

// ── K. empty identity ─────────────────────────────────────────────────────────────────

test("K. a story with no recognised brand has no identity at all", () => {
  assert.equal(identity("Museum returns looted artifacts"), null);
  assert.equal(identity(""), null);
  assert.equal(storyIdentity({}), null);
});

test("eventFamily 'other' is NOT the same as isProductStory false", () => {
  // A hands-on is excluded from the launch gate, so its family falls back to "other" —
  // but it names a product line, so it IS a product story.
  const handsOn = identity("Samsung's wider Z Fold 8 feels just right");
  assert.equal(handsOn?.eventFamily, "other");
  assert.equal(handsOn?.isProductStory, true);

  // An earnings story names no line and is not a product family → not a product story.
  const earnings = identity("Samsung quarterly profit rises on chip demand");
  assert.equal(earnings?.eventFamily, "earnings");
  assert.equal(earnings?.isProductStory, false);
});

test("a company name alone is not a product-story identity", () => {
  const legal = identity("Apple faces a new antitrust complaint in Brussels");
  assert.equal(legal?.brand, "apple");
  assert.equal(legal?.productFamily, null);
  assert.equal(legal?.isProductStory, false);
});

test("identity extraction is deterministic across repeated calls", () => {
  const title = "Samsung Galaxy Unpacked 2026: The 6 biggest announcements";
  const snippet = "Everything Samsung showed, from the Z Fold 8 to the Galaxy Watch.";
  assert.equal(
    JSON.stringify(storyIdentity({ title, snippet })),
    JSON.stringify(storyIdentity({ title, snippet })),
  );
});
