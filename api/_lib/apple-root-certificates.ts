/**
 * Apple root certificate bundle loader. (Phase 3B-3)
 *
 * The three public Apple root CAs are VENDORED in this repository and loaded from disk.
 * They are never downloaded at runtime: a trust anchor fetched over the network is a
 * trust anchor an attacker can influence.
 *
 * The manifest's `sha256` values are LOCALLY RECORDED integrity hashes — they detect an
 * unexpected change to a vendored file. They are NOT Apple-published fingerprints
 * (Apple does not publish fingerprints on its PKI page), so they prove "this file is the
 * one we reviewed", not "Apple says this is the file".
 *
 * These are PUBLIC certificates only. No private key material belongs in this directory.
 *
 * Everything fails closed: any missing, extra, altered, empty or unparsable certificate
 * raises and the verifier is never constructed.
 */

import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { X509Certificate } from "node:crypto";

export const SUPPORTED_MANIFEST_VERSION = 1;

/** Exactly these three, in this order. Anything else is a configuration error. */
export const EXPECTED_CERTIFICATE_FILENAMES = [
  "AppleIncRootCertificate.cer",
  "AppleRootCA-G2.cer",
  "AppleRootCA-G3.cer",
] as const;

export class AppleRootCertificateError extends Error {
  readonly reason: string;

  constructor(reason: string) {
    // Reason is a short mechanical description — never certificate bytes or a file path.
    super(`apple root certificates unusable: ${reason}`);
    this.name = "AppleRootCertificateError";
    this.reason = reason;
  }
}

export type CertificateManifestEntry = {
  filename: string;
  sha256: string;
  sourceUrl: string;
  recordedAt: string;
};

export type CertificateManifest = {
  manifestVersion: number;
  purpose: string;
  certificates: CertificateManifestEntry[];
};

const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;

function defaultCertificateDirectory(): string {
  // ESM-safe: resolve relative to this module, never an absolute developer path.
  return resolve(dirname(fileURLToPath(import.meta.url)), "..", "_certs", "apple");
}

export function validateManifest(raw: unknown): CertificateManifest {
  if (typeof raw !== "object" || raw === null) {
    throw new AppleRootCertificateError("manifest is not an object");
  }
  const manifest = raw as Partial<CertificateManifest>;

  if (manifest.manifestVersion !== SUPPORTED_MANIFEST_VERSION) {
    throw new AppleRootCertificateError("unsupported manifest version");
  }
  if (typeof manifest.purpose !== "string" || manifest.purpose.trim().length === 0) {
    throw new AppleRootCertificateError("manifest purpose is missing");
  }
  if (!Array.isArray(manifest.certificates)) {
    throw new AppleRootCertificateError("manifest certificates is not an array");
  }
  if (manifest.certificates.length !== EXPECTED_CERTIFICATE_FILENAMES.length) {
    throw new AppleRootCertificateError("manifest must list exactly three certificates");
  }

  const seenFilenames = new Set<string>();
  const seenUrls = new Set<string>();
  for (const entry of manifest.certificates) {
    if (typeof entry?.filename !== "string" || !SHA256_PATTERN.test(entry?.sha256 ?? "")) {
      throw new AppleRootCertificateError("manifest entry has an invalid filename or sha256");
    }
    if (typeof entry.sourceUrl !== "string" || !entry.sourceUrl.startsWith("https://www.apple.com/")) {
      throw new AppleRootCertificateError("manifest entry sourceUrl is not an official Apple https URL");
    }
    if (typeof entry.recordedAt !== "string" || !DATE_PATTERN.test(entry.recordedAt)) {
      throw new AppleRootCertificateError("manifest entry recordedAt is not an ISO date");
    }
    if (!(EXPECTED_CERTIFICATE_FILENAMES as readonly string[]).includes(entry.filename)) {
      throw new AppleRootCertificateError("manifest lists an unexpected certificate");
    }
    if (seenFilenames.has(entry.filename)) {
      throw new AppleRootCertificateError("manifest contains a duplicate filename");
    }
    if (seenUrls.has(entry.sourceUrl)) {
      throw new AppleRootCertificateError("manifest contains a duplicate sourceUrl");
    }
    seenFilenames.add(entry.filename);
    seenUrls.add(entry.sourceUrl);
  }

  for (const expected of EXPECTED_CERTIFICATE_FILENAMES) {
    if (!seenFilenames.has(expected)) {
      throw new AppleRootCertificateError("manifest is missing an expected certificate");
    }
  }

  return manifest as CertificateManifest;
}

export type LoadAppleRootCertificatesOptions = {
  /** Override the bundled directory — tests only. */
  directory?: string;
};

/**
 * Read, verify and return the three root certificates as DER Buffers, in the stable
 * order of `EXPECTED_CERTIFICATE_FILENAMES` (the order `SignedDataVerifier` receives).
 *
 * Not executed at module import: callers invoke this during request/app setup so a
 * certificate problem surfaces as a controlled failure rather than an import crash.
 */
export function loadAppleRootCertificates(
  options: LoadAppleRootCertificatesOptions = {},
): Buffer[] {
  const directory = options.directory ?? defaultCertificateDirectory();

  let manifestRaw: string;
  try {
    manifestRaw = readFileSync(join(directory, "manifest.json"), "utf8");
  } catch {
    throw new AppleRootCertificateError("manifest file is missing or unreadable");
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(manifestRaw);
  } catch {
    throw new AppleRootCertificateError("manifest is not valid JSON");
  }
  const manifest = validateManifest(parsed);

  const byFilename = new Map(manifest.certificates.map((entry) => [entry.filename, entry]));

  return EXPECTED_CERTIFICATE_FILENAMES.map((filename) => {
    const entry = byFilename.get(filename);
    if (!entry) throw new AppleRootCertificateError("manifest is missing an expected certificate");

    let bytes: Buffer;
    try {
      bytes = readFileSync(join(directory, filename));
    } catch {
      throw new AppleRootCertificateError("a certificate file is missing or unreadable");
    }
    if (bytes.length === 0) {
      throw new AppleRootCertificateError("a certificate file is empty");
    }

    const digest = createHash("sha256").update(bytes).digest("hex");
    if (digest !== entry.sha256) {
      // Never log either digest: a mismatch is reported, not described.
      throw new AppleRootCertificateError("a certificate file does not match its recorded hash");
    }

    try {
      // Proves the bytes are a parsable DER X.509 certificate before Apple's library
      // ever sees them.
      const certificate = new X509Certificate(bytes);
      if (!certificate.subject) {
        throw new Error("no subject");
      }
    } catch {
      throw new AppleRootCertificateError("a certificate file is not valid DER");
    }

    return bytes;
  });
}
