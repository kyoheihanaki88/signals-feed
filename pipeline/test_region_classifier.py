#!/usr/bin/env python3
import unittest

from region_classifier import classify_region


class RegionClassifierTests(unittest.TestCase):
    def primary(self, candidate):
        return classify_region(candidate, "japan")

    def test_title_evidence_is_primary_and_structured(self):
        result = self.primary({"title": "Tokyo updates heat rules", "summary": ""})
        self.assertEqual(result["strength"], "primary")
        self.assertIn("tokyo:title", result["evidence"])

    def test_summary_needs_multiple_independent_signals(self):
        result = self.primary({"title": "Central bank decision", "summary":
                               "The Bank of Japan acted as the yen strengthened."})
        self.assertEqual(result["strength"], "primary")
        self.assertIn("bank-of-japan:summary", result["evidence"])

    def test_sony_review_is_not_primary(self):
        self.assertEqual(self.primary({
            "title": "Sony headphones review", "summary": "Tested in London."
        })["strength"], "none")

    def test_toyota_us_launch_is_not_japan(self):
        self.assertEqual(self.primary({
            "title": "Toyota launches an electric vehicle in the US",
            "summary": "The model will be built and sold in Kentucky."
        })["strength"], "none")

    def test_nintendo_review_is_not_primary(self):
        self.assertEqual(self.primary({
            "title": "Nintendo adventure game review", "summary": "A critic reviews the game."
        })["strength"], "none")

    def test_single_summary_mention_is_incidental(self):
        result = self.primary({
            "title": "European ports update schedules",
            "summary": "Japan appears once in a list of trading partners."
        })
        self.assertEqual(result["strength"], "incidental")

    def test_samsung_is_not_japan(self):
        self.assertEqual(self.primary({
            "title": "Samsung unveils a new phone", "summary": "The launch took place in Seoul."
        })["strength"], "none")

    def test_publisher_location_is_ignored(self):
        self.assertEqual(self.primary({
            "title": "Flood defenses expand in France",
            "summary": "The work covers the Loire valley.",
            "publisherRegion": "japan"
        })["strength"], "none")

    def test_japanese_company_descriptor_alone_is_incidental(self):
        result = self.primary({
            "title": "Japanese company Sony releases new headphones", "summary": ""
        })
        self.assertEqual(result["strength"], "incidental")

    def test_world_is_not_generic_fallback(self):
        self.assertEqual(classify_region({
            "title": "A useful guide to battery life", "summary": "Available globally."
        }, "world")["strength"], "none")
        self.assertEqual(classify_region({
            "title": "G7 countries agree on methane rules", "summary": ""
        }, "world")["strength"], "primary")


if __name__ == "__main__":
    unittest.main()
