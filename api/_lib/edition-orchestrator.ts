/**
 * Edition orchestration boundary. (Phase 3D-2)
 *
 * The single place that decides whether an authenticated `/api/edition` request is served
 * by the Custom Mix selector or falls back. It exists so that four concerns stay separate
 * and separately testable:
 *
 *   request validation   → `custom-mix-contract.ts`   (unchanged)
 *   entitlement          → `auth-middleware.ts`        (unchanged)
 *   selection            → `custom-mix-selector.ts`    (unchanged)
 *   serialization        → `edition.ts`                (unchanged)
 *
 * ENTITLEMENT NOTE. This module performs no entitlement check, and that is deliberate:
 * reaching it already proves Pro. A Signals token is issued only by `/api/auth/exchange`
 * after Apple's signed transaction verifies, the App Store Server API confirms the purchase
 * is still current, the product is `com.signalsapp.pro.lifetime` as a NON_CONSUMABLE owned
 * by PURCHASED, and the subject is absent from the persistent denylist. No client-supplied
 * flag participates. An anonymous or Free caller cannot obtain a token at all, so it never
 * arrives here.
 *
 * THE STANDING BLOCKER. `loadCandidates` has no production implementation. Mix pools are
 * never published — `pipeline/mix_pool_cli.py` actively refuses a repository destination,
 * and `pipeline/` is excluded from the deployment — and no production API module may read
 * the filesystem. `candidatesUnavailable` is therefore the wired default, which keeps the
 * route on its existing `503 selector_not_connected` answer. Supplying a real candidate
 * source is the remaining prerequisite for enabling Custom Mix.
 *
 * DETERMINISM. `loadCandidates` receives the edition DATE only — never the subject — so an
 * identical date, preference set and candidate set produce an identical selection for every
 * caller. No clock, random source, locale-sensitive ordering, network call, subprocess or
 * environment read participates.
 */

import { selectCustomMix } from "./custom-mix-selector.js";
import type {
  MixCandidate,
  MixSelectionResult,
  SelectCustomMixOptions,
} from "./custom-mix-types.js";
import type { EditionRequest } from "./custom-mix-contract.js";
import type { EnrichedCandidate } from "./editorial-mix-pool-schema.js";

/**
 * The narrow set of internal path identifiers. These reach the security log's `reasonCode`
 * only — never the public response body, and never a preference payload or a subject.
 */
export type EditionPathId =
  | "custom_mix_pro"
  | "standard_custom_mix_disabled"
  | "standard_candidates_unavailable"
  | "standard_selector_unavailable";

export type EditionOrchestration =
  | {
      path: "custom_mix_pro";
      selection: MixSelectionResult;
      /** The selected stories, in selector order, ready for the feed adapter. */
      selected: EnrichedCandidate[];
    }
  | {
      path: Exclude<EditionPathId, "custom_mix_pro">;
      selection: null;
      selected: null;
    };

/** One pool load: the selector rows AND the enriched rows they were extracted from. */
export type MixCandidateBundle = {
  candidates: MixCandidate[];
  enriched: EnrichedCandidate[];
};

/** The seam the Editorial Mix Pool source plugs into. */
export interface MixCandidateSource {
  /** Resolves to null when no usable pool exists for the date — NOT an error. */
  loadCandidates(date: string): Promise<MixCandidateBundle | null>;
}

/**
 * The production candidate source today: there isn't one.
 *
 * Named so that a reader can see at the call site that Custom Mix is not connected, rather
 * than discovering it from behaviour.
 */
export const candidatesUnavailable: MixCandidateSource = {
  async loadCandidates(): Promise<MixCandidateBundle | null> {
    return null;
  },
};

export type EditionOrchestratorInput = {
  contract: EditionRequest;
};

export type EditionOrchestrator = (
  input: EditionOrchestratorInput,
) => Promise<EditionOrchestration>;

export type CreateEditionOrchestratorOptions = {
  candidates: MixCandidateSource;
  /** The Custom Mix kill switch, resolved from the validated runtime config. */
  customMixEnabled: boolean;
  /**
   * Injectable purely so a test can observe THAT the selector ran. The default is the real
   * production selector, which carries the ported editorial duplicate guard.
   */
  runSelector?: (options: SelectCustomMixOptions) => MixSelectionResult;
};

export function createEditionOrchestrator(
  options: CreateEditionOrchestratorOptions,
): EditionOrchestrator {
  const runSelector = options.runSelector ?? selectCustomMix;

  return async ({ contract }) => {
    if (!options.customMixEnabled) {
      // The global kill switch short-circuits BEFORE any pool load, so a disabled Custom
      // Mix performs no Upstash read at all.
      return { path: "standard_custom_mix_disabled", selection: null, selected: null };
    }

    let bundle: MixCandidateBundle | null;
    try {
      bundle = await options.candidates.loadCandidates(contract.date);
    } catch {
      // A candidate source that throws is indistinguishable from one that has nothing.
      return { path: "standard_candidates_unavailable", selection: null, selected: null };
    }
    if (!bundle || bundle.candidates.length === 0) {
      return { path: "standard_candidates_unavailable", selection: null, selected: null };
    }
    const { candidates, enriched } = bundle;

    try {
      const selection = runSelector({
        candidates,
        date: contract.date,
        regions: contract.active.regions,
        topics: contract.active.topics,
        size: contract.storyCount,
        selectorVersion: contract.selectorVersion,
        // No guard is passed: the selector's own default is the ported production
        // editorial duplicate guard, and this boundary must not weaken it.
      });

      // Map the chosen ids back to their enriched rows, PRESERVING SELECTOR ORDER — the
      // feed's `number`, `importance` and `lead` all derive from position. A selection that
      // cannot be fully resolved, or that is short of the required count, falls back rather
      // than returning a partial edition.
      const byId = new Map(
        enriched.map((row) => [String((row.selector as { id?: unknown }).id ?? ""), row]),
      );
      const selected: EnrichedCandidate[] = [];
      for (const id of selection.selectedIds) {
        const row = byId.get(id);
        if (!row) return { path: "standard_selector_unavailable", selection: null, selected: null };
        selected.push(row);
      }
      if (selected.length !== contract.storyCount) {
        return { path: "standard_selector_unavailable", selection: null, selected: null };
      }

      return { path: "custom_mix_pro", selection, selected };
    } catch {
      // Fail closed: an unexpected selector failure falls back rather than 500-ing.
      return { path: "standard_selector_unavailable", selection: null, selected: null };
    }
  };
}

/**
 * The orchestrator the production runtime wires today. Custom Mix stays unreachable because
 * no candidate source exists; every request resolves to
 * `standard_candidates_unavailable` and the route answers exactly as it does now.
 */
export function createDisconnectedEditionOrchestrator(
  customMixEnabled: boolean,
): EditionOrchestrator {
  return createEditionOrchestrator({
    candidates: candidatesUnavailable,
    customMixEnabled,
  });
}
