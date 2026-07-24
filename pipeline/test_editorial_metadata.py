#!/usr/bin/env python3
"""Unit tests for Morning Mix story metadata (editorial.py) — commit 2.

Covers the consumer-launch gate (required qualifying set + all exclusions), brand
neutrality, country/region/event_family/tone assignment, discovery_value, and the
"missing metadata is neutral, never invalidating" rule.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from editorial import story_metadata, is_consumer_launch, EVENT_FAMILIES

FAILURES = []

def check(name, ok):
    print(("✓ " if ok else "✗ ") + name)
    if not ok:
        FAILURES.append(name)

def md(title, snippet="", **kw):
    return story_metadata(title, snippet, **kw)


# ── consumer launch: qualifying cases ──────────────────────────────────────────────────
m = md("Samsung launches the Galaxy Z Fold 8 foldable phone",
       "The new flagship foldable goes on sale worldwide this week.", reliability="high")
check("official Samsung phone launch qualifies",
      m["consumer_launch"] and m["event_family"] == "consumer_launch" and m["tone"] == "forward_looking")

m = md("Google releases Android 17 with a redesigned lock screen",
       "The operating system update rolls out to Pixel devices today.", reliability="high")
check("major OS release qualifies", m["consumer_launch"])

# brand neutrality: an unknown maker of a mainstream product class qualifies identically
a = md("Samsung launches the Nova X flagship smartphone", "On sale worldwide today.", reliability="high")
b = md("Acme launches the Nova X flagship smartphone", "On sale worldwide today.", reliability="high")
check("no brand-specific preference (unknown brand qualifies identically)",
      a["consumer_launch"] and b["consumer_launch"]
      and a["event_family"] == b["event_family"] and a["tone"] == b["tone"])

# ── consumer launch: exclusions ────────────────────────────────────────────────────────
check("Apple foldable rumor does NOT qualify",
      not md("Apple's foldable iPhone rumored to launch next year",
             "Reports suggest the device may arrive in 2027.")["consumer_launch"])
check("phone discount does NOT qualify",
      not md("Samsung's Galaxy S26 gets a $300 discount in early deals",
             "The flagship phone is on sale ahead of the holidays.")["consumer_launch"])
check("Geekbench leak does NOT qualify",
      not md("Galaxy Z Fold 8 appears on Geekbench ahead of launch",
             "The leaked benchmark reveals the chipset.")["consumer_launch"])
check("Tesla earnings does NOT qualify",
      not md("Tesla reports record quarterly earnings as revenue jumps",
             "The company beat forecasts.")["consumer_launch"])
check("small accessory refresh does NOT qualify",
      not md("Anker releases an updated charger and cable lineup",
             "New chargers arrive next month.")["consumer_launch"])
check("hedged 'expected to' report does NOT qualify",
      not md("Sony expected to announce a new console this fall")["consumer_launch"])
check("low-reliability source does NOT qualify",
      not is_consumer_launch("BrandCo launches a new smartphone", "On sale now.", reliability="low"))
check("stale story does NOT qualify",
      not is_consumer_launch("BrandCo launches a new smartphone", "On sale now.",
                             reliability="high", published_at="2026-07-01T00:00:00+00:00",
                             now=__import__("datetime").datetime(2026, 7, 24, tzinfo=__import__("datetime").timezone.utc)))

# ── country / region (from story text, never publisher) ────────────────────────────────
m = md("Nigeria's central bank holds interest rates amid inflation fears")
check("BBC-style Nigeria story maps to Nigeria / Africa (not publisher's Europe)",
      m["country"] == "Nigeria" and m["region"] == "Africa")
check("Ukraine story maps to Europe", md("Ukraine repels overnight drone strikes")["region"] == "Europe")
check("Japan story maps to Asia-Pacific", md("Japan's economy grows for a third quarter")["region"] == "Asia-Pacific")
check("no recognizable country → None (neutral, not invalid)",
      md("Scientists discover a new deep-sea species")["country"] is None)

# ── event family + tone ────────────────────────────────────────────────────────────────
check("airstrike story → conflict / negative_conflict",
      md("Airstrikes hit the city as troops advance")["event_family"] == "conflict"
      and md("Airstrikes hit the city as troops advance")["tone"] == "negative_conflict")
check("flood disaster → disaster / negative_crisis",
      md("Floods kill dozens as rivers burst their banks")["event_family"] == "disaster"
      and md("Floods kill dozens as rivers burst their banks")["tone"] == "negative_crisis")
check("protest story → protest", md("Mass protests erupt over fuel prices")["event_family"] == "protest")
check("election story → election", md("Voters head to the polls in a tight election")["event_family"] == "election")
check("telescope story → science_discovery / discovery tone + discovery_value",
      (lambda m: m["event_family"] == "science_discovery" and m["tone"] == "discovery"
       and m["discovery_value"])(md("Astronomers discover the most distant galaxy yet with a new telescope")))
check("vaccine trial → health_breakthrough + discovery_value",
      (lambda m: m["event_family"] == "health_breakthrough" and m["discovery_value"])(
          md("New malaria vaccine shows strong results in a large clinical trial")))
check("earnings story → earnings", md("Retail giant's quarterly profit rises 12%")["event_family"] == "earnings")
check("culture story → culture", md("The festival opens with a restored classic film")["event_family"] == "culture")
check("unclassifiable story → other / neutral (never invalid)",
      (lambda m: m["event_family"] == "other" and m["tone"] == "neutral")(md("A quiet afternoon in the village")))
check("all emitted families are in the documented set",
      all(md(t)["event_family"] in EVENT_FAMILIES for t in
          ["Airstrikes hit the city", "Voters head to the polls", "Floods kill dozens", "xyz"]))

# ── real-data false positives (2026-07-24 counterfactual) ──────────────────────────────
check("acquisition story with 'announced' only in the snippet does NOT qualify",
      not md("Midjourney bought the astrology app Co-Star",
             "The AI startup announced the acquisition of the popular app on Thursday.",
             reliability="high")["consumer_launch"])
check("opinion piece mentioning a past launch does NOT qualify",
      not md("Be skeptical of the AI lab's rogue hacker agent story",
             "The company loudly announced its new chatbot platform years ago.",
             reliability="high")["consumer_launch"])
check("'her body was discovered' is a crisis story, NOT a science discovery",
      (lambda m: m["event_family"] != "science_discovery" and not m["discovery_value"]
       and m["tone"] == "negative_crisis")(
          md("Police say withdrawal caused woman's death in backcountry",
             "Her body was discovered in a pool of water with suspicious marks.")))

# ── neutrality / robustness ────────────────────────────────────────────────────────────
m = md(None, None)
check("None inputs → fully neutral metadata, no exception",
      m["country"] is None and m["event_family"] == "other" and m["tone"] == "neutral"
      and not m["consumer_launch"] and not m["discovery_value"])
check("deterministic (same input → same output)",
      md("Samsung launches the Galaxy Z Fold 8 foldable phone", reliability="high")
      == md("Samsung launches the Galaxy Z Fold 8 foldable phone", reliability="high"))

print()
if FAILURES:
    print(f"{len(FAILURES)} CHECK(S) FAILED"); sys.exit(1)
print("ALL PASS")
