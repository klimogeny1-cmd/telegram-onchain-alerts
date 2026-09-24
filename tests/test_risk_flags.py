"""Tests for alerts_bot.risk_flags - pure functions over Pair objects, no network."""
import unittest

from alerts_bot.analysis import dedupe_by_address, parse_pairs
from alerts_bot.risk_flags import RiskThresholds, compute_risk_flags
from tests.helpers import NOW, load_fixture


def pairs_by_address():
    pairs = dedupe_by_address(parse_pairs(load_fixture("search_response.json")["pairs"]))
    return {p.pair_address: p for p in pairs}


class ComputeRiskFlagsTests(unittest.TestCase):
    def setUp(self):
        self.pairs = pairs_by_address()
        self.thresholds = RiskThresholds()  # defaults: $25,000 / 30 min / 5.0x

    def test_healthy_new_pair_only_flags_its_age(self):
        flags = compute_risk_flags(self.pairs["pairAAAA"], self.thresholds, now=NOW)
        self.assertEqual(len(flags), 1)
        self.assertIn("Very new pair", flags[0])

    def test_older_pair_with_high_turnover_flags_volume_not_age(self):
        flags = compute_risk_flags(self.pairs["pairBBBB"], self.thresholds, now=NOW)
        flag_text = "; ".join(flags)
        self.assertNotIn("Very new pair", flag_text)
        self.assertIn("High 24h volume vs liquidity", flag_text)
        self.assertNotIn("Low liquidity", flag_text)  # $40,000 liquidity is above the $25,000 bar

    def test_thin_new_pair_flags_both_liquidity_and_age(self):
        flags = compute_risk_flags(self.pairs["pairCCCC"], self.thresholds, now=NOW)
        flag_text = "; ".join(flags)
        self.assertIn("Low liquidity", flag_text)
        self.assertIn("Very new pair", flag_text)

    def test_unknown_liquidity_is_flagged_explicitly(self):
        flags = compute_risk_flags(self.pairs["pairEEEE"], self.thresholds, now=NOW)
        self.assertTrue(any("Liquidity unknown" in f for f in flags))

    def test_default_thresholds_used_when_none_given(self):
        flags = compute_risk_flags(self.pairs["pairAAAA"], now=NOW)  # no thresholds arg at all
        self.assertEqual(len(flags), 1)

    def test_custom_thresholds_change_the_outcome(self):
        strict = RiskThresholds(low_liquidity_usd=100_000.0, very_new_pair_min=0.0,
                                high_vol_to_liquidity_ratio=100.0)
        flags = compute_risk_flags(self.pairs["pairAAAA"], strict, now=NOW)
        # liquidity 50000 < 100000 -> low liquidity now fires; age 5 min is no longer
        # "very new" since the bar was lowered to 0; the volume ratio is nowhere near 100x.
        self.assertEqual(flags, ["Low liquidity (${:,.0f})".format(50000.0)])

    def test_empty_flags_is_not_a_safety_claim(self):
        # A pair that clears every threshold gets an empty list - the function makes no
        # positive claim, it only reports what it found.
        lenient = RiskThresholds(low_liquidity_usd=1.0, very_new_pair_min=0.0, high_vol_to_liquidity_ratio=1000.0)
        self.assertEqual(compute_risk_flags(self.pairs["pairAAAA"], lenient, now=NOW), [])


if __name__ == "__main__":
    unittest.main()
