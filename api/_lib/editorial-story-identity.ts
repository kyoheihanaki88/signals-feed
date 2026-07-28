/**
 * Editorial story identity for consumer-product news. (Phase 3D-1.5)
 *
 * A faithful port of `story_identity()` and the two `story_metadata()` fields it reads,
 * from `pipeline/editorial.py`. Python is the specification; every regex below is
 * transcribed CHARACTER FOR CHARACTER, including the places where its alternation is not
 * parenthesised — for example `\bmicrosoft|xbox|surface\b` is genuinely
 * "`\bmicrosoft`" OR "`xbox`" OR "`surface\b`", and "correcting" it would change matching.
 *
 * Why this identity exists at all: `topic_fingerprint` only knows a fixed vocabulary, so a
 * story outside it fingerprints to the empty set and never overlaps with anything. That is
 * how one Samsung Unpacked event took two TECH slots on 2026-07-25. This is the second,
 * independent identity that closed that hole — brand + product family + launch event.
 *
 * It deliberately does NOT treat everything from one company as one story: a phone launch
 * and a chip-factory or earnings story are different event families, and only one of them
 * is a product story.
 *
 * No file, network, subprocess, environment or clock access. The duplicate path never
 * supplies `publishedAt`, so `isFresh` always returns true and nothing here depends on
 * wall-clock time.
 */

/** Ported from `_PRODUCT_EVENT_FAMILIES`. */
const PRODUCT_EVENT_FAMILIES = new Set(["consumer_launch", "other"]);

/** `_BRAND_RX` — order matters: the FIRST match becomes the brand. */
const BRAND_RX: [RegExp, string][] = [
  [/\bsamsung\b/i, "samsung"],
  [/\b(apple|iphone|ipad|macbook|airpods|apple watch|vision pro)\b/i, "apple"],
  [/\b(google|pixel|android)\b/i, "google"],
  // Transcribed verbatim: the alternation is intentionally un-grouped in Python.
  [/\bmicrosoft|xbox|surface\b/i, "microsoft"],
  [/\b(meta|quest|oculus)\b/i, "meta"],
  [/\b(sony|playstation|\bps5\b)\b/i, "sony"],
  [/\bnintendo|switch 2\b/i, "nintendo"],
  [/\b(openai|chatgpt)\b/i, "openai"],
  [/\banthropic|claude\b/i, "anthropic"],
  [/\bnothing phone\b/i, "nothing"],
  [/\bmotorola|moto g\b/i, "motorola"],
  [/\bxiaomi|redmi\b/i, "xiaomi"],
  [/\boneplus\b/i, "oneplus"],
  [/\btesla\b/i, "tesla"],
];

/**
 * `_FAMILY_RX` — MOST SPECIFIC FIRST. Values are space-separated hierarchies, so
 * "galaxy z fold" is inside "galaxy" and a broad roundup matches a specific model by prefix.
 */
const FAMILY_RX: [RegExp, string][] = [
  [/\b(galaxy\s+)?z\s*fold\b/i, "galaxy z fold"],
  [/\b(galaxy\s+)?z\s*flip\b/i, "galaxy z flip"],
  [/\bgalaxy\s+s\d+\b/i, "galaxy s"],
  [/\bgalaxy\s+watch\b/i, "galaxy watch"],
  [/\bgalaxy\s+buds\b/i, "galaxy buds"],
  [/\bgalaxy\b/i, "galaxy"],
  [/\biphone\b/i, "iphone"],
  [/\bapple\s+watch\b/i, "apple watch"],
  [/\bairpods\b/i, "airpods"],
  [/\bvision\s+pro\b/i, "vision pro"],
  [/\bmac(book)?\b/i, "mac"],
  [/\bipad\b/i, "ipad"],
  [/\bpixel\s+watch\b/i, "pixel watch"],
  [/\bpixel\b/i, "pixel"],
  [/\bplaystation|\bps5\b/i, "playstation"],
  [/\bxbox\b/i, "xbox"],
  [/\bswitch\s*2\b/i, "switch"],
  [/\bquest\s*\d?\b/i, "quest"],
];

/** `_LAUNCH_EVENT_RX` — the strongest same-story signal when two articles share one. */
const LAUNCH_EVENT_RX: [RegExp, string][] = [
  [/\bunpacked\b/i, "unpacked"],
  [/\bwwdc\b/i, "wwdc"],
  [/\bmade by google\b/i, "made by google"],
  [/\bgalaxy ai\b/i, "galaxy ai"],
  [/\b(ces|mwc|ifa)\b/i, "trade show"],
  [/\bkeynote\b/i, "keynote"],
];

/** `_ROUNDUP_RX` — matched against the TITLE only, never the snippet. */
const ROUNDUP_RX =
  /\b(everything|all the news|announcements?|roundup|round-up|biggest|recap|what was announced|highlights|liveblog|as it happened|how to watch)\b/i;

// ── consumer-launch gate (`is_consumer_launch`) ───────────────────────────────────────

const LAUNCH_VERB_RX = /\b(announc\w+|launch\w+|unveil\w+|introduc\w+|releas\w+|debut\w+)\b/i;
const CONSUMER_PRODUCT_RX =
  /\b(smart ?phones?|phones?|handsets?|foldables?|tablets?|laptops?|notebooks?|consoles?|headsets?|smart ?watch\w*|smart ?glasses|earbuds?|e-?readers?|cameras?|televisions?|\btvs?\b|operating system|android \d+|ios \d+|windows \d+|macos \w+|browsers?|apps?\b|platforms?|chatbots?|assistants?)\b/i;
const LAUNCH_EXCLUDE_RX =
  /\b(rumou?r\w*|leak\w*|reportedly|expected to|may\b|could\b|might\b|tipped to|deals?\b|discount\w*|% off|price (cut|drop)|flash sale|clearance|best \w+ to buy|buying guide|benchmark\w*|geekbench|antutu|earnings|revenue|profit|stocks?\b|shares?\b|affiliate|preview\b|hands-?on)\b/i;
const ACCESSORY_RX =
  /\b(cases?\b|covers?\b|cables?\b|chargers?\b|adapters?\b|dongles?\b|straps?\b|bands?\b|stands?\b|mounts?\b|styl(us|i)\b|screen protectors?|keychains?|skins?\b)\b/i;

const CRISIS_RX =
  /\b(kill\w*|death\w*|dead\b|dies?\b|died\b|casualt\w*|famine|outbreak|epidemic|crisis|collaps\w*|missing\b|injur\w*|victims?)\b/i;

/**
 * `_EVENT_RX` — checked in order, first match wins. `consumer_launch` is decided
 * separately and BEFORE this list.
 */
const EVENT_RX: [string, RegExp][] = [
  [
    "conflict",
    /\b(\bwar\b|airstrike\w*|missile\w*|shell\w+|invasion|offensiv\w+|troops|ceasefire|hostage\w*|militant\w*|insurgen\w+|drone strike\w*|artillery|frontline)\b/i,
  ],
  [
    "protest",
    /\b(protest\w*|demonstrat\w+|riot\w*|unrest|marche[sd]|rally|rallies|strike action|walkout)\b/i,
  ],
  [
    "election",
    /\b(election\w*|ballot\w*|\bvote\b|voters?|polls? open|primary|runoff|referendum)\b/i,
  ],
  [
    "political_scandal",
    /\b(scandal|impeach\w+|corruption|bribery|indict\w+|resign\w+ over|cover-?up)\b/i,
  ],
  [
    "disaster",
    /\b(earthquake|hurricane|typhoon|cyclone|wildfire\w*|flood\w*|landslide|tsunami|eruption|heatwave|storm\w* kill|death toll|derail\w*|plane crash|collapse\w* kill)\b/i,
  ],
  [
    "earnings",
    /\b(earnings|quarterly (results|profit|revenue)|q[1-4] (results|profit|revenue)|profit (rise|fall|jump|drop)\w*|revenue (beat|miss)\w*|forecast\w* (cut|raise)\w*)\b/i,
  ],
  [
    "markets",
    /\b(stocks?\b|shares?\b|markets? (rally|slide|fall|rise)|inflation|interest rates?|central bank|\bfed\b|bond yields?|\bgdp\b|recession)\b/i,
  ],
  [
    "science_discovery",
    /\b(discover\w+|breakthrough|telescope|astronomer\w*|fossil\w*|spacecraft|\bnasa\b|space mission|quantum|physicist\w*|new species|researchers? (found|find))\b/i,
  ],
  [
    "health_breakthrough",
    /\b(vaccine\w*|clinical trial\w*|cancer treatment|new drug|therapy shows|cure\w*|transplant\w*|alzheimer\w*|obesity drug\w*)\b/i,
  ],
  [
    "policy",
    /\b(\bbill\b|legislation|regulat\w+|\bban\b|sanction\w*|tariff\w*|policy|lawmaker\w*|court rul\w+|supreme court|minister\w* announc\w+|government plan\w*)\b/i,
  ],
  [
    "culture",
    /\b(film\b|movie\w*|box office|album\w*|concert\w*|festival\w*|museum\w*|exhibition\w*|\bnovel\b|booker|oscars?|grammy\w*|premiere\w*|theatre|theater\b)\b/i,
  ],
];

export type StoryIdentity = {
  brand: string;
  productFamily: string | null;
  /** Every product line named anywhere in the story, after specificity pruning. */
  coveredFamilies: string[];
  launchEvent: string | null;
  eventFamily: string;
  isRoundup: boolean;
  isProductStory: boolean;
};

export type StoryIdentityInput = {
  title?: string | null;
  snippet?: string | null;
  reliability?: string | null;
  publishedAt?: string | null;
};

function text(value: string | null | undefined): string {
  return value ?? "";
}

/**
 * `_is_fresh`. An unknown timestamp is FRESH — missing metadata is neutral and must never
 * invalidate a candidate. The duplicate path never supplies one, so this is always true
 * there and the guard has no clock dependency.
 */
export function isFresh(publishedAt?: string | null, nowMs?: number, maxHours = 72): boolean {
  if (!publishedAt) return true;
  const parsed = Date.parse(publishedAt);
  if (!Number.isFinite(parsed)) return true; // Python swallows the parse error
  const reference = nowMs ?? Date.now();
  return reference - parsed <= maxHours * 60 * 60 * 1_000;
}

/**
 * `is_consumer_launch`. Brand-neutral by construction — no brand list is consulted here.
 * Every gate must pass: no exclusion, no accessory, a launch VERB in the TITLE, a
 * recognisable consumer product anywhere, a credible source, and freshness.
 */
export function isConsumerLaunch(input: StoryIdentityInput, nowMs?: number): boolean {
  const blob = `${text(input.title)} ${text(input.snippet)}`;
  const titleBlob = text(input.title);

  if (LAUNCH_EXCLUDE_RX.test(blob)) return false;
  if (ACCESSORY_RX.test(titleBlob)) return false;
  // The launch verb must be the headline's claim, not a stray "announced" in the snippet.
  if (!LAUNCH_VERB_RX.test(titleBlob)) return false;
  if (!CONSUMER_PRODUCT_RX.test(blob)) return false;
  if (input.reliability === "low") return false;
  return isFresh(input.publishedAt, nowMs);
}

/**
 * The two `story_metadata` fields `story_identity` actually reads.
 *
 * `country`, `region`, `tone` and `discovery_value` are NOT ported: the duplicate path
 * never reads them, and porting the region tables would add a large unused surface that
 * could silently drift from Python.
 */
export function eventClassification(
  input: StoryIdentityInput,
  nowMs?: number,
): { eventFamily: string; consumerLaunch: boolean } {
  const blob = `${text(input.title)} ${text(input.snippet)}`;
  const consumerLaunch = isConsumerLaunch(input, nowMs);

  let eventFamily = "other";
  if (consumerLaunch) {
    eventFamily = "consumer_launch";
  } else {
    for (const [family, rx] of EVENT_RX) {
      if (!rx.test(blob)) continue;
      // "her body was discovered" is a death story, not a science discovery.
      if (family === "science_discovery" && CRISIS_RX.test(blob)) continue;
      eventFamily = family;
      break;
    }
  }
  return { eventFamily, consumerLaunch };
}

/**
 * `story_identity`. Returns null when the story is not recognisably about a consumer
 * brand — unknown is NOT a duplicate signal, and the fingerprint gate still applies.
 */
export function storyIdentity(
  input: StoryIdentityInput,
  nowMs?: number,
): StoryIdentity | null {
  const blob = `${text(input.title)} ${text(input.snippet)}`;

  const brandEntry = BRAND_RX.find(([rx]) => rx.test(blob));
  if (!brandEntry) return null;
  const brand = brandEntry[1];

  const meta = eventClassification(input, nowMs);
  const familyEntry = FAMILY_RX.find(([rx]) => rx.test(blob));
  const productFamily = familyEntry ? familyEntry[1] : null;
  const eventEntry = LAUNCH_EVENT_RX.find(([rx]) => rx.test(blob));
  const launchEvent = eventEntry ? eventEntry[1] : null;

  // Every product line named anywhere. Specificity pruning is essential: "Galaxy Watch 8"
  // matches BOTH "galaxy watch" and the broad "galaxy", and keeping the parent would make
  // every Samsung roundup appear to cover every Galaxy line — a Watch recap would swallow
  // a Z Fold story. A parent is dropped whenever a more specific descendant also matched.
  const matched = new Set<string>();
  for (const [rx, family] of FAMILY_RX) if (rx.test(blob)) matched.add(family);
  const covered: string[] = [];
  for (const family of matched) {
    const hasDescendant = [...matched].some(
      (other) => other !== family && other.startsWith(`${family} `),
    );
    if (!hasDescendant) covered.push(family);
  }

  return {
    brand,
    productFamily,
    // Python holds a set; sorted here so the value is stable and comparable.
    coveredFamilies: covered.sort(),
    launchEvent,
    eventFamily: meta.eventFamily,
    isRoundup: ROUNDUP_RX.test(text(input.title)),
    isProductStory:
      (meta.consumerLaunch || Boolean(productFamily) || Boolean(launchEvent)) &&
      PRODUCT_EVENT_FAMILIES.has(meta.eventFamily),
  };
}
