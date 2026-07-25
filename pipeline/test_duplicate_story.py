#!/usr/bin/env python3
"""Regression tests for the underlying-story duplicate guard (2026-07-25).

The failure this locks down: the first edition after PR #144 shipped BOTH
  "Samsung Galaxy Unpacked 2026: The 6 biggest announcements"   (roundup)
  "Samsung's wider Z Fold 8 feels just right"                   (hands-on)
— one product event consuming two TECH slots. Neither existing gate could see it:
the canon fingerprint vocabulary has no Samsung (both fingerprint to the EMPTY set,
and empty never overlaps), and the scout title-token Jaccard was 0.09 < 0.30.

Covers A–F from the fix spec plus the tie-break and the build-time composition gate.
"""
import contextlib
import datetime
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ranker            # noqa: E402
import build             # noqa: E402
from editorial import duplicate_story, story_identity, story_metadata  # noqa: E402

NOW = datetime.datetime(2026, 7, 25, 9, 0, tzinfo=datetime.timezone.utc)
FAILURES = []


def check(name, ok, detail=""):
    print(("✓ " if ok else "✗ ") + name + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def cand(title, category="TECH", source="The Verge (Mobile)", snippet=None, **kw):
    url = "https://example.com/news/" + "".join(ch if ch.isalnum() else "-" for ch in title.lower())[:60]
    c = {"title": title, "source": source, "category": category,
         "publisher": source.split(" (")[0], "url": url, "canonical_url": url,
         "published_at": "2026-07-25T06:00:00+00:00",
         "snippet": snippet if snippet is not None else
         ("Further reporting and background from correspondents follows in the full "
          "article body, with detail, context and reaction. " * 2),
         "paywalled": False, "source_reliability": "high",
         "cluster_id": url, "cluster_size": 2, "cluster_sources": 1}
    c.update(kw)
    return c


def run_pick(pool, lead, need=4):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        got = ranker.pick_supporting(pool, lead, NOW, 36, history=({}, []), need=need)
    return got, buf.getvalue()


LEAD = cand("Summit opens on maritime security cooperation", category="WORLD",
            source="BBC News (World)")

ROUNDUP = cand("Samsung Galaxy Unpacked 2026: The 6 biggest announcements")
HANDS_ON = cand("Samsung's wider Z Fold 8 feels just right")
FILLER = [
    cand("Lenders tighten mortgage rules for first-time buyers", category="ECONOMY",
         source="BBC News (World)"),
    cand("Museum returns looted artifacts after a decade-long dispute", category="CULTURE",
         source="The Guardian (Culture)"),
    cand("Astronomers map a distant galaxy cluster with a new telescope", category="SCIENCE",
         source="BBC News (Science & Environment)"),
    cand("Hosepipe ban begins as reservoirs fall to record lows", category="SCIENCE",
         source="The Guardian (Science)"),
]

# ── A. the exact production regression ────────────────────────────────────────────────
got, log = run_pick([ROUNDUP, HANDS_ON] + FILLER, LEAD)
titles = [c["title"] for c in got]
n_samsung = sum(1 for t in titles if "Samsung" in t)
check("A. Unpacked roundup + Z Fold hands-on → exactly ONE selected",
      n_samsung == 1 and len(got) == 4, f"samsung={n_samsung} titles={titles}")
check("A2. the rejection log is unambiguous (phase, rule, booleans, both identities)",
      all(k in log for k in ("duplicate rejected:", "selection_phase=", "matched_rule=",
                             "product_story=true", "roundup=", "covers=", "launch_event=",
                             "brand=samsung", "conflicts with selected id=")), log[:500])

# ── B. two differently worded articles about one announcement ─────────────────────────
b1 = cand("Samsung launches the Galaxy Z Fold 8 with a wider display",
          snippet="The new foldable goes on sale worldwide on Friday. " * 4)
b2 = cand("Hands on with Samsung's new foldable, the Z Fold 8",
          snippet="We spent an hour with the device before it reaches stores. " * 4)
got, log = run_pick([b1, b2] + FILLER, LEAD)
check("B. differently worded, same announcement → ONE selected",
      sum(1 for c in got if "Z Fold 8" in c["title"]) == 1, [c["title"] for c in got])

# ── C. consumer launch + unrelated business story about the same company ──────────────
c1 = cand("Samsung launches the Galaxy Z Fold 8 foldable phone",
          snippet="The flagship foldable is available worldwide from Friday. " * 4)
c2 = cand("Samsung's quarterly profit rises on strong chip demand", category="ECONOMY",
          source="BBC News (World)",
          snippet="The company reported higher earnings from its semiconductor division. " * 4)
dup, why, rule = duplicate_story(c1["title"], c1["snippet"], c2["title"], c2["snippet"])
check("C. launch vs unrelated business story → NOT duplicates", not dup, rule)
got, _ = run_pick([c1, c2] + FILLER, LEAD)
check("C2. both may be selected together",
      sum(1 for c in got if "Samsung" in c["title"]) == 2, [c["title"] for c in got])

# ── D. the same launch from different publishers ──────────────────────────────────────
d1 = cand("Samsung Unpacked 2026: all the news from the July foldable launch",
          source="The Verge (Mobile)")
d2 = cand("Everything Samsung announced at Unpacked 2026",
          source="BBC News (Technology)", cluster_id="other-cluster")
got, log = run_pick([d1, d2] + FILLER, LEAD)
check("D. same launch, two publishers → ONE selected",
      sum(1 for c in got if "Unpacked" in c["title"]) == 1, [c["title"] for c in got])

# ── E. a duplicate attempted through the EMERGENCY fill is still rejected ─────────────
# Thin pool: only WORLD fillers + the two Samsung stories, forcing the emergency path.
world = [cand(t, category="WORLD", source="BBC News (World)") for t in
         ["Cyclone nears the coast as evacuations begin",
          "Landslide buries villages in the mountain region",
          "Ferry inquiry opens into last month's sinking"]]
got, log = run_pick([ROUNDUP, HANDS_ON] + world, LEAD)
check("E. emergency fill never reintroduces the duplicate",
      sum(1 for c in got if "Samsung" in c["title"]) == 1
      and "WORLD emergency fill" in log, [c["title"] for c in got])
check("E2. the emergency-phase rejection is logged with its phase and rule",
      ("selection_phase=emergency-fill" in log or "selection_phase=supporting" in log)
      and "matched_rule=" in log, log[:300])

# ── F. the launch boost still applies BEFORE dedup ────────────────────────────────────
launch = cand("Samsung launches the Galaxy Z Fold 8 foldable phone",
              snippet="The flagship foldable is available worldwide from Friday. " * 4)
delta, notes = ranker.mix_static(launch, ({}, []), NOW)
check("F. launch boost is unchanged (+3.5) — dedup does not disable it",
      delta == 3.5 and any("launch+3.5" in n for n in notes), f"{delta} {notes}")
check("F2. metadata still marks it a consumer launch",
      story_metadata(launch["title"], launch["snippet"], reliability="high")["consumer_launch"])

# ── tie-break: the stronger candidate survives a collision ────────────────────────────
strong = cand("Samsung Unpacked 2026: all the news from the July foldable launch",
              cluster_size=4, cluster_sources=3)          # corroborated → higher base score
weak = cand("Samsung's wider Z Fold 8 feels just right", cluster_size=1, cluster_sources=1)
got, _ = run_pick([weak, strong] + FILLER, LEAD)
kept = [c["title"] for c in got if "Samsung" in c["title"]]
check("tie-break keeps the stronger (multi-publisher, higher-scoring) candidate",
      kept == [strong["title"]], kept)
got2, _ = run_pick([weak, strong] + FILLER, LEAD)
check("deterministic: the same collision resolves identically",
      [ranker.short_id(c) for c in got] == [ranker.short_id(c) for c in got2])

# ── build-time composition gate catches it too ────────────────────────────────────────
def sig(n, headline, summary):
    return {"number": n, "headline": headline, "summary": summary,
            "whyItMatters": "It shapes how readers understand the day's technology news."}

five = [sig(1, "Summit opens on maritime security cooperation", "Officials met to discuss patrols."),
        sig(2, "Samsung Galaxy Unpacked 2026: The 6 biggest announcements",
            "The company showed its new foldables and wearables at the July event."),
        sig(3, "Samsung's wider Z Fold 8 feels just right",
            "The new foldable is wider and lighter than last year's model."),
        sig(4, "Lenders tighten mortgage rules", "Banks raised deposit requirements."),
        sig(5, "Astronomers map a distant galaxy cluster", "A new telescope survey found it.")]
errs = build.composition_errors(five)
check("build gate rejects the same pair at composition time",
      any("duplicate underlying story" in e for e in errs), errs)

clean = list(five)
clean[2] = sig(3, "Samsung's quarterly profit rises on strong chip demand",
               "The semiconductor division reported higher earnings for the quarter.")
errs_clean = build.composition_errors(clean)
check("build gate does NOT reject a launch + unrelated business pair",
      not any("duplicate underlying story" in e for e in errs_clean), errs_clean)

# ══ Roundup precedence review (2026-07-25 follow-up) ══════════════════════════════════
# A generic roundup must NOT merge two materially unrelated product lines of one brand.

def dup(a, b, a_s="", b_s=""):
    return duplicate_story(a, a_s, b, b_s)

ok, why, rule = dup("Samsung Galaxy Unpacked 2026: The 6 biggest announcements",
                    "Samsung's wider Z Fold 8 feels just right")
check("R-A. Unpacked roundup + Z Fold hands-on → still duplicate",
      ok and rule == "same-product-family", rule)

ok, why, rule = dup("Apple's iPhone 18 event: the 5 biggest announcements",
                    "Hands on with Apple's new MacBook Pro")
check("R-B. iPhone roundup + MacBook hands-on (no shared event) → NOT duplicate",
      not ok and rule == "distinct-product-families", f"{ok} {rule}")

ok, why, rule = dup("Apple's September event: everything announced, from the iPhone 18 "
                    "to the new MacBook Pro",
                    "Hands on with Apple's new MacBook Pro")
check("R-C. broad roundup that NAMES the MacBook + MacBook story → duplicate",
      ok and rule == "roundup-covers-family", f"{ok} {rule}")

ok, why, rule = dup("Samsung Galaxy Watch 8 recap: everything that changed",
                    "Samsung's wider Z Fold 8 feels just right")
check("R-D. same brand, unrelated lines, one says 'recap' → NOT duplicate",
      not ok and rule == "distinct-product-families", f"{ok} {rule}")

ok, why, rule = dup("Galaxy Z Fold 8: the biggest changes Samsung announced",
                    "Hands on with Samsung's Galaxy Z Fold 8")
check("R-E. same family, roundup + hands-on → duplicate",
      ok and rule == "same-product-family", f"{ok} {rule}")

ok, why, rule = dup("Everything Samsung announced at Unpacked 2026",
                    "Samsung's Unpacked keynote: the Galaxy Watch 8 steals the show")
check("R-F. shared explicit launch event still wins over unrelated lines",
      ok and rule == "same-launch-event", f"{ok} {rule}")

# the DECISION inputs must be printable and unambiguous in the log
sid = story_identity("Samsung Galaxy Unpacked 2026: The 6 biggest announcements", "")
check("R-G. identity exposes product_story / roundup / covers explicitly",
      sid["is_product_story"] is True and sid["is_roundup"] is True
      and "galaxy" in sid["covered_families"], sid)
log_line = ranker._id_str(sid)
check("R-H. log string spells out the booleans the guard reads",
      "product_story=true" in log_line and "roundup=true" in log_line
      and "covers=" in log_line and "launch_event=unpacked" in log_line, log_line)

print()
if FAILURES:
    print(f"{len(FAILURES)} CHECK(S) FAILED")
    sys.exit(1)
print("ALL PASS")
