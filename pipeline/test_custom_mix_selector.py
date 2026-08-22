#!/usr/bin/env python3
import copy
import json
import os
import unittest

from custom_mix_selector import select_custom_mix

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "custom_mix_candidates.json")


def load_candidates():
    with open(FIXTURE, encoding="utf-8") as handle:
        return json.load(handle)["candidates"]


def by_id(candidates):
    return {c["id"]: c for c in candidates}


def is_primary(candidate, region):
    return any((m.get("region") or m.get("id")) == region
               and m.get("strength") == "primary"
               for m in candidate.get("regionMemberships", []))


class CustomMixSelectorTests(unittest.TestCase):
    def setUp(self):
        self.candidates = load_candidates()
        self.lookup = by_id(self.candidates)

    def select(self, candidates=None, regions=("japan",), topics=()):
        return select_custom_mix(candidates or self.candidates, "2026-07-27",
                                 regions, topics)

    def test_a_japan_only_is_five_of_five(self):
        result = self.select()
        self.assertEqual(len(result["selectedIds"]), 5)
        self.assertTrue(all(is_primary(self.lookup[i], "japan")
                            for i in result["selectedIds"]))
        self.assertEqual(result["metadata"]["selectedRegionStories"], 5)
        self.assertEqual(result["metadata"]["fallbackSlots"], 0)

    def test_b_three_japan_then_shortage_not_cross_region_fill(self):
        # v3: the region boundary is absolute — a japan-only mix NEVER fills its
        # remaining slots from world or US stories. It ships short instead.
        ids = {"jp-tech-robot", "jp-business", "jp-health",
               "world-climate", "world-health", "us-tech"}
        pool = [c for c in self.candidates if c["id"] in ids]
        result = self.select(pool)
        selected = result["selectedIds"]
        self.assertEqual(sum(is_primary(by_id(pool)[i], "japan") for i in selected), 3)
        self.assertEqual(len(selected), 3)
        self.assertEqual(result["metadata"]["fallbackSlots"], 0)
        self.assertTrue(result["metadata"]["shortage"])
        self.assertEqual(result["metadata"]["unfilledSlots"], 2)
        logs = {row["id"]: row for row in result["candidateLogs"]}
        for out_of_region in ("world-climate", "world-health", "us-tech"):
            self.assertEqual(logs[out_of_region]["rejectionReason"],
                             "not primary for any selected region")

    def test_c_duplicate_event_never_survives(self):
        result = self.select()
        self.assertLessEqual(len({"jp-quake-a", "jp-quake-b"}.intersection(result["selectedIds"])), 1)

    def test_d_category_diversity_is_retained(self):
        result = self.select()
        categories = {self.lookup[i]["category"] for i in result["selectedIds"]}
        self.assertGreaterEqual(len(categories), 3)

    def test_e_japan_and_us_us_takes_priority_quota(self):
        # v2: the US is highest priority and takes US_MIN_QUOTA=3 of the 5; japan the rest.
        result = self.select(regions=("japan", "united_states"))
        mix = result["metadata"]["finalRegionMix"]
        self.assertEqual(mix.get("united_states", 0), 3)
        self.assertEqual(mix.get("japan", 0), 2)
        self.assertEqual(result["metadata"]["fallbackSlots"], 0)

    def test_f_japan_tech_is_a_strict_allowlist(self):
        # v2: "tech" selected means ONLY tech stories — japan's tech stories first, and a
        # shortage is filled from tech stories elsewhere, never from off-topic japan ones.
        result = self.select(topics=("tech",))
        selected = result["selectedIds"]
        self.assertEqual(selected[:2], ["jp-tech-robot", "jp-tech-chips"])
        for story_id in selected:
            topics = {str(t).lower() for t in self.lookup[story_id].get("topics", [])}
            self.assertIn("tech", topics, f"{story_id} is off-topic for the tech allowlist")

    def test_g_regression_japan_candidates_cannot_yield_zero(self):
        result = self.select()
        self.assertGreater(result["metadata"]["selectedRegionStories"], 0)

    def test_i_input_order_same_identity_and_selection(self):
        a = self.select(regions=("japan", "united_states"), topics=("tech", "business"))
        b = self.select(regions=("US", "JPN"), topics=("business", "tech"))
        self.assertEqual(a["metadata"]["mixIdentity"], b["metadata"]["mixIdentity"])
        self.assertEqual(a["selectedIds"], b["selectedIds"])

    def test_k_explicit_shortage_without_duplicate_or_crash(self):
        ids = {"jp-quake-a", "global-quake-duplicate", "world-health", "false-positive-sony"}
        pool = [c for c in self.candidates if c["id"] in ids]
        result = self.select(pool)
        self.assertTrue(result["metadata"]["shortage"])
        self.assertGreater(result["metadata"]["unfilledSlots"], 0)
        selected_identities = [by_id(pool)[i]["underlyingStoryIdentity"]
                               for i in result["selectedIds"]]
        self.assertEqual(len(selected_identities), len(set(selected_identities)))

    def test_l_runs_are_byte_equivalent(self):
        first = self.select()
        second = self.select(copy.deepcopy(self.candidates))
        self.assertEqual(
            json.dumps(first, sort_keys=True, separators=(",", ":")),
            json.dumps(second, sort_keys=True, separators=(",", ":")),
        )

    def test_m_company_false_positive_not_primary(self):
        result = self.select()
        self.assertNotIn("false-positive-sony", result["selectedIds"])
        log = next(x for x in result["candidateLogs"] if x["id"] == "false-positive-sony")
        self.assertEqual(log["regionEligibility"], [])

    def test_n_incidental_japan_not_primary(self):
        result = self.select()
        self.assertNotIn("incidental-japan", result["selectedIds"])
        log = next(x for x in result["candidateLogs"] if x["id"] == "incidental-japan")
        self.assertEqual(log["regionEligibility"], [])

    def test_o_world_duplicate_of_selected_japan_is_rejected(self):
        # v3: the world-side duplicate is already outside the japan-only region
        # boundary, so it is rejected there — before duplicate checking even runs.
        ids = {"jp-quake-a", "jp-business", "jp-health",
               "global-quake-duplicate", "world-health", "world-culture"}
        pool = [c for c in self.candidates if c["id"] in ids]
        result = self.select(pool)
        self.assertIn("jp-quake-a", result["selectedIds"])
        self.assertNotIn("global-quake-duplicate", result["selectedIds"])
        log = next(x for x in result["candidateLogs"]
                   if x["id"] == "global-quake-duplicate")
        self.assertEqual(log["rejectionReason"],
                         "not primary for any selected region")

    def test_quality_and_freshness_checks_are_fail_closed(self):
        low = copy.deepcopy(self.lookup["jp-business"])
        low["id"] = "low-source"
        low["url"] = "https://example.com/low-source"
        low["underlyingStoryIdentity"] = "low-source"
        low["sourceReliability"] = "low"
        stale = copy.deepcopy(self.lookup["jp-health"])
        stale["id"] = "stale"
        stale["url"] = "https://example.com/stale"
        stale["underlyingStoryIdentity"] = "stale"
        stale["publishedAt"] = "2026-07-20T00:00:00Z"
        result = self.select([low, stale, self.lookup["world-health"]])
        logs = {row["id"]: row for row in result["candidateLogs"]}
        self.assertEqual(logs["low-source"]["rejectionReason"], "low source reliability")
        self.assertEqual(logs["stale"]["rejectionReason"], "outside 72-hour freshness window")


class CustomMixSelectorV2Tests(unittest.TestCase):
    """Selector v2 (2026-08-13): strict topic allowlist + fixed region priority.

    The user's settings are a CONTRACT: an unselected topic is never chosen (not even by
    fallback — ship short instead), the US outranks japan outranks world, the US keeps a
    minimum of 3 slots when selected and its pool suffices, and a UK story competes only
    as a world story."""

    def setUp(self):
        self.candidates = load_candidates()
        self.lookup = by_id(self.candidates)

    def select(self, candidates=None, regions=("japan",), topics=()):
        return select_custom_mix(candidates or self.candidates, "2026-07-27",
                                 regions, topics)

    @staticmethod
    def make(cid, *, topics, regions, score, category="WORLD", source=None):
        return {
            "id": cid,
            "headline": f"Headline for {cid} with distinct wording {cid}",
            "summary": f"A distinct summary for {cid} that shares no event with others.",
            "source": source or f"SOURCE-{cid}",
            "category": category,
            "url": f"https://example.com/{cid}",
            "publishedAt": "2026-07-27T06:00:00Z",
            "baseScore": score,
            "topics": list(topics),
            "underlyingStoryIdentity": f"story-{cid}",
            "regionMemberships": [{"id": r, "strength": "primary"} for r in regions],
        }

    def selected_topics_of(self, result, pool=None):
        lookup = by_id(pool) if pool else self.lookup
        return [{str(t).lower() for t in lookup[i].get("topics", [])}
                for i in result["selectedIds"]]

    def test_science_off_selects_no_science_candidate(self):
        # Science OFF (an allowlist without science): pure-science stories are 0/5.
        result = self.select(topics=("tech", "business", "health", "climate", "culture"))
        for story_id in result["selectedIds"]:
            topics = {str(t).lower() for t in self.lookup[story_id].get("topics", [])}
            self.assertTrue(topics & {"tech", "business", "health", "climate", "culture"})
        self.assertNotIn("jp-quake-a", result["selectedIds"])   # science-only stories
        self.assertNotIn("jp-quake-b", result["selectedIds"])

    def test_science_off_fallback_does_not_resurrect_science(self):
        # Force a fallback: a tiny japan pool + off-region candidates. Even with unfilled
        # slots, no science-only story may return through global_fallback.
        ids = {"jp-culture", "jp-quake-a", "us-science", "world-culture",
               "global-quake-duplicate"}
        pool = [c for c in self.candidates if c["id"] in ids]
        result = self.select(pool, topics=("culture",))
        for topics in self.selected_topics_of(result, pool):
            self.assertIn("culture", topics)
        self.assertNotIn("us-science", result["selectedIds"])
        self.assertNotIn("jp-quake-a", result["selectedIds"])
        self.assertNotIn("global-quake-duplicate", result["selectedIds"])
        self.assertTrue(result["metadata"]["shortage"])

    def test_us_beats_world_when_both_selected(self):
        # world-climate scores 91 — above every US story except us-tech — yet the US
        # priority quota keeps at least 3 US slots. v2.1: with the UK publisher cap the
        # world quota may shrink further (this fixture's world pool is BBC/Guardian
        # heavy), and the freed slots flow BACK to the US — never the other way.
        result = self.select(regions=("united_states", "world"))
        mix = result["metadata"]["finalRegionMix"]
        self.assertGreaterEqual(mix.get("united_states", 0), 3)
        self.assertEqual(sum(mix.values()), 5)
        from custom_mix_selector import _UK_PUBLISHER_FAMILIES, _publisher_family
        uk = sum(1 for i in result["selectedIds"]
                 if _publisher_family(self.lookup[i]["source"]) in _UK_PUBLISHER_FAMILIES)
        self.assertLessEqual(uk, 1)

    def test_us_minimum_three_of_five_with_all_three_regions(self):
        result = self.select(regions=("united_states", "japan", "world"))
        mix = result["metadata"]["finalRegionMix"]
        self.assertGreaterEqual(mix.get("united_states", 0), 3)
        self.assertEqual(sum(mix.values()), 5)
        # Priority order United States > Japan > World also decides the remainder.
        self.assertEqual(mix.get("japan", 0), 1)
        self.assertEqual(mix.get("world", 0), 1)

    def test_uk_story_is_a_world_story_and_cannot_displace_us_slots(self):
        # A very strong UK story (world membership — the classifier's rule for the UK)
        # must not push any US story out of the US quota.
        pool = [
            self.make("uk-science", topics=("science",), regions=("world",), score=99,
                      source="BBC News (Science & Environment)"),
            self.make("us-a", topics=("tech",), regions=("united_states",), score=70),
            self.make("us-b", topics=("business",), regions=("united_states",), score=69),
            self.make("us-c", topics=("health",), regions=("united_states",), score=68),
            self.make("world-a", topics=("climate",), regions=("world",), score=67),
        ]
        result = self.select(pool, regions=("united_states", "world"))
        mix = result["metadata"]["finalRegionMix"]
        self.assertEqual(mix.get("united_states", 0), 3)
        self.assertIn("uk-science", result["selectedIds"])   # as a world story only

    def test_uk_science_loses_to_us_when_science_is_off(self):
        pool = [
            self.make("uk-science", topics=("science",), regions=("world",), score=99,
                      category="SCIENCE", source="BBC News (Science & Environment)"),
            self.make("us-a", topics=("tech",), regions=("united_states",), score=70),
            self.make("us-b", topics=("business",), regions=("united_states",), score=69),
            self.make("us-c", topics=("health",), regions=("united_states",), score=68),
            self.make("world-a", topics=("climate",), regions=("world",), score=67),
        ]
        result = self.select(pool, regions=("united_states", "world"),
                             topics=("tech", "business", "health", "climate"))
        self.assertNotIn("uk-science", result["selectedIds"])
        logs = {row["id"]: row for row in result["candidateLogs"]}
        self.assertEqual(logs["uk-science"]["rejectionReason"],
                         "topic not selected (strict allowlist)")
        self.assertEqual(set(result["selectedIds"]), {"us-a", "us-b", "us-c", "world-a"})

    def test_fail_closed_rather_than_fill_with_violations(self):
        # Only ONE culture story exists in japan. v3: the world culture story is outside
        # the japan-only region boundary, so the mix ships 1/5 with four unfilled slots —
        # never five with off-topic or off-region filler.
        result = self.select(topics=("culture",))
        self.assertEqual(set(result["selectedIds"]), {"jp-culture"})
        self.assertTrue(result["metadata"]["shortage"])
        self.assertEqual(result["metadata"]["unfilledSlots"], 4)

    def test_v3_identity_and_version_invalidate_earlier_caches(self):
        result = self.select()
        self.assertEqual(result["metadata"]["selectorVersion"], 3)
        self.assertIn("|selector=3|", result["metadata"]["mixIdentity"])


class CustomMixSelectorV3RegionBoundaryTests(unittest.TestCase):
    """Selector v3 (2026-08-22): the region boundary is absolute.

    A candidate is eligible ONLY if it is primary for a selected region — no fallback
    pass (strict or relaxed) may ever cross the boundary. General news (canonical-empty
    categories like WORLD) gets no special pass either: a US general article is a US
    article, full stop."""

    make = staticmethod(CustomMixSelectorV2Tests.make)

    @staticmethod
    def select(pool, regions, topics=()):
        return select_custom_mix(pool, "2026-07-27", regions, topics)

    @staticmethod
    def reasons(result):
        return {row["id"]: row.get("rejectionReason") for row in result["candidateLogs"]}

    def us_pool(self, n, start_score=80):
        cycle = ("tech", "business", "health", "climate", "culture")
        return [self.make(f"us-{i}", topics=(cycle[i % 5],),
                          regions=("united_states",), score=start_score - i)
                for i in range(n)]

    def world_pool(self, n, start_score=79):
        cycle = ("climate", "health", "culture", "business", "tech")
        return [self.make(f"world-{i}", topics=(cycle[i % 5],),
                          regions=("world",), score=start_score - i)
                for i in range(n)]

    def test_us_only_never_fills_from_world_general(self):
        # US selected, World NOT selected, only 2 US articles: the mix ships 2/5 and the
        # three world GENERAL articles (category WORLD = canonical-empty) stay out.
        pool = self.us_pool(2) + [
            self.make(f"world-gen-{i}", topics=(), regions=("world",), score=95 - i)
            for i in range(3)
        ]
        result = self.select(pool, regions=("united_states",))
        self.assertEqual(set(result["selectedIds"]), {"us-0", "us-1"})
        self.assertTrue(result["metadata"]["shortage"])
        self.assertEqual(result["metadata"]["unfilledSlots"], 3)
        self.assertEqual(result["metadata"]["fallbackSlots"], 0)
        self.assertNotIn("world", result["metadata"]["finalRegionMix"])
        reasons = self.reasons(result)
        for i in range(3):
            self.assertEqual(reasons[f"world-gen-{i}"],
                             "not primary for any selected region")

    def test_world_only_never_fills_from_us_general(self):
        # The mirror image: World selected, US NOT selected — US general articles
        # (category WORLD, e.g. a CBS U.S. story) never leak into a world-only mix.
        pool = self.world_pool(2) + [
            self.make(f"us-gen-{i}", topics=(), regions=("united_states",),
                      score=95 - i, source="CBS News (U.S.)")
            for i in range(3)
        ]
        result = self.select(pool, regions=("world",))
        self.assertEqual(set(result["selectedIds"]), {"world-0", "world-1"})
        self.assertTrue(result["metadata"]["shortage"])
        self.assertEqual(result["metadata"]["fallbackSlots"], 0)
        self.assertNotIn("united_states", result["metadata"]["finalRegionMix"])
        reasons = self.reasons(result)
        for i in range(3):
            self.assertEqual(reasons[f"us-gen-{i}"],
                             "not primary for any selected region")

    def test_us_general_article_eligible_only_when_us_selected(self):
        us_general = self.make("cbs-us-gen", topics=(), regions=("united_states",),
                               score=99, source="CBS News (U.S.)")
        world = self.world_pool(5)
        without_us = self.select(world + [us_general], regions=("world",))
        self.assertNotIn("cbs-us-gen", without_us["selectedIds"])
        with_us = self.select(world + [us_general],
                              regions=("united_states", "world"))
        self.assertIn("cbs-us-gen", with_us["selectedIds"])

    def test_science_category_with_subtags_stays_off_across_regions(self):
        # SCIENCE-category stories carry the canonical science topic; secondary tags
        # (tech) never make them eligible while Science is OFF — in any region.
        pool = self.us_pool(5) + [
            self.make("us-science-tech", topics=("science", "tech"),
                      regions=("united_states",), score=99, category="SCIENCE"),
            self.make("world-science-tech", topics=("science", "tech"),
                      regions=("world",), score=98, category="SCIENCE"),
        ]
        result = self.select(pool, regions=("united_states", "world"),
                             topics=("tech", "business", "health", "climate", "culture"))
        self.assertNotIn("us-science-tech", result["selectedIds"])
        self.assertNotIn("world-science-tech", result["selectedIds"])
        reasons = self.reasons(result)
        self.assertEqual(reasons["us-science-tech"],
                         "topic not selected (strict allowlist)")
        self.assertEqual(reasons["world-science-tech"],
                         "topic not selected (strict allowlist)")

    def test_us_minimum_three_with_sufficient_candidates(self):
        pool = self.us_pool(5, start_score=60) + self.world_pool(5, start_score=95)
        result = self.select(pool, regions=("united_states", "world"))
        mix = result["metadata"]["finalRegionMix"]
        self.assertGreaterEqual(mix.get("united_states", 0), 3)
        self.assertEqual(len(result["selectedIds"]), 5)
        self.assertEqual(sum(mix.values()), 5)

    def test_uk_cap_survives_relaxed_fallback(self):
        # Only 2 US stories and a world pool that is ENTIRELY UK: even the relaxed
        # fallback pass (which loosens the generic family cap) must not exceed one UK
        # story total — the mix ships 3/5 instead.
        from custom_mix_selector import _UK_PUBLISHER_FAMILIES, _publisher_family
        pool = self.us_pool(2) + [
            self.make(f"bbc-{i}", topics=("climate",), regions=("world",),
                      score=90 - i, source=f"BBC News (Section {i})")
            for i in range(3)
        ] + [
            self.make("guardian-0", topics=("health",), regions=("world",),
                      score=85, source="The Guardian"),
        ]
        result = self.select(pool, regions=("united_states", "world"))
        lookup = by_id(pool)
        uk = sum(1 for i in result["selectedIds"]
                 if _publisher_family(lookup[i]["source"]) in _UK_PUBLISHER_FAMILIES)
        self.assertLessEqual(uk, 1)
        self.assertEqual(len(result["selectedIds"]), 3)
        self.assertTrue(result["metadata"]["shortage"])

    def test_success_is_always_five_unique_stories(self):
        # When a mix CAN be filled, it is exactly 5 with no duplicate ids and no
        # duplicate underlying stories.
        dup = self.make("us-dup", topics=("tech",), regions=("united_states",), score=99)
        dup["underlyingStoryIdentity"] = "story-us-0"   # duplicates us-0's story
        pool = self.us_pool(6) + self.world_pool(3) + [dup]
        result = self.select(pool, regions=("united_states", "world"))
        selected = result["selectedIds"]
        self.assertEqual(len(selected), 5)
        self.assertEqual(len(set(selected)), 5)
        lookup = by_id(pool)
        identities = [lookup[i]["underlyingStoryIdentity"] for i in selected]
        self.assertEqual(len(identities), len(set(identities)))
        self.assertEqual(sum(result["metadata"]["finalRegionMix"].values()), 5)
        self.assertFalse(result["metadata"]["shortage"])


if __name__ == "__main__":
    unittest.main()
