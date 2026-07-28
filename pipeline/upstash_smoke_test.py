#!/usr/bin/env python3
"""Manual, opt-in Upstash round-trip smoke test. (Phase 3D-3D)

This is the ONE step that can turn "the code is ready for provisioning" into "the storage
path actually works". It is deliberately awkward to run by accident:

  • no default key and no default date — both must be typed
  • a non-production confirmation flag must be passed
  • credentials must already be in the environment
  • the artifact to publish must be an existing, already-validated local file
  • it is referenced by no test and no workflow, and nothing imports it

WHAT IT PROVES. That the exact bytes Python published come back from Upstash unchanged:
same byte length, same SHA-256 over the raw body, same canonical bytes, and both identities
re-derived from the retrieved artifact rather than read out of it.

WHAT IT DOES NOT PROVE. Anything about the daily workflow, the API route, or entitlement.

TEST KEY SAFETY. The key is always built under a dedicated `smoke` namespace that the
reader and the daily publisher never look at, so a smoke artifact cannot be served to a
user even if cleanup is skipped. `--delete-after` removes that ONE exact key; if you prefer
not to grant delete, omit it and remove the key by hand in the Upstash console.

    KV_REST_API_URL=... KV_REST_API_WRITE_TOKEN=... \\
      python3 pipeline/upstash_smoke_test.py \\
        --artifact /tmp/editorial-pool.json \\
        --test-date 2026-01-01 \\
        --i-understand-this-is-not-production \\
        --delete-after
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

from editorial_mix_pool_schema import (
    editorial_pool_identity,
    selector_pool_identity,
    validate_editorial_mix_pool,
)
from mix_pool_schema import serialize
from upstash_mix_pool_transport import (
    DEFAULT_TIMEOUT_SECONDS,
    UpstashPoolObjectStore,
    UpstashTransportError,
    delete_key,
    read_back,
    resolve_config,
)

#: A namespace no reader and no publisher consults. A smoke artifact is unreachable by the
#: product even if it is left behind.
SMOKE_NAMESPACE = "signals:smoke:editorial-mix-pool:v1"
#: Short on purpose: a forgotten smoke key should disappear on its own within the hour.
SMOKE_TTL_SECONDS = 3600


def smoke_key(test_date: str) -> str:
    return f"{SMOKE_NAMESPACE}:{test_date}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--artifact", required=True, help="a local, already-validated pool file")
    ap.add_argument("--test-date", required=True, help="a NON-production date, e.g. 2026-01-01")
    ap.add_argument(
        "--i-understand-this-is-not-production",
        action="store_true",
        required=True,
        help="explicit confirmation that this writes a throwaway key",
    )
    ap.add_argument("--delete-after", action="store_true", help="remove the one test key")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    args = ap.parse_args(argv)

    if not args.i_understand_this_is_not_production:
        print("refusing: confirmation flag is required", file=sys.stderr)
        return 2

    artifact = json.loads(Path(args.artifact).read_text(encoding="utf-8"))
    validation = validate_editorial_mix_pool(artifact)
    if not validation["valid"]:
        print(json.dumps({"status": "refused", "reason": "artifact_invalid",
                          "errorCount": len(validation["errors"])}), file=sys.stderr)
        return 3

    body = serialize(artifact)
    key = smoke_key(args.test_date)

    try:
        config = resolve_config(dict(os.environ), timeout_seconds=args.timeout)
    except UpstashTransportError as error:
        print(json.dumps({"status": "not_configured", "reason": error.reason}), file=sys.stderr)
        return 4

    try:
        UpstashPoolObjectStore(config).put(key, body, ttl_seconds=SMOKE_TTL_SECONDS)
        retrieved = read_back(config, key)
    except UpstashTransportError as error:
        print(json.dumps({"status": "failed", "reason": error.reason}), file=sys.stderr)
        return 5

    if retrieved is None:
        print(json.dumps({"status": "failed", "reason": "round_trip_missing"}), file=sys.stderr)
        return 5

    round_tripped = json.loads(retrieved.decode("utf-8"))
    report = {
        "status": "ok",
        "key": key,
        "publishedBytes": len(body),
        "retrievedBytes": len(retrieved),
        "bytesIdentical": retrieved == body,
        "sha256Identical": hashlib.sha256(retrieved).hexdigest()
        == hashlib.sha256(body).hexdigest(),
        "canonicalIdentical": serialize(round_tripped) == body,
        # Re-DERIVED from the retrieved candidates, not read out of the envelope.
        "selectorIdentityMatches": selector_pool_identity(round_tripped)
        == artifact["selectorPoolIdentity"],
        "editorialIdentityMatches": editorial_pool_identity(round_tripped["candidates"])
        == artifact["editorialPoolIdentity"],
        "candidateCount": round_tripped.get("candidateCount"),
    }

    if args.delete_after:
        try:
            report["deleted"] = delete_key(config, key)
        except UpstashTransportError as error:
            report["deleteFailed"] = error.reason
    else:
        report["cleanup"] = f"delete this exact key manually: {key}"

    ok = all(
        report[flag]
        for flag in (
            "bytesIdentical",
            "sha256Identical",
            "canonicalIdentical",
            "selectorIdentityMatches",
            "editorialIdentityMatches",
        )
    )
    if not ok:
        report["status"] = "mismatch"
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if ok else 6


if __name__ == "__main__":
    raise SystemExit(main())
