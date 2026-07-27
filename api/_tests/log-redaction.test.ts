import assert from "node:assert/strict";
import test from "node:test";
import { createAuthExchangeHandler } from "../auth/exchange.js";
import { createEditionHandler } from "../edition.js";
import {
  createDependencies,
  editionBody,
  editionRequest,
  exchangeRequest,
  TEST_JWS,
} from "./test-helpers.js";

test("O: exchange logs omit JWS, transaction ID, subject, body, and secrets", async () => {
  const dependencies = createDependencies();
  const handle = createAuthExchangeHandler(
    { enabled: true, environment: "Production" },
    dependencies,
  );
  const response = await handle(
    exchangeRequest({
      signedTransactionInfo: TEST_JWS,
      appVersion: "sensitive-version",
      selectorVersion: 1,
    }),
  );
  assert.equal(response.status, 200);
  const body = (await response.json()) as { accessToken: string };
  const subject = dependencies.tokens.deriveSubject(
    dependencies.verifier.entitlement.originalTransactionId,
    "Production",
  );
  const logs = JSON.stringify(dependencies.logger.events);
  for (const forbidden of [
    TEST_JWS,
    body.accessToken,
    subject,
    dependencies.verifier.entitlement.originalTransactionId,
    "sensitive-version",
    "signedTransactionInfo",
  ]) {
    assert.equal(logs.includes(forbidden), false);
  }
});

test("edition logs omit Authorization, active/pending mixes, and request body", async () => {
  const dependencies = createDependencies();
  const subject = dependencies.tokens.deriveSubject("transaction", "Production");
  const token = dependencies.tokens.issue({
    subject,
    environment: "Production",
  }).accessToken;
  const handle = createEditionHandler(
    { enabled: true, environment: "Production" },
    {
      tokens: dependencies.tokens,
      revocations: dependencies.revocations,
      limiter: dependencies.editionLimiter,
      logger: dependencies.logger,
      clock: dependencies.clock,
      requestId: dependencies.requestId,
    },
  );
  const requestBody = {
    ...editionBody(),
    pending: {
      mode: "custom",
      regions: ["world"],
      topics: ["culture"],
    },
  };
  await handle(editionRequest(token, requestBody));
  const logs = JSON.stringify(dependencies.logger.events);
  for (const forbidden of [
    token,
    subject,
    "Authorization",
    "japan",
    "tech",
    "world",
    "culture",
    "pending",
  ]) {
    assert.equal(logs.includes(forbidden), false);
  }
});
