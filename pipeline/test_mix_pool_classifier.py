#!/usr/bin/env python3
"""Phase 3D-3G.6 — every `MixPoolError` must reach a stable, payload-free category.

Run 30381280477 failed with `{"unknown_mix_pool_error": 1}` and nothing else. The error was
real and specific — `invalid published_at: None` — but the classifier matched camelCase
field names while `build_mix_pool` raises snake_case, so the one message that mattered fell
through to the fallback and the failure was unattributable.

This suite exists so that cannot recur: it ENUMERATES the raise sites in `mix_pool.py` by
reading the source, drives each message form through the classifier, and fails if any of
them lands on `unknown_mix_pool_error`. It also asserts the classifier never returns a URL,
a headline, a summary or a candidate id.

No network, no fixtures beyond the committed one, no candidate content printed.
"""

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import editorial_mix_pool_cli as CLI   # noqa: E402
import mix_pool                        # noqa: E402

FAILURES = []


def check(name, ok, detail=""):
    print(("✓ " if ok else "✗ ") + name + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def only(result):
    """The single category name in a one-error classification."""
    assert len(result) == 1, result
    return next(iter(result))


# ── 1. every raise site in mix_pool.py is classified ──────────────────────────────────
#
# Sampled message forms, one per raise site plus every `validate_mix_pool` error string.
# If `mix_pool.py` grows a new raise, test 1b below notices that this list is short.

FORMS = {
    # build_mix_pool / _parse_time — single raises
    "candidate id collision: a1b2c3":                       "duplicate_candidate_id",
    "invalid published_at: None":                           "invalid_timestamp",
    "invalid published_at: ''":                             "invalid_timestamp",
    "invalid generatedAt: 'nope'":                          "invalid_timestamp",
    "invalid now: 'nope'":                                  "invalid_timestamp",
    "invalid date: '2026-13-99'":                           "invalid_timestamp",
    "source candidates must be a list":                     "schema_invalid",
    # validate_mix_pool — joined errors
    "unsupported schemaVersion":                            "schema_invalid",
    "date must be YYYY-MM-DD":                              "invalid_timestamp",
    "candidates must be a list":                            "schema_invalid",
    "candidateCount does not match candidates":             "schema_invalid",
    "candidate[7] missing id,headline,summary":             "missing_required_field",
    "duplicate candidate id: a1b2c3":                       "duplicate_candidate_id",
    "duplicate canonical URL: https://example.com/a":       "duplicate_canonical_url",
    "candidate[2] invalid URL":                             "schema_invalid",
    "candidate[9] empty required copy":                     "empty_required_copy",
    "candidate[4] invalid published_at: 'x'":               "invalid_timestamp",
}

unknown = []
wrong = []
for message, expected in FORMS.items():
    got = only(CLI.safe_mix_pool_error(Exception(message)))
    if got == "unknown_mix_pool_error":
        unknown.append(message[:40])
    elif got != expected:
        wrong.append(f"{message[:34]} -> {got} (want {expected})")

check("1. no known MixPoolError form falls through to unknown", unknown == [], str(unknown))
check("1b. every known form maps to its intended category", wrong == [], str(wrong))

# The raise sites are counted from source, so a new one shows up as a coverage gap.
SRC = open(os.path.join(HERE, "mix_pool.py"), encoding="utf-8").read()
raise_sites = len(re.findall(r"raise MixPoolError\(", SRC))
check("1c. the raise-site count is the one this suite was written against",
      raise_sites == 5, f"{raise_sites} raises — add the new form to FORMS")

# ── 2-4. the classifier leaks nothing ─────────────────────────────────────────────────

LEAKY = (
    "duplicate canonical URL: https://news.example.com/2026/07/secret-story?token=abc123; "
    "candidate[3] empty required copy; "
    "candidate id collision: 9f8e7d; "
    "invalid published_at: 'Tokyo expands its care programme'"
)
blob = json.dumps(CLI.safe_mix_pool_error(Exception(LEAKY)))

check("2. no URL appears in the classifier output",
      not re.search(r"https?://|www\.|\.com|token=", blob), blob)
check("3. no candidate copy appears in the classifier output",
      not re.search(r"Tokyo|care programme|story", blob), blob)
check("4. no candidate id appears in the classifier output",
      "9f8e7d" not in blob and "a1b2c3" not in blob, blob)
check("4b. the output is category -> count only",
      all(isinstance(k, str) and isinstance(v, int)
          for k, v in CLI.safe_mix_pool_error(Exception(LEAKY)).items()))
check("4c. mixed errors are counted per category, not collapsed",
      CLI.safe_mix_pool_error(Exception(LEAKY)) ==
      {"duplicate_canonical_url": 1, "empty_required_copy": 1,
       "duplicate_candidate_id": 1, "invalid_timestamp": 1})

# ── 5. the REAL reproduced live failure now has a real category ───────────────────────

base = json.load(open(os.path.join(HERE, "fixtures", "mix_pool_scout_candidates.json"),
                     encoding="utf-8"))


def with_bad_timestamp():
    live = json.loads(json.dumps(base))
    live["candidates"][3]["published_at"] = None   # exactly what scout.to_iso returns
    return live


live = with_bad_timestamp()
raw = live["candidates"][3]
probe = dict(raw)
probe["canonical_url"] = mix_pool._canonical_url(probe.get("canonical_url") or probe["url"])
probe["id"] = mix_pool._candidate_id(probe)
reference = mix_pool._parse_time("2026-07-29T09:00:00Z", "now")

check("5. the live row is NOT excluded by mix_pool (unknown age is not stale)",
      mix_pool._exclusion_reason(probe, reference, 48) is None)

try:
    mix_pool.build_mix_pool(json.loads(json.dumps(live)), "2026-07-29", "2026-07-29T09:00:00Z")
    check("5b. an unparseable timestamp still breaks the raw build", False, "built anyway")
except mix_pool.MixPoolError as error:
    check("5b. an unparseable timestamp still breaks the raw build", True)
    check("5c. it now classifies as invalid_timestamp, not unknown",
          CLI.safe_mix_pool_error(error) == {"invalid_timestamp": 1},
          str(CLI.safe_mix_pool_error(error)))

# ── 6. after the adapter fix, the same live-shaped input builds cleanly ───────────────

prepared, dropped = CLI.prepare_scout_source(with_bad_timestamp())
check("6. the adapter drops the unparseable-timestamp row",
      dropped["invalid_timestamp"] == 1, str(dropped))
try:
    pool = mix_pool.build_mix_pool(prepared, "2026-07-29", "2026-07-29T09:00:00Z")
    check("6b. the raw pool then builds successfully", pool["candidateCount"] >= 1,
          str(pool["candidateCount"]))
except mix_pool.MixPoolError as error:
    check("6b. the raw pool then builds successfully", False,
          str(CLI.safe_mix_pool_error(error)))

# ── 7-8. adapter contract ─────────────────────────────────────────────────────────────

ordered = {"candidates": [
    {"title": "A", "snippet": "s", "source": "S", "url": "https://e.com/a",
     "canonical_url": "https://e.com/a", "published_at": "2026-07-29T00:00:00Z"},
    {"title": "", "snippet": "s", "source": "S", "url": "https://e.com/b",
     "canonical_url": "https://e.com/b", "published_at": "2026-07-29T00:00:00Z"},
    {"title": "C", "snippet": "s", "source": "S", "url": "https://e.com/c",
     "canonical_url": "https://e.com/c", "published_at": "2026-07-29T00:00:00Z"},
    {"title": "D", "snippet": "s", "source": "S", "url": "https://e.com/d",
     "canonical_url": "https://e.com/d", "published_at": None},
]}
kept, counts = CLI.prepare_scout_source(ordered)
check("7. adapter filtering preserves input order",
      [row["title"] for row in kept["candidates"]] == ["A", "C"],
      str([row["title"] for row in kept["candidates"]]))
check("8. adapter reports aggregate counts only, no per-row detail",
      counts == {"empty_copy": 1, "unusable_url": 0, "invalid_timestamp": 1,
                 "duplicate_canonical": 0},
      str(counts))
check("8b. the counts blob carries no candidate content",
      not re.search(r"https?://|title|snippet", json.dumps(counts)))

# ── 12. the route stays disconnected ──────────────────────────────────────────────────

edition = os.path.join(HERE, "..", "api", "edition.ts")
check("12. /api/edition remains disconnected",
      open(edition, encoding="utf-8").read().count("selector_not_connected") == 3)

print()
if FAILURES:
    print(f"FAILED {len(FAILURES)}:")
    for name in FAILURES:
        print("  -", name)
    raise SystemExit(1)
print("All Mix Pool classifier checks passed.")
