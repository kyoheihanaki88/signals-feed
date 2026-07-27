import assert from "node:assert/strict";
import test from "node:test";
import { createAuthExchangeHandler } from "../auth/exchange.js";
import { InMemorySlidingWindowRateLimiter } from "../_lib/rate-limit.js";
import {
  AppleVerificationError,
  createDependencies,
  exchangeRequest,
  responseCode,
  TEST_JWS,
} from "./test-helpers.js";

function handler(
  environment: "Production" | "Sandbox" = "Production",
  enabled = true,
) {
  const dependencies = createDependencies(environment);
  return {
    dependencies,
    handle: createAuthExchangeHandler(
      { enabled, environment },
      dependencies,
    ),
  };
}

test("A: valid fake Production entitlement issues a no-store token", async () => {
  const { handle, dependencies } = handler();
  const response = await handle(exchangeRequest());
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("cache-control"), "private, no-store");
  const body = (await response.json()) as Record<string, unknown>;
  assert.equal(typeof body.accessToken, "string");
  assert.deepEqual(body.scope, ["custom_mix"]);
  assert.equal(dependencies.verifier.calls.length, 1);
});

test("B: valid fake Sandbox entitlement works against Sandbox", async () => {
  const { handle } = handler("Sandbox");
  const response = await handle(exchangeRequest());
  assert.equal(response.status, 200);
});

test("C: Sandbox proof is rejected by Production route", async () => {
  const { handle, dependencies } = handler();
  dependencies.verifier.entitlement.environment = "Sandbox";
  const response = await handle(exchangeRequest());
  assert.equal(response.status, 401);
  assert.equal(await responseCode(response), "unsupported_environment");
});

test("D/E: wrong bundle and wrong product are rejected", async (t) => {
  await t.test("wrong bundle", async () => {
    const { handle, dependencies } = handler();
    dependencies.verifier.entitlement.bundleId = "com.example.fake";
    const response = await handle(exchangeRequest());
    assert.equal(await responseCode(response), "wrong_bundle");
  });
  await t.test("wrong product", async () => {
    const { handle, dependencies } = handler();
    dependencies.verifier.entitlement.productId = "fake.product";
    const response = await handle(exchangeRequest());
    assert.equal(await responseCode(response), "wrong_product");
  });
});

test("wrong product type and ownership are rejected", async (t) => {
  await t.test("wrong type", async () => {
    const { handle, dependencies } = handler();
    Object.assign(dependencies.verifier.entitlement, {
      productType: "CONSUMABLE",
    });
    assert.equal(
      await responseCode(await handle(exchangeRequest())),
      "wrong_product_type",
    );
  });
  await t.test("wrong ownership", async () => {
    const { handle, dependencies } = handler();
    Object.assign(dependencies.verifier.entitlement, {
      ownershipType: "FAMILY_SHARED",
    });
    assert.equal(
      await responseCode(await handle(exchangeRequest())),
      "wrong_ownership",
    );
  });
});

test("F: revoked entitlement or denylisted subject is rejected", async (t) => {
  await t.test("Apple entitlement revoked", async () => {
    const { handle, dependencies } = handler();
    dependencies.verifier.entitlement.revoked = true;
    assert.equal(
      await responseCode(await handle(exchangeRequest())),
      "revoked",
    );
  });
  await t.test("server denylist revoked", async () => {
    const { handle, dependencies } = handler();
    const entitlement = dependencies.verifier.entitlement;
    const subject = dependencies.tokens.deriveSubject(
      entitlement.originalTransactionId,
      entitlement.environment,
    );
    dependencies.revocations.revoke(subject);
    assert.equal(
      await responseCode(await handle(exchangeRequest())),
      "revoked",
    );
  });
});

test("G/H/P: malformed, oversized, or boolean-only requests never invoke verifier", async (t) => {
  await t.test("malformed JWS", async () => {
    const { handle, dependencies } = handler();
    const response = await handle(
      exchangeRequest({ signedTransactionInfo: "not-jws", selectorVersion: 1 }),
    );
    assert.equal(response.status, 400);
    assert.equal(dependencies.verifier.calls.length, 0);
  });
  await t.test("oversized JWS", async () => {
    const { handle, dependencies } = handler();
    const oversized = `${"a".repeat(12_500)}.b.c`;
    const response = await handle(
      exchangeRequest({
        signedTransactionInfo: oversized,
        selectorVersion: 1,
      }),
    );
    assert.equal(response.status, 400);
    assert.equal(dependencies.verifier.calls.length, 0);
  });
  await t.test("DEBUG-like isPro boolean", async () => {
    const { handle, dependencies } = handler();
    const response = await handle(exchangeRequest({ isPro: true }));
    assert.equal(response.status, 400);
    assert.equal(dependencies.verifier.calls.length, 0);
  });
});

test("M: exchange IP rate limiting returns retry-after", async () => {
  const { handle, dependencies } = handler();
  dependencies.ipLimiter = new InMemorySlidingWindowRateLimiter(1, 60_000);
  const limitedHandle = createAuthExchangeHandler(
    { enabled: true, environment: "Production" },
    dependencies,
  );
  assert.equal((await limitedHandle(exchangeRequest())).status, 200);
  const response = await limitedHandle(exchangeRequest());
  assert.equal(response.status, 429);
  assert.equal(await responseCode(response), "rate_limited");
  assert.equal(response.headers.get("retry-after"), "60");
});

test("V: verification unavailable returns 503 without internals", async () => {
  const { handle, dependencies } = handler();
  dependencies.verifier.error = new AppleVerificationError(
    "verification_unavailable",
  );
  const response = await handle(exchangeRequest());
  assert.equal(response.status, 503);
  assert.equal(await responseCode(response), "verification_unavailable");
});

test("invalid verifier error maps to stable invalid_proof", async () => {
  const { handle, dependencies } = handler();
  dependencies.verifier.error = new Error(`secret:${TEST_JWS}`);
  const response = await handle(exchangeRequest());
  assert.equal(response.status, 401);
  assert.equal(await responseCode(response), "invalid_proof");
  assert.equal(JSON.stringify(dependencies.logger.events).includes(TEST_JWS), false);
});

test("missing or malformed original transaction ID is invalid proof", async () => {
  const { handle, dependencies } = handler();
  dependencies.verifier.entitlement.originalTransactionId = "";
  const response = await handle(exchangeRequest());
  assert.equal(response.status, 401);
  assert.equal(await responseCode(response), "invalid_proof");
});

test("rate limiter outage fails closed before Apple verification", async () => {
  const { dependencies } = handler();
  dependencies.ipLimiter = {
    async consume() {
      throw new Error("store unavailable");
    },
  };
  const handle = createAuthExchangeHandler(
    { enabled: true, environment: "Production" },
    dependencies,
  );
  const response = await handle(exchangeRequest());
  assert.equal(response.status, 503);
  assert.equal(await responseCode(response), "verification_unavailable");
  assert.equal(dependencies.verifier.calls.length, 0);
});

test("Z: kill switch prevents verifier use", async () => {
  const { handle, dependencies } = handler("Production", false);
  const response = await handle(exchangeRequest());
  assert.equal(response.status, 503);
  assert.equal(await responseCode(response), "custom_mix_disabled");
  assert.equal(dependencies.verifier.calls.length, 0);
});
