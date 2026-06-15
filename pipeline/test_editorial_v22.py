#!/usr/bin/env python3
"""
Editorial Quality v2.2 — entertainment false-positive fix.

Loose entertainment words (anime, movie, celebrity, game…) must NOT auto-reject a story that is
really about politics / society / business / labor / platform policy. Explicit review/recap/profile
FORMATS still always reject. Run: python3 pipeline/test_editorial_v22.py  (stdlib only).
"""
import os, sys, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ranker  # noqa: E402

PASS, FAIL = "✓", "✗"
failures = 0
NOW = datetime.datetime(2026, 6, 17, 12, 0, tzinfo=datetime.timezone.utc)


def check(name, cond, detail=""):
    global failures
    print(f"  {PASS if cond else FAIL} {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures += 1


def cand(title, source="BBC News (World)", category="WORLD"):
    url = "https://example.com/news/" + title.lower().replace(" ", "-")[:30]
    return {"title": title, "source": source, "category": category, "url": url,
            "canonical_url": url, "cluster_size": 2, "cluster_id": title,
            "published_at": "2026-06-17T08:00:00Z",
            "snippet": ("A substantial news snippet with enough genuine reporting body text to clear "
                        "the unknown-source threshold for the writer to draft from. " * 2)}


# ── 1) The reported false positive is fixed ──
print("1) BBC anime/backlash/Trump story is NOT entertainment junk:")
bbc = "Growing backlash in Japan over Trump's use of anime characters"
check("not classified as junk", ranker.editorial_kind(cand(bbc)) == "",
      f"kind={ranker.editorial_kind(cand(bbc))!r}")
check("eligible for selection", ranker.eligible(cand(bbc), NOW, 48))


# ── 2) Explicit review/recap/profile FORMATS still rejected (regression) ──
print("\n2) Explicit reviews / recaps / profiles still rejected:")
for t in ["Album review: the new record is a quiet triumph",
          "Music review: an indie band's comeback single",
          "Movie review: the sequel nobody asked for",
          "Film review: a slow but rewarding drama",
          "TV review: the finale stumbles",
          "Game review: a gorgeous but shallow adventure",
          "Celebrity profile: a day in the life of a pop star",
          "The 20 best songs of the year so far",
          "New album from the chart-topping duo lands Friday",
          "Season three recap: everything you missed"]:
    k = ranker.editorial_kind(cand(t, source="The Verge"))
    check(f"{t[:38]!r} rejected", k != "", f"kind={k!r}")


# ── 3) Pure entertainment (no civic angle) still rejected ──
print("\n3) Pure entertainment without civic context still rejected:")
for t in ["New anime series announced for next year",
          "Marvel's next movie gets a first trailer",
          "X-Men and Masters of the Universe: an entertainment analysis"]:
    k = ranker.editorial_kind(cand(t, source="The Verge"))
    check(f"{t[:38]!r} rejected", k != "", f"kind={k!r}")


# ── 4) Entertainment + civic/business/labor/platform context passes ──
print("\n4) Entertainment-adjacent stories WITH public-impact context stay eligible:")
for t in ["Hollywood writers' strike over AI enters its third week",
          "Spotify's new platform policy sparks artist backlash",
          "Netflix faces an antitrust lawsuit over streaming bundles",
          "Congress debates a law on deepfake political ads",
          "Game studio layoffs spread across the industry amid union fight"]:
    k = ranker.editorial_kind(cand(t, source="The Verge", category="TECH"))
    check(f"{t[:42]!r} eligible", k == "" and ranker.eligible(cand(t, source="The Verge", category="TECH"), NOW, 48),
          f"kind={k!r}")


print(f"\n{'ALL PASS' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
