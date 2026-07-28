import assert from "node:assert/strict";
import test from "node:test";
import {
  PRO_PRODUCT_ID,
  SignalsTokenError,
  TOKEN_AUDIENCE,
  TOKEN_ISSUER,
  TOKEN_SCOPE,
} from "../_lib/signals-token.js";
import {
  claimsFor,
  createTokenFixture,
  generateTestKey,
  signClaims,
} from "./test-helpers.js";

test("subject is deterministic, opaque, and environment-bound", () => {
  const { tokens } = createTokenFixture();
  const production = tokens.deriveSubject("1234567890", "Production");
  assert.equal(
    production,
    tokens.deriveSubject("1234567890", "Production"),
  );
  assert.notEqual(production, "1234567890");
  assert.notEqual(
    production,
    tokens.deriveSubject("1234567890", "Sandbox"),
  );
});

test("issued token has minimal claims and no mix or transaction data", () => {
  const { tokens } = createTokenFixture();
  const subject = tokens.deriveSubject("original-secret-id", "Production");
  const { accessToken, claims } = tokens.issue({
    subject,
    environment: "Production",
  });
  assert.equal(claims.exp - claims.iat, 900);
  const serialized = JSON.stringify(claims);
  for (const forbidden of [
    "regions",
    "topics",
    "original-secret-id",
    "originalTransactionId",
    "email",
  ]) {
    assert.equal(serialized.includes(forbidden), false);
  }
  assert.equal(
    tokens.verify({
      token: accessToken,
      expectedEnvironment: "Production",
    }).sub,
    subject,
  );
});

test("I/W: expiration, not-before, and 60-second skew are enforced", async (t) => {
  await t.test("59 seconds after exp accepted, 61 rejected", () => {
    const { tokens, clock } = createTokenFixture();
    const issued = tokens.issue({
      subject: "subject",
      environment: "Production",
    });
    clock.advance((900 + 59) * 1_000);
    assert.doesNotThrow(() =>
      tokens.verify({
        token: issued.accessToken,
        expectedEnvironment: "Production",
      }),
    );
    clock.advance(2_000);
    assert.throws(
      () =>
        tokens.verify({
          token: issued.accessToken,
          expectedEnvironment: "Production",
        }),
      (error: unknown) =>
        error instanceof SignalsTokenError &&
        error.code === "expired_token",
    );
  });
  await t.test("future nbf beyond skew rejected", () => {
    const fixture = createTokenFixture();
    const claims = claimsFor(fixture.tokens);
    claims.nbf += 61;
    const token = signClaims(fixture.key, claims);
    assert.throws(() =>
      fixture.tokens.verify({
        token,
        expectedEnvironment: "Production",
      }),
    );
  });
});

test("J/K/L and fixed claims reject issuer, audience, scope, product, environment", async (t) => {
  const cases: Array<{
    name: string;
    change: (claims: Record<string, unknown>) => void;
    code: string;
  }> = [
    {
      name: "issuer",
      change: (claims) => {
        claims.iss = "fake";
      },
      code: "wrong_issuer",
    },
    {
      name: "audience",
      change: (claims) => {
        claims.aud = "fake";
      },
      code: "wrong_audience",
    },
    {
      name: "scope",
      change: (claims) => {
        claims.scope = ["admin"];
      },
      code: "wrong_scope",
    },
    {
      name: "product",
      change: (claims) => {
        claims.product = "fake";
      },
      code: "wrong_product",
    },
  ];
  for (const item of cases) {
    await t.test(item.name, () => {
      const fixture = createTokenFixture();
      const claims = { ...claimsFor(fixture.tokens) };
      item.change(claims);
      const token = signClaims(fixture.key, claims);
      assert.throws(
        () =>
          fixture.tokens.verify({
            token,
            expectedEnvironment: "Production",
          }),
        (error: unknown) =>
          error instanceof SignalsTokenError && error.code === item.code,
      );
    });
  }

  await t.test("expected environment", () => {
    const fixture = createTokenFixture();
    const token = fixture.tokens.issue({
      subject: "subject",
      environment: "Sandbox",
    }).accessToken;
    assert.throws(
      () =>
        fixture.tokens.verify({
          token,
          expectedEnvironment: "Production",
        }),
      (error: unknown) =>
        error instanceof SignalsTokenError &&
        error.code === "wrong_environment",
    );
  });
});

test("malformed and signature-tampered tokens are rejected", () => {
  const { tokens } = createTokenFixture();
  assert.throws(() =>
    tokens.verify({ token: "bad", expectedEnvironment: "Production" }),
  );
  const issued = tokens.issue({
    subject: "subject",
    environment: "Production",
  }).accessToken;
  assert.throws(() =>
    tokens.verify({
      token: `${issued.slice(0, -2)}aa`,
      expectedEnvironment: "Production",
    }),
  );
});

test("X/Y: previous key accepted during rotation, then rejected after removal", () => {
  const oldKey = generateTestKey("old");
  const currentKey = generateTestKey("current");
  const oldService = createTokenFixture({ signingKey: oldKey }).tokens;
  const oldToken = oldService.issue({
    subject: "subject",
    environment: "Production",
  }).accessToken;

  const rotating = createTokenFixture({
    signingKey: currentKey,
    verificationKeys: [currentKey, oldKey],
  }).tokens;
  assert.doesNotThrow(() =>
    rotating.verify({
      token: oldToken,
      expectedEnvironment: "Production",
    }),
  );

  const afterRemoval = createTokenFixture({
    signingKey: currentKey,
    verificationKeys: [currentKey],
  }).tokens;
  assert.throws(() =>
    afterRemoval.verify({
      token: oldToken,
      expectedEnvironment: "Production",
    }),
  );
});

test("documented token constants are exact", () => {
  assert.equal(TOKEN_ISSUER, "signals-auth");
  assert.equal(TOKEN_AUDIENCE, "signals-custom-mix");
  assert.equal(TOKEN_SCOPE, "custom_mix");
  assert.equal(PRO_PRODUCT_ID, "com.signalsapp.pro.lifetime");
});
