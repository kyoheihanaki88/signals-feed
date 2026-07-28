/**
 * Phase 3D-2 — Custom Mix integration at the real `/api/edition` boundary.
 *
 * WHAT THIS SUITE ESTABLISHES, and what it deliberately does not.
 *
 * `/api/edition` is a Pro-ONLY endpoint. A Signals token is issued only by
 * `/api/auth/exchange` after Apple's signed transaction verifies AND the App Store Server
 * API confirms the purchase is still current for `com.signalsapp.pro.lifetime`. There is no
 * "Free user with a token" state to test, and an anonymous caller is rejected at
 * authentication — the Free daily edition is served by the STATIC `latest.json`, not by
 * this route. So the classic "Free falls back to the standard edition inside the handler"
 * shape does not exist here, and these tests assert the real behaviour instead.
 *
 * Custom Mix is NOT production-connected. The production orchestrator has no candidate
 * source, so every real request resolves to `standard_candidates_unavailable` and the route
 * answers `503 selector_not_connected`, byte-identical to before this phase. The tests
 * below drive the selector through the real route by injecting a fixture candidate source
 * at the orchestration seam — which is exactly what that seam exists for.
 */

import assert from "node:assert/strict";
import test from "node:test";
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { createEditionHandler } from "../edition.js";
import {
  candidatesUnavailable,
  createDisconnectedEditionOrchestrator,
  createEditionOrchestrator,
  type MixCandidateSource,
} from "../_lib/edition-orchestrator.js";
import { selectCustomMix } from "../_lib/custom-mix-selector.js";
import type { MixCandidate, MixSelectionResult, SelectCustomMixOptions } from "../_lib/custom-mix-types.js";
import type { EditionRequest } from "../_lib/custom-mix-contract.js";
import { MemorySecurityLogger } from "../_lib/security-logging.js";
import {
  createDependencies,
  editionBody,
  editionRequest,
  responseCode,
} from "./test-helpers.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURE_DIR = resolve(HERE, "..", "_fixtures");

function findPipelineDir(): string {
  let current = HERE;
  for (let depth = 0; depth < 8; depth += 1) {
    const candidate = join(current, "pipeline");
    if (existsSync(join(candidate, "custom_mix_selector.py"))) return candidate;
    const parent = dirname(current);
    if (parent === current) break;
    current = parent;
  }
  return join(resolve(HERE, "..", ".."), "pipeline");
}
const PIPELINE_DIR = findPipelineDir();

const editorialCase = JSON.parse(
  readFileSync(join(FIXTURE_DIR, "custom_mix_editorial_duplicate_case.json"), "utf8"),
) as { date: string; candidates: MixCandidate[] };
const editorialGolden = JSON.parse(
  readFileSync(join(FIXTURE_DIR, "custom_mix_editorial_duplicate_golden.json"), "utf8"),
) as { result: MixSelectionResult };
const baseCandidates = (
  JSON.parse(readFileSync(join(FIXTURE_DIR, "custom_mix_candidates.json"), "utf8")) as {
    candidates: MixCandidate[];
  }
).candidates;

const DATE = "2026-07-27";

function sourceOf(candidates: MixCandidate[]): MixCandidateSource {
  return { async loadCandidates() { return candidates.map((c) => structuredClone(c)); } };
}

/** Builds the real route with real auth and a chosen orchestrator. */
function buildRoute(options: {
  orchestrator?: ReturnType<typeof createEditionOrchestrator>;
  enabled?: boolean;
} = {}) {
  const deps = createDependencies("Production");
  const logger = new MemorySecurityLogger();
  const handler = createEditionHandler(
    { enabled: options.enabled ?? true, environment: "Production" },
    {
      tokens: deps.tokens,
      revocations: deps.revocations,
      limiter: deps.editionLimiter,
      logger,
      clock: deps.clock,
      requestId: deps.requestId,
      ...(options.orchestrator ? { orchestrator: options.orchestrator } : {}),
    },
  );
  const token = deps.tokens.issue({
    subject: deps.tokens.deriveSubject("2000000999999999", "Production"),
    environment: "Production",
  }).accessToken;
  return { handler, logger, token, deps };
}

function lastPath(logger: MemorySecurityLogger): string {
  return logger.events[logger.events.length - 1]?.reasonCode ?? "";
}

// ── 1. existing request with no Custom Mix orchestration ──────────────────────────────

test("1. an authenticated request with no orchestrator is unchanged", async () => {
  const { handler, token, logger } = buildRoute();
  const response = await handler(editionRequest(token));

  assert.equal(response.status, 503);
  assert.deepEqual(await response.clone().json(), {
    status: "not_connected",
    code: "selector_not_connected",
  });
  assert.equal(lastPath(logger), "selector_not_connected");
});

test("1b. the PRODUCTION orchestrator leaves the response byte-identical", async () => {
  const withOut = buildRoute();
  const withProd = buildRoute({ orchestrator: createDisconnectedEditionOrchestrator(true) });

  const a = await withOut.handler(editionRequest(withOut.token));
  const b = await withProd.handler(editionRequest(withProd.token));

  assert.equal(a.status, b.status);
  assert.equal(await a.clone().text(), await b.clone().text());
  // Only the internal log path differs, and it names the real reason.
  assert.equal(lastPath(withProd.logger), "standard_candidates_unavailable");
});

// ── 2/3/4. no client-supplied entitlement is ever trusted ─────────────────────────────

test("2+3. an anonymous caller never reaches the orchestrator", async () => {
  let called = false;
  const orchestrator = createEditionOrchestrator({
    candidates: { async loadCandidates() { called = true; return baseCandidates; } },
    customMixEnabled: true,
  });
  const { handler, logger } = buildRoute({ orchestrator });

  const response = await handler(editionRequest(null));
  assert.equal(response.status, 401);
  assert.equal(await responseCode(response), "missing_token");
  assert.equal(called, false, "an unauthenticated request reached candidate loading");
  assert.equal(lastPath(logger), "missing_token");
});

test("4. a client-claimed Pro flag in the body is rejected by the contract, not honoured", async () => {
  let called = false;
  const orchestrator = createEditionOrchestrator({
    candidates: { async loadCandidates() { called = true; return baseCandidates; } },
    customMixEnabled: true,
  });
  const { handler, token } = buildRoute({ orchestrator });

  const response = await handler(
    editionRequest(token, { ...editionBody(), isPro: true } as Record<string, unknown>),
  );
  assert.equal(response.status, 400);
  assert.equal(await responseCode(response), "invalid_request");
  assert.equal(called, false, "an unknown body field reached the selector");
});

test("4b. a forged token cannot reach the orchestrator", async () => {
  let called = false;
  const orchestrator = createEditionOrchestrator({
    candidates: { async loadCandidates() { called = true; return baseCandidates; } },
    customMixEnabled: true,
  });
  const { handler } = buildRoute({ orchestrator });

  for (const bad of ["not-a-token", "a.b.c", ""]) {
    const response = await handler(editionRequest(bad));
    assert.equal(response.status, 401);
  }
  assert.equal(called, false);
});

// ── 5. kill switch ────────────────────────────────────────────────────────────────────

test("5. Custom Mix disabled: the selector never runs", async () => {
  let selectorCalls = 0;
  const orchestrator = createEditionOrchestrator({
    candidates: sourceOf(baseCandidates),
    customMixEnabled: false,
    runSelector: (o) => {
      selectorCalls += 1;
      return selectCustomMix(o);
    },
  });
  const outcome = await orchestrator({
    contract: {
      date: DATE,
      active: { mode: "custom" as const, regions: ["japan" as const], topics: [] },
      pending: null,
      selectorVersion: 1 as const,
      storyCount: 5 as const,
    },
  });
  assert.equal(outcome.path, "standard_custom_mix_disabled");
  assert.equal(outcome.selection, null);
  assert.equal(selectorCalls, 0);
});

test("5b. the route-level kill switch answers before authentication", async () => {
  const { handler, token, logger } = buildRoute({
    enabled: false,
    orchestrator: createDisconnectedEditionOrchestrator(false),
  });
  const response = await handler(editionRequest(token));
  assert.equal(response.status, 503);
  assert.equal(await responseCode(response), "custom_mix_disabled");
  assert.equal(lastPath(logger), "custom_mix_disabled");
});

// ── 6/7. verified Pro with a candidate source: the selector runs ──────────────────────

test("6+7. verified Lifetime Pro with candidates: the selector runs and matches the golden", async () => {
  let selectorCalls = 0;
  let seen: SelectCustomMixOptions | null = null;
  const orchestrator = createEditionOrchestrator({
    candidates: sourceOf(editorialCase.candidates),
    customMixEnabled: true,
    runSelector: (o) => {
      selectorCalls += 1;
      seen = o;
      return selectCustomMix(o);
    },
  });
  const { handler, token, logger } = buildRoute({ orchestrator });

  const response = await handler(
    editionRequest(token, { ...editionBody(), date: editorialCase.date }),
  );

  assert.equal(selectorCalls, 1, "the selector did not run for a verified Pro request");
  assert.equal(lastPath(logger), "custom_mix_pro");
  // The public response is still the 503 — no 200 contract exists yet.
  assert.equal(response.status, 503);

  const outcome = await orchestrator({
    contract: {
      date: editorialCase.date,
      active: { mode: "custom" as const, regions: ["japan" as const], topics: [] },
      pending: null,
      selectorVersion: 1 as const,
      storyCount: 5 as const,
    },
  });
  assert.equal(outcome.path, "custom_mix_pro");
  assert.deepEqual(
    JSON.parse(JSON.stringify(outcome.selection)),
    editorialGolden.result,
    "selection drifted from the committed Python golden",
  );

  // The contract's validated preferences are what reached the selector.
  assert.ok(seen);
  assert.deepEqual((seen as SelectCustomMixOptions).regions, ["japan"]);
  assert.equal((seen as SelectCustomMixOptions).size, 5);
});

// ── 8. entitlement / dependency failure ───────────────────────────────────────────────

test("8. a candidate-source failure falls back instead of 500-ing", async () => {
  const orchestrator = createEditionOrchestrator({
    candidates: {
      async loadCandidates() {
        throw new Error("pool store unreachable");
      },
    },
    customMixEnabled: true,
  });
  const { handler, token, logger } = buildRoute({ orchestrator });

  const response = await handler(editionRequest(token));
  assert.equal(response.status, 503);
  assert.equal(await responseCode(response), "selector_not_connected");
  assert.equal(lastPath(logger), "standard_candidates_unavailable");
});

test("8b. a selector failure falls back instead of 500-ing", async () => {
  const orchestrator = createEditionOrchestrator({
    candidates: sourceOf(baseCandidates),
    customMixEnabled: true,
    runSelector: () => {
      throw new Error("selector exploded");
    },
  });
  const { handler, token, logger } = buildRoute({ orchestrator });

  const response = await handler(editionRequest(token));
  assert.equal(response.status, 503);
  assert.equal(lastPath(logger), "standard_selector_unavailable");
});

test("8c. a revoked subject is rejected before the orchestrator", async () => {
  let called = false;
  const orchestrator = createEditionOrchestrator({
    candidates: { async loadCandidates() { called = true; return baseCandidates; } },
    customMixEnabled: true,
  });
  const deps = createDependencies("Production");
  const logger = new MemorySecurityLogger();
  const subject = deps.tokens.deriveSubject("2000000999999999", "Production");
  deps.revocations.revoke(subject);
  const handler = createEditionHandler(
    { enabled: true, environment: "Production" },
    {
      tokens: deps.tokens,
      revocations: deps.revocations,
      limiter: deps.editionLimiter,
      logger,
      clock: deps.clock,
      requestId: deps.requestId,
      orchestrator,
    },
  );
  const token = deps.tokens.issue({ subject, environment: "Production" }).accessToken;

  const response = await handler(editionRequest(token));
  assert.equal(response.status, 401);
  assert.equal(await responseCode(response), "revoked");
  assert.equal(called, false, "a revoked subject reached candidate loading");
});

// ── 9. malformed payload ──────────────────────────────────────────────────────────────

test("9. a malformed Custom Mix payload never reaches the selector", async () => {
  let called = false;
  const orchestrator = createEditionOrchestrator({
    candidates: { async loadCandidates() { called = true; return baseCandidates; } },
    customMixEnabled: true,
  });
  const { handler, token } = buildRoute({ orchestrator });

  const bad: [Record<string, unknown>, number, string][] = [
    [{ ...editionBody(), active: { mode: "custom", regions: ["mars"], topics: [] } }, 400, "invalid_region"],
    [{ ...editionBody(), active: { mode: "custom", regions: ["japan"], topics: ["sports"] } }, 400, "invalid_topic"],
    [{ ...editionBody(), selectorVersion: 2 }, 422, "unsupported_selector_version"],
    [{ ...editionBody(), storyCount: 6 }, 400, "invalid_request"],
    [{ ...editionBody(), active: { mode: "custom", regions: [], topics: [] } }, 400, "invalid_region"],
  ];
  for (const [body, status, code] of bad) {
    const response = await handler(editionRequest(token, body));
    assert.equal(response.status, status, `body ${JSON.stringify(body).slice(0, 60)}`);
    assert.equal(await responseCode(response), code);
  }
  assert.equal(called, false, "an invalid contract reached the selector");
});

// ── 10. candidate shortage ────────────────────────────────────────────────────────────

test("10. too few matching candidates degrades deterministically with no duplicate leakage", async () => {
  const thin = baseCandidates.filter((c) =>
    ["jp-quake-a", "global-quake-duplicate", "world-health"].includes(c.id),
  );
  const orchestrator = createEditionOrchestrator({
    candidates: sourceOf(thin),
    customMixEnabled: true,
  });
  const contract: EditionRequest = {
    date: DATE,
    active: { mode: "custom" as const, regions: ["japan"], topics: [] },
    pending: null,
    selectorVersion: 1 as const,
    storyCount: 5 as const,
  };

  const first = await orchestrator({ contract });
  const second = await orchestrator({ contract });
  assert.equal(first.path, "custom_mix_pro");
  assert.equal(JSON.stringify(first.selection), JSON.stringify(second.selection));

  const selection = first.selection as MixSelectionResult;
  assert.equal(selection.metadata.shortage, true);
  assert.ok(selection.metadata.unfilledSlots > 0);
  // jp-quake-a and global-quake-duplicate share an underlying story: only one may survive.
  const survivors = selection.selectedIds.filter((id) =>
    ["jp-quake-a", "global-quake-duplicate"].includes(id),
  );
  assert.equal(survivors.length, 1, `duplicate leaked: ${survivors.join(", ")}`);
});

// ── 11. the editorial-only duplicate through the real route ───────────────────────────

test("11. the production editorial guard fires through the real edition path", async () => {
  const orchestrator = createEditionOrchestrator({
    candidates: sourceOf(editorialCase.candidates),
    customMixEnabled: true,
  });
  const outcome = await orchestrator({
    contract: {
      date: editorialCase.date,
      active: { mode: "custom" as const, regions: ["japan" as const], topics: [] },
      pending: null,
      selectorVersion: 1 as const,
      storyCount: 5 as const,
    },
  });
  assert.equal(outcome.path, "custom_mix_pro");
  const selection = outcome.selection as MixSelectionResult;

  assert.ok(selection.selectedIds.includes("jp-samsung-roundup"));
  assert.ok(!selection.selectedIds.includes("jp-samsung-handson"));

  // The exact rejection reason is observable in the INTERNAL selection logs…
  const log = selection.candidateLogs.find((row) => row.id === "jp-samsung-handson");
  assert.equal(
    log?.rejectionReason,
    "existing duplicate guard: same-product-family: " +
      "same brand (samsung) + same product family (galaxy z fold)",
  );

  // …and must not leak into the public response.
  const { handler, token } = buildRoute({ orchestrator });
  const response = await handler(
    editionRequest(token, { ...editionBody(), date: editorialCase.date }),
  );
  const body = await response.text();
  assert.ok(!body.includes("duplicate guard"));
  assert.ok(!body.includes("jp-samsung-handson"));
  assert.deepEqual(JSON.parse(body), {
    status: "not_connected",
    code: "selector_not_connected",
  });
});

// ── 12. determinism ───────────────────────────────────────────────────────────────────

test("12. an identical request produces an identical result", async () => {
  const orchestrator = createEditionOrchestrator({
    candidates: sourceOf(editorialCase.candidates),
    customMixEnabled: true,
  });
  const contract: EditionRequest = {
    date: editorialCase.date,
    active: { mode: "custom" as const, regions: ["japan"], topics: [] },
    pending: null,
    selectorVersion: 1 as const,
    storyCount: 5 as const,
  };
  const runs = await Promise.all([
    orchestrator({ contract }),
    orchestrator({ contract }),
    orchestrator({ contract }),
  ]);
  const serialized = runs.map((r) => JSON.stringify(r));
  assert.equal(new Set(serialized).size, 1);
});

// ── 13. cache safety ──────────────────────────────────────────────────────────────────

test("13. every edition response is private and uncacheable", async () => {
  const { handler, token } = buildRoute({
    orchestrator: createEditionOrchestrator({
      candidates: sourceOf(editorialCase.candidates),
      customMixEnabled: true,
    }),
  });
  for (const request of [editionRequest(token), editionRequest(null)]) {
    const response = await handler(request);
    assert.equal(response.headers.get("Cache-Control"), "private, no-store");
    assert.equal(response.headers.get("Pragma"), "no-cache");
    // No shared-cache or CORS header that could expose one user's edition to another.
    assert.equal(response.headers.get("Access-Control-Allow-Origin"), null);
    assert.equal(response.headers.get("ETag"), null);
    assert.equal(response.headers.get("Vary"), null);
  }
});

// ── logging boundary ──────────────────────────────────────────────────────────────────

test("path identifiers are narrow, and no preference or subject is ever logged", async () => {
  const { handler, token, logger, deps } = buildRoute({
    orchestrator: createEditionOrchestrator({
      candidates: sourceOf(editorialCase.candidates),
      customMixEnabled: true,
    }),
  });
  await handler(editionRequest(token, { ...editionBody(), date: editorialCase.date }));
  await handler(editionRequest(null));

  const allowed = new Set([
    "custom_mix_pro",
    "standard_custom_mix_disabled",
    "standard_candidates_unavailable",
    "standard_selector_unavailable",
    "selector_not_connected",
    "missing_token",
    "invalid_token",
    "expired_token",
    "revoked",
    "rate_limited",
    "verification_unavailable",
    "invalid_request",
    "custom_mix_disabled",
    "wrong_scope",
    "wrong_environment",
    "invalid_region",
    "invalid_topic",
    "unsupported_selector_version",
  ]);
  const serialized = JSON.stringify(logger.events);
  for (const event of logger.events) {
    assert.ok(allowed.has(event.reasonCode), `unexpected path id: ${event.reasonCode}`);
  }
  for (const forbidden of [
    token,
    "Bearer ",
    "japan",
    "tech",
    deps.tokens.deriveSubject("2000000999999999", "Production"),
    "jp-samsung-roundup",
    "2000000999999999",
  ]) {
    assert.ok(!serialized.includes(forbidden), `leaked into the log: ${forbidden.slice(0, 24)}`);
  }
});

// ── live Python parity for the connected path ─────────────────────────────────────────

test("live Python agrees with the selection the edition path would serve", (t) => {
  if (!existsSync(join(PIPELINE_DIR, "custom_mix_selector.py"))) {
    t.skip("live Python comparison skipped — pipeline/custom_mix_selector.py is not present");
    return;
  }
  const script = `
import json, os, sys
sys.path.insert(0, os.environ["PIPELINE_DIR"])
from custom_mix_selector import select_custom_mix
p = json.loads(os.environ["PAYLOAD"])
sys.stdout.write(json.dumps(select_custom_mix(p["candidates"], p["date"], p["regions"],
                                              p["topics"], p["size"], p["selectorVersion"])))
`;
  const proc = spawnSync("python3", ["-c", script], {
    encoding: "utf8",
    env: {
      ...process.env,
      PIPELINE_DIR,
      PAYLOAD: JSON.stringify({
        candidates: editorialCase.candidates,
        date: editorialCase.date,
        regions: ["japan"],
        topics: [],
        size: 5,
        selectorVersion: 1,
      }),
    },
    maxBuffer: 32 * 1_024 * 1_024,
  });
  if (proc.error || proc.status !== 0) {
    t.skip(`live Python comparison skipped — python3 unavailable (${proc.status})`);
    return;
  }

  const pythonResult = JSON.parse(proc.stdout) as MixSelectionResult;
  const tsResult = selectCustomMix({
    candidates: editorialCase.candidates.map((c) => structuredClone(c)),
    date: editorialCase.date,
    regions: ["japan"],
    topics: [],
    size: 5,
    selectorVersion: 1,
  });

  assert.deepEqual(JSON.parse(JSON.stringify(tsResult)), JSON.parse(JSON.stringify(pythonResult)));
  assert.deepEqual(JSON.parse(JSON.stringify(pythonResult)), editorialGolden.result);
});

// ── dependency direction ──────────────────────────────────────────────────────────────

test("the selector is reachable only through the orchestrator", () => {
  const orchestratorSource = readFileSync(
    join(HERE, "..", "_lib", "edition-orchestrator.ts"),
    "utf8",
  );
  assert.ok(orchestratorSource.includes("custom-mix-selector.js"));

  // edition.ts imports only the orchestrator TYPE; the runtime factory imports the
  // disconnected orchestrator. Neither reaches the selector or the guard directly.
  for (const relative of ["edition.ts", "_lib/vercel-runtime.ts"]) {
    const source = readFileSync(join(HERE, "..", relative), "utf8");
    for (const needle of [
      "custom-mix-selector",
      "editorial-duplicate-guard",
      "editorial-story-identity",
    ]) {
      assert.ok(!source.includes(needle), `${relative} imports ${needle} directly`);
    }
  }
  const factory = readFileSync(join(HERE, "..", "_lib", "runtime-factory.ts"), "utf8");
  assert.ok(factory.includes("edition-orchestrator.js"));
  for (const needle of ["custom-mix-selector", "editorial-duplicate-guard"]) {
    assert.ok(!factory.includes(needle), `runtime-factory.ts imports ${needle} directly`);
  }
});

test("the production wiring keeps Custom Mix unreachable", async () => {
  assert.equal(await candidatesUnavailable.loadCandidates("2026-07-27"), null);
  const outcome = await createDisconnectedEditionOrchestrator(true)({
    contract: {
      date: DATE,
      active: { mode: "custom" as const, regions: ["japan" as const], topics: [] },
      pending: null,
      selectorVersion: 1 as const,
      storyCount: 5 as const,
    },
  });
  assert.equal(outcome.path, "standard_candidates_unavailable");
  assert.equal(outcome.selection, null);
});
