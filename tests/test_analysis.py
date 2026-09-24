"""Tests for alerts_bot.analysis, using both hand-built Pair objects (for tightly
controlled edge cases) and the shared fixture (for realistic, DexScreener-shaped data)."""
import unittest
from datetime import datetime, timedelta, timezone

from alerts_bot.analysis import (Pair, dedupe_by_address, filter_by_chains,
                                 filter_by_min_liquidity, find_new_pairs,
                                 find_volume_anomalies, parse_pair, parse_pairs)
from tests.helpers import NOW, load_fixture


def deduped_solana_fixture_pairs():
    """The shared fixture, parsed and deduplicated - the same shape runner.py works with
    once collect_candidate_pairs() has run, before the liquidity/chain filters below."""
    return dedupe_by_address(parse_pairs(load_fixture("search_response.json")["pairs"]))


class ParsePairTests(unittest.TestCase):
    def test_full_pair_parses_all_fields(self):
        raw = load_fixture("search_response.json")["pairs"][0]  # pair A
        pair = parse_pair(raw)
        self.assertEqual(pair.chain_id, "solana")
        self.assertEqual(pair.pair_address, "pairAAAA")
        self.assertEqual(pair.base_symbol, "AAA")
        self.assertEqual(pair.quote_symbol, "SOL")
        self.assertEqual(pair.liquidity_usd, 50000.0)
        self.assertEqual(pair.volume_h1, 2200.0)
        self.assertEqual(pair.volume_h24, 48000.0)
        self.assertEqual(pair.pair_created_at, datetime.fromtimestamp(1734999700000 / 1000, tz=timezone.utc))

    def test_missing_liquidity_and_created_at_become_none_not_zero(self):
        raw = load_fixture("search_response.json")["pairs"][4]  # pair E
        pair = parse_pair(raw)
        self.assertIsNone(pair.liquidity_usd)
        self.assertIsNone(pair.pair_created_at)
        self.assertIsNone(pair.age_minutes(now=NOW))

    def test_completely_empty_dict_does_not_raise(self):
        pair = parse_pair({})
        self.assertEqual(pair.chain_id, "")
        self.assertEqual(pair.base_symbol, "?")
        self.assertIsNone(pair.liquidity_usd)
        self.assertIsNone(pair.pair_created_at)

    def test_non_numeric_price_becomes_none(self):
        pair = parse_pair({"priceUsd": "not-a-number"})
        self.assertIsNone(pair.price_usd)


class PairDerivedValueTests(unittest.TestCase):
    def _pair(self, **overrides):
        base = dict(chain_id="solana", pair_address="x", dex_id="d", base_symbol="A", base_address="",
                   quote_symbol="SOL", url="", price_usd=1.0, liquidity_usd=1000.0, volume_h1=500.0,
                   volume_h24=2400.0, price_change_h1=None, price_change_h24=None, pair_created_at=None)
        base.update(overrides)
        return Pair(**base)

    def test_volume_spike_ratio(self):
        # avg hourly = 2400/24 = 100; ratio = 500/100 = 5
        self.assertEqual(self._pair().volume_spike_ratio, 5.0)

    def test_volume_spike_ratio_none_when_24h_volume_unknown(self):
        self.assertIsNone(self._pair(volume_h24=None).volume_spike_ratio)

    def test_volume_spike_ratio_none_when_1h_volume_unknown(self):
        self.assertIsNone(self._pair(volume_h1=None).volume_spike_ratio)

    def test_age_minutes(self):
        created = NOW - timedelta(minutes=42)
        pair = self._pair(pair_created_at=created)
        self.assertAlmostEqual(pair.age_minutes(now=NOW), 42.0, places=6)

    def test_age_minutes_none_when_created_at_unknown(self):
        self.assertIsNone(self._pair(pair_created_at=None).age_minutes(now=NOW))


class DedupeByAddressTests(unittest.TestCase):
    def test_keeps_first_occurrence(self):
        pairs = parse_pairs(load_fixture("search_response.json")["pairs"])
        result = dedupe_by_address(pairs)
        addresses = [(p.chain_id, p.pair_address) for p in result]
        self.assertEqual(len(addresses), len(set(addresses)))
        self.assertEqual(len(result), 5)  # 6 raw entries in the fixture, one duplicate removed
        # pairAAAA appears twice (entries 0 and 5); the first (liquidity 50000) must be
        # the one that survives, not the duplicate (liquidity 51000).
        kept = [p for p in result if p.pair_address == "pairAAAA"][0]
        self.assertEqual(kept.liquidity_usd, 50000.0)

    def test_pair_without_address_is_dropped(self):
        pairs = [parse_pair({"chainId": "solana"})]  # no pairAddress at all
        self.assertEqual(dedupe_by_address(pairs), [])


class FilterByChainsTests(unittest.TestCase):
    def test_keeps_only_allowed_chains(self):
        result = filter_by_chains(deduped_solana_fixture_pairs(), ["solana"])
        self.assertTrue(all(p.chain_id == "solana" for p in result))
        self.assertEqual(len(result), 4)  # every deduped fixture pair except pairDDDD (ethereum)

    def test_empty_chain_list_keeps_everything(self):
        pairs = deduped_solana_fixture_pairs()
        self.assertEqual(len(filter_by_chains(pairs, [])), len(pairs))

    def test_is_case_insensitive(self):
        result = filter_by_chains(deduped_solana_fixture_pairs(), ["SOLANA"])
        self.assertEqual(len(result), 4)


class FilterByMinLiquidityTests(unittest.TestCase):
    def test_drops_below_threshold_and_unknown(self):
        result = filter_by_min_liquidity(deduped_solana_fixture_pairs(), 5000.0)
        addresses = {p.pair_address for p in result}
        self.assertNotIn("pairCCCC", addresses)  # liquidity 800 < 5000
        self.assertNotIn("pairEEEE", addresses)  # liquidity unknown, excluded, not assumed OK
        self.assertIn("pairAAAA", addresses)

    def test_none_threshold_keeps_everything(self):
        pairs = deduped_solana_fixture_pairs()
        self.assertEqual(len(filter_by_min_liquidity(pairs, None)), len(pairs))


class FindNewPairsTests(unittest.TestCase):
    def test_window_filters_by_age(self):
        result = find_new_pairs(deduped_solana_fixture_pairs(), window_minutes=60, now=NOW)
        addresses = {p.pair_address for p in result}
        # A (5 min), C (2 min), D (10 min) are within 60 min; B (2 days) and E (no
        # timestamp at all) are not.
        self.assertEqual(addresses, {"pairAAAA", "pairCCCC", "pairDDDD"})

    def test_sorted_newest_first(self):
        result = find_new_pairs(deduped_solana_fixture_pairs(), window_minutes=60, now=NOW)
        ages = [p.age_minutes(now=NOW) for p in result]
        self.assertEqual(ages, sorted(ages))

    def test_unknown_created_at_is_never_new(self):
        result = find_new_pairs(deduped_solana_fixture_pairs(), window_minutes=10_000_000, now=NOW)
        self.assertNotIn("pairEEEE", {p.pair_address for p in result})

    def test_narrow_window_excludes_older_new_pairs(self):
        result = find_new_pairs(deduped_solana_fixture_pairs(), window_minutes=3, now=NOW)
        # only pairCCCC (2 min old) fits inside a 3-minute window
        self.assertEqual({p.pair_address for p in result}, {"pairCCCC"})


class FindVolumeAnomaliesTests(unittest.TestCase):
    def test_finds_spike_above_ratio(self):
        result = find_volume_anomalies(deduped_solana_fixture_pairs(), spike_ratio=3.0, min_volume_usd=1000.0)
        addresses = {p.pair_address for p in result}
        self.assertIn("pairBBBB", addresses)     # ratio 5x, volume well above the floor
        self.assertNotIn("pairAAAA", addresses)  # ratio ~1.1x, not a spike
        self.assertNotIn("pairCCCC", addresses)  # ratio ~4.8x but h1 volume only $100 < floor

    def test_min_volume_floor_suppresses_dust_spikes(self):
        result = find_volume_anomalies(deduped_solana_fixture_pairs(), spike_ratio=3.0, min_volume_usd=0.0)
        self.assertIn("pairCCCC", {p.pair_address for p in result})  # floor lifted -> now counts

    def test_ranked_highest_ratio_first(self):
        result = find_volume_anomalies(deduped_solana_fixture_pairs(), spike_ratio=1.0, min_volume_usd=0.0)
        ratios = [p.volume_spike_ratio for p in result]
        self.assertEqual(ratios, sorted(ratios, reverse=True))


if __name__ == "__main__":
    unittest.main()
