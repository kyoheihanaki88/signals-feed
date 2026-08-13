/**
 * Phase 3D-1 — canonical mix identity.
 *
 * Covers required tests A (alias normalization parity), B (input-order identity parity)
 * and N (unsupported region/topic fails).
 *
 * The Python module `pipeline/mix_identity.py` is the specification; these assertions are
 * the same ones its own unittest makes, plus the cases the port could plausibly get wrong.
 */

import assert from "node:assert/strict";
import test from "node:test";

import {
  canonicalKey,
  mixIdentity,
  normalizeMix,
  normalizeRegions,
  normalizeTopics,
} from "../_lib/custom-mix-identity.js";
import { UnsupportedMixValue } from "../_lib/custom-mix-types.js";

// ── A. alias normalization ────────────────────────────────────────────────────────────

test("A. every Japan alias collapses to one identity", () => {
  const identities = new Set(
    ["Japan", " japan ", "JP", "JPN", "japan"].map((alias) =>
      mixIdentity("2026-07-27", [alias], ["tech"]),
    ),
  );
  assert.equal(identities.size, 1);
  assert.equal(
    [...identities][0],
    "date=2026-07-27|regions=japan|topics=tech|selector=2|size=5",
  );
});

test("A2. every United States alias collapses to one canonical id", () => {
  assert.deepEqual(
    normalizeRegions(["US", "USA", "U.S.", "United States", "united_states"]),
    ["united_states"],
  );
  // The punctuation path is the subtle one: "U.S." → "u_s_" → strip → "u_s".
  assert.equal(canonicalKey("U.S."), "u_s");
  assert.equal(canonicalKey("  United   States  "), "united_states");
  assert.equal(canonicalKey("u-s"), "u_s");
});

test("A3. topics normalise, dedupe and sort", () => {
  assert.deepEqual(normalizeTopics(["Tech", "business", "tech"]), ["business", "tech"]);
  assert.deepEqual(normalizeTopics([]), []);
  assert.deepEqual(normalizeTopics(undefined), []);
});

test("A4. regions dedupe and sort", () => {
  assert.deepEqual(normalizeRegions(["world", "JP", "japan"]), ["japan", "world"]);
  assert.deepEqual(normalizeRegions(["world", "US", "JP"]), [
    "japan",
    "united_states",
    "world",
  ]);
});

// ── B. input order ────────────────────────────────────────────────────────────────────

test("B. input ordering never changes the identity", () => {
  const a = mixIdentity("2026-07-27", ["world", "JP"], ["tech", "business"]);
  const b = mixIdentity("2026-07-27", ["japan", "world"], ["business", "tech"]);
  const c = mixIdentity("2026-07-27", ["JPN", "world", "japan"], ["tech", "business", "tech"]);
  assert.equal(a, b);
  assert.equal(b, c);
});

test("B2. a different mix is a different identity", () => {
  assert.notEqual(
    mixIdentity("2026-07-27", ["japan"], ["tech"]),
    mixIdentity("2026-07-27", ["japan"], ["health"]),
  );
  assert.notEqual(
    mixIdentity("2026-07-27", ["japan"], ["tech"]),
    mixIdentity("2026-07-28", ["japan"], ["tech"]),
  );
  assert.notEqual(
    mixIdentity("2026-07-27", ["japan"], ["tech"], 1, 5),
    mixIdentity("2026-07-27", ["japan"], ["tech"], 1, 6),
  );
  assert.notEqual(
    mixIdentity("2026-07-27", ["japan"], ["tech"], 1),
    mixIdentity("2026-07-27", ["japan"], ["tech"], 2),
  );
});

test("B3. the identity string has the exact Python shape", () => {
  assert.equal(
    mixIdentity(" 2026-07-27 ", ["US", "JP"], ["tech", "business"]),
    "date=2026-07-27|regions=japan,united_states|topics=business,tech|selector=2|size=5",
  );
  // Empty topics still emit the key with an empty value.
  assert.equal(
    mixIdentity("2026-07-27", ["japan"], []),
    "date=2026-07-27|regions=japan|topics=|selector=2|size=5",
  );
});

test("B4. selector version and size are truncated toward zero, as Python's int() does", () => {
  assert.equal(
    mixIdentity("2026-07-27", ["japan"], [], 1.9, 5.7),
    "date=2026-07-27|regions=japan|topics=|selector=1|size=5",
  );
});

// ── N. unsupported values fail explicitly ─────────────────────────────────────────────

test("N. an unsupported region fails explicitly", () => {
  assert.throws(
    () => normalizeRegions(["mars"]),
    (error: unknown) =>
      error instanceof UnsupportedMixValue && /unsupported region: mars/.test(error.message),
  );
});

test("N2. an unsupported topic fails explicitly", () => {
  assert.throws(
    () => normalizeTopics(["politics"]),
    (error: unknown) =>
      error instanceof UnsupportedMixValue && /unsupported topic: politics/.test(error.message),
  );
});

test("N3. every unsupported value is reported at once, sorted, using the ORIGINAL text", () => {
  assert.throws(
    () => normalizeRegions(["mars", "atlantis", "Mars"]),
    (error: unknown) =>
      error instanceof UnsupportedMixValue &&
      error.message === "unsupported region: Mars, atlantis, mars",
  );
});

test("N4. a supported value alongside an unsupported one still fails", () => {
  assert.throws(() => normalizeRegions(["japan", "mars"]), UnsupportedMixValue);
  assert.throws(() => normalizeMix(["japan"], ["tech", "sports"]), UnsupportedMixValue);
});

test("N5. empty and whitespace-only values are unsupported, not silently dropped", () => {
  assert.throws(() => normalizeRegions([""]), UnsupportedMixValue);
  assert.throws(() => normalizeRegions(["   "]), UnsupportedMixValue);
  assert.throws(() => normalizeRegions([null]), UnsupportedMixValue);
});

test("normalizeMix returns both halves normalised", () => {
  assert.deepEqual(normalizeMix(["US", "JP"], ["tech", "business"]), {
    regions: ["japan", "united_states"],
    topics: ["business", "tech"],
  });
});
