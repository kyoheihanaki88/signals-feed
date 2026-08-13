import assert from "node:assert/strict";
import test from "node:test";
import { createEditionHandler } from "../edition.js";
import { InMemorySlidingWindowRateLimiter } from "../_lib/rate-limit.js";
import {
  createDependencies,
  editionBody,
  editionRequest,
  responseCode,
} from "./test-helpers.js";

function setup(
  environment: "Production" | "Sandbox" = "Production",
  enabled = true,
) {
  const dependencies = createDependencies(environment);
  const subject = dependencies.tokens.deriveSubject(
    "transaction-for-edition",
    environment,
  );
  const token = dependencies.tokens.issue({ subject, environment }).accessToken;
  const handle = createEditionHandler(
    {
      enabled,
      environment,
      isDateAllowed: (date) => date === "2026-07-27",
    },
    {
      tokens: dependencies.tokens,
      revocations: dependencies.revocations,
      limiter: dependencies.editionLimiter,
      logger: dependencies.logger,
      clock: dependencies.clock,
      requestId: dependencies.requestId,
    },
  );
  return { dependencies, handle, subject, token };
}

test("Q: a valid token reaches the connected edition path", async () => {
  const { handle, token } = setup();
  const response = await handle(editionRequest(token));
  assert.equal(response.status, 503);
  assert.equal(await responseCode(response), "custom_mix_unavailable");
  assert.equal(response.headers.get("cache-control"), "private, no-store");
});

test("R: missing token rejected", async () => {
  const { handle } = setup();
  const response = await handle(editionRequest(null));
  assert.equal(response.status, 401);
  assert.equal(await responseCode(response), "missing_token");
});

test("S/T: unsupported selector, invalid region, and invalid topic rejected", async (t) => {
  await t.test("selector version", async () => {
    const { handle, token } = setup();
    const response = await handle(
      editionRequest(token, editionBody({ selectorVersion: 1 })),
    );
    assert.equal(response.status, 422);
    assert.equal(
      await responseCode(response),
      "unsupported_selector_version",
    );
  });
  await t.test("invalid region", async () => {
    const { handle, token } = setup();
    const response = await handle(
      editionRequest(token, {
        ...editionBody(),
        active: {
          mode: "custom",
          regions: ["Japan"],
          topics: ["tech"],
        },
      }),
    );
    assert.equal(await responseCode(response), "invalid_region");
  });
  await t.test("invalid topic", async () => {
    const { handle, token } = setup();
    const response = await handle(
      editionRequest(token, {
        ...editionBody(),
        active: {
          mode: "custom",
          regions: ["japan"],
          topics: ["sports"],
        },
      }),
    );
    assert.equal(await responseCode(response), "invalid_topic");
  });
});

test("duplicates and empty regions are rejected; normalized internals sort", async (t) => {
  await t.test("duplicate region", async () => {
    const { handle, token } = setup();
    const response = await handle(
      editionRequest(token, {
        ...editionBody(),
        active: {
          mode: "custom",
          regions: ["japan", "japan"],
          topics: [],
        },
      }),
    );
    assert.equal(await responseCode(response), "invalid_region");
  });
  await t.test("empty region", async () => {
    const { handle, token } = setup();
    const response = await handle(
      editionRequest(token, {
        ...editionBody(),
        active: { mode: "custom", regions: [], topics: [] },
      }),
    );
    assert.equal(await responseCode(response), "invalid_region");
  });
  await t.test("duplicate topic", async () => {
    const { handle, token } = setup();
    const response = await handle(
      editionRequest(token, {
        ...editionBody(),
        active: {
          mode: "custom",
          regions: ["japan"],
          topics: ["tech", "tech"],
        },
      }),
    );
    assert.equal(await responseCode(response), "invalid_topic");
  });
  await t.test("invalid pending structure", async () => {
    const { handle, token } = setup();
    const response = await handle(
      editionRequest(token, {
        ...editionBody(),
        pending: { arbitrary: "data" },
      }),
    );
    assert.equal(await responseCode(response), "invalid_request");
  });
  await t.test("impossible calendar date", async () => {
    const { handle, token } = setup();
    const response = await handle(
      editionRequest(token, {
        ...editionBody(),
        date: "2026-02-31",
      }),
    );
    assert.equal(await responseCode(response), "invalid_request");
  });
});

test("U: revocation after issuance blocks edition", async () => {
  const { handle, dependencies, subject, token } = setup();
  dependencies.revocations.revoke(subject);
  const response = await handle(editionRequest(token));
  assert.equal(response.status, 401);
  assert.equal(await responseCode(response), "revoked");
});

test("N: edition limiter is isolated and returns retry-after", async () => {
  const setupValue = setup();
  setupValue.dependencies.editionLimiter =
    new InMemorySlidingWindowRateLimiter(1, 60_000);
  const handle = createEditionHandler(
    { enabled: true, environment: "Production" },
    {
      tokens: setupValue.dependencies.tokens,
      revocations: setupValue.dependencies.revocations,
      limiter: setupValue.dependencies.editionLimiter,
      logger: setupValue.dependencies.logger,
      clock: setupValue.dependencies.clock,
      requestId: setupValue.dependencies.requestId,
    },
  );
  assert.equal((await handle(editionRequest(setupValue.token))).status, 503);
  const response = await handle(editionRequest(setupValue.token));
  assert.equal(response.status, 429);
  assert.equal(await responseCode(response), "rate_limited");
  assert.equal(response.headers.get("retry-after"), "60");
});

test("wrong environment token is rejected", async () => {
  const { handle, dependencies } = setup();
  const token = dependencies.tokens.issue({
    subject: "subject",
    environment: "Sandbox",
  }).accessToken;
  const response = await handle(editionRequest(token));
  assert.equal(response.status, 403);
  assert.equal(await responseCode(response), "wrong_environment");
});

test("kill switch disabled returns 503", async () => {
  const { handle, token } = setup("Production", false);
  const response = await handle(editionRequest(token));
  assert.equal(response.status, 503);
  assert.equal(await responseCode(response), "custom_mix_disabled");
});
