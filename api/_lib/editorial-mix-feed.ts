/**
 * Editorial Mix Pool → SignalsFeed adapter. (Phase 3D-3C.1)
 *
 * The last mile: five already-enriched candidates, in selector order, become exactly the
 * feed document the iOS client already decodes. Every value is either copied from the
 * candidate's story-level block or assigned from the caller's position in the selection —
 * nothing is generated, fetched or inferred here.
 *
 * FIELD ORIGINS, matching `pipeline/build.py` exactly:
 *   number      1..5 in selector order            (edition-level)
 *   importance  = number                          (build.py: "human order = importance")
 *   lead        true only for the first signal    (build.py: selectedRole == "lead")
 *   date        supplied by the caller            (edition-level)
 *   focus       "MIXED"                           (build.py FOCUS constant)
 *   version     1                                 (build.py VERSION constant)
 *   everything else                               copied verbatim from `editorial`
 *
 * WHY THIS IS SAFE FOR THE CLIENT. `SignalsFeed.swift` declares `number, lead, category,
 * source, headline, summary, keyTakeaways, whyItMatters, originalURL, readTime, imageURL`
 * as non-optional, and `importance, placeTime, audioURL, localized, listen` as optional.
 * This adapter emits every non-optional field plus `importance`, `placeTime` and
 * `audioURL`, and omits `localized`/`listen` — which decode as nil, exactly as an
 * English-only static edition already does.
 *
 * Pure: no network, filesystem, environment, logging, clock or random source. Not imported
 * by `/api/edition`, the orchestrator or the runtime factory.
 */

import {
  validateEditorialStory,
  type EditorialStory,
  type EnrichedCandidate,
} from "./editorial-mix-pool-schema.js";

export const FEED_FOCUS = "MIXED";
export const FEED_VERSION = 1;
export const FEED_SIGNAL_COUNT = 5;

/** Mirrors `FeedSignal` in `Signals/Models/SignalsFeed.swift`. */
export type FeedSignal = {
  number: number;
  importance: number;
  lead: boolean;
  category: string;
  source: string;
  headline: string;
  summary: string;
  keyTakeaways: string[];
  whyItMatters: string;
  originalURL: string;
  readTime: number;
  imageURL: string;
  placeTime: string;
  audioURL: string;
};

/** Mirrors `SignalsFeed`. */
export type SignalsFeed = {
  date: string;
  focus: string;
  version: number;
  signals: FeedSignal[];
};

export class EditionAssemblyError extends Error {
  readonly reason: string;

  constructor(reason: string) {
    // A short mechanical reason with field paths only — never story content.
    super(`edition assembly failed: ${reason}`);
    this.name = "EditionAssemblyError";
    this.reason = reason;
  }
}

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

function isValidUtcDate(date: string): boolean {
  if (!DATE_RE.test(date)) return false;
  const [year, month, day] = date.split("-").map(Number);
  const probe = new Date(Date.UTC(year, month - 1, day));
  return (
    probe.getUTCFullYear() === year &&
    probe.getUTCMonth() === month - 1 &&
    probe.getUTCDate() === day
  );
}

function deepFreeze<T>(value: T): T {
  if (value !== null && typeof value === "object") {
    for (const child of Object.values(value as Record<string, unknown>)) deepFreeze(child);
    Object.freeze(value);
  }
  return value;
}

/**
 * Build the feed document from exactly five selected candidates, in selector order.
 *
 * Throws `EditionAssemblyError` rather than emitting a partial feed: a document missing a
 * non-optional field would fail to decode on the client, which is strictly worse than a
 * server-side failure the caller can turn into its own error response.
 */
export function assembleSignalsFeed(
  date: string,
  selected: readonly EnrichedCandidate[],
): SignalsFeed {
  if (!isValidUtcDate(date)) throw new EditionAssemblyError("date must be ISO YYYY-MM-DD");
  if (!Array.isArray(selected)) throw new EditionAssemblyError("selected must be an array");
  if (selected.length !== FEED_SIGNAL_COUNT) {
    throw new EditionAssemblyError(
      `expected exactly ${FEED_SIGNAL_COUNT} candidates, received ${selected.length}`,
    );
  }

  const ids = new Set<string>();
  const urls = new Set<string>();

  const signals: FeedSignal[] = selected.map((candidate, index) => {
    const position = `selected[${index}]`;
    if (candidate === null || typeof candidate !== "object") {
      throw new EditionAssemblyError(`${position} must be an object`);
    }

    const selector = candidate.selector as Record<string, unknown> | undefined;
    const editorial = candidate.editorial as EditorialStory | undefined;
    if (!selector || typeof selector !== "object") {
      throw new EditionAssemblyError(`${position}.selector is missing`);
    }
    if (!editorial || typeof editorial !== "object") {
      throw new EditionAssemblyError(`${position}.editorial is missing`);
    }

    const errors: string[] = [];
    validateEditorialStory(editorial, `${position}.editorial`, errors);
    if (errors.length > 0) throw new EditionAssemblyError(errors[0]);

    const id = String(selector.id ?? "");
    if (!id) throw new EditionAssemblyError(`${position}.selector.id is missing`);
    if (ids.has(id)) throw new EditionAssemblyError(`duplicate selected candidate id`);
    ids.add(id);

    const url = editorial.originalURL;
    if (urls.has(url)) throw new EditionAssemblyError("duplicate selected originalURL");
    urls.add(url);

    const number = index + 1;
    return {
      number,
      importance: number, // build.py: human order == importance
      lead: index === 0, // build.py: exactly one lead, the first selected story
      category: editorial.category,
      source: editorial.source,
      headline: editorial.headline,
      summary: editorial.summary,
      keyTakeaways: [...editorial.keyTakeaways],
      whyItMatters: editorial.whyItMatters,
      originalURL: editorial.originalURL,
      readTime: editorial.readTime,
      imageURL: editorial.imageURL,
      placeTime: editorial.placeTime,
      audioURL: editorial.audioURL,
    };
  });

  return deepFreeze({
    date,
    focus: FEED_FOCUS,
    version: FEED_VERSION,
    signals,
  });
}

/** The exact non-optional `FeedSignal` fields, for contract assertions. */
export const REQUIRED_FEED_SIGNAL_FIELDS = [
  "number",
  "lead",
  "category",
  "source",
  "headline",
  "summary",
  "keyTakeaways",
  "whyItMatters",
  "originalURL",
  "readTime",
  "imageURL",
] as const;

/** Optional in Swift, but always emitted here for parity with the static feed. */
export const OPTIONAL_FEED_SIGNAL_FIELDS = ["importance", "placeTime", "audioURL"] as const;
