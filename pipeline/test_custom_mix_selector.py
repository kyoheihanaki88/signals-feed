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

    def test_e_japan_and_us_are_balanced(self):
        result = self.select(regions=("japan", "united_states"))
        mix = result["metadata"]["finalRegionMix"]
        self.assertEqual(sum(mix.get(r, 0) for r in ("japan", "united_states")), 5)
        self.assertEqual(sorted([mix.get("japan", 0), mix.get("united_states", 0)]), [2, 3])
        self.assertEqual(result["metadata"]["fallbackSlots"], 0)

    def test_f_japan_tech_then_other_japan_before_global_tech(self):
        result = self.select(topics=("tech",))
        selected = result["selectedIds"]
        self.assertEqual(selected[:2], ["jp-tech-robot", "jp-tech-chips"])
        self.assertNotIn("us-tech", selected)
        self.assertTrue(all(is_primary(self.lookup[i], "japan") for i in selected))

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


if __name__ == "__main__":
    unittest.main()
