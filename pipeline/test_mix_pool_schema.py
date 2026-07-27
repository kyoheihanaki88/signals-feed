#!/usr/bin/env python3
"""Fail-closed checks for the frozen Phase 2C mix-pool schema."""

import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from mix_pool import build_mix_pool
from mix_pool_schema import freeze_artifact, serialize, validate_artifact

FIXTURE = os.path.join(HERE, "fixtures", "mix_pool_scout_candidates.json")
DATE = "2026-07-27"
STAMP = "2026-07-27T10:00:00Z"
FAILURES = []


def check(name, condition, detail=""):
    print(("✓ " if condition else "✗ ") + name)
    if not condition:
        FAILURES.append(f"{name}: {detail}")


def invalid(name, mutate, expected):
    changed = copy.deepcopy(artifact)
    mutate(changed)
    result = validate_artifact(changed)
    check(name, not result["valid"] and any(expected in item for item in result["errors"]), result)


with open(FIXTURE, encoding="utf-8") as handle:
    source = json.load(handle)
pool = build_mix_pool(source, DATE, STAMP, now=STAMP)
artifact = freeze_artifact(
    pool, source_input=source, source="offline-fixture", reference_at=STAMP
)

check("A. valid fixture accepted", validate_artifact(artifact)["valid"])
check("A2. frozen membership uses id", all(
    set(row) == {"id", "strength", "evidence"}
    for candidate in artifact["candidates"]
    for row in candidate["regionMemberships"]
))
invalid("B. count mismatch rejected", lambda value: value.update(candidateCount=99), "candidateCount")
invalid(
    "C. duplicate id rejected",
    lambda value: value["candidates"][1].update(id=value["candidates"][0]["id"]),
    "duplicate candidate id",
)
invalid(
    "D. duplicate URL rejected",
    lambda value: value["candidates"][1].update(url=value["candidates"][0]["url"]),
    "duplicate candidate url",
)
invalid("E. malformed URL rejected", lambda value: value["candidates"][0].update(url="ftp://bad"), ".url")
invalid(
    "F. noncanonical region rejected",
    lambda value: value["candidates"][0]["regionMemberships"][0].update(id="JP"),
    ".id is not canonical",
)


def empty_primary(value):
    membership = next(
        row for candidate in value["candidates"] for row in candidate["regionMemberships"]
        if row["strength"] == "primary"
    )
    membership["evidence"] = []


invalid("G. primary membership needs evidence", empty_primary, "requires evidence")
invalid("H. forbidden rawBody rejected", lambda value: value["candidates"][0].update(rawBody="x"), "forbidden")
check("I. serialization is byte deterministic", serialize(artifact) == serialize(copy.deepcopy(artifact)))

reversed_source = copy.deepcopy(source)
reversed_source["candidates"].reverse()
reversed_pool = build_mix_pool(reversed_source, DATE, STAMP, now=STAMP)
reversed_artifact = freeze_artifact(
    reversed_pool, source_input=reversed_source, source="offline-fixture", reference_at=STAMP
)
check("J. reversed input cannot change artifact", reversed_artifact == artifact)
invalid("R. invalid selector version rejected", lambda value: value.update(selectorVersion=999), "selectorVersion")
invalid("S. invalid schema version rejected", lambda value: value.update(schemaVersion=999), "schemaVersion")
invalid(
    "S2. local filesystem path rejected",
    lambda value: value["provenance"].update(source="file:///Users/example/input.json"),
    "local path",
)
check("identity is a deterministic digest", len(artifact["poolIdentity"]) == 64)
check("validation is structured", set(artifact["validation"]) == {"valid", "errors", "warnings"})
check("top-level schema is exact", set(artifact) == {
    "schemaVersion", "selectorVersion", "date", "generatedAt", "poolIdentity",
    "candidateCount", "candidates", "validation", "provenance",
})

if FAILURES:
    print("\n" + "\n".join(FAILURES))
    sys.exit(1)
print(f"\n{17 - len(FAILURES)}/17 PASS")
