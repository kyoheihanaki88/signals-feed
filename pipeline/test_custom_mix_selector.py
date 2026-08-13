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

    def test_b_three_japan_then_two_fallbacks(self):
        ids = {"jp-tech-robot", "jp-business", "jp-health",
               "world-climate", "world-health", "us-tech"}
        pool = [c for c in self.candidates if c["id"] in ids]
        result = self.select(pool)
        selected = result["selectedIds"]
        self.assertEqual(sum(is_primary(by_id(pool)[i], "japan") for i in selected), 3)
        self.assertEqual(result["metadata"]["fallbackSlots"], 2)
        self.assertEqual(result["metadata"]["fallbackReason"],
                         "insufficient qualifying regional candidates")

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

    def test_o_fallback_duplicate_of_selected_japan_is_rejected(self):
        ids = {"jp-quake-a", "jp-business", "jp-health",
               "global-quake-duplicate", "world-health", "world-culture"}
        pool = [c for c in self.candidates if c["id"] in ids]
        result = self.select(pool)
        self.assertIn("jp-quake-a", result["selectedIds"])
        self.assertNotIn("global-quake-duplicate", result["selectedIds"])
        log = next(x for x in result["candidateLogs"]
                   if x["id"] == "global-quake-duplicate")
        self.assertIn("duplicate underlyingStoryIdentity", log["rejectionReason"])

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
    def make(cid, *, topics, regions, score, category="WORLD"):
        return {
            "id": cid,
            "headline": f"Headline for {cid} with distinct wording {cid}",
            "summary": f"A distinct summary for {cid} that shares no event with others.",
            "source": f"SOURCE-{cid}",
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
        # priority quota keeps 3 US slots.
        result = self.select(regions=("united_states", "world"))
        mix = result["metadata"]["finalRegionMix"]
        self.assertEqual(mix.get("united_states", 0), 3)
        self.assertEqual(mix.get("world", 0), 2)

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
            self.make("uk-science", topics=("science",), regions=("world",), score=99),
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
            self.make("uk-science", topics=("science",), regions=("world",), score=99),
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
        # Only ONE culture story exists in japan and one in world: the mix ships 2/5 with
        # three unfilled slots — never five with off-topic filler.
        result = self.select(topics=("culture",))
        self.assertEqual(set(result["selectedIds"]), {"jp-culture", "world-culture"})
        self.assertTrue(result["metadata"]["shortage"])
        self.assertEqual(result["metadata"]["unfilledSlots"], 3)

    def test_v2_identity_and_version_invalidate_v1_caches(self):
        result = self.select()
        self.assertEqual(result["metadata"]["selectorVersion"], 2)
        self.assertIn("|selector=2|", result["metadata"]["mixIdentity"])


if __name__ == "__main__":
    unittest.main()
