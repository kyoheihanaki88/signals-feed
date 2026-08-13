// v2 = strict topic allowlist + fixed region priority (see custom-mix-types.ts).
// The Production env var CUSTOM_MIX_SELECTOR_VERSION must move to 2 with this deploy.
export const SUPPORTED_SELECTOR_VERSION = 2;
export const STORY_COUNT = 5;
export const ALLOWED_REGIONS = [
  "japan",
  "united_states",
  "world",
] as const;
export const ALLOWED_TOPICS = [
  "business",
  "tech",
  "ai",
  "science",
  "climate",
  "health",
  "culture",
] as const;

export type Region = (typeof ALLOWED_REGIONS)[number];
export type Topic = (typeof ALLOWED_TOPICS)[number];
export type ActiveMix = {
  mode: "custom";
  regions: Region[];
  topics: Topic[];
};
export type EditionRequest = {
  date: string;
  active: ActiveMix;
  pending: ActiveMix | null;
  selectorVersion: typeof SUPPORTED_SELECTOR_VERSION;
  storyCount: typeof STORY_COUNT;
};

export class ContractValidationError extends Error {
  constructor(
    readonly code:
      | "invalid_request"
      | "invalid_region"
      | "invalid_topic"
      | "unsupported_selector_version",
  ) {
    super(code);
    this.name = "ContractValidationError";
  }
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function assertOnlyKeys(
  value: Record<string, unknown>,
  allowed: readonly string[],
): void {
  if (Object.keys(value).some((key) => !allowed.includes(key))) {
    throw new ContractValidationError("invalid_request");
  }
}

function validateCanonicalList<T extends string>(
  value: unknown,
  allowed: readonly T[],
  code: "invalid_region" | "invalid_topic",
): T[] {
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
    throw new ContractValidationError(code);
  }
  const items = value as string[];
  // A REPEATED entry is refused, not silently de-duplicated. That is deliberate: a client
  // sending ["japan","japan"] has a bug, and quietly absorbing it would let two different
  // client states map to one edition with no signal that anything was wrong. Order, by
  // contrast, IS normalised below — sorting makes the mix identity independent of the
  // order the user happened to tap the toggles in.
  if (
    new Set(items).size !== items.length ||
    items.some((item) => !allowed.includes(item as T))
  ) {
    throw new ContractValidationError(code);
  }
  return [...(items as T[])].sort();
}

function validateMix(value: unknown): ActiveMix {
  if (!isObject(value)) {
    throw new ContractValidationError("invalid_request");
  }
  assertOnlyKeys(value, ["mode", "regions", "topics"]);
  if (value.mode !== "custom") {
    throw new ContractValidationError("invalid_request");
  }
  const regions = validateCanonicalList(
    value.regions,
    ALLOWED_REGIONS,
    "invalid_region",
  );
  if (regions.length === 0) {
    throw new ContractValidationError("invalid_region");
  }
  const topics = validateCanonicalList(
    value.topics,
    ALLOWED_TOPICS,
    "invalid_topic",
  );
  return { mode: "custom", regions, topics };
}

function isStrictIsoDate(value: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    return false;
  }
  const [year, month, day] = value.split("-").map(Number);
  const date = new Date(Date.UTC(year, month - 1, day));
  return (
    date.getUTCFullYear() === year &&
    date.getUTCMonth() === month - 1 &&
    date.getUTCDate() === day
  );
}

export function validateEditionRequest(value: unknown): EditionRequest {
  if (!isObject(value)) {
    throw new ContractValidationError("invalid_request");
  }
  assertOnlyKeys(value, [
    "date",
    "active",
    "pending",
    "selectorVersion",
    "storyCount",
  ]);
  if (value.selectorVersion !== SUPPORTED_SELECTOR_VERSION) {
    throw new ContractValidationError("unsupported_selector_version");
  }
  if (value.storyCount !== STORY_COUNT) {
    throw new ContractValidationError("invalid_request");
  }
  if (
    typeof value.date !== "string" ||
    !isStrictIsoDate(value.date)
  ) {
    throw new ContractValidationError("invalid_request");
  }
  const active = validateMix(value.active);
  const pending = value.pending === null ? null : validateMix(value.pending);

  return {
    date: value.date,
    active,
    pending,
    selectorVersion: SUPPORTED_SELECTOR_VERSION,
    storyCount: STORY_COUNT,
  };
}

export function normalizedMixCacheIdentity(request: EditionRequest): string {
  return [
    `selector=${request.selectorVersion}`,
    `count=${request.storyCount}`,
    `regions=${request.active.regions.join(",")}`,
    `topics=${request.active.topics.join(",")}`,
  ].join("|");
}
