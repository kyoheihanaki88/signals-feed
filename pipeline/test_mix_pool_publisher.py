#!/usr/bin/env python3
"""Phase 3D-3B — Mix Pool publisher contract.

CONTRACT TESTS ONLY. No storage provider is provisioned, so the transport is a capturing
fake. These prove the publisher refuses bad input before any network call and emits only
safe metadata — not that a production publishing path exists.
"""
import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import mix_pool                                        # noqa: E402
import mix_pool_schema as S                            # noqa: E402
from mix_pool_publisher import (                       # noqa: E402
    DEFAULT_TTL_SECONDS,
    MixPoolPublishError,
    mix_pool_key,
    publish_mix_pool,
)

DATE = "2026-07-27"
GENERATED_AT = "2026-07-27T09:00:00Z"
FAILURES = []


def check(name, ok, detail=""):
    print(("✓ " if ok else "✗ ") + name + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


class Capture:
    """The transport seam. Records exactly one call."""

    def __init__(self, explode=None):
        self.calls = []
        self.explode = explode

    def put(self, key, body, *, ttl_seconds):
        if self.explode:
            raise self.explode
        self.calls.append({"key": key, "body": body, "ttl": ttl_seconds})


def artifact():
    with open(os.path.join(HERE, "fixtures", "mix_pool_scout_candidates.json"),
              encoding="utf-8") as handle:
        src = json.load(handle)
    pool = mix_pool.build_mix_pool(src, DATE, GENERATED_AT, now=GENERATED_AT)
    return S.freeze_artifact(pool, source_input=src, source="offline-fixture",
                             reference_at=GENERATED_AT)


def refuses(mutate, name, store=None):
    art = copy.deepcopy(artifact())
    mutate(art)
    capture = store or Capture()
    try:
        publish_mix_pool(art, store=capture)
    except MixPoolPublishError:
        check(name, len(capture.calls) == 0, "the transport was still called")
        return
    check(name, False, "the artifact was published")


# ── key contract ──────────────────────────────────────────────────────────────────────

check("1. the key is namespaced, versioned and date-keyed",
      mix_pool_key(DATE) == "signals:mix-pool:v1:2026-07-27", mix_pool_key(DATE))
for bad in ("2026-7-27", "26-07-27", "2026-13-01", "2026-02-30", "", "today"):
    try:
        mix_pool_key(bad)
        check(f"4. malformed date rejected: {bad!r}", False, "accepted")
    except MixPoolPublishError:
        check(f"4. malformed date rejected: {bad!r}", True)

# ── publisher: the happy path ─────────────────────────────────────────────────────────

capture = Capture()
result = publish_mix_pool(artifact(), store=capture)

check("7. a valid artifact is published", result["status"] == "published")
check("12. exactly the expected key is used",
      capture.calls[0]["key"] == "signals:mix-pool:v1:2026-07-27", capture.calls[0]["key"])
check("11. the bytes sent are exactly the schema serializer output",
      capture.calls[0]["body"] == S.serialize(artifact()))
check("13. the TTL is applied", capture.calls[0]["ttl"] == DEFAULT_TTL_SECONDS)
check("17. publication is a single atomic write", len(capture.calls) == 1,
      f"{len(capture.calls)} calls")

check("14. the result carries safe metadata only",
      sorted(result) == ["byteLength", "candidateCount", "date", "key", "poolIdentityPrefix",
                         "schemaVersion", "selectorVersion", "status", "ttlSeconds"],
      str(sorted(result)))
serialized = json.dumps(result).lower()
check("18. no candidate content or credential appears in the result",
      not any(word in serialized for word in ("headline", "summary", "http", "token", "bearer")),
      serialized[:120])
check("14b. only a poolIdentity PREFIX is exposed",
      len(result["poolIdentityPrefix"]) == 12
      and result["poolIdentityPrefix"] == artifact()["poolIdentity"][:12])

# ── publisher: everything is refused BEFORE the transport ─────────────────────────────

refuses(lambda a: a.update({"schemaVersion": 99}), "8a. unsupported schemaVersion refused")
refuses(lambda a: a.update({"selectorVersion": 99}), "8b. unsupported selectorVersion refused")
refuses(lambda a: a["provenance"].update({"generatorVersion": 99}),
        "8c. unsupported generatorVersion refused")
refuses(lambda a: a.update({"candidateCount": 99}), "8d. candidateCount mismatch refused")
refuses(lambda a: a["candidates"][0].update({"category": "NOPE"}),
        "8e. noncanonical taxonomy refused")
refuses(lambda a: a["candidates"][0].update({"rawBody": "x"}), "8f. forbidden key refused")
refuses(lambda a: a["candidates"][0].update({"baseScore": 7.1234567}),
        "8g. out-of-contract numeric refused")
refuses(lambda a: a.update({"poolIdentity": "0" * 64}), "9. wrong poolIdentity refused")

# The date check only bites when an EXPLICIT publication date is supplied; with no date the
# artifact's own date is authoritative and the key derives from it.
capture = Capture()
try:
    publish_mix_pool(artifact(), store=capture, date="2026-07-26")
    check("9b. artifact date not matching an explicit publication date refused",
          False, "published")
except MixPoolPublishError:
    check("9b. artifact date not matching an explicit publication date refused",
          len(capture.calls) == 0)

capture = Capture()
published_for_own_date = publish_mix_pool(artifact(), store=capture)
check("9b2. with no explicit date, the artifact's own date keys the write",
      capture.calls[0]["key"].endswith(artifact()["date"])
      and published_for_own_date["date"] == artifact()["date"])

refuses(lambda a: a.update({"candidates": [], "candidateCount": 0, "poolIdentity": S.pool_identity([])}),
        "9c. empty candidate pool refused")

capture = Capture()
try:
    publish_mix_pool(artifact(), store=capture, max_bytes=100)
    check("10. oversized artifact refused", False, "published")
except MixPoolPublishError:
    check("10. oversized artifact refused", len(capture.calls) == 0)

try:
    publish_mix_pool(artifact(), store=None)
    check("45. an unconfigured store refuses", False, "published")
except MixPoolPublishError:
    check("45. an unconfigured store refuses", True)

# ── transport failure propagates, and writes nothing partial ──────────────────────────

exploding = Capture(explode=RuntimeError("connection reset"))
try:
    publish_mix_pool(artifact(), store=exploding)
    check("15/16. a transport failure surfaces", False, "silently succeeded")
except MixPoolPublishError:
    check("15/16. a transport failure surfaces", False, "misreported as a contract error")
except RuntimeError:
    check("15/16. a transport failure surfaces distinctly from a contract refusal",
          len(exploding.calls) == 0)

# ── the publisher never mutates the caller's artifact ─────────────────────────────────

original = artifact()
snapshot = copy.deepcopy(original)
publish_mix_pool(original, store=Capture())
check("caller-owned artifact is not mutated", original == snapshot)

# ── repository protection and standard edition remain untouched ───────────────────────

check("19. mix_pool_cli repository protection is untouched",
      "refusing repository/production destination" in
      open(os.path.join(HERE, "mix_pool_cli.py"), encoding="utf-8").read())
check("20. the publisher never writes a file",
      not any(token in open(os.path.join(HERE, "mix_pool_publisher.py"), encoding="utf-8").read()
              for token in ("open(", "Path(", "os.replace", "write_text")))
check("18b. the publisher logs nothing",
      not any(token in open(os.path.join(HERE, "mix_pool_publisher.py"), encoding="utf-8").read()
              for token in ("print(", "logging", "logger")))

print()
print(f"{len(FAILURES)} failure(s)" if FAILURES else "all checks passed")
for name in FAILURES:
    print("  ✗", name)
sys.exit(1 if FAILURES else 0)
