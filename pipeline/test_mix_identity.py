#!/usr/bin/env python3
import unittest

from mix_identity import UnsupportedMixValue, mix_identity, normalize_regions, normalize_topics


class MixIdentityTests(unittest.TestCase):
    def test_japan_aliases_share_identity(self):
        identities = {mix_identity("2026-07-27", [alias], ["tech"])
                      for alias in ("Japan", " japan ", "JP", "JPN")}
        self.assertEqual(len(identities), 1)

    def test_us_aliases(self):
        self.assertEqual(normalize_regions(["US", "USA", "U.S.", "United States",
                                            "united_states"]), ("united_states",))

    def test_sort_and_dedupe(self):
        self.assertEqual(normalize_regions(["world", "JP", "japan"]),
                         ("japan", "world"))
        self.assertEqual(normalize_topics(["Tech", "business", "tech"]),
                         ("business", "tech"))

    def test_order_independent_identity(self):
        a = mix_identity("2026-07-27", ["world", "JP"], ["tech", "business"])
        b = mix_identity("2026-07-27", ["japan", "world"], ["business", "tech"])
        self.assertEqual(a, b)

    def test_different_mix_different_identity(self):
        self.assertNotEqual(
            mix_identity("2026-07-27", ["japan"], ["tech"]),
            mix_identity("2026-07-27", ["japan"], ["health"]),
        )

    def test_unsupported_is_explicit(self):
        with self.assertRaisesRegex(UnsupportedMixValue, "unsupported region"):
            normalize_regions(["mars"])


if __name__ == "__main__":
    unittest.main()
