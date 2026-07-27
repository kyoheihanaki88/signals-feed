/**
 * One-way derivation for anything that becomes part of a persistent key. (Phase 3B-3)
 *
 * Raw Apple identifiers (originalTransactionId, transactionId), client IPs and tokens
 * must never be written to Redis in the clear: keys are readable by anyone with database
 * access and frequently show up in provider dashboards, metrics and slow-query logs.
 *
 * A keyed HMAC (not a bare hash) is used so that a leaked key set cannot be brute-forced
 * back to the small, guessable space of Apple transaction identifiers.
 */

import { createHmac } from "node:crypto";

let pepper: string | undefined;

/**
 * Set the process-wide pepper. Call once at startup from the env contract
 * (`SIGNALS_TOKEN_HMAC_SECRET`). Keeping it out of the function signature stops the
 * secret from being threaded through — and accidentally logged by — call sites.
 */
export function configureKeyDerivation(secret: string): void {
  if (!secret || secret.length < 32) {
    throw new Error("key derivation secret must be at least 32 characters");
  }
  pepper = secret;
}

/** Test-only reset so suites stay independent. */
export function resetKeyDerivationForTests(): void {
  pepper = undefined;
}

/**
 * Derive a short, stable, non-reversible component for a Redis key.
 * Truncated to 32 hex chars (128 bits) — ample against collisions, compact in keys.
 */
export function deriveKeyComponent(raw: string): string {
  if (pepper === undefined) {
    throw new Error("key derivation is not configured");
  }
  return createHmac("sha256", pepper).update(raw).digest("hex").slice(0, 32);
}
