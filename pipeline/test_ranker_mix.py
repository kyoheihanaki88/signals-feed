#!/usr/bin/env python3
"""Fixture tests for the Morning Mix balance in ranker.py — commit 3.

14 scenarios: launch boost, discovery boost, earnings penalty, same-country penalty +
both waivers, third-conflict-tone penalty, WORLD cap, dominant-news override (and its
hard limit), discovery slot (reserve + never force), history penalty with escalation,
thin-day exactly-N + explicit relaxation logging, and determinism.
"""
import contextlib
import datetime
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ranker  # noqa: E402

NOW = datetime.datetime(2026, 7, 24, 9, 0, tzinfo=datetime.timezone.utc)
EMPTY_HIST = ({}, [])
FAILURES = []


def check(name, ok, detail=""):
    print(("✓ " if ok else "✗ ") + name + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def cand(title, category="WORLD", source="BBC News (World)", cluster_size=2,
         cluster_sources=1, snippet=None, reliability="high"):
    url = "https://example.com/news/" + "".join(ch if ch.isalnum() else "-" for ch in title.lower())[:60]
    return {"title": title, "source": source, "category": category,
            "publisher": source.split(" (")[0], "url": url, "canonical_url": url,
            "published_at": "2026-07-24T06:00:00+00:00",
            "snippet": snippet if snippet is not None else
            ("Further reporting and background from correspondents follows in the full "
             "article body, with detail, context and reaction from those involved. " * 2),
            "paywalled": False, "source_reliability": reliability,
            "cluster_id": url, "cluster_size": cluster_size, "cluster_sources": cluster_sources}


def run_pick(pool, lead, history=EMPTY_HIST, need=4):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        got = ranker.pick_supporting(pool, lead, NOW, 36, history=history, need=need)
    return got, buf.getvalue()


NEUTRAL_LEAD = cand("Summit opens on maritime security cooperation")

# ── 1–3: static boosts / penalties ─────────────────────────────────────────────────────
launch = cand("Acme launches its first smartphone for the mass market", category="TECH",
              source="The Verge (Mobile)", snippet="The device is available worldwide from Friday.")
d, notes = ranker.mix_static(launch, EMPTY_HIST, NOW)
check("1. consumer launch gets +3.5 (logged)", d == 3.5 and any("launch+3.5" in n for n in notes), str(notes))

disc = cand("Astronomers discover a distant galaxy cluster", category="SCIENCE")
d, notes = ranker.mix_static(disc, EMPTY_HIST, NOW)
check("2. discovery value gets +1.5 (logged)", d == 1.5 and any("discovery+1.5" in n for n in notes), str(notes))

earn = cand("Retailer's quarterly profit rises 12 percent", category="ECONOMY")
d, notes = ranker.mix_static(earn, EMPTY_HIST, NOW)
check("3. routine earnings gets -1.5 (logged)", d == -1.5 and any("earnings-1.5" in n for n in notes), str(notes))

# ── 4–6: same-country penalty and its waivers ──────────────────────────────────────────
jp_disaster = cand("Japan evacuates towns as the eruption continues")
chosen_jp = [cand("Japan raises its alert level after a volcanic eruption killed two")]
d, notes = ranker.mix_dynamic(jp_disaster, chosen_jp, NOW)
check("4. second same-country same-family story gets -3.0", d == -3.0 and any("same-country" in n for n in notes), str(notes))

jp_policy = cand("Japan's government plans a new industrial strategy")
d, notes = ranker.mix_dynamic(jp_policy, chosen_jp, NOW)
check("5. country penalty WAIVED when event family materially different",
      d == 0.0 and any("waived" in n and "family" in n for n in notes), str(notes))

jp_dom = cand("Japan evacuates towns as the eruption continues", cluster_size=5, cluster_sources=3)
d, notes = ranker.mix_dynamic(jp_dom, chosen_jp, NOW)
check("6. country penalty WAIVED for dominant multi-publisher story",
      d == 0.0 and any("waived" in n and "dominant" in n for n in notes), str(notes))

# ── 7: third conflict tone ─────────────────────────────────────────────────────────────
two_conflicts = [cand("Airstrikes hit the eastern city overnight"),
                 cand("Troops advance along the frontline positions")]
third = cand("Shelling intensifies near the border villages")
d, notes = ranker.mix_dynamic(third, two_conflicts, NOW)
check("7. THIRD conflict-toned story gets -4.0", d == -4.0 and any("third-conflict-tone" in n for n in notes), str(notes))
d, notes = ranker.mix_dynamic(third, two_conflicts[:1], NOW)
check("7b. a second conflict story is NOT penalized", d == 0.0, str(notes))

# ── 8: WORLD hard cap ──────────────────────────────────────────────────────────────────
world_pool = [cand("Cyclone nears the coast as evacuations begin"),
              cand("Landslide buries villages in the mountain region"),
              cand("Ferry inquiry opens into last month's sinking"),
              cand("Officials report record migrant crossings at the border")]
other_pool = [cand("Chipmaker opens a giant new factory complex", category="TECH", source="The Verge"),
              cand("Lenders tighten mortgage rules for first-time buyers", category="ECONOMY"),
              cand("Museum returns looted artifacts after a decade-long dispute", category="CULTURE")]
got, log = run_pick(world_pool + other_pool, NEUTRAL_LEAD)
n_world = sum(1 for c in got if c["category"] == "WORLD")
check("8. WORLD capped at 2 incl. the lead (1 supporting WORLD)", len(got) == 4 and n_world == 1,
      f"world={n_world} len={len(got)}")

# ── 9: dominant-news override allows a LOGGED 3rd WORLD ────────────────────────────────
dominant = cand("Volcanic eruption forces a mass evacuation of the capital",
                cluster_size=4, cluster_sources=3)
# the two ordinary WORLD stories outrank the dominant one (importance cue), so the cap is
# already full when the dominant story is considered — the override is the only path in
pool9 = [cand("Government unveils a sweeping housing reform", cluster_size=4),
         cand("Court ruling reshapes policing oversight rules", cluster_size=4),
         dominant,
         cand("Chipmaker opens a giant new factory complex", category="TECH", source="The Verge"),
         cand("Lenders tighten mortgage rules for first-time buyers", category="ECONOMY")]
got, log = run_pick(pool9, NEUTRAL_LEAD)
n_world = sum(1 for c in got if c["category"] == "WORLD")
check("9. 3rd WORLD allowed only via dominant-news override, explicitly logged",
      n_world == 2 and any(c is dominant for c in got) and "WORLD cap override" in log,
      f"world={n_world} log_has_override={'WORLD cap override' in log}")

# ── 10: override is bounded — NEVER a 4th WORLD, never unlimited ───────────────────────
pool10 = [cand(t, cluster_size=5, cluster_sources=3) for t in
          ["Cyclone nears the coast as evacuations begin",
           "Landslide buries villages in the mountain region",
           "Ferry inquiry opens into last month's sinking",
           "Officials report record migrant crossings at the border",
           "Volcanic eruption forces a mass evacuation of the capital",
           "Bridge collapse inquiry hears from engineers"]]
got, log = run_pick(pool10, NEUTRAL_LEAD)
check("10. all-WORLD pool: normal cap + ONE override, then only the emergency completion "
      "fallback fills the rest (edition still gets five; override never repeats)",
      len(got) == 4 and log.count("WORLD cap override") == 1
      and log.count("WORLD emergency fill") == 2
      and sum(1 for c in got if c.get("_mix_tag") == "emergency-fill") == 2,
      f"len={len(got)} overrides={log.count('WORLD cap override')} fills={log.count('WORLD emergency fill')}")

# ── 11–12: discovery slot ──────────────────────────────────────────────────────────────
pool11 = [launch] + world_pool[:2] + [other_pool[1], other_pool[2]]
got, log = run_pick(pool11, NEUTRAL_LEAD)
check("11. discovery slot reserves a supporting slot for the qualifying launch",
      any(c is launch for c in got) and "mix-pick[discovery-slot]" in log, log[:200])

got, log = run_pick(world_pool[:2] + other_pool, NEUTRAL_LEAD)
check("12. no qualifying launch/discovery → nothing forced, decision logged",
      "not forcing one" in log and len(got) == 4, f"len={len(got)}")

# ── 13: history penalty with escalation, exact match logged ────────────────────────────
ua = cand("Airstrikes pound the eastern front near Kharkiv in Ukraine")
h1 = ({("Ukraine", "conflict"): ["2026-07-23"]}, ["2026-07-23", "2026-07-22", "2026-07-21"])
h2 = ({("Ukraine", "conflict"): ["2026-07-23", "2026-07-22"]}, ["2026-07-23", "2026-07-22", "2026-07-21"])
h3 = ({("Ukraine", "conflict"): ["2026-07-23", "2026-07-22", "2026-07-21"]}, ["2026-07-23", "2026-07-22", "2026-07-21"])
hgap = ({("Ukraine", "conflict"): ["2026-07-22"]}, ["2026-07-23", "2026-07-22", "2026-07-21"])
d1 = ranker.mix_static(ua, h1, NOW)
d2 = ranker.mix_static(ua, h2, NOW)
d3 = ranker.mix_static(ua, h3, NOW)
dg = ranker.mix_static(ua, hgap, NOW)
check("13. history penalty -2.5 (prev day), escalating -3.5 / -4.5 for 2 / 3 consecutive days",
      d1[0] == -2.5 and d2[0] == -3.5 and d3[0] == -4.5,
      f"{d1[0]} {d2[0]} {d3[0]}")
check("13b. exact history match logged (country/family + edition dates)",
      any("Ukraine/conflict in 2026-07-23, 2026-07-22" in n for n in d2[1]), str(d2[1]))
check("13c. non-consecutive appearance (gap day) → no penalty", dg[0] == 0.0, str(dg[1]))
check("13d. load_history reads at most 3 committed dates, all before today",
      (lambda h: len(h[1]) <= 3 and all(x < "2026-07-24" for x in h[1]))(
          ranker.load_history(os.path.join(HERE, "..", "editions"), datetime.date(2026, 7, 24))))

# ── 14: thin day still fills, relaxation is EXPLICIT, and selection is deterministic ───
thin = [cand(t, category="TECH", source="The Verge") for t in
        ["Chipmaker opens a giant new factory complex",
         "Streaming service tightens password sharing rules",
         "Ride-hailing firm expands to three more cities",
         "Battery maker doubles output at its flagship plant"]]
got, log = run_pick(thin, NEUTRAL_LEAD)
check("14. thin day returns exactly `need` with EXPLICIT logged non-WORLD relaxation",
      len(got) == 4 and "category cap relaxed to 3" in log and "category cap relaxed to 4" in log)
ids_a = [ranker.short_id(c) for c in run_pick(world_pool + other_pool, NEUTRAL_LEAD)[0]]
ids_b = [ranker.short_id(c) for c in run_pick(world_pool + other_pool, NEUTRAL_LEAD)[0]]
check("14b. deterministic — same inputs, same five, same order", ids_a == ids_b, f"{ids_a} vs {ids_b}")

print()
if FAILURES:
    print(f"{len(FAILURES)} CHECK(S) FAILED")
    sys.exit(1)
print("ALL PASS")
