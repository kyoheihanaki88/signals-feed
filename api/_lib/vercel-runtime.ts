/**
 * Lazy runtime lifecycle for the Vercel route modules. (Phase 3C-2)
 *
 * A serverless function is imported once per cold start and then handles many requests, so
 * the interesting question is not "how do we build the runtime" — Phase 3C-1 answered that —
 * but WHEN. This module's answers:
 *
 *   • Never at import. Importing a route module reads no file, opens no socket and touches
 *     no environment variable, so a misconfigured deployment produces a clean 503 per
 *     request instead of a module-load crash with a stack trace in the build log.
 *   • Once per cold start, on the first request that needs it.
 *   • NEVER after a failure. A failed initialisation caches nothing, so the next request
 *     retries from scratch rather than inheriting a half-built stack. This is the difference
 *     between "Redis was briefly unreachable" and "this instance is poisoned until it dies".
 *   • Configuration changes require a cold start. That is Vercel's own model for environment
 *     variables, and pretending otherwise would invite a stale-config bug.
 *
 * It also enforces the deployment boundary that configuration alone cannot: `VERCEL_ENV`
 * must agree with the Signals mode. A Preview deployment holding Production credentials is
 * refused outright rather than allowed to verify real purchases from a preview URL.
 */

import { AsyncLocalStorage } from "node:async_hooks";
import { randomUUID } from "node:crypto";

import type { RawEnv } from "./env.js";
import {
  loadRuntimeConfig,
  type RuntimeConfig,
} from "./runtime-config.js";
import {
  createDevelopmentDependencies,
  createRuntimeDependencies,
  type DevelopmentOverrides,
  type RuntimeDependencies,
  type RuntimeTransports,
} from "./runtime-dependencies.js";
import {
  createProductionEditionHandler,
  createProductionExchangeHandler,
  type RouteHandler,
} from "./runtime-factory.js";
import { adaptVercelRequest } from "./vercel-request.js";
import { errorResponse, failClosed, harden, methodNotAllowed } from "./vercel-response.js";

/** Per-route body caps, matching the Phase 3B-1 route contracts exactly. */
export const MAX_EXCHANGE_BODY_BYTES = 16 * 1_024;
export const MAX_EDITION_BODY_BYTES = 8 * 1_024;

export class VercelRuntimeError extends Error {
  readonly reason: string;

  constructor(reason: string) {
    // A short mechanical code. Never a variable value, a credential or a stack.
    super(`vercel runtime unavailable: ${reason}`);
    this.name = "VercelRuntimeError";
    this.reason = reason;
  }
}

export type RequestContext = { requestId: string };

export type VercelRuntime = {
  config: RuntimeConfig;
  dependencies: RuntimeDependencies;
  exchange: RouteHandler;
  edition: RouteHandler;
  /** Carries the request id into the route's logger without any shared mutable global. */
  requestContext: AsyncLocalStorage<RequestContext>;
};

/**
 * The cold-start cache. Module-level by necessity — that IS the serverless instance's
 * lifetime — and reachable from tests only through the explicit reset/prime helpers below.
 */
let cachedRuntime: VercelRuntime | null = null;

/** Test-only: drop the cached runtime so the next call rebuilds from scratch. */
export function resetVercelRuntimeForTests(): void {
  cachedRuntime = null;
}

/** Test-only: observe whether a runtime is currently cached. */
export function peekVercelRuntimeForTests(): VercelRuntime | null {
  return cachedRuntime;
}

function read(env: RawEnv, name: string): string | undefined {
  const raw = env[name];
  if (raw === undefined) return undefined;
  const trimmed = raw.trim();
  return trimmed.length === 0 ? undefined : trimmed;
}

/**
 * The deployment boundary.
 *
 * `loadRuntimeConfig` already guarantees a Sandbox deployment cannot hold a Production app
 * id and vice versa. This adds the second half: the deployment TARGET must agree with the
 * configuration, so a Preview URL can never serve Production entitlements even if someone
 * pastes Production secrets into Preview's environment.
 */
export function assertDeploymentSafety(config: RuntimeConfig, env: RawEnv): void {
  if (config.mode === "development") {
    // The production route modules must never auto-enable a development stack.
    throw new VercelRuntimeError("development_runtime_not_permitted");
  }

  const target = read(env, "VERCEL_ENV")?.toLowerCase();
  if (target === undefined) return; // local `node --test` or a non-Vercel host

  if (target === "development") {
    throw new VercelRuntimeError("use_development_entry_point");
  }
  if (target === "preview" && config.mode !== "sandbox") {
    throw new VercelRuntimeError("preview_requires_sandbox_configuration");
  }
  if (target === "production" && config.mode !== "production") {
    // Never silently downgrade: a Production deployment serves Production or nothing.
    throw new VercelRuntimeError("production_requires_production_configuration");
  }
  if (target !== "preview" && target !== "production") {
    throw new VercelRuntimeError("unknown_deployment_target");
  }
}

function buildRuntime(env: RawEnv, transports: RuntimeTransports = {}): VercelRuntime {
  const config = loadRuntimeConfig(env);
  assertDeploymentSafety(config, env);

  const requestContext = new AsyncLocalStorage<RequestContext>();
  const dependencies = createRuntimeDependencies(config, {
    // The route logs the id Vercel assigned, without a shared mutable global.
    requestId: () => requestContext.getStore()?.requestId ?? randomUUID(),
    ...transports,
  });

  return {
    config,
    dependencies,
    exchange: createProductionExchangeHandler(config, dependencies),
    edition: createProductionEditionHandler(config, dependencies),
    requestContext,
  };
}

/**
 * The runtime for this serverless instance, built on first use.
 * Throws `VercelRuntimeError` or `RuntimeConfigError`; a throw caches NOTHING.
 */
export function getVercelRuntime(env: RawEnv = process.env): VercelRuntime {
  if (cachedRuntime !== null) return cachedRuntime;
  const built = buildRuntime(env); // may throw — assignment below is never reached
  cachedRuntime = built;
  return built;
}

/** Test-only: install a runtime built with injected transports. */
export function primeVercelRuntimeForTests(options: {
  env: RawEnv;
  transports?: RuntimeTransports;
}): VercelRuntime {
  const built = buildRuntime(options.env, options.transports ?? {});
  cachedRuntime = built;
  return built;
}

/**
 * The SEPARATE development entry point.
 *
 * Requires `development` mode and an explicit fake verifier, and never writes the module
 * cache the production routes read — so no route can reach a fake stack by accident.
 */
export function createDevelopmentVercelRuntime(options: {
  env: RawEnv;
  overrides: DevelopmentOverrides;
}): VercelRuntime {
  const config = loadRuntimeConfig(options.env);
  if (config.mode !== "development") {
    throw new VercelRuntimeError("development_entry_point_requires_development_mode");
  }
  const requestContext = new AsyncLocalStorage<RequestContext>();
  const dependencies = createDevelopmentDependencies(config, {
    requestId: () => requestContext.getStore()?.requestId ?? randomUUID(),
    ...options.overrides,
  });
  return {
    config,
    dependencies,
    exchange: createProductionExchangeHandler(config, dependencies),
    edition: createProductionEditionHandler(config, dependencies),
    requestContext,
  };
}

type RouteKind = "exchange" | "edition";

async function handle(
  kind: RouteKind,
  request: Request,
  env: RawEnv = process.env,
): Promise<Response> {
  // Cheapest and configuration-free: a wrong method never touches the runtime.
  if (request.method !== "POST") return methodNotAllowed();

  let runtime: VercelRuntime;
  try {
    runtime = getVercelRuntime(env);
  } catch {
    // Fail closed. The reason stays server-side; the client sees only "try later".
    return failClosed();
  }

  // Mirror the route's own ordering: the kill switch answers before any body is read.
  if (!runtime.dependencies.killSwitch.customMixEnabled) {
    return errorResponse(503, "custom_mix_disabled");
  }

  const adapted = await adaptVercelRequest(request, {
    maxBodyBytes:
      kind === "exchange" ? MAX_EXCHANGE_BODY_BYTES : MAX_EDITION_BODY_BYTES,
  });
  if (!adapted.ok) return harden(adapted.response);

  const route = kind === "exchange" ? runtime.exchange : runtime.edition;
  try {
    const response = await runtime.requestContext.run({ requestId: adapted.requestId }, () =>
      route(adapted.request),
    );
    return harden(response);
  } catch {
    // An unexpected throw must not surface a stack, a message or a partial body.
    return failClosed();
  }
}

export function handleExchangeRequest(
  request: Request,
  env?: RawEnv,
): Promise<Response> {
  return handle("exchange", request, env);
}

export function handleEditionRequest(
  request: Request,
  env?: RawEnv,
): Promise<Response> {
  return handle("edition", request, env);
}
