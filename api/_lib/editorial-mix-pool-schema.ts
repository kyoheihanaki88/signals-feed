/**
 * Editorial Mix Pool artifact contract. (Phase 3D-3C.1)
 *
 * A SEPARATE artifact from the raw Mix Pool. The raw pool (schema version 1) carries only
 * what the selector needs; this one adds the story-level editorial fields the iOS client
 * needs, so a selected five can become a `SignalsFeed` with no request-time generation.
 *
 * STORY-LEVEL vs EDITION-LEVEL — the central separation.
 *   Story-level fields belong to a candidate and are stable wherever it appears:
 *     headline, summary, keyTakeaways, whyItMatters, source, originalURL, readTime,
 *     imageURL, placeTime, audioURL, category.
 *   Edition-level fields depend on WHICH five were chosen and in what order:
 *     number, importance, lead, date, focus, version.
 * Storing an edition-level field on a reusable candidate would be a lie — the same story
 * is `number: 1` in one mix and `number: 4` in another. The schema therefore REJECTS
 * `number`, `importance` and `lead` inside a stored candidate, and `editorial-mix-feed.ts`
 * assigns them after selection.
 *
 * WHY `focus` IS CONSTANT. `pipeline/build.py` sets `FOCUS = "MIXED"` and `VERSION = 1` as
 * module constants and writes them verbatim; neither is derived from the selected stories
 * and neither needs a model call. A Custom Mix edition can use the same values.
 *
 * WHY LISTEN IS NOT A BLOCKER. In the live feed `audioURL` is `""`, and `listen` and
 * `localized` are optional in `SignalsFeed.swift`. A personalized edition decodes without
 * any audio artifact. Personalized Listen is a separate concern, deliberately out of scope.
 *
 * TWO IDENTITIES, DELIBERATELY.
 *   `selectorPoolIdentity`  hashes ONLY the extracted selector candidates, reusing the raw
 *                           Mix Pool identity function — so it must reproduce the original
 *                           raw pool's `poolIdentity` exactly. This proves enrichment did
 *                           not disturb selection inputs.
 *   `editorialPoolIdentity` hashes the whole enriched candidate set. Editorial drift moves
 *                           this one and leaves the selector identity untouched, which is
 *                           what makes the two failure modes distinguishable in diagnostics.
 *
 * All raw-candidate validation and numeric rules are DELEGATED to `mix-pool-schema.ts`.
 * There is no second copy of the numeric contract.
 *
 * Pure: no filesystem, network, environment, clock or logging. Not imported by any route,
 * the orchestrator or the runtime factory.
 */

import { createHash } from "node:crypto";

import {
  CANDIDATE_OPTIONAL,
  CANDIDATE_REQUIRED,
  canonicalMixPoolBytes,
  mixPoolIdentity,
  validateMixPoolArtifact,
  type JsonValue,
} from "./mix-pool-schema.js";

export const EDITORIAL_ARTIFACT_TYPE = "editorial-mix-pool";
export const EDITORIAL_SCHEMA_VERSION = 1;
export const EDITORIAL_SELECTOR_VERSION = 1;
export const EDITORIAL_VERSION = 1;
export const EDITORIAL_GENERATOR_VERSION = 1;

/** Future pipeline targets, recorded here so the contract and the plan cannot drift. */
export const TARGET_POOL_SIZE = 20;
export const MINIMUM_PUBLISHABLE_POOL_SIZE = 15;

export const EDITORIAL_TOP_KEYS = [
  "artifactType",
  "schemaVersion",
  "selectorVersion",
  "editorialVersion",
  "date",
  "generatedAt",
  "candidateCount",
  "selectorPoolIdentity",
  "editorialPoolIdentity",
  "candidates",
  "provenance",
] as const;

/** Story-level fields. Every one is required; none is edition-dependent. */
export const EDITORIAL_REQUIRED = [
  "category",
  "source",
  "headline",
  "summary",
  "keyTakeaways",
  "whyItMatters",
  "originalURL",
  "readTime",
  "imageURL",
  "placeTime",
  "audioURL",
] as const;

/** Never storable on a candidate: these depend on the chosen five. */
export const EDITION_LEVEL_FIELDS = ["number", "importance", "lead", "focus", "version"] as const;

/** Never storable anywhere: provider internals, prompts, bodies, local paths. */
export const EDITORIAL_FORBIDDEN_KEYS = [
  "prompt",
  "prompts",
  "rawbody",
  "raw_body",
  "articlebody",
  "article_body",
  "fulltext",
  "full_text",
  "sourcetext",
  "source_text",
  "providerresponse",
  "provider_response",
  "apikey",
  "api_key",
] as const;

const READ_TIME_MIN = 1;
const READ_TIME_MAX = 60;
const MAX_TAKEAWAYS = 8;

export type EditorialStory = {
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

export type EnrichedCandidate = {
  selector: Record<string, JsonValue>;
  editorial: EditorialStory;
};

export type EditorialValidationResult = { valid: boolean; errors: string[] };

function isObject(value: unknown): value is Record<string, JsonValue> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function nonEmpty(value: unknown): boolean {
  return typeof value === "string" && value.trim().length > 0;
}

function isHttpsUrl(value: unknown): boolean {
  if (typeof value !== "string") return false;
  try {
    const parsed = new URL(value);
    return parsed.protocol === "https:" && Boolean(parsed.host);
  } catch {
    return false;
  }
}

/** Extract the raw selector candidates, in stored order. */
export function extractSelectorCandidates(artifact: unknown): JsonValue[] {
  if (!isObject(artifact) || !Array.isArray(artifact.candidates)) return [];
  return (artifact.candidates as JsonValue[]).map((c) =>
    isObject(c) ? ((c.selector ?? {}) as JsonValue) : ({} as JsonValue),
  );
}

/** Reuses the RAW pool identity function, so it must equal the original pool's value. */
export function selectorPoolIdentityOf(artifact: unknown): string {
  return mixPoolIdentity(extractSelectorCandidates(artifact));
}

/** Hash of the whole enriched candidate set, sorted by selector id for stability. */
export function editorialPoolIdentityOf(candidates: JsonValue[]): string {
  const ordered = [...candidates].sort((a, b) => {
    const idA = String((a as { selector?: { id?: unknown } })?.selector?.id ?? "");
    const idB = String((b as { selector?: { id?: unknown } })?.selector?.id ?? "");
    return idA < idB ? -1 : idA > idB ? 1 : 0;
  });
  return createHash("sha256").update(canonicalMixPoolBytes(ordered)).digest("hex");
}

function forbiddenKeyPaths(value: JsonValue, path = "$"): string[] {
  const found: string[] = [];
  if (Array.isArray(value)) {
    value.forEach((child, i) => found.push(...forbiddenKeyPaths(child, `${path}[${i}]`)));
  } else if (isObject(value)) {
    for (const [key, child] of Object.entries(value)) {
      const childPath = `${path}.${key}`;
      if ((EDITORIAL_FORBIDDEN_KEYS as readonly string[]).includes(key.toLowerCase())) {
        found.push(childPath);
      }
      found.push(...forbiddenKeyPaths(child, childPath));
    }
  } else if (typeof value === "string" && (value.startsWith("file://") || value.includes("/Users/"))) {
    found.push(path);
  }
  return found;
}

/** Validate one story-level block. Errors carry field paths only — never content. */
export function validateEditorialStory(story: unknown, prefix: string, errors: string[]): void {
  if (!isObject(story)) {
    errors.push(`${prefix} must be an object`);
    return;
  }
  const present = new Set(Object.keys(story));
  for (const key of [...EDITORIAL_REQUIRED].filter((k) => !present.has(k)).sort()) {
    errors.push(`${prefix} missing field: ${key}`);
  }
  for (const key of [...present].filter(
    (k) => !(EDITORIAL_REQUIRED as readonly string[]).includes(k),
  ).sort()) {
    errors.push(`${prefix} unsupported field: ${key}`);
  }

  for (const field of ["category", "source", "headline", "summary", "whyItMatters", "placeTime"] as const) {
    if (!nonEmpty(story[field])) errors.push(`${prefix}.${field} must be nonempty`);
  }

  const takeaways = story.keyTakeaways;
  if (!Array.isArray(takeaways) || takeaways.length === 0) {
    errors.push(`${prefix}.keyTakeaways must be a nonempty array`);
  } else {
    if (takeaways.length > MAX_TAKEAWAYS) {
      errors.push(`${prefix}.keyTakeaways exceeds ${MAX_TAKEAWAYS} entries`);
    }
    takeaways.forEach((item, index) => {
      if (!nonEmpty(item)) errors.push(`${prefix}.keyTakeaways[${index}] must be nonempty`);
    });
  }

  // `build.py` requires a real https article URL; mirror that exactly.
  if (!isHttpsUrl(story.originalURL)) {
    errors.push(`${prefix}.originalURL must be an https article URL`);
  }
  if (!isHttpsUrl(story.imageURL)) {
    errors.push(`${prefix}.imageURL must be an https URL`);
  }

  const readTime = story.readTime;
  if (typeof readTime === "boolean" || !Number.isSafeInteger(readTime)) {
    errors.push(`${prefix}.readTime must be an integer`);
  } else if ((readTime as number) < READ_TIME_MIN || (readTime as number) > READ_TIME_MAX) {
    errors.push(`${prefix}.readTime must be within [${READ_TIME_MIN}, ${READ_TIME_MAX}]`);
  }

  // v1 compatibility: the live feed carries "" here.
  if (typeof story.audioURL !== "string") errors.push(`${prefix}.audioURL must be a string`);
}

/** Validate the whole Editorial Mix Pool artifact. */
export function validateEditorialMixPool(artifact: unknown): EditorialValidationResult {
  const errors: string[] = [];
  if (!isObject(artifact)) return { valid: false, errors: ["artifact must be an object"] };

  const keys = new Set(Object.keys(artifact));
  for (const key of [...EDITORIAL_TOP_KEYS].filter((k) => !keys.has(k)).sort()) {
    errors.push(`missing top-level field: ${key}`);
  }
  for (const key of [...keys].filter(
    (k) => !(EDITORIAL_TOP_KEYS as readonly string[]).includes(k),
  ).sort()) {
    errors.push(`unsupported top-level field: ${key}`);
  }

  if (artifact.artifactType !== EDITORIAL_ARTIFACT_TYPE) errors.push("unsupported artifactType");
  if (artifact.schemaVersion !== EDITORIAL_SCHEMA_VERSION) errors.push("unsupported schemaVersion");
  if (artifact.selectorVersion !== EDITORIAL_SELECTOR_VERSION) {
    errors.push("unsupported selectorVersion");
  }
  if (artifact.editorialVersion !== EDITORIAL_VERSION) errors.push("unsupported editorialVersion");

  if (typeof artifact.date !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(artifact.date)) {
    errors.push("date must be ISO YYYY-MM-DD");
  }
  if (!nonEmpty(artifact.generatedAt) || !Number.isFinite(Date.parse(String(artifact.generatedAt)))) {
    errors.push("generatedAt must be an ISO datetime");
  }

  const candidates = Array.isArray(artifact.candidates) ? (artifact.candidates as JsonValue[]) : [];
  if (!Array.isArray(artifact.candidates)) errors.push("candidates must be an array");
  if (artifact.candidateCount !== candidates.length) {
    errors.push("candidateCount does not match candidates");
  }

  const ids = new Set<string>();
  const urls = new Set<string>();
  candidates.forEach((raw, index) => {
    const prefix = `candidates[${index}]`;
    if (!isObject(raw)) {
      errors.push(`${prefix} must be an object`);
      return;
    }
    const shape = Object.keys(raw).sort().join(",");
    if (shape !== "editorial,selector") {
      errors.push(`${prefix} must contain exactly selector and editorial`);
    }

    const selector = raw.selector;
    if (!isObject(selector)) {
      errors.push(`${prefix}.selector must be an object`);
    } else {
      // Edition-level fields must never be stored on a reusable candidate.
      for (const field of EDITION_LEVEL_FIELDS) {
        if (field in selector) errors.push(`${prefix}.selector must not carry ${field}`);
      }
      const id = selector.id;
      if (!nonEmpty(id)) errors.push(`${prefix}.selector.id must be nonempty`);
      else if (ids.has(id as string)) errors.push(`duplicate candidate id: ${id}`);
      else ids.add(id as string);

      const url = selector.url;
      if (typeof url === "string" && url.length > 0) {
        if (urls.has(url)) errors.push(`duplicate candidate url`);
        else urls.add(url);
      }
    }

    const editorial = raw.editorial;
    if (!isObject(editorial)) {
      errors.push(`${prefix}.editorial must be an object`);
    } else {
      for (const field of EDITION_LEVEL_FIELDS) {
        if (field in editorial) errors.push(`${prefix}.editorial must not carry ${field}`);
      }
      validateEditorialStory(editorial, `${prefix}.editorial`, errors);

      // The editorial block must not contradict the selector's taxonomy.
      if (isObject(selector) && editorial.category !== selector.category) {
        errors.push(`${prefix}.editorial.category does not match the selector category`);
      }
      if (isObject(selector) && nonEmpty(selector.source) && editorial.source !== selector.source) {
        errors.push(`${prefix}.editorial.source does not match the selector source`);
      }
    }
  });

  // Delegate ALL raw candidate rules — numeric contract, taxonomy, evidence — to the raw
  // schema by validating a synthetic raw artifact built from the extracted selectors.
  const selectors = extractSelectorCandidates(artifact);
  if (selectors.length > 0) {
    const rawProbe = {
      schemaVersion: 1,
      selectorVersion: 1,
      date: artifact.date,
      generatedAt: artifact.generatedAt,
      poolIdentity: mixPoolIdentity(selectors),
      candidateCount: selectors.length,
      candidates: selectors,
      validation: { valid: true, errors: [], warnings: [] },
      provenance: {
        source: "editorial-mix-pool",
        inputIdentity: "0".repeat(64),
        generatorVersion: 1,
      },
    } as unknown as JsonValue;
    for (const error of validateMixPoolArtifact(rawProbe).errors) {
      // Skip envelope-only complaints that belong to the raw artifact, not to a candidate.
      if (error.startsWith("candidates[") || error.startsWith("duplicate ")) {
        errors.push(`selector: ${error}`);
      }
    }
  }

  if (artifact.selectorPoolIdentity !== mixPoolIdentity(selectors)) {
    errors.push("selectorPoolIdentity does not match the extracted selector candidates");
  }
  if (artifact.editorialPoolIdentity !== editorialPoolIdentityOf(candidates)) {
    errors.push("editorialPoolIdentity does not match the enriched candidates");
  }

  const provenance = artifact.provenance;
  if (!isObject(provenance)) {
    errors.push("provenance must be an object");
  } else {
    if (!nonEmpty(provenance.source)) errors.push("provenance.source must be nonempty");
    if (provenance.generatorVersion !== EDITORIAL_GENERATOR_VERSION) {
      errors.push("unsupported provenance.generatorVersion");
    }
  }

  for (const path of forbiddenKeyPaths(artifact as JsonValue)) {
    errors.push(`forbidden field or local path: ${path}`);
  }

  return { valid: errors.length === 0, errors };
}

/** Canonical bytes for the enriched artifact, using the proven raw-pool rules. */
export function canonicalEditorialBytes(artifact: JsonValue): Buffer {
  return canonicalMixPoolBytes(artifact);
}

export { CANDIDATE_REQUIRED, CANDIDATE_OPTIONAL };
