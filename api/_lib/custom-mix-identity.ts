/**
 * Canonical Custom Mix identity. (Phase 3D-1)
 *
 * A direct port of `pipeline/mix_identity.py`. Every step below exists because Python does
 * it, in the order Python does it:
 *
 *   _key()      lowercase → collapse `.`, whitespace and `-` into `_` → collapse repeated
 *               `_` → strip leading/trailing `_`.   ("U.S." → "u_s_" → "u_s")
 *   _normalize  alias lookup into a SET (so duplicates vanish), unsupported values
 *               collected and reported together, sorted alphabetically on the way out.
 *   identity    "date=…|regions=…|topics=…|selector=…|size=…"
 *
 * The result is order-independent and duplicate-independent by construction: the same mix
 * expressed any way produces one identity string, which is what makes a cached selection
 * safe to reuse.
 *
 * No file, network, process or environment access.
 */

import {
  SELECTOR_VERSION,
  SUPPORTED_REGIONS,
  SUPPORTED_TOPICS,
  UnsupportedMixValue,
} from "./custom-mix-types.js";

/** Python `_key`. */
export function canonicalKey(value: unknown): string {
  const text = value === null || value === undefined ? "" : String(value);
  return text
    .trim()
    .toLowerCase()
    .replace(/[.\s-]+/g, "_")
    .replace(/_+/g, "_")
    .replace(/^_+|_+$/g, "");
}

/** Python `_REGION_ALIASES`. */
const REGION_ALIASES: Readonly<Record<string, string>> = {
  japan: "japan",
  jp: "japan",
  jpn: "japan",
  us: "united_states",
  usa: "united_states",
  u_s: "united_states",
  united_states: "united_states",
  world: "world",
};

/**
 * Python `_TOPIC_ALIASES` — every supported topic maps to itself. Written out literally
 * rather than derived, so this module executes nothing at import time.
 */
const TOPIC_ALIASES: Readonly<Record<string, string>> = {
  ai: "ai",
  business: "business",
  climate: "climate",
  culture: "culture",
  health: "health",
  science: "science",
  tech: "tech",
};

function normalize(
  values: readonly unknown[] | undefined,
  aliases: Readonly<Record<string, string>>,
  kind: string,
): string[] {
  const canonical = new Set<string>();
  const unsupported: string[] = [];

  for (const value of values ?? []) {
    const key = canonicalKey(value);
    const mapped = Object.prototype.hasOwnProperty.call(aliases, key)
      ? aliases[key]
      : undefined;
    if (mapped !== undefined) {
      canonical.add(mapped);
    } else {
      // Python reports `str(value)` — the ORIGINAL, not the normalised key.
      unsupported.push(value === null || value === undefined ? "None" : String(value));
    }
  }

  if (unsupported.length > 0) {
    // Python: `', '.join(sorted(unsupported))`.
    throw new UnsupportedMixValue(
      `unsupported ${kind}: ${[...unsupported].sort().join(", ")}`,
    );
  }
  // Python returns `tuple(sorted(canonical))`.
  return [...canonical].sort();
}

export function normalizeRegions(values: readonly unknown[] | undefined): string[] {
  return normalize(values, REGION_ALIASES, "region");
}

export function normalizeTopics(values: readonly unknown[] | undefined): string[] {
  return normalize(values, TOPIC_ALIASES, "topic");
}

export function normalizeMix(
  regions: readonly unknown[] | undefined,
  topics: readonly unknown[] | undefined,
): { regions: string[]; topics: string[] } {
  return { regions: normalizeRegions(regions), topics: normalizeTopics(topics) };
}

/**
 * Python `mix_identity`. `int()` truncates toward zero, which `Math.trunc` reproduces;
 * `str(date).strip()` is the only transformation applied to the date.
 */
export function mixIdentity(
  date: unknown,
  regions: readonly unknown[] | undefined,
  topics: readonly unknown[] | undefined,
  selectorVersion: number = SELECTOR_VERSION,
  size = 5,
): string {
  const normalized = normalizeMix(regions, topics);
  const dateText = (date === null || date === undefined ? "" : String(date)).trim();
  return (
    `date=${dateText}` +
    `|regions=${normalized.regions.join(",")}` +
    `|topics=${normalized.topics.join(",")}` +
    `|selector=${Math.trunc(selectorVersion)}` +
    `|size=${Math.trunc(size)}`
  );
}

export { SELECTOR_VERSION, SUPPORTED_REGIONS, SUPPORTED_TOPICS, UnsupportedMixValue };
