/**
 * Phase 3E-1 — `/api/edition` connected to the published Editorial Mix Pool, end to end.
 *
 * The sibling suite `edition-custom-mix-integration.test.ts` drives the SELECTOR through
 * the route with an injected candidate source. This suite closes the remaining gap: it
 * wires the REAL retrieval boundary (`editorial-mix-source.ts`) to the REAL route, backed
 * by a fake object store holding a schema-valid artifact — so the chain under test is
 *
 *     stored bytes → reader → orchestrator → selector → feed adapter → HTTP response
 *
 * with only the network replaced.
 *
 * NO NETWORK, NO WRITES. The fake store exposes `get` and nothing else, so a write is not
 * merely unused — it is unrepresentable. Nothing here contacts Upstash or publishes.
 *
 * The response contract asserted here is `SignalsFeed`, which is ground truth in
 * `Signals/Models/SignalsFeed.swift`: a field the client requires and does not receive is a
 * decode failure on a real device.
 */

import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { createEditionHandler } from "../edition.js";
import { createEditionOrchestrator } from "../_lib/edition-orchestrator.js";
import {
  createEditorialCandidateSource,
  editorialMixPoolKey,
} from "../_lib/editorial-mix-source.js";
import {
  MINIMUM_PUBLISHABLE_POOL_SIZE,
  editorialPoolIdentityOf,
  selectorPoolIdentityOf,
} from "../_lib/editorial-mix-pool-schema.js";
import { createUtcDateWindow } from "../_lib/runtime-factory.js";
import type { JsonValue } from "../_lib/mix-pool-schema.js";
import type { PoolObjectStore } from "../_lib/mix-pool-source.js";
import { MemorySecurityLogger } from "../_lib/security-logging.js";
import { createDependencies, editionBody, editionRequest } from "./test-helpers.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURE_DIR = resolve(HERE, "..", "_fixtures");

const DATE = "2026-07-27";
const NOW_MS = Date.parse("2026-07-27T10:00:00Z");

type Artifact = Record<string, JsonValue>;
type Row = Record<string, Record<string, JsonValue>>;

const FIXTURE = JSON.parse(
  readFileSync(join(FIXTURE_DIR, "editorial_mix_pool.json"), "utf8"),
) as Artifact;

/**
 * A region membership is an OBJECT — `{id, strength, evidence}` — not a bare string. The
 * selector reads `strength`, so a candidate is only really "in" a region when its
 * membership is primary; the other two must be present and explicitly `none`.
 */
function membershipsFor(region: string): JsonValue {
  return ["japan", "united_states", "world"].map((id) => ({
    id,
    strength: id === region ? "primary" : "none",
    evidence: id === region ? [`${id}:title`] : [],
  })) as unknown as JsonValue;
}

/**
 * Build a publishable, internally consistent pool.
 *
 * Regions and topics are spread deliberately: the selector must be able to fill five slots
 * across the requested mix, or every test here would degenerate into a shortage test.
 */
function poolOf(
  count = MINIMUM_PUBLISHABLE_POOL_SIZE,
  options: {
    /** The edition date this pool is FOR. Drives `publishedAt`, not just the envelope. */
    forDate?: string;
    /** When the publisher ran. Defaults to an hour before the reader's clock. */
    nowMs?: number;
    overrides?: Partial<Artifact>;
    mutate?: (rows: Row[]) => void;
  } = {},
): Artifact {
  const forDate = options.forDate ?? DATE;
  // The selector enforces a 72-hour freshness window measured from the EDITION date, so a
  // pool built for one date genuinely cannot serve an edition several days away. Stories
  // are therefore dated to their own edition.
  const publishedAt = `${forDate}T06:00:00Z`;
  // `generatedAt` tracks the PUBLISHER's clock, not the edition date: tomorrow's pool is
  // built today, so tying it to the edition date would look like a future artifact.
  const generatedAt = new Date((options.nowMs ?? NOW_MS) - 60 * 60 * 1000).toISOString();
  const base = FIXTURE.candidates as unknown as Row[];
  const rows: Row[] = [];
  const regions = ["japan", "united_states", "world"];
  const topics = ["tech", "business", "ai", "science", "culture"];

  for (let index = 0; index < count; index += 1) {
    const clone = JSON.parse(JSON.stringify(base[index % base.length])) as Row;
    const suffix = `-c${index}`;
    clone.selector.id = `${String(clone.selector.id)}${suffix}`;
    clone.selector.url = `https://example.test/story${suffix}`;
    clone.selector.headline = `Headline ${index}`;
    clone.selector.underlyingStoryIdentity = `story${suffix}`;
    clone.selector.topicFingerprint = `fingerprint${suffix}`;
    clone.selector.regionMemberships = membershipsFor(
      regions[index % regions.length],
    ) as unknown as JsonValue;
    clone.selector.topics = [topics[index % topics.length]] as unknown as JsonValue;
    clone.selector.eligible = true;
    clone.selector.publishedAt = publishedAt as unknown as JsonValue;
    clone.editorial.headline = `Headline ${index}`;
    clone.editorial.originalURL = `https://example.test/story${suffix}`;
    clone.editorial.imageURL = `https://images.example.test/story${suffix}.jpg`;
    rows.push(clone);
  }
  options.mutate?.(rows);

  const artifact: Artifact = {
    ...FIXTURE,
    date: forDate,
    generatedAt,
    candidates: rows as unknown as JsonValue,
    candidateCount: rows.length,
    ...options.overrides,
  };
  artifact.selectorPoolIdentity = selectorPoolIdentityOf(artifact);
  artifact.editorialPoolIdentity = editorialPoolIdentityOf(artifact.candidates as JsonValue[]);
  return artifact;
}

/** A store that can ONLY be read. There is no `put`, `set` or `del` to call. */
function storeOf(
  entries: Record<string, Uint8Array>,
  behaviour?: { throws?: Error },
): PoolObjectStore & { reads: string[] } {
  const reads: string[] = [];
  return {
    reads,
    async get(key: string) {
      reads.push(key);
      if (behaviour?.throws) throw behaviour.throws;
      return entries[key] ?? null;
    },
  } as PoolObjectStore & { reads: string[] };
}

function storeWith(artifact: Artifact, date = DATE) {
  return storeOf({
    [editorialMixPoolKey(date)]: new TextEncoder().encode(JSON.stringify(artifact)),
  });
}

/**
 * The real route, composed the way `runtime-factory.ts` composes it in production: real
 * auth, real orchestrator, real reader, real selector, real feed adapter.
 */
function buildRoute(
  options: {
    store?: (PoolObjectStore & { reads: string[] }) | null;
    enabled?: boolean;
    customMixEnabled?: boolean;
    nowMs?: number;
    dateWindow?: boolean;
  } = {},
) {
  const store = options.store === undefined ? storeWith(poolOf()) : options.store;
  const nowMs = options.nowMs ?? NOW_MS;
  const deps = createDependencies("Production");
  const logger = new MemorySecurityLogger();
  const reasons: string[] = [];

  const handler = createEditionHandler(
    {
      enabled: options.enabled ?? true,
      environment: "Production",
      ...(options.dateWindow === false
        ? {}
        : { isDateAllowed: createUtcDateWindow(() => nowMs) }),
    },
    {
      tokens: deps.tokens,
      revocations: deps.revocations,
      limiter: deps.editionLimiter,
      logger,
      clock: deps.clock,
      requestId: deps.requestId,
      orchestrator: createEditionOrchestrator({
        customMixEnabled: options.customMixEnabled ?? true,
        candidates: createEditorialCandidateSource({
          store,
          now: () => nowMs,
          onResult: (result) => reasons.push(result.ok ? "ok" : result.reason),
        }),
      }),
    },
  );

  const token = deps.tokens.issue({
    subject: deps.tokens.deriveSubject("2000000999999999", "Production"),
    environment: "Production",
  }).accessToken;

  return { handler, logger, token, store, reasons };
}

type Feed = {
  date: string;
  focus: string;
  version: number;
  signals: Record<string, unknown>[];
};

const MIX = { mode: "custom", regions: ["japan", "united_states", "world"], topics: ["tech", "business", "ai", "science", "culture"] };

function request(token: string, overrides: Record<string, unknown> = {}) {
  return editionRequest(token, editionBody({ date: DATE, active: MIX, ...overrides }));
}

async function feedOf(route: ReturnType<typeof buildRoute>, overrides = {}): Promise<Feed> {
  const response = await route.handler(request(route.token, overrides));
  assert.equal(response.status, 200, `expected 200, got ${response.status}`);
  return (await response.json()) as Feed;
}

async function failureOf(route: ReturnType<typeof buildRoute>, overrides = {}) {
  const response = await route.handler(request(route.token, overrides));
  return { status: response.status, body: (await response.json()) as Record<string, unknown> };
}

function lastPath(logger: MemorySecurityLogger): string {
  return logger.events[logger.events.length - 1]?.reasonCode ?? "";
}

// ── 1. the success path ───────────────────────────────────────────────────────────────

test("1. an authenticated Pro request returns 200 from the stored pool", async () => {
  const route = buildRoute();
  const response = await route.handler(request(route.token));
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("content-type")?.includes("application/json"), true);
  assert.deepEqual(route.store?.reads, ["signals:editorial-mix-pool:v1:2026-07-27"]);
  assert.equal(lastPath(route.logger), "custom_mix_pro");
});

test("2. the body is exactly the SignalsFeed envelope the client decodes", async () => {
  const feed = await feedOf(buildRoute());
  assert.deepEqual(Object.keys(feed).sort(), ["date", "focus", "signals", "version"]);
  assert.equal(feed.date, DATE);
  assert.equal(feed.focus, "MIXED");
  assert.equal(feed.version, 1);
});

test("3. exactly five signals, numbered 1..5 without gaps", async () => {
  const feed = await feedOf(buildRoute());
  assert.equal(feed.signals.length, 5);
  assert.deepEqual(feed.signals.map((signal) => signal.number), [1, 2, 3, 4, 5]);
});

test("3b. every field the Swift model requires is present and non-empty", async () => {
  // Ground truth: Signals/Models/SignalsFeed.swift. These are NON-optional there, so a
  // missing one is a decode failure on a real device, not a cosmetic gap.
  const required = [
    "number", "lead", "category", "source", "headline", "summary",
    "keyTakeaways", "whyItMatters", "originalURL", "readTime", "imageURL",
  ];
  const feed = await feedOf(buildRoute());
  for (const signal of feed.signals) {
    for (const field of required) {
      assert.ok(field in signal, `signal ${String(signal.number)} is missing ${field}`);
      const value = signal[field];
      assert.notEqual(value, null, `${field} is null`);
      if (typeof value === "string") assert.notEqual(value.trim(), "", `${field} is empty`);
      if (Array.isArray(value)) assert.ok(value.length > 0, `${field} is an empty array`);
    }
  }
});

test("3c. the five signals are five DISTINCT stories", async () => {
  const feed = await feedOf(buildRoute());
  assert.equal(new Set(feed.signals.map((signal) => signal.headline)).size, 5);
  assert.equal(new Set(feed.signals.map((signal) => signal.originalURL)).size, 5);
});

// ── 4. determinism ────────────────────────────────────────────────────────────────────

test("4. identical inputs produce a byte-identical response", async () => {
  const route = buildRoute();
  const first = await route.handler(request(route.token));
  const second = await route.handler(request(route.token));
  assert.equal(await first.text(), await second.text());
});

test("4b. the same pool served twice to the same preferences does not drift", async () => {
  const route = buildRoute();
  const a = await feedOf(route);
  const b = await feedOf(route);
  assert.deepEqual(a, b);
});

// ── 5. preferences ────────────────────────────────────────────────────────────────────

test("5. `active` drives the selection and `pending` is ignored", async () => {
  const route = buildRoute();
  const withoutPending = await feedOf(route);
  const withPending = await feedOf(route, {
    pending: { mode: "custom", regions: ["japan"], topics: ["culture"] },
  });
  // A pending mix is a NEXT-EDITION preference. Honouring it today would silently change
  // what the user is reading before they confirmed the change.
  assert.deepEqual(withPending, withoutPending);
});

test("5b. REORDERED preferences normalise to the same edition", async () => {
  // The contract sorts each list, so preference order is not a hidden input to the mix.
  const route = buildRoute();
  const canonical = await feedOf(route);
  const shuffled = await feedOf(route, {
    active: {
      mode: "custom",
      regions: ["world", "japan", "united_states"],
      topics: ["science", "business", "culture", "ai", "tech"],
    },
  });
  assert.deepEqual(shuffled, canonical);
});

test("5c. DUPLICATE preferences are refused, not silently absorbed", async () => {
  // `validateCanonicalList` rejects a repeated entry rather than de-duplicating it. That is
  // deliberate: a client sending ["japan","japan"] has a bug, and quietly accepting it
  // would let two different client states map to one edition with no signal that anything
  // was wrong. The failure is a plain 400 — no pool read, no partial edition.
  const route = buildRoute();
  const { status, body } = await failureOf(route, {
    active: { mode: "custom", regions: ["japan", "japan", "world"], topics: ["tech"] },
  });
  assert.equal(status, 400);
  assert.equal(body.code ?? (body.error as { code?: string })?.code, "invalid_region");
  assert.deepEqual(route.store?.reads, [], "an invalid contract still reached the store");
});

// ── 6. duplicate protection survives the whole chain ──────────────────────────────────

test("6. two candidates for the SAME underlying story cannot both be served", async () => {
  // Same story identity, different ids and URLs: the crude "is this the same URL" check
  // would let both through. The editorial guard is what must catch it.
  const pool = poolOf(MINIMUM_PUBLISHABLE_POOL_SIZE, {
    mutate: (rows) => {
      for (const index of [0, 1]) {
        rows[index].selector.underlyingStoryIdentity = "shared-story";
        rows[index].selector.regionMemberships = membershipsFor("japan");
        rows[index].selector.topics = ["tech"] as unknown as JsonValue;
      }
    },
  });
  const feed = await feedOf(buildRoute({ store: storeWith(pool) }));
  const served = new Set(feed.signals.map((signal) => signal.headline));
  assert.equal(served.size, 5);
  assert.ok(
    !(served.has("Headline 0") && served.has("Headline 1")),
    "both sides of the same underlying story were served",
  );
});

test("6b. an exact repeat of one story is never served twice", async () => {
  const feed = await feedOf(buildRoute());
  const identities = feed.signals.map((signal) => `${String(signal.headline)}|${String(signal.originalURL)}`);
  assert.equal(new Set(identities).size, 5);
});

// ── 7. fewer than five resolvable stories ─────────────────────────────────────────────

test("7. a pool that cannot fill five slots returns 503, never a partial edition", async () => {
  // Every candidate sits in ONE region/topic the request does not ask for, so the selector
  // cannot fill the mix. The route must refuse rather than serve three signals.
  const pool = poolOf(MINIMUM_PUBLISHABLE_POOL_SIZE, {
    mutate: (rows) => {
      rows.forEach((row, index) => {
        row.selector.regionMemberships = membershipsFor("japan");
        row.selector.topics = ["health"] as unknown as JsonValue;
        row.selector.eligible = index < 2;
      });
    },
  });
  const route = buildRoute({ store: storeWith(pool) });
  const { status, body } = await failureOf(route, {
    active: { mode: "custom", regions: ["japan"], topics: ["health"] },
  });
  assert.equal(status, 503);
  assert.equal(body.code, "custom_mix_unavailable");
  assert.equal("signals" in body, false, "a partial edition was served");
});

// ── 8. every failure path is the SAME public answer ───────────────────────────────────

test("8. missing configuration, missing pool, malformed pool, stale pool, wrong date and "
  + "provider failure are publicly indistinguishable", async () => {
  const bad = new TextEncoder().encode("{ not json");
  const cases: Record<string, ReturnType<typeof buildRoute>> = {
    not_configured: buildRoute({ store: null }),
    missing: buildRoute({ store: storeOf({}) }),
    malformed: buildRoute({ store: storeOf({ [editorialMixPoolKey(DATE)]: bad }) }),
    stale: buildRoute({
      store: storeWith(poolOf(15, { overrides: { generatedAt: "2026-07-01T09:00:00Z" } })),
    }),
    wrong_date: buildRoute({
      // Stored under TODAY's key but carrying yesterday's date.
      store: storeOf({
        [editorialMixPoolKey(DATE)]: new TextEncoder().encode(
          JSON.stringify(poolOf(15, { overrides: { date: "2026-07-26" } })),
        ),
      }),
    }),
    too_small: buildRoute({ store: storeWith(poolOf(4)) }),
    identity: buildRoute({
      store: storeWith({ ...poolOf(15), selectorPoolIdentity: "0".repeat(64) }),
    }),
    provider: buildRoute({ store: storeOf({}, { throws: new Error("NOPERM at db.upstash.io") }) }),
    timeout: buildRoute({
      store: storeOf({}, { throws: Object.assign(new Error("t"), { name: "AbortError" }) }),
    }),
  };

  const bodies = new Set<string>();
  for (const [label, route] of Object.entries(cases)) {
    const { status, body } = await failureOf(route);
    assert.equal(status, 503, `${label} did not answer 503`);
    assert.deepEqual(body, { status: "unavailable", code: "custom_mix_unavailable" }, label);
    bodies.add(JSON.stringify(body));
  }
  // One body for every cause: the client cannot probe the storage layer through the route.
  assert.equal(bodies.size, 1);
});

test("8b. the internal reason is still recorded, so operators are not blind", async () => {
  const route = buildRoute({ store: storeOf({}) });
  await failureOf(route);
  assert.deepEqual(route.reasons, ["candidate_pool_missing"]);
  assert.equal(lastPath(route.logger), "standard_candidates_unavailable");
});

// ── 9. the UTC ±1 day window ──────────────────────────────────────────────────────────

test("9. today, yesterday and tomorrow (UTC) are accepted", async () => {
  for (const [label, date] of [
    ["today", "2026-07-27"],
    ["yesterday", "2026-07-26"],
    ["tomorrow", "2026-07-28"],
  ]) {
    const pool = poolOf(15, { forDate: date });
    const route = buildRoute({ store: storeWith(pool, date) });
    const response = await route.handler(request(route.token, { date }));
    assert.equal(response.status, 200, `${label} (${date}) was refused`);
  }
});

test("9b. a date outside ±1 day is refused WITHOUT touching the store", async () => {
  for (const date of ["2026-07-25", "2026-07-29", "2026-01-01", "2027-07-27"]) {
    const route = buildRoute();
    const response = await route.handler(request(route.token, { date }));
    assert.notEqual(response.status, 200, `${date} was accepted`);
    assert.deepEqual(route.store?.reads, [], `${date} reached the store`);
  }
});

test("9c. the window spans a MONTH boundary correctly", async () => {
  // 1 August: 31 July and 2 August must both be inside the window.
  const nowMs = Date.parse("2026-08-01T12:00:00Z");
  const allowed = createUtcDateWindow(() => nowMs);
  assert.equal(allowed("2026-07-31"), true);
  assert.equal(allowed("2026-08-01"), true);
  assert.equal(allowed("2026-08-02"), true);
  assert.equal(allowed("2026-07-30"), false);
  assert.equal(allowed("2026-08-03"), false);

  const date = "2026-07-31";
  const pool = poolOf(15, { forDate: date, nowMs });
  const route = buildRoute({ store: storeWith(pool, date), nowMs });
  assert.equal((await route.handler(request(route.token, { date }))).status, 200);
});

test("9d. the window spans a YEAR boundary correctly", async () => {
  const nowMs = Date.parse("2027-01-01T00:30:00Z");
  const allowed = createUtcDateWindow(() => nowMs);
  assert.equal(allowed("2026-12-31"), true);
  assert.equal(allowed("2027-01-01"), true);
  assert.equal(allowed("2027-01-02"), true);
  assert.equal(allowed("2026-12-30"), false);

  const date = "2026-12-31";
  const pool = poolOf(15, { forDate: date, nowMs });
  const route = buildRoute({ store: storeWith(pool, date), nowMs });
  assert.equal((await route.handler(request(route.token, { date }))).status, 200);
});

test("9e. the window is computed from UTC MIDNIGHT, not from the current instant", async () => {
  // Late-evening UTC must not shrink the window: a user at 23:50 still gets tomorrow.
  const late = createUtcDateWindow(() => Date.parse("2026-07-27T23:50:00Z"));
  const early = createUtcDateWindow(() => Date.parse("2026-07-27T00:05:00Z"));
  for (const allowed of [late, early]) {
    assert.equal(allowed("2026-07-26"), true);
    assert.equal(allowed("2026-07-28"), true);
    assert.equal(allowed("2026-07-25"), false);
  }
});

test("9f. a malformed date never reaches the window or the store", async () => {
  const allowed = createUtcDateWindow(() => NOW_MS);
  for (const bad of ["", "not-a-date", "2026-13-01", "2026-02-30"]) {
    assert.equal(allowed(bad), false, `${bad} was allowed`);
  }
  const route = buildRoute();
  const response = await route.handler(request(route.token, { date: "2026-13-01" }));
  assert.notEqual(response.status, 200);
  assert.deepEqual(route.store?.reads, []);
});

// ── 10. the kill switch ───────────────────────────────────────────────────────────────

test("10. the global kill switch short-circuits BEFORE any pool read", async () => {
  const route = buildRoute({ customMixEnabled: false });
  const { status, body } = await failureOf(route);
  assert.equal(status, 503);
  assert.equal(body.code, "custom_mix_unavailable");
  // The point of a kill switch is to stop the work, not just to hide the answer.
  assert.deepEqual(route.store?.reads, [], "the kill switch still hit the store");
  assert.deepEqual(route.reasons, [], "the kill switch still ran the reader");
  assert.equal(lastPath(route.logger), "standard_custom_mix_disabled");
});

test("10b. the route-level `enabled` flag also prevents any read", async () => {
  const route = buildRoute({ enabled: false });
  const response = await route.handler(request(route.token));
  assert.notEqual(response.status, 200);
  assert.deepEqual(route.store?.reads, []);
});

// ── 11. nothing internal escapes ──────────────────────────────────────────────────────

test("11. a 200 response carries no selector metadata of any kind", async () => {
  const response = await buildRoute().handler(request(buildRoute().token));
  const text = await response.text();
  for (const leak of [
    "selector", "candidateLogs", "rejectionReason", "duplicate", "mixIdentity",
    "unfilledSlots", "shortage", "baseScore", "quality", "eligible",
    "underlyingStoryIdentity", "topicFingerprint", "regionMemberships",
    "selectorPoolIdentity", "editorialPoolIdentity", "provenance",
  ]) {
    assert.ok(!text.includes(leak), `the 200 body leaked ${leak}`);
  }
});

test("11b. no response of any kind names the store, a key or a credential", async () => {
  const routes = [buildRoute(), buildRoute({ store: storeOf({}) }), buildRoute({ store: null })];
  for (const route of routes) {
    const response = await route.handler(request(route.token));
    const text = (await response.text()).toLowerCase();
    for (const secret of [
      "upstash", "kv_rest", "redis", "signals:editorial-mix-pool", "candidate_pool",
      "http_4", "http_5", "token", "bearer",
    ]) {
      assert.ok(!text.includes(secret), `a response leaked ${secret}`);
    }
  }
});

test("11c. an internal reason code never becomes a public code", async () => {
  const route = buildRoute({ store: storeOf({}, { throws: new Error("NOPERM") }) });
  const { body } = await failureOf(route);
  assert.equal(body.code, "custom_mix_unavailable");
  assert.equal(route.reasons[0], "candidate_pool_provider_error");
  assert.notEqual(body.code, route.reasons[0]);
});

// ── 12. the read path is read-only, and the static path is untouched ──────────────────

test("12. the store the route is given exposes only `get`", () => {
  const store = storeWith(poolOf());
  const methods = Object.keys(store).filter(
    (key) => typeof (store as unknown as Record<string, unknown>)[key] === "function",
  );
  assert.deepEqual(methods, ["get"], `the store exposes ${methods.join(", ")}`);
});

test("12b. no module in the request path names the write token", () => {
  const strip = (text: string) =>
    text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
  for (const relative of [
    "edition.ts",
    "_lib/edition-orchestrator.ts",
    "_lib/editorial-mix-source.ts",
    "_lib/editorial-mix-feed.ts",
    "_lib/runtime-factory.ts",
    "_lib/upstash-pool-store.ts",
  ]) {
    const code = strip(readFileSync(join(HERE, "..", relative), "utf8"));
    assert.ok(!code.includes("KV_REST_API_WRITE_TOKEN"), `${relative} names the write token`);
  }
});

test("12c. the request path issues no mutating command", () => {
  const strip = (text: string) =>
    text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
  for (const relative of ["_lib/editorial-mix-source.ts", "_lib/edition-orchestrator.ts", "edition.ts"]) {
    const code = strip(readFileSync(join(HERE, "..", relative), "utf8"));
    for (const command of ["\"set\"", "\"del\"", "\"expire\"", "flushdb", "FLUSHALL", ".put("]) {
      assert.ok(!code.includes(command), `${relative} can issue ${command}`);
    }
  }
});

test("12d. connecting Custom Mix did not put the route into the Free/static path", () => {
  // The Free daily edition is a STATIC CDN artifact. If a route ever started reading it
  // server-side, a Custom Mix failure could silently serve the standard edition under a
  // 200 — which is exactly the confusion this endpoint must not create.
  const strip = (text: string) =>
    text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
  for (const relative of ["edition.ts", "_lib/edition-orchestrator.ts", "_lib/runtime-factory.ts"]) {
    const code = strip(readFileSync(join(HERE, "..", relative), "utf8"));
    for (const needle of ["latest.json", "editions/", "readFileSync"]) {
      assert.ok(!code.includes(needle), `${relative} reaches for the static edition (${needle})`);
    }
    // `edition.ts` DEFINES `async fetch(request)` — that is the Vercel entry point. What
    // must not exist is an OUTBOUND fetch, which is how a static edition would be pulled in.
    for (const outbound of ["await fetch(", "globalThis.fetch", "fetch(\"http", "fetch(`http"]) {
      assert.ok(!code.includes(outbound), `${relative} makes an outbound request (${outbound})`);
    }
  }
});

test("12e. the route requires a Pro token — there is no anonymous Custom Mix", async () => {
  const route = buildRoute();
  for (const token of [null, "not-a-token", ""]) {
    const response = await route.handler(request(token as unknown as string));
    assert.equal(response.status, 401, `token ${String(token)} was accepted`);
  }
  assert.deepEqual(route.store?.reads, [], "an unauthenticated caller reached the store");
});
