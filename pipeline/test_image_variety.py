#!/usr/bin/env python3
"""
Tests for the image-variety mechanism (build.py recent-reuse prevention + lead-first + source
passthrough, and validate_feed.image_reuse_errors). Stdlib only. Run: python3 pipeline/test_image_variety.py
"""
import os, sys, json, tempfile
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import build  # noqa: E402
import validate_feed as V  # noqa: E402

PASS, FAIL = "✓", "✗"
failures = 0
def check(name, cond, detail=""):
    global failures
    print(f"  {PASS if cond else FAIL} {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond: failures += 1

def edition(date, urls):
    return {"date": date, "focus": "MIXED", "version": 1,
            "signals": [{"number": i + 1, "imageURL": u} for i, u in enumerate(urls)]}

MATCHERS = build.build_topic_matchers({"election": ["election", "vote"]})
CAT = {"WORLD": [{"imageURL": "w1"}, {"imageURL": "w2"}, {"imageURL": "w3"}],
       "TECH":  [{"imageURL": "t1", "source": "wikimedia", "license": "CC BY-SA 4.0", "credit": "X"},
                 {"imageURL": "t2"}]}
TOPIC = {"election": [{"imageURL": "e1"}, {"imageURL": "e2"}]}

def items():
    return [{"number": 1, "headline": "Election result today", "summary": "", "category": "WORLD"},
            {"number": 2, "headline": "A new chip", "summary": "", "category": "TECH"}]


# ── 1) recent_image_urls: reads recent editions, excludes the current date, respects window ──
print("1) recent_image_urls:")
with tempfile.TemporaryDirectory() as d:
    for dt, urls in [("2026-06-01", ["a", "b"]), ("2026-06-02", ["c"]), ("2026-06-03", ["d"])]:
        json.dump(edition(dt, urls), open(os.path.join(d, f"{dt}.json"), "w"))
    r = build.recent_image_urls(d, exclude_date="2026-06-03")   # default window now 90
    check("default (90) uses all history when <90 editions", r == {"a", "b", "c"}, str(r))
    check("excludes current date's URLs", "d" not in r)
    r2 = build.recent_image_urls(d, exclude_date="2026-06-03", window=1)  # only 2026-06-02
    check("window limits to most recent N", r2 == {"c"}, str(r2))
    check("missing dir → empty", build.recent_image_urls("/no/such/dir", "x") == set())


# ── 2) avoid recently-used URLs when a fresh candidate exists (+ logging) ──
print("\n2) recent-reuse avoidance + logging:")
logs = []
picks = build.assign_images(items(), CAT, {}, [], TOPIC, MATCHERS, yday=0,
                            avoid={"e1"}, lead_index=0, log=lambda m: logs.append(m))
check("lead skips recent e1 → uses e2", picks[0]["imageURL"] == "e2", picks[0]["imageURL"])
check("logged the recent-skip", any("skip recently-used image e1" in m for m in logs), str(logs))
check("no chosen URL is in avoid", all(p["imageURL"] not in {"e1"} for p in picks))


# ── 3) lead-first + source/credit passthrough (internal) ──
print("\n3) lead-first + provenance:")
picks = build.assign_images(items(), CAT, {}, [], TOPIC, MATCHERS, yday=0, avoid=set(), lead_index=0)
check("lead (signal 1) got topic image e1", picks[0]["imageURL"] == "e1" and picks[0]["isLead"])
check("signal 2 got TECH image", picks[1]["imageURL"] in {"t1", "t2"})
check("default source = unsplash", picks[0]["source"] == "unsplash")
tech = picks[1]
check("wikimedia provenance passes through", (tech["source"], tech["license"]) == ("wikimedia", "CC BY-SA 4.0")
      if tech["imageURL"] == "t1" else True)


# ── 4) graceful fallback: every fresh candidate recently used → reuse, never blank ──
print("\n4) graceful fallback (pool exhausted by avoid):")
small = {"WORLD": [{"imageURL": "w1"}, {"imageURL": "w2"}]}
one = [{"number": 1, "headline": "plain world story", "summary": "", "category": "WORLD"}]
logs = []
picks = build.assign_images(one, small, {}, [], {}, MATCHERS, yday=0,
                            avoid={"w1", "w2"}, lead_index=0, log=lambda m: logs.append(m))
check("still returns a non-blank image", picks[0]["imageURL"] in {"w1", "w2"}, picks[0]["imageURL"])
check("warned about reuse", any("reusing" in m or "recent pool exhausted" in m for m in logs), str(logs))


# ── 5) validate_feed.image_reuse_errors ──
print("\n5) image_reuse_errors:")
with tempfile.TemporaryDirectory() as root:
    ed = os.path.join(root, "editions"); os.makedirs(ed)
    json.dump(edition("2026-06-01", ["x1", "x2"]), open(os.path.join(ed, "2026-06-01.json"), "w"))
    json.dump(edition("2026-06-02", ["x3", "x2"]), open(os.path.join(ed, "2026-06-02.json"), "w"))  # x2 reused
    errs = V.image_reuse_errors(root)   # default 90-edition cooldown
    check("flags reused imageURL in newest (90-window)", any("x2" in e for e in errs), str(errs))
    # unique newest → clean
    json.dump(edition("2026-06-02", ["x3", "x9"]), open(os.path.join(ed, "2026-06-02.json"), "w"))
    check("clean when newest is all-unique", V.image_reuse_errors(root) == [])


# ── 6) cooldown semantics: prefer a non-cooldown image over reusing an in-cooldown one; log reuse ──
print("\n6) cooldown (90) semantics:")
# WORLD pool fully in cooldown, but TECH has a fresh one → should cross-pool to TECH, not reuse WORLD.
cat = {"WORLD": [{"imageURL": "w1"}, {"imageURL": "w2"}], "TECH": [{"imageURL": "tfresh"}]}
one = [{"number": 1, "headline": "plain world story", "summary": "", "category": "WORLD"}]
logs = []
picks = build.assign_images(one, cat, {}, [], {}, MATCHERS, yday=0,
                            avoid={"w1", "w2"}, lead_index=0, log=lambda m: logs.append(m))
check("prefers non-cooldown image (tfresh) over reusing WORLD", picks[0]["imageURL"] == "tfresh", picks[0]["imageURL"])

# reuse-after-cooldown: chosen URL was seen before but is OUTSIDE the cooldown → allowed + logged.
logs = []
picks = build.assign_images(one, {"WORLD": [{"imageURL": "old1"}]}, {}, [], {}, MATCHERS, yday=0,
                            avoid=set(), seen_ever={"old1"}, lead_index=0, log=lambda m: logs.append(m))
check("reuses image whose cooldown expired", picks[0]["imageURL"] == "old1")
check("logs cooldown-expiry reuse", any("reuse allowed (outside" in m for m in logs), str(logs))

print(f"\n{'ALL PASS' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
