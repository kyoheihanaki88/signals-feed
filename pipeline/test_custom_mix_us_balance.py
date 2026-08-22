#!/usr/bin/env python3
"""Custom Mix US balance (v2.1, 2026-08-18) — CBS structured region, publisher-family
caps, canonical topic rule, and coverage-aware enrichment.

    python3 pipeline/test_custom_mix_us_balance.py

The production incident: with only NPR World as a general US source and BBC/Guardian
contributing four section feeds each, a "United States" Custom Mix could read like a UK
front page, and Science stories leaked into Science-OFF mixes via the standard-edition
fallback. Pinned here, end to end and offline (no network):

  •  Scout → Mix Pool: the CBS U.S. section feed's `region` metadata survives as
     `structured_regions` and yields `united_states: primary` WITHOUT any US keyword;
     a general CBS feed inherits nothing (no publisher-wide US rule).
  •  Region classifier audit: state names / Capitol Hill are US evidence; a publisher
     name alone, or an ambiguous word alone, never is.
  •  Selector v2.1: canonical (category) topic allowlist, per-family cap, UK-family cap
     while the US is active, relaxed-fallback still reaching exactly five.
  •  Editorial enrichment pool: coverage floors for US/japan/world and per-topic
     representation, deterministic under input reordering.
"""

import copy
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from custom_mix_selector import (  # noqa: E402
    _CANONICAL_TOPICS_BY_CATEGORY,
    _publisher_family,
    _UK_PUBLISHER_FAMILIES,
    select_custom_mix,
)
from editorial_mix_pool import select_for_enrichment  # noqa: E402
from mix_pool import _CATEGORY_TOPICS, build_mix_pool  # noqa: E402
from region_classifier import classify_region  # noqa: E402

DATE = "2026-08-18"
PUBLISHED = "2026-08-18T06:00:00Z"
NOW = "2026-08-18T09:00:00Z"


def make(cid, *, topics=("tech",), regions=("world",), score=50.0,
         category="WORLD", source=None):
    return {
        "id": cid,
        "headline": f"Headline for {cid} with distinct wording {cid}",
        "summary": f"A distinct summary for {cid} that shares no event with others.",
        "source": source or f"SOURCE-{cid}",
        "category": category,
        "url": f"https://example.com/{cid}",
        "publishedAt": PUBLISHED,
        "baseScore": score,
        "topics": list(topics),
        "underlyingStoryIdentity": f"story-{cid}",
        "regionMemberships": [{"id": r, "strength": "primary"} for r in regions],
    }


class ScoutToPoolStructuredRegionTests(unittest.TestCase):
    """Required tests 1–3: the CBS U.S. feed's region metadata, end to end."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="cbs_scout_")
        cache = os.path.join(cls.tmp, "cache")
        os.makedirs(cache)
        # Two feeds of the SAME publisher: only the U.S. section carries `region`.
        sources = """
sources:
  - name: CBS News (U.S.)
    feed_id: cbs-us
    url: https://www.cbsnews.com/latest/rss/us
    category: WORLD
    region: united_states
    paywalled: false
    status: verified
    notes: test fixture
  - name: CBS News (World)
    feed_id: cbs-world
    url: https://www.cbsnews.com/latest/rss/world
    category: WORLD
    paywalled: false
    status: verified
    notes: test fixture
"""
        with open(os.path.join(cls.tmp, "sources.yaml"), "w") as handle:
            handle.write(sources)

        pub = "Tue, 18 Aug 2026 06:00:00 GMT"

        def rss(items):
            rows = "".join(
                f"<item><title>{t}</title><link>{u}</link>"
                f"<description>{d}</description><pubDate>{pub}</pubDate></item>"
                for t, u, d in items
            )
            return f'<?xml version="1.0"?><rss version="2.0"><channel>{rows}</channel></rss>'

        # The U.S.-section headline deliberately contains NO US keyword at all.
        with open(os.path.join(cache, "cbs-us.xml"), "w") as handle:
            handle.write(rss([
                ("Storm shelters expand after historic flooding",
                 "https://www.cbsnews.com/news/storm-shelters-expand-flooding/",
                 "Officials said shelters will remain open through the weekend."),
                ("The Uplift: a video item that must be dropped",
                 "https://www.cbsnews.com/video/the-uplift-dropped/",
                 "Video items never become candidates."),
            ]))
        # A general CBS feed: a plainly foreign story from the SAME publisher.
        with open(os.path.join(cache, "cbs-world.xml"), "w") as handle:
            handle.write(rss([
                ("Elections in Indonesia enter a second round",
                 "https://www.cbsnews.com/news/indonesia-election-second-round/",
                 "Voters return to the polls next month."),
            ]))

        out = os.path.join(cls.tmp, "candidates.json")
        result = subprocess.run(
            [sys.executable, os.path.join(HERE, "scout.py"),
             "--sources", os.path.join(cls.tmp, "sources.yaml"),
             "--cache-dir", cache, "--out", out, "--max-age-hours", "876000"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr[-400:]
        cls.scout = json.load(open(out))
        cls.by_url = {c["url"]: c for c in cls.scout["candidates"]}

    def test_1_us_section_metadata_survives_scout(self):
        us = self.by_url["https://www.cbsnews.com/news/storm-shelters-expand-flooding/"]
        self.assertEqual(us.get("structured_regions"), ["united_states"])
        self.assertEqual(us["publisher"], "CBS News")
        self.assertEqual(us["source"], "CBS News (U.S.)")

    def test_1b_video_item_dropped(self):
        self.assertNotIn("https://www.cbsnews.com/video/the-uplift-dropped/", self.by_url)

    def test_2_us_primary_without_any_us_keyword(self):
        pool = build_mix_pool(self.scout, DATE, NOW, now=NOW)
        row = next(c for c in pool["candidates"]
                   if c["url"].endswith("storm-shelters-expand-flooding"))
        us = next(m for m in row["regionMemberships"] if m["region"] == "united_states")
        self.assertEqual(us["strength"], "primary")
        self.assertIn("united_states:structured", us["evidence"])

    def test_3_general_cbs_feed_inherits_nothing(self):
        world = self.by_url["https://www.cbsnews.com/news/indonesia-election-second-round/"]
        self.assertNotIn("structured_regions", world)
        pool = build_mix_pool(self.scout, DATE, NOW, now=NOW)
        row = next(c for c in pool["candidates"]
                   if c["url"].endswith("indonesia-election-second-round"))
        us = next(m for m in row["regionMemberships"] if m["region"] == "united_states")
        self.assertNotEqual(us["strength"], "primary",
                            "a CBS publisher name must never imply a US story")


class RegionClassifierAuditTests(unittest.TestCase):
    def test_state_name_in_title_is_primary(self):
        row = classify_region({"title": "California expands wildfire insurance rules",
                               "summary": ""}, "united_states")
        self.assertEqual(row["strength"], "primary")

    def test_capitol_hill_is_primary(self):
        row = classify_region({"title": "Capitol Hill braces for the budget vote",
                               "summary": ""}, "united_states")
        self.assertEqual(row["strength"], "primary")

    def test_ambiguous_words_alone_are_not_us(self):
        # "Senate", "federal", "Supreme Court" were deliberately NOT added.
        for title in ("Senate approves the coalition budget",
                      "Federal police raid offices in the capital",
                      "Supreme Court rejects the appeal"):
            row = classify_region({"title": title, "summary": ""}, "united_states")
            self.assertNotEqual(row["strength"], "primary", title)


class SelectorV21Tests(unittest.TestCase):
    def select(self, pool, regions, topics=()):
        return select_custom_mix(pool, DATE, regions, topics)

    def test_canonical_map_matches_mix_pool(self):
        self.assertEqual(dict(_CANONICAL_TOPICS_BY_CATEGORY),
                         {k: tuple(v) for k, v in _CATEGORY_TOPICS.items()})

    def test_7_science_category_never_survives_science_off(self):
        # Category SCIENCE + a tech text-tag: LOOKS like Science → rejected. Category
        # TECH + an incidental science text-tag: looks like Tech → kept.
        pool = [
            make("sci-tech-tagged", category="SCIENCE", topics=("science", "tech"),
                 regions=("united_states",), score=99),
            make("tech-sci-tagged", category="TECH", topics=("tech", "science"),
                 regions=("united_states",), score=70),
            make("us-a", category="WORLD", topics=(), regions=("united_states",), score=60),
            make("us-b", category="BUSINESS", topics=("business",),
                 regions=("united_states",), score=59),
            make("world-a", category="HEALTH", topics=("health",), regions=("world",), score=58),
        ]
        result = self.select(pool, ("united_states", "world"),
                             ("tech", "business", "health", "climate", "culture", "ai"))
        self.assertNotIn("sci-tech-tagged", result["selectedIds"])
        self.assertIn("tech-sci-tagged", result["selectedIds"])
        logs = {row["id"]: row for row in result["candidateLogs"]}
        self.assertEqual(logs["sci-tech-tagged"]["rejectionReason"],
                         "topic not selected (strict allowlist)")

    def test_8_relaxed_fallback_never_resurrects_off_topics(self):
        # Even when the pool is too thin and the family cap is relaxed, an off-topic
        # article stays out — the mix ships short instead.
        pool = [
            make("us-a", category="WORLD", topics=(), regions=("united_states",), score=60),
            make("sci", category="SCIENCE", topics=("science",),
                 regions=("united_states",), score=99),
            make("sci2", category="SCIENCE", topics=("science",), regions=("world",), score=98),
        ]
        result = self.select(pool, ("united_states", "world"),
                             ("tech", "business", "health", "climate", "culture", "ai"))
        self.assertEqual(result["selectedIds"], ["us-a"])
        self.assertTrue(result["metadata"]["shortage"])

    def test_9_one_story_per_publisher_family_when_pool_allows(self):
        # Two section feeds of one publisher offer strong stories; only one survives
        # while distinct-family alternatives exist.
        pool = [
            make("bbc-1", source="BBC News (World)", regions=("world",), score=90,
                 category="WORLD", topics=()),
            make("bbc-2", source="BBC News (Health)", regions=("world",), score=89,
                 category="HEALTH", topics=("health",)),
            make("npr-1", source="NPR (World)", regions=("world",), score=60,
                 category="WORLD", topics=()),
            make("aj-1", source="Al Jazeera", regions=("world",), score=59,
                 category="WORLD", topics=()),
            make("cbs-1", source="CBS News (U.S.)", regions=("world",), score=58,
                 category="WORLD", topics=()),
            make("verge-1", source="The Verge (Tech)", regions=("world",), score=57,
                 category="TECH", topics=("tech",)),
        ]
        result = self.select(pool, ("world",))
        families = [_publisher_family(next(c for c in pool if c["id"] == i)["source"])
                    for i in result["selectedIds"]]
        self.assertEqual(len(result["selectedIds"]), 5)
        self.assertEqual(len(families), len(set(families)),
                         "one story per publisher family")
        self.assertNotIn("bbc-2", result["selectedIds"])

    def test_5_uk_flood_cannot_take_more_than_one_slot_with_us_active(self):
        # Eight strong BBC/Guardian stories versus modest US coverage: the final five
        # carry at most ONE UK-family story, and the US still gets its three.
        pool = (
            [make(f"bbc-{i}", source="BBC News (World)", regions=("world",),
                  score=99 - i, category="WORLD", topics=()) for i in range(4)]
            + [make(f"guardian-{i}", source="The Guardian (World)", regions=("world",),
                    score=95 - i, category="WORLD", topics=()) for i in range(4)]
            + [make("us-a", source="CBS News (U.S.)", regions=("united_states",),
                    score=50, category="WORLD", topics=()),
               make("us-b", source="NPR (World)", regions=("united_states",),
                    score=49, category="WORLD", topics=()),
               make("us-c", source="The Verge", regions=("united_states",),
                    score=48, category="TECH", topics=("tech",)),
               make("world-aj", source="Al Jazeera", regions=("world",),
                    score=40, category="WORLD", topics=())]
        )
        result = self.select(pool, ("united_states", "world"))
        chosen = {c["id"]: c for c in pool if c["id"] in result["selectedIds"]}
        uk = sum(1 for c in chosen.values()
                 if _publisher_family(c["source"]) in _UK_PUBLISHER_FAMILIES)
        us = sum(1 for c in chosen.values()
                 if any(m["id"] == "united_states" for m in c["regionMemberships"]))
        self.assertEqual(len(result["selectedIds"]), 5)
        self.assertLessEqual(uk, 1)
        self.assertGreaterEqual(us, 3)

    def test_4_exactly_five_with_us_minimum_three(self):
        pool = (
            [make(f"us-{i}", source=f"US Source {i}", regions=("united_states",),
                  score=60 - i, category="WORLD", topics=()) for i in range(4)]
            + [make(f"world-{i}", source=f"World Source {i}", regions=("world",),
                    score=80 - i, category="WORLD", topics=()) for i in range(4)]
        )
        result = self.select(pool, ("united_states", "world"))
        self.assertEqual(len(result["selectedIds"]), 5)
        us = sum(1 for i in result["selectedIds"] if i.startswith("us-"))
        self.assertGreaterEqual(us, 3)

    def test_10_input_order_reversal_is_identical(self):
        pool = (
            [make(f"bbc-{i}", source="BBC News (World)", regions=("world",),
                  score=99 - i, category="WORLD", topics=()) for i in range(3)]
            + [make("us-a", source="CBS News (U.S.)", regions=("united_states",),
                    score=50, category="WORLD", topics=()),
               make("us-b", source="NPR (World)", regions=("united_states",),
                    score=49, category="WORLD", topics=()),
               make("us-c", source="US Daily", regions=("united_states",),
                    score=48, category="WORLD", topics=()),
               make("world-aj", source="Al Jazeera", regions=("world",),
                    score=40, category="WORLD", topics=()),
               make("world-x", source="World Weekly", regions=("world",),
                    score=39, category="WORLD", topics=())]
        )
        forward = self.select(copy.deepcopy(pool), ("united_states", "world"))
        backward = self.select(list(reversed(copy.deepcopy(pool))), ("united_states", "world"))
        self.assertEqual(forward["selectedIds"], backward["selectedIds"])
        self.assertEqual(
            json.dumps(forward, sort_keys=True), json.dumps(backward, sort_keys=True)
        )


class EnrichmentCoverageTests(unittest.TestCase):
    def build_pool(self):
        rows = []
        rows += [make(f"us-{i}", regions=("united_states",), score=30 + i,
                      category="WORLD", topics=()) for i in range(8)]
        rows += [make(f"jp-{i}", regions=("japan",), score=60 + i,
                      category="WORLD", topics=()) for i in range(6)]
        rows += [make(f"world-{i}", regions=("world",), score=80 + i,
                      category="WORLD", topics=()) for i in range(8)]
        topic_rows = [
            ("t-ai", "AI", ("ai", "tech")), ("t-biz", "BUSINESS", ("business",)),
            ("t-cli", "CLIMATE", ("climate",)), ("t-cul", "CULTURE", ("culture",)),
            ("t-hea", "HEALTH", ("health",)), ("t-sci", "SCIENCE", ("science",)),
            ("t-tec", "TECH", ("tech",)),
        ]
        rows += [make(cid, regions=("world",), score=5, category=cat, topics=topics)
                 for cid, cat, topics in topic_rows]
        return rows

    def test_regional_floors_and_topic_coverage(self):
        chosen = select_for_enrichment(self.build_pool())
        self.assertEqual(len(chosen), 20)

        def primaries(region):
            return sum(1 for c in chosen
                       if any(m["id"] == region and m["strength"] == "primary"
                              for m in c["regionMemberships"]))
        # The US floor holds even though every US candidate scores BELOW world/japan.
        self.assertGreaterEqual(primaries("united_states"), 6)
        self.assertGreaterEqual(primaries("japan"), 4)
        self.assertGreaterEqual(primaries("world"), 4)
        for topic in ("ai", "business", "climate", "culture", "health", "science", "tech"):
            self.assertTrue(any(topic in c["topics"] for c in chosen),
                            f"topic {topic} missing from the enrichment pool")

    def test_deterministic_under_reordering(self):
        forward = select_for_enrichment(self.build_pool())
        backward = select_for_enrichment(list(reversed(self.build_pool())))
        self.assertEqual([c["id"] for c in forward], [c["id"] for c in backward])


if __name__ == "__main__":
    unittest.main()
