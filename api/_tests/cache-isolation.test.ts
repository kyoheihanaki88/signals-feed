import assert from "node:assert/strict";
import test from "node:test";
import {
  normalizedMixCacheIdentity,
  validateEditionRequest,
} from "../_lib/custom-mix-contract.js";
import { editionBody } from "./test-helpers.js";

test("identical normalized mixes share identity without token subject", () => {
  const first = validateEditionRequest({
    ...editionBody(),
    active: {
      mode: "custom",
      regions: ["world", "japan"],
      topics: ["tech", "ai"],
    },
  });
  const second = validateEditionRequest({
    ...editionBody(),
    active: {
      mode: "custom",
      regions: ["japan", "world"],
      topics: ["ai", "tech"],
    },
  });
  const firstIdentity = normalizedMixCacheIdentity(first);
  const secondIdentity = normalizedMixCacheIdentity(second);
  assert.equal(firstIdentity, secondIdentity);
  assert.equal(firstIdentity.includes("subject"), false);
  assert.equal(firstIdentity.includes("token"), false);
  assert.equal(firstIdentity.includes("pending"), false);
});

test("pending is structurally accepted but excluded from cache/auth identity", () => {
  const first = validateEditionRequest({
    ...editionBody(),
    pending: { mode: "custom", regions: ["world"], topics: ["culture"] },
  });
  const second = validateEditionRequest({
    ...editionBody(),
    pending: null,
  });
  assert.equal(
    normalizedMixCacheIdentity(first),
    normalizedMixCacheIdentity(second),
  );
});
