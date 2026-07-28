import assert from "node:assert/strict";
import test from "node:test";
import {
  APIException,
  Environment,
  InAppOwnershipType,
  Type,
  type HistoryResponse,
  type JWSTransactionDecodedPayload,
} from "@apple/app-store-server-library";
import {
  AppleEntitlementClient,
  AppleServiceBreaker,
  type TransactionHistoryFetcher,
} from "../_lib/apple-client.js";
import {
  AppleEntitlementError,
  SIGNALS_BUNDLE_ID,
  SIGNALS_PRO_PRODUCT_ID,
  type SignedTransactionDecoder,
} from "../_lib/apple-verifier-real.js";

const ORIGINAL_TRANSACTION_ID = "2000000123456789";
const PRIVATE_KEY_SHAPED = "-----BEGIN PRIVATE KEY-----FAKE-NEVER-REAL-----END PRIVATE KEY-----";

function payload(
  overrides: Partial<JWSTransactionDecodedPayload> = {},
): JWSTransactionDecodedPayload {
  return {
    originalTransactionId: ORIGINAL_TRANSACTION_ID,
    bundleId: SIGNALS_BUNDLE_ID,
    productId: SIGNALS_PRO_PRODUCT_ID,
    type: Type.NON_CONSUMABLE,
    inAppOwnershipType: InAppOwnershipType.PURCHASED,
    environment: Environment.PRODUCTION,
    ...overrides,
  } as JWSTransactionDecodedPayload;
}

/** Decoder that maps a signed string to a payload, so pages can differ. */
function decoderFor(map: Record<string, JWSTransactionDecodedPayload>): SignedTransactionDecoder {
  return {
    verifyAndDecodeTransaction: async (signed: string) => {
      const found = map[signed];
      if (!found) throw new Error("unknown signed transaction");
      return found;
    },
  };
}

function fetcherReturning(pages: HistoryResponse[]): TransactionHistoryFetcher & { calls: number } {
  let index = 0;
  const fetcher = {
    calls: 0,
    async getTransactionHistory(): Promise<HistoryResponse> {
      fetcher.calls += 1;
      const page = pages[Math.min(index, pages.length - 1)];
      index += 1;
      return page;
    },
  };
  return fetcher;
}

function client(
  fetcher: TransactionHistoryFetcher,
  decoder: SignedTransactionDecoder,
  overrides: Partial<ConstructorParameters<typeof AppleEntitlementClient>[0]> = {},
  breaker?: AppleServiceBreaker,
) {
  let clock = 0;
  return new AppleEntitlementClient(
    {
      fetcher,
      decoder,
      environment: "Production",
      sleep: async () => {
        clock += 1; // instant: no wall-clock waiting anywhere in these tests
      },
      now: () => clock,
      retryBaseMs: 1,
      ...overrides,
    },
    breaker,
  );
}

// ── Q. valid current entitlement ─────────────────────────────────────────────────────

test("Q. a valid current entitlement is accepted", async () => {
  const c = client(
    fetcherReturning([{ signedTransactions: ["s1"], hasMore: false }]),
    decoderFor({ s1: payload() }),
  );
  const entitlement = await c.getCurrentProEntitlement(ORIGINAL_TRANSACTION_ID);
  assert.equal(entitlement.originalTransactionId, ORIGINAL_TRANSACTION_ID);
  assert.equal(entitlement.productId, SIGNALS_PRO_PRODUCT_ID);
  assert.equal(entitlement.revoked, false);
});

// ── R. refunded / revoked ────────────────────────────────────────────────────────────

test("R. a refunded current entitlement is rejected", async () => {
  const c = client(
    fetcherReturning([{ signedTransactions: ["s1"], hasMore: false }]),
    decoderFor({ s1: payload({ revocationDate: 1_760_000_000_000 }) }),
  );
  await assert.rejects(
    () => c.getCurrentProEntitlement(ORIGINAL_TRANSACTION_ID),
    (error: unknown) => error instanceof AppleEntitlementError && error.reason === "revoked",
  );
});

test("R2. FAMILY_SHARED in history is rejected", async () => {
  const c = client(
    fetcherReturning([{ signedTransactions: ["s1"], hasMore: false }]),
    decoderFor({ s1: payload({ inAppOwnershipType: InAppOwnershipType.FAMILY_SHARED }) }),
  );
  await assert.rejects(
    () => c.getCurrentProEntitlement(ORIGINAL_TRANSACTION_ID),
    (error: unknown) => error instanceof AppleEntitlementError && error.reason === "wrong_ownership",
  );
});

// ── S. no matching product / empty history ───────────────────────────────────────────

test("S. history without the Pro product is rejected", async () => {
  const c = client(
    fetcherReturning([{ signedTransactions: ["s1"], hasMore: false }]),
    decoderFor({ s1: payload({ productId: "com.signalsapp.other" }) }),
  );
  await assert.rejects(
    () => c.getCurrentProEntitlement(ORIGINAL_TRANSACTION_ID),
    (error: unknown) => error instanceof AppleEntitlementError && error.reason === "wrong_product",
  );
});

test("S2. an empty history fails closed", async () => {
  const c = client(fetcherReturning([{ signedTransactions: [], hasMore: false }]), decoderFor({}));
  await assert.rejects(
    () => c.getCurrentProEntitlement(ORIGINAL_TRANSACTION_ID),
    (error: unknown) => error instanceof AppleEntitlementError && error.reason === "wrong_product",
  );
});

test("S3. a malformed response fails closed", async () => {
  const c = client(
    { getTransactionHistory: async () => null as unknown as HistoryResponse },
    decoderFor({}),
  );
  await assert.rejects(
    () => c.getCurrentProEntitlement(ORIGINAL_TRANSACTION_ID),
    (error: unknown) =>
      error instanceof AppleEntitlementError && error.reason === "verification_unavailable",
  );
});

// ── T. pagination ────────────────────────────────────────────────────────────────────

test("T. pagination follows the revision cursor to a later page", async () => {
  const fetcher = fetcherReturning([
    { signedTransactions: ["old"], hasMore: true, revision: "rev-1" },
    { signedTransactions: ["current"], hasMore: false },
  ]);
  const c = client(
    fetcher,
    decoderFor({
      old: payload({ productId: "com.signalsapp.other" }),
      current: payload(),
    }),
  );
  const entitlement = await c.getCurrentProEntitlement(ORIGINAL_TRANSACTION_ID);
  assert.equal(entitlement.productId, SIGNALS_PRO_PRODUCT_ID);
  assert.equal(fetcher.calls, 2, "the second page was fetched");
});

test("T2. pagination stops when hasMore is false", async () => {
  const fetcher = fetcherReturning([{ signedTransactions: [], hasMore: false, revision: "rev-1" }]);
  const c = client(fetcher, decoderFor({}));
  await assert.rejects(() => c.getCurrentProEntitlement(ORIGINAL_TRANSACTION_ID));
  assert.equal(fetcher.calls, 1);
});

test("T3. pagination is bounded by maxPages", async () => {
  const fetcher = fetcherReturning([
    { signedTransactions: [], hasMore: true, revision: "loop" },
  ]);
  const c = client(fetcher, decoderFor({}), { maxPages: 3 });
  await assert.rejects(() => c.getCurrentProEntitlement(ORIGINAL_TRANSACTION_ID));
  assert.equal(fetcher.calls, 3, "never loops forever");
});

// ── U. retry behavior ────────────────────────────────────────────────────────────────

test("U. a retryable 429 is retried and can then succeed", async () => {
  let calls = 0;
  const fetcher: TransactionHistoryFetcher = {
    async getTransactionHistory() {
      calls += 1;
      if (calls === 1) throw new APIException(429);
      return { signedTransactions: ["s1"], hasMore: false };
    },
  };
  const c = client(fetcher, decoderFor({ s1: payload() }));
  const entitlement = await c.getCurrentProEntitlement(ORIGINAL_TRANSACTION_ID);
  assert.equal(entitlement.revoked, false);
  assert.equal(calls, 2);
});

test("U2. retries stop at the maximum and map to verification_unavailable", async () => {
  let calls = 0;
  const fetcher: TransactionHistoryFetcher = {
    async getTransactionHistory() {
      calls += 1;
      throw new APIException(503);
    },
  };
  const c = client(fetcher, decoderFor({}), { maxRetries: 2 });
  await assert.rejects(
    () => c.getCurrentProEntitlement(ORIGINAL_TRANSACTION_ID),
    (error: unknown) =>
      error instanceof AppleEntitlementError && error.reason === "verification_unavailable",
  );
  assert.equal(calls, 3, "1 initial attempt + 2 retries");
});

test("U3. a network failure is retryable", async () => {
  let calls = 0;
  const fetcher: TransactionHistoryFetcher = {
    async getTransactionHistory() {
      calls += 1;
      if (calls < 3) throw new Error("ECONNRESET");
      return { signedTransactions: ["s1"], hasMore: false };
    },
  };
  const c = client(fetcher, decoderFor({ s1: payload() }));
  await c.getCurrentProEntitlement(ORIGINAL_TRANSACTION_ID);
  assert.equal(calls, 3);
});

test("U4. a non-retryable 4xx is not retried", async () => {
  let calls = 0;
  const fetcher: TransactionHistoryFetcher = {
    async getTransactionHistory() {
      calls += 1;
      throw new APIException(400);
    },
  };
  const c = client(fetcher, decoderFor({}));
  await assert.rejects(
    () => c.getCurrentProEntitlement(ORIGINAL_TRANSACTION_ID),
    (error: unknown) =>
      error instanceof AppleEntitlementError && error.reason === "verification_unavailable",
  );
  assert.equal(calls, 1, "a deterministic failure is never retried");
});

test("U5. Retry-After is honored when Apple supplies it", async () => {
  const waits: number[] = [];
  let calls = 0;
  const error = Object.assign(new APIException(429), { retryAfter: 3 });
  const fetcher: TransactionHistoryFetcher = {
    async getTransactionHistory() {
      calls += 1;
      if (calls === 1) throw error;
      return { signedTransactions: ["s1"], hasMore: false };
    },
  };
  let clock = 0;
  const c = new AppleEntitlementClient({
    fetcher,
    decoder: decoderFor({ s1: payload() }),
    environment: "Production",
    retryBaseMs: 1,
    sleep: async (ms) => {
      waits.push(ms);
      clock += ms;
    },
    now: () => clock,
  });
  await c.getCurrentProEntitlement(ORIGINAL_TRANSACTION_ID);
  assert.deepEqual(waits, [3_000], "waited the Retry-After duration, not the base backoff");
});

test("U6. the total time budget is respected", async () => {
  let clock = 0;
  const fetcher: TransactionHistoryFetcher = {
    async getTransactionHistory() {
      clock += 5_000; // each attempt burns most of the budget
      throw new APIException(503);
    },
  };
  const c = new AppleEntitlementClient({
    fetcher,
    decoder: decoderFor({}),
    environment: "Production",
    totalBudgetMs: 8_000,
    sleep: async () => {},
    now: () => clock,
  });
  await assert.rejects(
    () => c.getCurrentProEntitlement(ORIGINAL_TRANSACTION_ID),
    (error: unknown) =>
      error instanceof AppleEntitlementError && error.reason === "verification_unavailable",
  );
  assert.ok(clock <= 20_000, "gave up rather than retrying past the budget");
});

// ── circuit breaker ──────────────────────────────────────────────────────────────────

test("the breaker opens after repeated Apple failures and recovers deterministically", async () => {
  const breaker = new AppleServiceBreaker(2, 1_000);
  let clock = 0;
  const failing: TransactionHistoryFetcher = {
    async getTransactionHistory() {
      throw new APIException(503);
    },
  };
  const makeClient = (fetcher: TransactionHistoryFetcher, decoder: SignedTransactionDecoder) =>
    new AppleEntitlementClient(
      {
        fetcher, decoder, environment: "Production", maxRetries: 0,
        sleep: async () => {}, now: () => clock,
      },
      breaker,
    );

  for (let i = 0; i < 2; i += 1) {
    await assert.rejects(() =>
      makeClient(failing, decoderFor({})).getCurrentProEntitlement(ORIGINAL_TRANSACTION_ID),
    );
  }

  // Open: fails immediately without touching Apple.
  let calls = 0;
  const counting: TransactionHistoryFetcher = {
    async getTransactionHistory() {
      calls += 1;
      return { signedTransactions: ["s1"], hasMore: false };
    },
  };
  await assert.rejects(
    () => makeClient(counting, decoderFor({ s1: payload() })).getCurrentProEntitlement(ORIGINAL_TRANSACTION_ID),
    (error: unknown) =>
      error instanceof AppleEntitlementError && error.reason === "verification_unavailable",
  );
  assert.equal(calls, 0, "the breaker short-circuited the request");

  // After the open window, it half-opens and a success closes it.
  clock += 1_001;
  const entitlement = await makeClient(
    counting,
    decoderFor({ s1: payload() }),
  ).getCurrentProEntitlement(ORIGINAL_TRANSACTION_ID);
  assert.equal(entitlement.revoked, false);
  assert.equal(calls, 1);
});

test("a definitive rejection does not trip the breaker", async () => {
  const breaker = new AppleServiceBreaker(2, 1_000);
  const c = () =>
    new AppleEntitlementClient(
      {
        fetcher: fetcherReturning([{ signedTransactions: ["s1"], hasMore: false }]),
        decoder: decoderFor({ s1: payload({ revocationDate: 1 }) }),
        environment: "Production",
        sleep: async () => {},
        now: () => 0,
      },
      breaker,
    );
  for (let i = 0; i < 3; i += 1) {
    await assert.rejects(() => c().getCurrentProEntitlement(ORIGINAL_TRANSACTION_ID));
  }
  assert.equal(breaker.isOpen(0), false, "revoked is an answer, not an outage");
});

// ── history transactions must verify ─────────────────────────────────────────────────

test("an unverifiable history transaction is never trusted", async () => {
  const c = client(
    fetcherReturning([{ signedTransactions: ["tampered"], hasMore: false }]),
    {
      verifyAndDecodeTransaction: async () => {
        throw new Error("signature check failed");
      },
    },
  );
  await assert.rejects(
    () => c.getCurrentProEntitlement(ORIGINAL_TRANSACTION_ID),
    (error: unknown) =>
      error instanceof AppleEntitlementError && error.reason === "verification_unavailable",
  );
});

// ── V / W / X / Z. redaction ─────────────────────────────────────────────────────────

test("V/W/X/Z. client errors contain no JWS, transaction id, payload or credentials", async () => {
  const signedBlob = "eyJhbGciOiJFUzI1NiJ9.HISTORY-PAYLOAD.SIG";
  const c = client(
    fetcherReturning([{ signedTransactions: [signedBlob], hasMore: false }]),
    decoderFor({ [signedBlob]: payload({ revocationDate: 1 }) }),
  );
  try {
    await c.getCurrentProEntitlement(ORIGINAL_TRANSACTION_ID);
    assert.fail("expected a rejection");
  } catch (error) {
    assert.ok(error instanceof AppleEntitlementError);
    const serialized = `${error.message} ${error.name} ${error.stack ?? ""} ${JSON.stringify(error)}`;
    assert.ok(!serialized.includes(signedBlob), "raw JWS leaked");
    assert.ok(!serialized.includes(ORIGINAL_TRANSACTION_ID), "transaction id leaked");
    assert.ok(!serialized.includes(PRIVATE_KEY_SHAPED), "credential leaked");
    assert.ok(!serialized.includes("PRIVATE KEY"), "key material leaked");
    assert.equal(error.message, "revoked");
  }
});

test("Z. an Apple APIException body never escapes the adapter", async () => {
  const fetcher: TransactionHistoryFetcher = {
    async getTransactionHistory() {
      throw new APIException(500, 4040010, `internal detail for ${ORIGINAL_TRANSACTION_ID}`);
    },
  };
  const c = client(fetcher, decoderFor({}), { maxRetries: 0 });
  try {
    await c.getCurrentProEntitlement(ORIGINAL_TRANSACTION_ID);
    assert.fail("expected a rejection");
  } catch (error) {
    assert.ok(error instanceof AppleEntitlementError);
    const serialized = `${error.message} ${error.stack ?? ""}`;
    assert.ok(!serialized.includes(ORIGINAL_TRANSACTION_ID));
    assert.ok(!serialized.includes("internal detail"));
  }
});
