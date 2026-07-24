#!/usr/bin/env python3
"""Fixture tests for the WORLD emergency completion fallback (PR #144 follow-up).

Balance preferences must never fail an edition that has five credible distinct stories.
Proves: WORLD-heavy thin day completes to exactly five with the fallback logged; healthy
pools never touch it; the dominant-news override still handles the 3rd WORLD without it;
the fallback preserves topic/id/URL dedup and junk gates; output is byte-identical."""
import contextlib
import datetime
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ranker  # noqa: E402

NOW = datetime.datetime(2026, 7, 24, 9, 0, tzinfo=datetime.timezone.utc)
EMERGENCY_LOG = "WORLD emergency fill: balance relaxed only to avoid a balance-caused failed edition"
FAILURES = []


def check(name, ok, detail=""):
    print(("✓ " if ok else "✗ ") + name + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def cand(title, category="WORLD", source="BBC News (World)", **kw):
    url = "https://example.com/news/" + "".join(ch if ch.isalnum() else "-" for ch in title.lower())[:60]
    c = {"title": title, "source": source, "category": category,
         "publisher": source.split(" (")[0], "url": url, "canonical_url": url,
         "published_at": "2026-07-24T06:00:00+00:00",
         "snippet": ("Further reporting and background from correspondents follows in the "
                     "full article body, with detail, context and reaction. " * 2),
         "paywalled": False, "source_reliability": "high",
         "cluster_id": url, "cluster_size": 2, "cluster_sources": 1}
    c.update(kw)
    return c


def run_pick(pool, lead, need=4):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        got = ranker.pick_supporting(pool, lead, NOW, 36, history=({}, []), need=need)
    return got, buf.getvalue()


LEAD = cand("Summit opens on maritime security cooperation")

# 15 distinct WORLD titles with non-overlapping topic fingerprints
WORLD_TITLES = [
    "Cyclone nears the coast as evacuations begin",
    "Landslide buries villages in the mountain region",
    "Ferry inquiry opens into last month's sinking",
    "Officials report record migrant crossings at the border",
    "Bridge collapse inquiry hears from engineers",
    "Volcanic eruption forces a mass evacuation of the capital",
    "Heat warnings issued as temperatures soar across the plains",
    "Rescue teams reach the stranded expedition group",
    "Mine accident traps workers underground overnight",
    "Grain shipments resume through the northern corridor",
    "Fishing fleet dispute escalates over coastal waters",
    "Historic drought-hit reservoir reopens to farmers",
    "New rail link connects the two largest cities",
    "Census results show a sharp shift toward the suburbs",
    "Airline grounds its oldest jets after an inspection",
]

# ── 1. WORLD-heavy thin day: exactly five, no balance failure, fallback logged ─────────
pool1 = [cand(t) for t in WORLD_TITLES] + \
        [cand("Chipmaker opens a giant new factory complex", category="TECH", source="The Verge")]
got, log = run_pick(pool1, LEAD)
n_world = sum(1 for c in got if c["category"] == "WORLD")
check("1. 15 WORLD + 1 TECH → exactly four supporting selected (five-story edition holds)",
      len(got) == 4, f"len={len(got)}")
check("1b. emergency fill used and logged with the exact required line",
      EMERGENCY_LOG in log and "mix-pick[emergency-fill]" in log)
check("1c. WORLD exceeds three ONLY via the emergency path (3 supporting WORLD + lead)",
      n_world == 3 and log.count(EMERGENCY_LOG) == 2, f"world={n_world} fills={log.count(EMERGENCY_LOG)}")
check("1d. emergency picks are distinct topics and distinct ids/urls",
      len({ranker.short_id(c) for c in got + [LEAD]}) == 5
      and len({c["canonical_url"] for c in got + [LEAD]}) == 5)

# ── 2. healthy mixed pool: WORLD stays capped at 2, no emergency fill ──────────────────
pool2 = [cand(t) for t in WORLD_TITLES[:4]] + [
    cand("Chipmaker opens a giant new factory complex", category="TECH", source="The Verge"),
    cand("Lenders tighten mortgage rules for first-time buyers", category="ECONOMY"),
    cand("Museum returns looted artifacts after a decade-long dispute", category="CULTURE"),
]
got, log = run_pick(pool2, LEAD)
check("2. healthy pool → WORLD capped at 2 incl. lead, emergency fill NOT used",
      sum(1 for c in got if c["category"] == "WORLD") == 1 and len(got) == 4
      and EMERGENCY_LOG not in log)

# ── 3. dominant-news day: 3rd WORLD via override, fallback unused ──────────────────────
dominant = cand("Volcanic eruption forces a mass evacuation of the capital",
                cluster_size=4, cluster_sources=3)
pool3 = [cand("Government unveils a sweeping housing reform", cluster_size=4),
         cand("Court ruling reshapes policing oversight rules", cluster_size=4),
         dominant,
         cand("Chipmaker opens a giant new factory complex", category="TECH", source="The Verge"),
         cand("Lenders tighten mortgage rules for first-time buyers", category="ECONOMY")]
got, log = run_pick(pool3, LEAD)
check("3. 3rd WORLD still enters via dominant override; emergency fallback unused",
      any(c is dominant for c in got) and "WORLD cap override" in log
      and EMERGENCY_LOG not in log)

# ── 4. emergency fill preserves every content gate ─────────────────────────────────────
dup_url = cand("Cyclone nears the coast as evacuations begin")            # same URL/id as pool twin
dup_topic = cand("Ukraine repels drone attacks on the eastern front")
dup_topic2 = cand("Ukraine hit by new drone attacks near the front",      # fingerprint-overlaps dup_topic
                  url="https://example.com/news/ua-2", canonical_url="https://example.com/news/ua-2",
                  cluster_id="https://example.com/news/ua-2")
junk = cand("Prime day deal: the best gadget discounts this week")        # editorial junk
bad_url = cand("A story with no real article page", url="https://example.com/",
               canonical_url="https://example.com/", cluster_id="x-bad")
pool4 = [cand(t) for t in WORLD_TITLES[:3]] + [dup_url, dup_topic, dup_topic2, junk, bad_url]
got, log = run_pick(pool4, LEAD)
ids = [ranker.short_id(c) for c in got]
check("4. emergency fill rejects duplicate URL/id, duplicate topic, junk, and bad URLs",
      len(ids) == len(set(ids))
      and sum(1 for c in got if c in (dup_topic, dup_topic2)) <= 1
      and junk not in got and bad_url not in got
      and len({c["canonical_url"] for c in got}) == len(got))
check("4b. it still completes five when enough clean distinct stories exist",
      len(got) == 4 and EMERGENCY_LOG in log, f"len={len(got)}")

# ── 5. determinism: byte-identical output and logs ─────────────────────────────────────
g1, l1 = run_pick(pool1, LEAD)
g2, l2 = run_pick(pool1, LEAD)
check("5. same inputs → byte-identical picks and logs",
      [ranker.short_id(c) for c in g1] == [ranker.short_id(c) for c in g2] and l1 == l2)

print()
if FAILURES:
    print(f"{len(FAILURES)} CHECK(S) FAILED")
    sys.exit(1)
print("ALL PASS")
