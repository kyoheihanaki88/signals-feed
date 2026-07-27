import assert from "node:assert/strict";
import test from "node:test";
import { mkdtempSync, readFileSync, writeFileSync, rmSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  AppleRootCertificateError,
  EXPECTED_CERTIFICATE_FILENAMES,
  loadAppleRootCertificates,
  validateManifest,
} from "../_lib/apple-root-certificates.js";

const REAL_CERT_DIR = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "..",
  "_certs",
  "apple",
);

/** Copy the real bundle into a temp dir so a test can tamper with it safely. */
function stagedBundle(): string {
  const dir = mkdtempSync(join(tmpdir(), "apple-certs-"));
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, "manifest.json"), readFileSync(join(REAL_CERT_DIR, "manifest.json")));
  for (const filename of EXPECTED_CERTIFICATE_FILENAMES) {
    writeFileSync(join(dir, filename), readFileSync(join(REAL_CERT_DIR, filename)));
  }
  return dir;
}

function readManifest(dir: string): Record<string, unknown> {
  return JSON.parse(readFileSync(join(dir, "manifest.json"), "utf8"));
}

function writeManifest(dir: string, manifest: unknown): void {
  writeFileSync(join(dir, "manifest.json"), JSON.stringify(manifest, null, 2));
}

// ── A. valid manifest + bundle ───────────────────────────────────────────────────────

test("A. the vendored bundle loads and matches its recorded hashes", () => {
  const certificates = loadAppleRootCertificates({ directory: REAL_CERT_DIR });
  assert.equal(certificates.length, 3);
  for (const buffer of certificates) assert.ok(buffer.length > 0);
});

test("A2. certificates are returned in a stable order", () => {
  const first = loadAppleRootCertificates({ directory: REAL_CERT_DIR });
  const second = loadAppleRootCertificates({ directory: REAL_CERT_DIR });
  assert.deepEqual(first.map((b) => b.length), second.map((b) => b.length));
});

test("A3. the manifest declares its purpose as a local integrity hash", () => {
  const manifest = readManifest(REAL_CERT_DIR) as { purpose: string };
  assert.match(manifest.purpose, /Not an Apple-published fingerprint/i);
});

test("A4. every manifest sourceUrl is an official Apple https endpoint", () => {
  const manifest = readManifest(REAL_CERT_DIR) as {
    certificates: { sourceUrl: string }[];
  };
  for (const entry of manifest.certificates) {
    assert.ok(entry.sourceUrl.startsWith("https://www.apple.com/"));
  }
});

// ── B. hash mismatch ─────────────────────────────────────────────────────────────────

test("B. a modified certificate fails closed", () => {
  const dir = stagedBundle();
  try {
    const target = join(dir, EXPECTED_CERTIFICATE_FILENAMES[0]);
    const bytes = readFileSync(target);
    bytes[bytes.length - 1] ^= 0xff; // flip one byte
    writeFileSync(target, bytes);
    assert.throws(
      () => loadAppleRootCertificates({ directory: dir }),
      (error: unknown) =>
        error instanceof AppleRootCertificateError &&
        error.reason.includes("does not match its recorded hash"),
    );
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

// ── C. missing file ──────────────────────────────────────────────────────────────────

test("C. a missing certificate fails closed", () => {
  const dir = stagedBundle();
  try {
    rmSync(join(dir, EXPECTED_CERTIFICATE_FILENAMES[1]));
    assert.throws(
      () => loadAppleRootCertificates({ directory: dir }),
      (error: unknown) =>
        error instanceof AppleRootCertificateError && error.reason.includes("missing or unreadable"),
    );
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("C2. a missing manifest fails closed", () => {
  const dir = stagedBundle();
  try {
    rmSync(join(dir, "manifest.json"));
    assert.throws(
      () => loadAppleRootCertificates({ directory: dir }),
      AppleRootCertificateError,
    );
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

// ── D. malformed DER / empty ─────────────────────────────────────────────────────────

test("D. malformed DER fails closed even when the hash matches", async () => {
  const dir = stagedBundle();
  try {
    const filename = EXPECTED_CERTIFICATE_FILENAMES[2];
    const junk = Buffer.from("this is definitely not a DER certificate");
    writeFileSync(join(dir, filename), junk);
    // Update the recorded hash so the DER parse — not the hash — is what rejects it.
    const manifest = readManifest(dir) as {
      certificates: { filename: string; sha256: string }[];
    };
    const { createHash } = await import("node:crypto");
    const entry = manifest.certificates.find((c) => c.filename === filename)!;
    entry.sha256 = createHash("sha256").update(junk).digest("hex");
    writeManifest(dir, manifest);

    assert.throws(
      () => loadAppleRootCertificates({ directory: dir }),
      (error: unknown) =>
        error instanceof AppleRootCertificateError && error.reason.includes("not valid DER"),
    );
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("D2. an empty certificate file fails closed", () => {
  const dir = stagedBundle();
  try {
    writeFileSync(join(dir, EXPECTED_CERTIFICATE_FILENAMES[0]), Buffer.alloc(0));
    assert.throws(
      () => loadAppleRootCertificates({ directory: dir }),
      AppleRootCertificateError,
    );
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

// ── E. manifest version ──────────────────────────────────────────────────────────────

test("E. an unsupported manifest version fails closed", () => {
  assert.throws(
    () => validateManifest({ manifestVersion: 99, purpose: "x", certificates: [] }),
    (error: unknown) =>
      error instanceof AppleRootCertificateError &&
      error.reason.includes("unsupported manifest version"),
  );
});

// ── F. extra / missing / duplicate entries ───────────────────────────────────────────

test("F. an extra manifest entry fails closed", () => {
  const manifest = readManifest(REAL_CERT_DIR) as {
    certificates: Record<string, string>[];
  };
  manifest.certificates.push({
    filename: "AttackerRoot.cer",
    sha256: "a".repeat(64),
    sourceUrl: "https://www.apple.com/certificateauthority/AttackerRoot.cer",
    recordedAt: "2026-07-27",
  });
  assert.throws(
    () => validateManifest(manifest),
    (error: unknown) =>
      error instanceof AppleRootCertificateError &&
      error.reason.includes("exactly three"),
  );
});

test("F2. a duplicate filename fails closed", () => {
  const manifest = readManifest(REAL_CERT_DIR) as {
    certificates: Record<string, string>[];
  };
  manifest.certificates[1] = { ...manifest.certificates[0], sourceUrl: "https://www.apple.com/x.cer" };
  assert.throws(
    () => validateManifest(manifest),
    (error: unknown) =>
      error instanceof AppleRootCertificateError && error.reason.includes("duplicate filename"),
  );
});

test("F3. a duplicate sourceUrl fails closed", () => {
  const manifest = readManifest(REAL_CERT_DIR) as {
    certificates: Record<string, string>[];
  };
  manifest.certificates[1].sourceUrl = manifest.certificates[0].sourceUrl;
  assert.throws(
    () => validateManifest(manifest),
    (error: unknown) =>
      error instanceof AppleRootCertificateError && error.reason.includes("duplicate sourceUrl"),
  );
});

test("F4. an invalid SHA-256 format fails closed", () => {
  const manifest = readManifest(REAL_CERT_DIR) as {
    certificates: Record<string, string>[];
  };
  manifest.certificates[0].sha256 = "not-a-hash";
  assert.throws(
    () => validateManifest(manifest),
    (error: unknown) =>
      error instanceof AppleRootCertificateError && error.reason.includes("invalid filename or sha256"),
  );
});

test("F5. a non-Apple sourceUrl fails closed", () => {
  const manifest = readManifest(REAL_CERT_DIR) as {
    certificates: Record<string, string>[];
  };
  manifest.certificates[0].sourceUrl = "https://evil.example.com/root.cer";
  assert.throws(
    () => validateManifest(manifest),
    (error: unknown) =>
      error instanceof AppleRootCertificateError && error.reason.includes("official Apple https URL"),
  );
});

// ── Y. no certificate bytes or private material leak ─────────────────────────────────

test("Y. errors never contain certificate bytes or file paths", () => {
  const dir = stagedBundle();
  try {
    const target = join(dir, EXPECTED_CERTIFICATE_FILENAMES[0]);
    const bytes = readFileSync(target);
    bytes[0] ^= 0xff;
    writeFileSync(target, bytes);
    try {
      loadAppleRootCertificates({ directory: dir });
      assert.fail("expected a failure");
    } catch (error) {
      assert.ok(error instanceof AppleRootCertificateError);
      const serialized = `${error.message} ${error.stack ?? ""}`;
      assert.ok(!serialized.includes(dir), "a filesystem path leaked");
      assert.ok(!serialized.includes(bytes.toString("base64").slice(0, 24)), "bytes leaked");
    }
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("Y2. the bundled directory contains no private key material", () => {
  for (const filename of EXPECTED_CERTIFICATE_FILENAMES) {
    const text = readFileSync(join(REAL_CERT_DIR, filename)).toString("latin1");
    assert.ok(!/PRIVATE KEY/i.test(text), `${filename} contains private key material`);
  }
  const manifest = readFileSync(join(REAL_CERT_DIR, "manifest.json"), "utf8");
  assert.ok(!/PRIVATE KEY/i.test(manifest));
});
