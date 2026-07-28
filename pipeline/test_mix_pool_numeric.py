#!/usr/bin/env python3
"""Phase 3D-3A.1 — the Mix Pool numeric contract.

Proves that every value the schema ACCEPTS has exactly one canonical representation, and
that every value the real producer PRODUCES is accepted. Those two halves together are
what makes the content hash reproducible in TypeScript later.

Nothing here writes a fixture or a golden.
"""
import copy
import datetime as dt
import hashlib
import itertools
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import mix_pool                                                        # noqa: E402
import mix_pool_schema as S                                            # noqa: E402
import ranker                                                          # noqa: E402
from mix_pool_schema import (                                          # noqa: E402
    MixPoolNumericError,
    SCORE_MAX,
    SCORE_MAX_DECIMALS,
    SCORE_MIN,
    SCORE_MIN_MAGNITUDE,
    freeze_artifact,
    normalize_score,
    validate_artifact,
)

FIXTURE = os.path.join(HERE, "fixtures", "mix_pool_scout_candidates.json")
DATE = "2026-07-27"
GENERATED_AT = "2026-07-27T09:00:00Z"

# The values pinned before any change was made.
BASELINE = {
    "poolIdentity": "38d9c03d43bd5e94eb0205387b363d9dde795283bbac277d8ec1847d45806a3d",
    "canonicalSha256": "41f9bb608da21a1aed737c21fdb5f50fad3c69aea6593468e93d8c49615a626b",
    "canonicalLength": 5499,
    "serializeSha256": "2ea69a392d31833c0b9f5680bbf189204e1564590304c19df56052a63ca14464",
}

FAILURES = []


def check(name, ok, detail=""):
    print(("✓ " if ok else "✗ ") + name + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def rejects(value, name, path="$.baseScore"):
    try:
        normalize_score(value, path)
    except MixPoolNumericError as exc:
        check(name, True)
        return str(exc)
    check(name, False, f"{value!r} was accepted")
    return ""


def source():
    with open(FIXTURE, encoding="utf-8") as handle:
        return json.load(handle)


def real_artifact():
    src = source()
    pool = mix_pool.build_mix_pool(src, DATE, GENERATED_AT, now=GENERATED_AT)
    return src, freeze_artifact(
        pool, source_input=src, source="offline-fixture", reference_at=GENERATED_AT
    )


# ── baseScore: acceptance and normalization ───────────────────────────────────────────

check("1. ordinary fractional score accepted", normalize_score(11.753704) == 11.753704)
check("2. exactly 6 fractional digits accepted", normalize_score(5.744444) == 5.744444)
rejects(1.1234567, "3. more than 6 fractional digits rejected")
check("4. integral float normalized to int", normalize_score(7.0) == 7
      and type(normalize_score(7.0)) is int)
check("5. integer input stays an integer", normalize_score(7) == 7
      and type(normalize_score(7)) is int)
check("6. trailing zeroes normalize to the shortest value",
      normalize_score(7.500000) == 7.5 and json.dumps(normalize_score(7.5)) == "7.5")
check("7. negative zero normalizes to integer 0",
      normalize_score(-0.0) == 0 and type(normalize_score(-0.0)) is int
      and json.dumps(normalize_score(-0.0)) == "0")
check("8. positive float zero normalizes to integer 0",
      type(normalize_score(0.0)) is int and json.dumps(normalize_score(0.0)) == "0")
check("9. integer zero stays integer 0", type(normalize_score(0)) is int)
check("10. 0.0001 accepted without exponent",
      json.dumps(normalize_score(0.0001)) == "0.0001")
check("11. -0.0001 accepted (negative scores are in range)",
      json.dumps(normalize_score(-0.0001)) == "-0.0001")
rejects(0.00001, "12. 0.00001 rejected (exponent domain)")
rejects(0.000001, "13. 0.000001 rejected (exponent domain)")
rejects(1e16, "14. exponent-producing large value rejected")
rejects(float("nan"), "15. NaN rejected")
rejects(float("inf"), "16. +infinity rejected")
rejects(float("-inf"), "17. -infinity rejected")
rejects(True, "18. boolean rejected")
rejects("7.5", "19. string rejected")
rejects(float(SCORE_MIN) - 1, "20. below score range rejected")
rejects(float(SCORE_MAX) + 1, "21. above score range rejected")

message = rejects(1.1234567, "22a. over-precision rejected", "candidates[jp-1].baseScore")
check("22. error carries the candidate and field path",
      "candidates[jp-1].baseScore" in message, message)

original = {"baseScore": 7.0, "quality": {"clusterSize": 2}}
snapshot = copy.deepcopy(original)
normalize_score(original["baseScore"])
check("23. caller-owned input is not mutated", original == snapshot)

# ── integral fields ───────────────────────────────────────────────────────────────────

for field, good in (("candidateCount", 6), ("clusterSize", 2),
                    ("clusterSources", 1), ("sourceRisk", 0)):
    errors = []
    S.validate_integral(good, field, f"$.{field}", errors)
    check(f"24. {field}: valid integer accepted", errors == [], str(errors))

    errors = []
    S.validate_integral(True, field, f"$.{field}", errors)
    check(f"25. {field}: boolean rejected", len(errors) == 1 and "boolean" in errors[0])

    errors = []
    S.validate_integral(1.0, field, f"$.{field}", errors)
    check(f"26. {field}: integral float rejected", len(errors) == 1)

    bounds = S.INTEGRAL_FIELD_RANGES[field]
    errors = []
    S.validate_integral(bounds[0] - 1, field, f"$.{field}", errors)
    check(f"27. {field}: below minimum rejected", len(errors) == 1, str(errors))

    errors = []
    S.validate_integral(bounds[1] + 1, field, f"$.{field}", errors)
    check(f"28. {field}: above maximum rejected", len(errors) == 1, str(errors))

    errors = []
    S.validate_integral(1.5, field, f"$.candidates[2].quality.{field}", errors)
    check(f"29. {field}: field path appears in the error",
          errors and f"candidates[2].quality.{field}" in errors[0], str(errors))

# ── canonical serialization ───────────────────────────────────────────────────────────

src, artifact = real_artifact()
canonical = S.canonical_bytes(artifact)

check("30. repeated serialization is byte-identical",
      S.canonical_bytes(artifact) == canonical)

shuffled = {key: artifact[key] for key in reversed(list(artifact))}
check("31. reordered object keys produce identical canonical bytes",
      S.canonical_bytes(shuffled) == canonical)

reversed_pool = mix_pool.build_mix_pool(src, DATE, GENERATED_AT, now=GENERATED_AT)
reversed_pool["candidates"] = list(reversed(reversed_pool["candidates"]))
reversed_artifact = freeze_artifact(
    reversed_pool, source_input=src, source="offline-fixture", reference_at=GENERATED_AT
)
check("32. candidate ordering is normalized by id before hashing",
      reversed_artifact["poolIdentity"] == artifact["poolIdentity"])

check("33. unicode stays unescaped UTF-8",
      S.canonical_bytes({"t": "café 日本"}) == '{"t":"café 日本"}'.encode("utf-8"))
check("34. compact separators are unchanged",
      S.canonical_bytes({"a": 1, "b": 2}) == b'{"a":1,"b":2}')

text = canonical.decode("utf-8")
scores = [json.dumps(c["baseScore"]) for c in artifact["candidates"]]
check("35. no score serializes in exponent notation",
      all("e" not in s.lower() for s in scores), str(scores))
check("36. no score serializes as negative zero",
      all(s not in ("-0", "-0.0") for s in scores), str(scores))
check("37. no score serializes with a trailing .0",
      all(not s.endswith(".0") for s in scores), str(scores))

renormalized = freeze_artifact(
    mix_pool.build_mix_pool(src, DATE, GENERATED_AT, now=GENERATED_AT),
    source_input=src, source="offline-fixture", reference_at=GENERATED_AT,
)
check("38. canonicalization is idempotent", S.canonical_bytes(renormalized) == canonical)
check("39. pool_identity is stable across repeated calls",
      S.pool_identity(artifact["candidates"]) == S.pool_identity(artifact["candidates"]))

# 40. normalization happens BEFORE hashing — proven with an integral-float score.
integral_pool = mix_pool.build_mix_pool(src, DATE, GENERATED_AT, now=GENERATED_AT)
integral_pool["candidates"][0]["baseScore"] = 7.0
integral_artifact = freeze_artifact(
    integral_pool, source_input=src, source="offline-fixture", reference_at=GENERATED_AT
)
frozen_score = next(c["baseScore"] for c in integral_artifact["candidates"]
                    if c["id"] == integral_pool["candidates"][0]["id"])
check("40. an integral float is an int in the frozen artifact (so the hash is portable)",
      type(frozen_score) is int and frozen_score == 7
      and b'"baseScore":7,' in S.canonical_bytes(integral_artifact),
      repr(frozen_score))

# 41. an externally supplied NON-canonical artifact is rejected.
external = copy.deepcopy(artifact)
external["candidates"][0]["baseScore"] = 7.0
external["poolIdentity"] = S.pool_identity(external["candidates"])
result = validate_artifact(external)
check("41. validate_artifact rejects a non-canonical 7.0 from an external artifact",
      not result["valid"] and any("baseScore" in e for e in result["errors"]),
      str(result["errors"])[:160])

# ── regression: the pinned baseline ───────────────────────────────────────────────────

check("B1. poolIdentity unchanged", artifact["poolIdentity"] == BASELINE["poolIdentity"],
      artifact["poolIdentity"])
check("B2. sha256(canonical_bytes) unchanged",
      hashlib.sha256(canonical).hexdigest() == BASELINE["canonicalSha256"])
check("B3. canonical byte length unchanged", len(canonical) == BASELINE["canonicalLength"],
      str(len(canonical)))
check("B4. sha256(serialize) unchanged",
      hashlib.sha256(S.serialize(artifact)).hexdigest() == BASELINE["serializeSha256"])
check("B5. the real frozen artifact is still valid", validate_artifact(artifact)["valid"],
      str(validate_artifact(artifact)["errors"])[:200])

# ── regression: existing validation still works ───────────────────────────────────────

def broken(mutate):
    copy_ = copy.deepcopy(artifact)
    mutate(copy_)
    return validate_artifact(copy_)


def dup_id(a):
    a["candidates"][1]["id"] = a["candidates"][0]["id"]


def dup_url(a):
    a["candidates"][1]["url"] = a["candidates"][0]["url"]


check("42. duplicate candidate id still rejected", not broken(dup_id)["valid"])
check("43. duplicate canonical URL still rejected", not broken(dup_url)["valid"])
check("44. forbidden candidate key still rejected",
      not broken(lambda a: a["candidates"][0].update({"rawBody": "x"}))["valid"])
check("45. local-path leakage still rejected",
      not broken(lambda a: a.update({"provenance": {**a["provenance"],
                                                    "source": "/Users/me/pool.json"}}))["valid"])
check("46. required evidence validation still works",
      not broken(lambda a: a["candidates"][0]["regionMemberships"][0].update(
          {"strength": "primary", "evidence": []}))["valid"])
check("47. canonical taxonomy validation still works",
      not broken(lambda a: a["candidates"][0].update({"category": "NOT_A_CATEGORY"}))["valid"])
check("48. candidateCount validation still works",
      not broken(lambda a: a.update({"candidateCount": 99}))["valid"])
check("49. version validation still works",
      not broken(lambda a: a.update({"schemaVersion": 99}))["valid"])
check("50. boolean fields still require real booleans",
      not broken(lambda a: a["candidates"][0].update({"eligible": 1}))["valid"])

# ── producer sweep: every real score satisfies the contract ───────────────────────────

now = dt.datetime(2026, 7, 27, 9, tzinfo=dt.timezone.utc)
violations = []
observed = []
combos = 0
for cs, rel, cat, pay, hrs, live, imp in itertools.product(
    (1, 2, 4, 8),
    ("high", "medium", "low", "unknown"),
    ("TECH", "WORLD", "CULTURE", "OTHER", "SCIENCE", "ECONOMY", "HEALTH"),
    (False, True),
    (0, 1, 6, 12, 36, 72, 240),
    (False, True),
    (None, "major"),
):
    candidate = {
        "url": "https://example.com/a",
        "published_at": (now - dt.timedelta(hours=hrs)).isoformat().replace("+00:00", "Z"),
        "cluster_size": cs, "source_reliability": rel,
        "title": ("LIVE: rolling updates" if live else "A headline"),
        "snippet": "s", "source": "S", "category": cat, "paywalled": pay,
    }
    if imp:
        candidate["importance"] = imp
    combos += 1
    score = round(float(ranker.base_score(candidate, now, 36)), 6)   # the REAL producer
    observed.append(score)
    if not math.isfinite(score):
        violations.append((candidate, score, "not finite"))
        continue
    try:
        normalized = normalize_score(score)
    except MixPoolNumericError as exc:
        violations.append((cs, rel, cat, pay, hrs, live, imp, score, str(exc)))
        continue
    rendered = json.dumps(normalized)
    if "e" in rendered.lower() or rendered.endswith(".0") or rendered in ("-0", "-0.0"):
        violations.append((cs, rel, cat, pay, hrs, live, imp, score, rendered))

check(f"P1. all {combos} real producer scores satisfy the numeric contract",
      not violations, str(violations[:3]))
check("P2. the observed producer range sits inside the approved range",
      float(SCORE_MIN) <= min(observed) and max(observed) <= float(SCORE_MAX),
      f"observed [{min(observed)}, {max(observed)}] vs approved [{SCORE_MIN}, {SCORE_MAX}]")
check("P3. the sweep actually reaches an integral score (the hazard is real)",
      any(float(v).is_integer() for v in observed),
      "no integral score produced — the hazard would be unreachable")
print(f"    producer sweep: {combos} combinations, observed range "
      f"[{min(observed)}, {max(observed)}], "
      f"{sum(1 for v in observed if float(v).is_integer())} integral scores")

print()
print(f"{len(FAILURES)} failure(s)" if FAILURES else "all checks passed")
for name in FAILURES:
    print("  ✗", name)
sys.exit(1 if FAILURES else 0)
