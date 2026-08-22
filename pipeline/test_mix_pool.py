#!/usr/bin/env python3
"""Deterministic fixture tests for Phase 2B Custom Mix pool generation."""

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from custom_mix_selector import select_custom_mix
from mix_pool import MixPoolError, build_mix_pool, validate_mix_pool, write_mix_pool

FIXTURE = os.path.join(HERE, "fixtures", "mix_pool_scout_candidates.json")
DATE = "2026-07-27"
STAMP = "2026-07-27T10:00:00Z"
FAILURES = []


def check(name, condition, detail=""):
    print(("✓ " if condition else "✗ ") + name)
    if not condition:
        FAILURES.append(f"{name}: {detail}")


with open(FIXTURE, encoding="utf-8") as handle:
    source = json.load(handle)

pool_a = build_mix_pool(source, DATE, STAMP)
pool_b = build_mix_pool(source, DATE, STAMP)

check("1. deterministic full artifact", pool_a == pool_b)
check("2. source count recorded", pool_a["sourceCandidateCount"] == 8)
check("3. stale and editorial candidates excluded", pool_a["candidateCount"] == 6)
check("4. exclusion count recorded", pool_a["excludedCount"] == 2)
check(
    "5. exclusion reasons are auditable",
    {row["reason"] for row in pool_a["excludedCandidates"]}
    == {"stale", "editorial_exclusion:product/deal"},
)
check("6. candidate ordering stable", [c["id"] for c in pool_a["candidates"]]
      == sorted(c["id"] for c in pool_a["candidates"]))
check("7. generated timestamp normalized", pool_a["generatedAt"] == STAMP)
check("8. pool identity stable", len(pool_a["poolIdentity"]) == 64)

by_id = {candidate["id"]: candidate for candidate in pool_a["candidates"]}
tokyo = by_id["f4d7d4"]
boj = by_id["776677"]
quake_a = by_id["619a26"]
quake_b = by_id["162b98"]
world = by_id["78c7a7"]

check("9. Scout title maps to headline", tokyo["headline"].startswith("Tokyo hospitals"))
check("10. Scout snippet maps to summary", tokyo["summary"].startswith("Japan's health"))
check("11. canonical URL preserved", "?" not in tokyo["url"])
check("12. category topic inferred", tokyo["topics"] == ["health", "tech"])
check("13. Japan title evidence is primary", any(
    row["region"] == "japan" and row["strength"] == "primary"
    for row in tokyo["regionMemberships"]
))
check("14. Bank of Japan evidence is primary", any(
    row["region"] == "japan" and "bank-of-japan:title" in row["evidence"]
    for row in boj["regionMemberships"]
))
check("15. genuine world story classified world-primary", any(
    row["region"] == "world" and row["strength"] == "primary"
    for row in world["regionMemberships"]
))
check("16. cross-publisher cluster shares underlying identity",
      quake_a["underlyingStoryIdentity"] == quake_b["underlyingStoryIdentity"])
check("17. singleton stories have distinct identities",
      tokyo["underlyingStoryIdentity"] != boj["underlyingStoryIdentity"])
check("18. base score is numeric", isinstance(tokyo["baseScore"], float))
check("19. quality metadata retained", tokyo["quality"]["eligible"] is True)
check("20. no raw article body stored", all("articleBody" not in c for c in pool_a["candidates"]))
check("21. no localized or audio payload stored", all(
    "localized" not in c and "listen" not in c and "audioURL" not in c
    for c in pool_a["candidates"]
))

reversed_source = dict(source)
reversed_source["candidates"] = list(reversed(source["candidates"]))
pool_reversed = build_mix_pool(reversed_source, DATE, STAMP)
check("22. source ordering cannot change artifact", pool_reversed == pool_a)

changed_stamp = build_mix_pool(source, DATE, "2026-07-27T11:00:00Z", now=STAMP)
check("23. explicit generatedAt changes only timestamp", {
    key: value for key, value in changed_stamp.items() if key != "generatedAt"
} == {
    key: value for key, value in pool_a.items() if key != "generatedAt"
})

try:
    validate_mix_pool({**pool_a, "candidateCount": 99})
    check("24. count mismatch rejected", False)
except MixPoolError:
    check("24. count mismatch rejected", True)

collision_source = json.loads(json.dumps(source))
collision_source["candidates"][1]["id"] = collision_source["candidates"][0]["id"]
try:
    build_mix_pool(collision_source, DATE, STAMP)
    check("25. source id collision rejected", False)
except MixPoolError:
    check("25. source id collision rejected", True)

with tempfile.TemporaryDirectory() as directory:
    path = os.path.join(directory, "2026-07-27.json")
    write_mix_pool(path, pool_a)
    with open(path, encoding="utf-8") as handle:
        written = json.load(handle)
    check("26. written artifact validates", written == pool_a)
    try:
        write_mix_pool(path, pool_a)
        check("27. immutable path refuses overwrite", False)
    except FileExistsError:
        check("27. immutable path refuses overwrite", True)

check("28. fixture reference time is explicit", pool_a["generatedAt"] == STAMP)
check("29. module creates no production output", not os.path.exists(
    os.path.join(HERE, "..", "mix-pools", DATE + ".json")
))
check("30. normalized artifact validates", validate_mix_pool(pool_a) is None)

selected = select_custom_mix(pool_a["candidates"], DATE, ["Japan"])
check("31. generated pool is directly compatible with Phase 2A selector",
      # v3: the region boundary is absolute — a japan-only mix ships its 3 japan
      # stories and reports a shortage instead of crossing into world/US stories.
      len(selected["selectedIds"]) == 3
      and selected["metadata"]["selectedRegionStories"] == 3
      and selected["metadata"]["fallbackSlots"] == 0
      and selected["metadata"]["shortage"] is True
      and selected["metadata"]["unfilledSlots"] == 2)

if FAILURES:
    print("\n" + "\n".join(FAILURES))
    sys.exit(1)
print("\n31/31 PASS")
