/**
 * Mix Pool artifact contract — TypeScript side. (Phase 3D-3A.2)
 *
 * A byte-exact counterpart to `pipeline/mix_pool_schema.py`. Python is the source of
 * truth; this module reproduces its validation, canonical serialization and SHA-256
 * hashing so a pool artifact published by the daily pipeline can be verified at runtime.
 *
 * WHY A CANONICAL SERIALIZER IS NEEDED HERE (and was rejected on the Python side).
 * Python's `json.dumps(sort_keys=True)` already sorts keys recursively; `JSON.stringify`
 * does not sort at all. So TypeScript needs a small recursive key-sorting pass before
 * stringifying. Everything else — compact separators, unescaped Unicode, string escaping —
 * already matches, so the serializer stays tiny: sort, then `JSON.stringify`.
 *
 * THE ONE LANGUAGE DIFFERENCE, DELIBERATELY NORMALIZED.
 * JSON text `7.0` parses to `7` in JavaScript with no surviving trace, so TypeScript
 * cannot tell a producer's integral float from an integer. That is exactly why the Python
 * contract normalizes integral scores to integers BEFORE hashing: by the time an artifact
 * reaches this module, `7.0` cannot legally exist in it. TypeScript therefore validates the
 * semantic canonical form and reserializes — it does not need raw-token inspection.
 *
 * The numeric domain (see `mix_pool_schema.py` for the full rationale):
 *   baseScore   finite, [-1000, 1000], exactly 0 or |v| >= 0.0001, <= 6 decimals,
 *               integral values serialize as integers, never -0, never exponent form
 *   integral    real integers only; booleans and fractional values rejected
 *
 * Pure: no filesystem, network, subprocess, environment or logging. Not wired to
 * `/api/edition`, the orchestrator, the runtime factory or any candidate source.
 */

import { createHash } from "node:crypto";

export const MIX_POOL_SCHEMA_VERSION = 1;
export const MIX_POOL_GENERATOR_VERSION = 1;
export const MIX_POOL_SELECTOR_VERSION = 1;

/** Mirrors Python `SCORE_MIN` / `SCORE_MAX` / `SCORE_MAX_DECIMALS` / `SCORE_MIN_MAGNITUDE`. */
export const SCORE_MIN = -1000;
export const SCORE_MAX = 1000;
export const SCORE_MAX_DECIMALS = 6;
export const SCORE_MIN_MAGNITUDE = 0.0001;

/** Mirrors Python `INTEGRAL_FIELD_RANGES`. */
export const INTEGRAL_FIELD_RANGES: Readonly<Record<string, [number, number]>> = {
  candidateCount: [0, 100_000],
  clusterSize: [1, 100_000],
  clusterSources: [1, 100_000],
  sourceRisk: [0, 100],
};

export const TOP_KEYS = [
  "schemaVersion",
  "selectorVersion",
  "date",
  "generatedAt",
  "poolIdentity",
  "candidateCount",
  "candidates",
  "validation",
  "provenance",
] as const;

export const CANDIDATE_REQUIRED = [
  "id",
  "headline",
  "summary",
  "source",
  "url",
  "publishedAt",
  "category",
  "topics",
  "regionMemberships",
  "baseScore",
  "sourceReliability",
  "topicFingerprint",
  "underlyingStoryIdentity",
] as const;

export const CANDIDATE_OPTIONAL = ["quality", "eligible"] as const;

/**
 * Every numeric field the schema permits, and how it is validated. The unknown-numeric
 * guard in the tests asserts this map covers every allowed numeric path — so a new numeric
 * field cannot enter the hash without an explicit contract.
 */
export const NUMERIC_FIELD_KINDS: Readonly<Record<string, "score" | "integral">> = {
  "$.schemaVersion": "integral",
  "$.selectorVersion": "integral",
  "$.candidateCount": "integral",
  "$.provenance.generatorVersion": "integral",
  "$.candidates[].baseScore": "score",
  "$.candidates[].quality.clusterSize": "integral",
  "$.candidates[].quality.clusterSources": "integral",
  "$.candidates[].quality.sourceRisk": "integral",
};

export const BOOLEAN_FIELD_PATHS = [
  "$.candidates[].eligible",
  "$.candidates[].quality.eligible",
  "$.candidates[].quality.paywalled",
  "$.validation.valid",
] as const;

export const PROVENANCE_KEYS = ["source", "inputIdentity", "generatorVersion", "referenceAt"] as const;
export const STRENGTHS = ["primary", "incidental", "none"] as const;
export const SOURCE_RELIABILITIES = ["high", "medium", "low", "unknown"] as const;
export const CATEGORIES = [
  "AI", "BUSINESS", "CLIMATE", "CULTURE", "ECONOMY", "FINANCE",
  "HEALTH", "JAPAN", "SCIENCE", "TECH", "WORLD", "OTHER",
] as const;
export const SUPPORTED_REGIONS = ["japan", "united_states", "world"] as const;
export const SUPPORTED_TOPICS = [
  "ai", "business", "climate", "culture", "health", "science", "tech",
] as const;
export const FORBIDDEN_KEYS = [
  "rawbody", "raw_body", "fulltext", "full_text", "audiodata",
  "audio_data", "authtoken", "auth_token",
] as const;

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

export class MixPoolNumericError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "MixPoolNumericError";
  }
}

export type JsonValue =
  | null
  | boolean
  | number
  | string
  | JsonValue[]
  | { [key: string]: JsonValue };

// ── numeric contract ──────────────────────────────────────────────────────────────────

function isPlainNumber(value: unknown): value is number {
  // `typeof true === "boolean"`, so booleans are already excluded; this also rejects
  // numeric strings, which JavaScript would otherwise coerce silently.
  return typeof value === "number";
}

/**
 * Fractional decimal digits, judged from JavaScript's SHORTEST representation — the
 * closest analogue to Python's `Decimal(str(value))`. Using the exact binary expansion
 * instead would make almost every real score appear to have 17+ digits.
 *
 * Returns -1 when the shortest representation uses exponent notation, which the caller
 * treats as outside the domain.
 */
export function fractionalDigits(value: number): number {
  const rendered = String(value);
  if (rendered.includes("e") || rendered.includes("E")) return -1;
  const dot = rendered.indexOf(".");
  return dot === -1 ? 0 : rendered.length - dot - 1;
}

/**
 * The canonical form of a score, or a thrown `MixPoolNumericError`.
 * Mirrors Python `normalize_score`, including the self-check on the emitted text.
 */
export function normalizeScore(value: unknown, path = "$.baseScore"): number {
  if (typeof value === "boolean") {
    throw new MixPoolNumericError(`${path} must be a number, not a boolean`);
  }
  if (!isPlainNumber(value)) {
    throw new MixPoolNumericError(`${path} must be a JSON number`);
  }
  if (!Number.isFinite(value)) {
    throw new MixPoolNumericError(`${path} must be finite`);
  }
  if (value < SCORE_MIN || value > SCORE_MAX) {
    throw new MixPoolNumericError(`${path} must be within [${SCORE_MIN}, ${SCORE_MAX}]`);
  }

  const digits = fractionalDigits(value);
  if (digits === -1) {
    throw new MixPoolNumericError(`${path} must not serialize in exponent notation`);
  }
  if (digits > SCORE_MAX_DECIMALS) {
    throw new MixPoolNumericError(
      `${path} must have at most ${SCORE_MAX_DECIMALS} fractional decimal digits`,
    );
  }

  if (value === 0) return 0; // collapses both 0 and -0 (Object.is(-0, 0) is false, === is true)
  if (Math.abs(value) < SCORE_MIN_MAGNITUDE) {
    throw new MixPoolNumericError(
      `${path} nonzero magnitude must be at least ${SCORE_MIN_MAGNITUDE} ` +
        "(smaller values serialize in exponent notation)",
    );
  }

  const rendered = JSON.stringify(value);
  if (rendered.includes("e") || rendered.includes("E")) {
    throw new MixPoolNumericError(`${path} must not serialize in exponent notation`);
  }
  if (rendered === "-0") {
    throw new MixPoolNumericError(`${path} must not serialize as negative zero`);
  }
  if (rendered.endsWith(".0")) {
    throw new MixPoolNumericError(`${path} must not serialize with a trailing .0`);
  }
  return value;
}

/** Mirrors Python `validate_integral`. */
export function validateIntegral(
  value: unknown,
  field: string,
  path: string,
  errors: string[],
): void {
  if (typeof value === "boolean") {
    errors.push(`${path} must be an integer, not a boolean`);
    return;
  }
  if (!isPlainNumber(value) || !Number.isSafeInteger(value)) {
    errors.push(`${path} must be an integer`);
    return;
  }
  const bounds = INTEGRAL_FIELD_RANGES[field];
  if (bounds && (value < bounds[0] || value > bounds[1])) {
    errors.push(`${path} must be within [${bounds[0]}, ${bounds[1]}]`);
  }
}

// ── canonical serialization ───────────────────────────────────────────────────────────

/**
 * Recursively sort object keys. `JSON.stringify` preserves insertion order, so building a
 * key-sorted copy and stringifying it reproduces Python's `sort_keys=True` exactly.
 * Array order is never touched — it is contract-defined.
 */
export function deepSortKeys(value: JsonValue): JsonValue {
  if (Array.isArray(value)) return value.map((item) => deepSortKeys(item));
  if (value !== null && typeof value === "object") {
    const sorted: { [key: string]: JsonValue } = {};
    for (const key of Object.keys(value).sort()) {
      sorted[key] = deepSortKeys((value as { [key: string]: JsonValue })[key]);
    }
    return sorted;
  }
  return value;
}

/**
 * Python `canonical_bytes`: compact separators, sorted keys, `ensure_ascii=False`, UTF-8.
 * `JSON.stringify` already emits `{"a":1,"b":2}` with no spaces, escapes `"`, `\` and
 * control characters the same way, and leaves non-ASCII raw — so sorting is the only gap.
 */
export function canonicalMixPoolBytes(value: JsonValue): Buffer {
  return Buffer.from(JSON.stringify(deepSortKeys(value)), "utf8");
}

/** Python `serialize`: `indent=2`, sorted keys, UTF-8, plus a trailing newline. */
export function serializeMixPoolArtifact(value: JsonValue): Buffer {
  return Buffer.from(`${JSON.stringify(deepSortKeys(value), null, 2)}\n`, "utf8");
}

function sha256Hex(bytes: Buffer): string {
  return createHash("sha256").update(bytes).digest("hex");
}

/**
 * Python `pool_identity`: SHA-256 over the candidates sorted BY ID.
 *
 * This is deliberately different from the artifact's own candidate array order, and
 * different from `mixPoolArtifactHash`. Sorting here is what makes the identity stable
 * regardless of the order the producer happened to emit.
 */
export function mixPoolIdentity(candidates: JsonValue[]): string {
  const ordered = [...candidates].sort((a, b) => {
    const idA = String((a as { id?: unknown })?.id ?? "");
    const idB = String((b as { id?: unknown })?.id ?? "");
    return idA < idB ? -1 : idA > idB ? 1 : 0;
  });
  return sha256Hex(canonicalMixPoolBytes(ordered));
}

/** SHA-256 of the FULL artifact's canonical bytes. Not the same as `poolIdentity`. */
export function mixPoolArtifactHash(artifact: JsonValue): string {
  return sha256Hex(canonicalMixPoolBytes(artifact));
}

// ── artifact validation ───────────────────────────────────────────────────────────────

export type ValidationResult = { valid: boolean; errors: string[]; warnings: string[] };

function isObject(value: unknown): value is { [key: string]: JsonValue } {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function nonEmptyString(value: unknown): boolean {
  return typeof value === "string" && value.trim().length > 0;
}

function isIsoDateTime(value: unknown): boolean {
  if (typeof value !== "string" || value.length === 0) return false;
  return Number.isFinite(Date.parse(value.replace(/Z$/, "+00:00")));
}

function isHttpUrl(value: unknown): boolean {
  if (typeof value !== "string") return false;
  try {
    const parsed = new URL(value);
    return (parsed.protocol === "http:" || parsed.protocol === "https:") && Boolean(parsed.host);
  } catch {
    return false;
  }
}

/** Python `_forbidden_paths`: forbidden keys anywhere, plus local-path leakage. */
function forbiddenPaths(value: JsonValue, path = "$"): string[] {
  const found: string[] = [];
  if (Array.isArray(value)) {
    value.forEach((child, index) => found.push(...forbiddenPaths(child, `${path}[${index}]`)));
  } else if (isObject(value)) {
    for (const [key, child] of Object.entries(value)) {
      const childPath = `${path}.${key}`;
      if ((FORBIDDEN_KEYS as readonly string[]).includes(key.toLowerCase())) {
        found.push(childPath);
      }
      found.push(...forbiddenPaths(child, childPath));
    }
  } else if (typeof value === "string" && (value.startsWith("file://") || value.includes("/Users/"))) {
    found.push(path);
  }
  return found;
}

/**
 * Port of Python `validate_artifact`.
 *
 * Error strings carry safe field paths only — never a headline, URL, summary or any other
 * payload value.
 */
export function validateMixPoolArtifact(artifact: unknown): ValidationResult {
  const errors: string[] = [];
  const warnings: string[] = [];
  if (!isObject(artifact)) {
    return { valid: false, errors: ["artifact must be an object"], warnings };
  }

  const keys = new Set(Object.keys(artifact));
  for (const key of [...TOP_KEYS].filter((k) => !keys.has(k)).sort()) {
    errors.push(`missing top-level field: ${key}`);
  }
  for (const key of [...keys].filter((k) => !(TOP_KEYS as readonly string[]).includes(k)).sort()) {
    errors.push(`unsupported top-level field: ${key}`);
  }
  if (artifact.schemaVersion !== MIX_POOL_SCHEMA_VERSION) errors.push("unsupported schemaVersion");
  if (artifact.selectorVersion !== MIX_POOL_SELECTOR_VERSION) {
    errors.push("unsupported selectorVersion");
  }
  for (const field of ["schemaVersion", "selectorVersion"] as const) {
    if (typeof artifact[field] === "boolean" || !Number.isSafeInteger(artifact[field])) {
      errors.push(`${field} must be an integer`);
    }
  }

  const date = artifact.date;
  if (typeof date !== "string" || !DATE_RE.test(date)) {
    errors.push("date must be ISO YYYY-MM-DD");
  } else {
    const [y, m, d] = date.split("-").map(Number);
    const probe = new Date(Date.UTC(y, m - 1, d));
    if (probe.getUTCFullYear() !== y || probe.getUTCMonth() !== m - 1 || probe.getUTCDate() !== d) {
      errors.push("date must be a real calendar date");
    }
  }
  if (!isIsoDateTime(artifact.generatedAt)) errors.push("generatedAt must be an ISO datetime");

  let candidates: JsonValue[] = [];
  if (!Array.isArray(artifact.candidates)) {
    errors.push("candidates must be an array");
  } else {
    candidates = artifact.candidates;
  }
  validateIntegral(artifact.candidateCount, "candidateCount", "candidateCount", errors);
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
    const candidate = raw;
    const present = new Set(Object.keys(candidate));
    for (const key of [...CANDIDATE_REQUIRED].filter((k) => !present.has(k)).sort()) {
      errors.push(`${prefix} missing field: ${key}`);
    }
    const allowed = new Set<string>([...CANDIDATE_REQUIRED, ...CANDIDATE_OPTIONAL]);
    for (const key of [...present].filter((k) => !allowed.has(k)).sort()) {
      errors.push(`${prefix} unsupported field: ${key}`);
    }

    const id = candidate.id;
    if (!nonEmptyString(id)) errors.push(`${prefix}.id must be nonempty`);
    else if (ids.has(id as string)) errors.push(`duplicate candidate id: ${id}`);
    else ids.add(id as string);

    const url = candidate.url;
    if (!isHttpUrl(url)) errors.push(`${prefix}.url must be http(s)`);
    else if (urls.has(url as string)) errors.push(`duplicate candidate url: ${url}`);
    else urls.add(url as string);

    for (const field of ["headline", "summary", "source", "underlyingStoryIdentity"] as const) {
      if (!nonEmptyString(candidate[field])) errors.push(`${prefix}.${field} must be nonempty`);
    }

    const fingerprint = candidate.topicFingerprint;
    if (typeof fingerprint !== "string" && !Array.isArray(fingerprint)) {
      errors.push(`${prefix}.topicFingerprint must be a string or array`);
    } else if (Array.isArray(fingerprint) && fingerprint.some((item) => !nonEmptyString(item))) {
      errors.push(`${prefix}.topicFingerprint entries must be nonempty`);
    }
    if (!isIsoDateTime(candidate.publishedAt)) {
      errors.push(`${prefix}.publishedAt must be an ISO datetime`);
    }
    if (!(CATEGORIES as readonly unknown[]).includes(candidate.category)) {
      errors.push(`${prefix}.category is not canonical`);
    }

    const topics = candidate.topics;
    if (!Array.isArray(topics) || topics.some((t) => !(SUPPORTED_TOPICS as readonly unknown[]).includes(t))) {
      errors.push(`${prefix}.topics contains a noncanonical topic`);
    } else if (new Set(topics).size !== topics.length) {
      errors.push(`${prefix}.topics contains duplicates`);
    }

    try {
      normalizeScore(candidate.baseScore, `${prefix}.baseScore`);
    } catch (error) {
      errors.push(error instanceof Error ? error.message : `${prefix}.baseScore is invalid`);
    }

    if (!(SOURCE_RELIABILITIES as readonly unknown[]).includes(candidate.sourceReliability)) {
      errors.push(`${prefix}.sourceReliability is not allowed`);
    }

    if ("eligible" in candidate && typeof candidate.eligible !== "boolean") {
      errors.push(`${prefix}.eligible must be a boolean`);
    }
    if ("quality" in candidate) {
      const quality = candidate.quality;
      if (!isObject(quality)) {
        errors.push(`${prefix}.quality must be an object`);
      } else {
        for (const field of ["clusterSize", "clusterSources", "sourceRisk"] as const) {
          if (field in quality) {
            validateIntegral(quality[field], field, `${prefix}.quality.${field}`, errors);
          }
        }
        for (const field of ["eligible", "paywalled"] as const) {
          if (field in quality && typeof quality[field] !== "boolean") {
            errors.push(`${prefix}.quality.${field} must be a boolean`);
          }
        }
      }
    }

    const memberships = candidate.regionMemberships;
    if (!Array.isArray(memberships)) {
      errors.push(`${prefix}.regionMemberships must be an array`);
      return;
    }
    const regionIds = new Set<string>();
    memberships.forEach((membership, memberIndex) => {
      const mp = `${prefix}.regionMemberships[${memberIndex}]`;
      if (!isObject(membership)) {
        errors.push(`${mp} must be an object`);
        return;
      }
      const membershipKeys = Object.keys(membership).sort().join(",");
      if (membershipKeys !== "evidence,id,strength") {
        errors.push(`${mp} must contain only id, strength, evidence`);
      }
      const region = membership.id;
      if (!(SUPPORTED_REGIONS as readonly unknown[]).includes(region)) {
        errors.push(`${mp}.id is not canonical`);
      } else if (regionIds.has(region as string)) {
        errors.push(`${prefix} has duplicate region membership: ${region}`);
      } else {
        regionIds.add(region as string);
      }
      const strength = membership.strength;
      if (!(STRENGTHS as readonly unknown[]).includes(strength)) {
        errors.push(`${mp}.strength is not allowed`);
      }
      const evidence = membership.evidence;
      if (!Array.isArray(evidence) || evidence.some((item) => !nonEmptyString(item))) {
        errors.push(`${mp}.evidence must contain nonempty strings`);
      }
      if (strength === "primary" && Array.isArray(evidence) && evidence.length === 0) {
        errors.push(`${mp} primary membership requires evidence`);
      }
    });
  });

  if (artifact.poolIdentity !== mixPoolIdentity(candidates)) {
    errors.push("poolIdentity does not match candidates");
  }

  const provenance = artifact.provenance;
  if (!isObject(provenance)) {
    errors.push("provenance must be an object");
  } else {
    if (Object.keys(provenance).some((k) => !(PROVENANCE_KEYS as readonly string[]).includes(k))) {
      errors.push("provenance contains unsupported fields");
    }
    for (const field of ["source", "inputIdentity"] as const) {
      if (!nonEmptyString(provenance[field])) errors.push(`provenance.${field} must be nonempty`);
    }
    validateIntegral(
      provenance.generatorVersion,
      "generatorVersion",
      "provenance.generatorVersion",
      errors,
    );
    if (provenance.generatorVersion !== MIX_POOL_GENERATOR_VERSION) {
      errors.push("unsupported provenance.generatorVersion");
    }
    if ("referenceAt" in provenance && !isIsoDateTime(provenance.referenceAt)) {
      errors.push("provenance.referenceAt must be an ISO datetime");
    }
  }

  const embedded = artifact.validation;
  if (!isObject(embedded) || Object.keys(embedded).sort().join(",") !== "errors,valid,warnings") {
    errors.push("validation must contain valid, errors, warnings");
  } else if (
    typeof embedded.valid !== "boolean" ||
    !Array.isArray(embedded.errors) ||
    !Array.isArray(embedded.warnings)
  ) {
    errors.push("validation fields have invalid types");
  }

  for (const path of forbiddenPaths(artifact)) {
    errors.push(`forbidden field or local path: ${path}`);
  }

  return { valid: errors.length === 0, errors, warnings };
}

/** Parse JSON text and validate it in one step. Never throws on malformed input. */
export function parseMixPoolArtifact(
  text: string,
): { ok: true; artifact: JsonValue } | { ok: false; errors: string[] } {
  let parsed: JsonValue;
  try {
    parsed = JSON.parse(text) as JsonValue;
  } catch {
    return { ok: false, errors: ["artifact is not valid JSON"] };
  }
  const result = validateMixPoolArtifact(parsed);
  return result.valid ? { ok: true, artifact: parsed } : { ok: false, errors: result.errors };
}
