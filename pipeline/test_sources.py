#!/usr/bin/env python3
"""Tests for the Morning Mix ingestion widening (section feeds) — commit 1.

Covers: production sources.yaml integrity (existing feeds untouched, new section feeds
well-formed), publisher identity collapsing, canonical-URL dedup across feeds of one
publisher, cluster_sources never inflated by multi-feed coverage, category assignment,
and safe failure for malformed / missing feeds. Offline: uses the Scout's --cache-dir
mode with tiny RSS fixtures.
"""
import json
import os
import subprocess
import sys
import tempfile
from email.utils import format_datetime
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
sys.path.insert(0, HERE)
from scout import publisher_key  # noqa: E402

FAILURES = []


def check(name, ok):
    if ok:
        print(f"✓ {name}")
    else:
        print(f"✗ {name}")
        FAILURES.append(name)


def rss(items):
    now = format_datetime(datetime.datetime.now(datetime.timezone.utc))
    body = "".join(
        f"<item><title>{t}</title><link>{u}</link><description>{d}</description>"
        f"<pubDate>{now}</pubDate></item>" for t, u, d in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>fixture</title>{body}</channel></rss>'


def main():
    import yaml
    src = yaml.safe_load(open(os.path.join(HERE, "sources.yaml")))["sources"]
    by_name = {s["name"]: s for s in src}

    # ── production sources.yaml integrity ───────────────────────────────────────────
    original = {
        "BBC News (World)": ("https://feeds.bbci.co.uk/news/world/rss.xml", "WORLD", False, "verified"),
        "NPR (World)": ("https://feeds.npr.org/1004/rss.xml", "WORLD", False, "verified"),
        "The Guardian (World)": ("https://www.theguardian.com/world/rss", "WORLD", False, "known"),
        "The Verge": ("https://www.theverge.com/rss/index.xml", "TECH", False, "known"),
        "Al Jazeera": ("https://www.aljazeera.com/xml/rss/all.xml", "WORLD", False, "reachable"),
        "Financial Times": ("https://www.ft.com/rss/home", "ECONOMY", True, "flagged"),
    }
    check("existing feeds unchanged (name/url/category/paywalled/status)",
          all(n in by_name
              and by_name[n]["url"] == u and by_name[n]["category"] == c
              and bool(by_name[n].get("paywalled")) == p and by_name[n]["status"] == st
              for n, (u, c, p, st) in original.items()))

    new = [s for s in src if s.get("feed_id")]
    check("eight section feeds added", len(new) == 8)
    check("section feeds: https, category in TECH/SCIENCE/HEALTH/CULTURE, purpose noted",
          all(s["url"].startswith("https://")
              and s["category"] in {"TECH", "SCIENCE", "HEALTH", "CULTURE"}
              and "section feed" in (s.get("notes") or "")
              and s.get("paywalled") is False
              for s in new))
    check("feed_id values are unique", len({s["feed_id"] for s in new}) == len(new))
    urls = [s["url"] for s in src if s.get("url")]
    check("no section feed is an alias of an existing feed (all URLs distinct)",
          len(urls) == len(set(urls)))
    check("section feeds belong to already-audited publishers only",
          {publisher_key(s["name"]) for s in new}
          <= {"The Verge", "BBC News", "The Guardian"})

    # ── publisher identity ──────────────────────────────────────────────────────────
    check("publisher_key collapses section names to one publisher",
          publisher_key("BBC News (World)") == "BBC News"
          and publisher_key("BBC News (Technology)") == "BBC News"
          and publisher_key("The Verge (Mobile)") == "The Verge"
          and publisher_key("The Verge") == "The Verge")

    # ── offline scout behavior with section feeds ───────────────────────────────────
    with tempfile.TemporaryDirectory() as tmp:
        cache = os.path.join(tmp, "cache")
        os.makedirs(cache)
        fixture_sources = {"sources": [
            {"name": "Pub A (World)", "feed_id": "puba-world",
             "url": "https://puba.example/world/rss", "category": "WORLD",
             "paywalled": False, "status": "verified"},
            {"name": "Pub A (Tech)", "feed_id": "puba-tech",
             "url": "https://puba.example/tech/rss", "category": "TECH",
             "paywalled": False, "status": "verified"},
            {"name": "Pub B", "feed_id": "pubb",
             "url": "https://pubb.example/rss", "category": "TECH",
             "paywalled": False, "status": "verified"},
            {"name": "Pub C (Broken)", "feed_id": "pubc-broken",
             "url": "https://pubc.example/rss", "category": "TECH",
             "paywalled": False, "status": "verified"},
            {"name": "Pub D (Missing)", "feed_id": "pubd-missing",
             "url": "https://pubd.example/rss", "category": "TECH",
             "paywalled": False, "status": "verified"},
        ]}
        import yaml as _y
        with open(os.path.join(tmp, "sources.yaml"), "w") as f:
            _y.safe_dump(fixture_sources, f)

        shared = ("Acme launches the Widget Phone Nine flagship",
                  "https://puba.example/articles/widget-phone-nine", "Launch coverage.")
        with open(os.path.join(cache, "puba-world.xml"), "w") as f:
            f.write(rss([shared,
                         ("Storm floods river towns in the north",
                          "https://puba.example/articles/storm-floods", "Weather.")]))
        with open(os.path.join(cache, "puba-tech.xml"), "w") as f:
            f.write(rss([shared,   # SAME article via a second feed of the same publisher
                         ("Acme Widget Phone Nine flagship launches with new display",
                          "https://puba.example/articles/widget-phone-display", "More launch detail.")]))
        with open(os.path.join(cache, "pubb.xml"), "w") as f:
            f.write(rss([("Acme launches Widget Phone Nine flagship worldwide",
                          "https://pubb.example/articles/acme-widget", "Independent coverage.")]))
        with open(os.path.join(cache, "pubc-broken.xml"), "w") as f:
            f.write("<rss><channel><item><title>broken")   # malformed on purpose
        # pubd-missing.xml intentionally absent

        r = subprocess.run([PY, os.path.join(HERE, "scout.py"),
                            "--sources", os.path.join(tmp, "sources.yaml"),
                            "--cache-dir", cache,
                            "--out", os.path.join(tmp, "candidates.json")],
                           capture_output=True, text=True)
        check("scout run succeeds despite one malformed and one missing feed", r.returncode == 0)
        check("malformed feed reported, not fatal", "Pub C (Broken)" in r.stdout)
        check("missing cache feed reported, not fatal", "no-cache-file" in r.stdout)

        data = json.load(open(os.path.join(tmp, "candidates.json")))
        cands = data["candidates"]
        urls = [c["canonical_url"] for c in cands]
        check("duplicate article across two feeds of one publisher → ONE candidate",
              urls.count("https://puba.example/articles/widget-phone-nine") == 1
              and len(cands) == 4)
        launch = [c for c in cands if "widget" in c["url"] or "acme" in c["url"]]
        cluster_ids = {c["cluster_id"] for c in launch}
        check("the launch story clusters across publishers", len(cluster_ids) == 1 and len(launch) == 3)
        check("cluster_size counts entries (3) but cluster_sources counts PUBLISHERS (2)",
              launch[0]["cluster_size"] == 3 and launch[0]["cluster_sources"] == 2)
        check("publisher field collapses Pub A's two feeds",
              {c["publisher"] for c in cands if c["source"].startswith("Pub A")} == {"Pub A"})
        cats = {c["url"]: c["category"] for c in cands}
        check("category comes from the FEED that carried the article",
              cats["https://puba.example/articles/storm-floods"] == "WORLD"
              and cats["https://pubb.example/articles/acme-widget"] == "TECH")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} CHECK(S) FAILED")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
