"""Tests for alerts_bot.solami.events - parsing Blur frames (synthetic fixtures built from
the documented shapes, see tests/fixtures/solami/README.md). No network."""
import unittest

from alerts_bot.solami.events import (Graduation, LiquidityChange, OtherEvent, PoolCreated, StreamNotice, Swap,
                                      TokenLaunch, TokenMetadata, TokenTransfer, VolumeBreakout, parse_blur_event,
                                      parse_blur_message, to_float, to_int)
from tests.solami_helpers import DEMO, POOL_A, PROVIDER, USDC, WSOL, XTRA, blur_events


def by_signature(signature):
    return next(e for e in blur_events() if e.signature == signature)


class NumberHelperTests(unittest.TestCase):
    def test_to_float_accepts_decimal_strings_and_numbers(self):
        self.assertEqual(to_float("0.0000008672"), 0.0000008672)
        self.assertEqual(to_float(1284.55), 1284.55)
        self.assertEqual(to_float(" 3 "), 3.0)

    def test_to_float_rejects_what_is_not_a_finite_number(self):
        for bad in (None, "", "abc", "nan", "inf", True, [], {}):
            self.assertIsNone(to_float(bad), bad)

    def test_to_int_keeps_big_raw_amounts_exact(self):
        self.assertEqual(to_int("123456789012345678901234"), 123456789012345678901234)
        self.assertEqual(to_int(1500), 1500)
        self.assertEqual(to_int(1500.0), 1500)
        self.assertEqual(to_int("12.0"), 12)

    def test_to_int_rejects_fractions_and_garbage(self):
        for bad in ("12.5", 12.5, "x", None, False):
            self.assertIsNone(to_int(bad), bad)


class FixtureParsingTests(unittest.TestCase):
    def test_swap_fields_are_typed(self):
        swap = by_signature("SigSwapBuyLarge")
        self.assertIsInstance(swap, Swap)
        self.assertEqual((swap.mint, swap.quote_mint, swap.pool, swap.dex), (DEMO, WSOL, POOL_A, "pumpswap"))
        self.assertEqual(swap.side, "buy")
        self.assertEqual(swap.base_amount, 10_000_000_000_000)
        self.assertEqual(swap.volume_usd, 7500.0)
        self.assertEqual(swap.price_usd, 0.00075)
        self.assertTrue(swap.candle_ok)
        self.assertAlmostEqual(swap.quote_price_usd, 150.0)       # price_usd / price = SOL in USD

    def test_amounts_given_as_strings_parse_the_same(self):
        swap = by_signature("SigSwapSellSmall")
        self.assertEqual(swap.base_amount, 160_000_000_000)
        self.assertEqual(swap.quote_reserve, 499_204_000_000)

    def test_outlier_swap_keeps_its_flags(self):
        swap = by_signature("SigSwapOutlier")
        self.assertIs(swap.candle_ok, False)
        self.assertEqual(swap.price_impact_pct, 23.5)

    def test_liquidity_uses_base_mint(self):
        liq = by_signature("SigLiqRemove")
        self.assertIsInstance(liq, LiquidityChange)
        self.assertEqual((liq.kind, liq.mint, liq.quote_mint, liq.provider), ("remove", DEMO, WSOL, PROVIDER))
        self.assertEqual(liq.quote_amount, 150_000_000_000)
        self.assertIsNone(liq.value_usd)          # not in the documented shape - never invented

    def test_pool_create_accepts_base_mint(self):
        pool = by_signature("SigPoolCreate")
        self.assertIsInstance(pool, PoolCreated)
        self.assertEqual((pool.mint, pool.quote_mint), (DEMO, USDC))

    def test_launch_graduation_breakout_transfer_metadata(self):
        events = blur_events()
        launch = next(e for e in events if isinstance(e, TokenLaunch))
        self.assertEqual((launch.mint, launch.symbol), (XTRA, "XTRA"))
        self.assertIn("<b>", launch.name)         # raw text kept; escaping is the formatter's job
        grad = next(e for e in events if isinstance(e, Graduation))
        self.assertEqual((grad.launchpad, grad.dex), ("pumpfun", "pumpswap"))
        surge = next(e for e in events if isinstance(e, VolumeBreakout) and e.type == "surge")
        self.assertEqual((surge.multiple, surge.window_secs, surge.trades), (4.45, 300, 212))
        self.assertEqual(surge.block_time, surge.trigger_time)
        burn = next(e for e in events if isinstance(e, TokenTransfer))
        self.assertEqual((burn.kind, burn.amount), ("burn", 20_000_000_000_000))
        meta = next(e for e in events if isinstance(e, TokenMetadata) and e.mint == DEMO)
        self.assertEqual((meta.symbol, meta.decimals), ("DEMO", 6))

    def test_every_documented_shape_and_oddity_in_the_fixture(self):
        events = blur_events()
        types = [e.type for e in events]
        self.assertIn("candle", types)
        self.assertIn("future_event_type_nobody_documented_yet", types)   # unknown -> OtherEvent, no crash
        self.assertTrue(any(isinstance(e, StreamNotice) and e.message == "example server notice" for e in events))
        # the JSON array line yields its metadata event; the non-JSON line yields nothing
        self.assertTrue(any(isinstance(e, TokenMetadata) and e.mint == XTRA for e in events))
        self.assertEqual(len(events), 18)


class RobustnessTests(unittest.TestCase):
    def test_non_objects_and_bad_json(self):
        self.assertIsNone(parse_blur_event("swap"))
        self.assertIsNone(parse_blur_event(None))
        self.assertEqual(parse_blur_message("not json"), [])
        self.assertEqual(parse_blur_message("42"), [])

    def test_missing_type_and_wrong_field_types_do_not_raise(self):
        self.assertIsInstance(parse_blur_event({}), OtherEvent)
        weird = parse_blur_event({"type": "swap", "volume_usd": {"nested": 1}, "base_amount": [1], "side": 7})
        self.assertIsInstance(weird, Swap)
        self.assertIsNone(weird.volume_usd)
        self.assertIsNone(weird.base_amount)
        self.assertEqual(weird.side, "")

    def test_signed_liquidity_and_transfer_amounts_become_sizes(self):
        liq = parse_blur_event({"type": "liquidity", "kind": "remove", "quote_amount": "-150", "base_amount": -30})
        self.assertEqual((liq.quote_amount, liq.base_amount), (150, 30))
        burn = parse_blur_event({"type": "transfer", "kind": "burn", "amount": "-5"})
        self.assertEqual(burn.amount, 5)

    def test_nested_metadata_shape(self):
        event = parse_blur_event({"type": "metadata", "mint": DEMO, "metadata": {"symbol": "DEMO", "decimals": 6}})
        self.assertEqual((event.symbol, event.decimals), ("DEMO", 6))


class KeyTests(unittest.TestCase):
    def test_same_event_delivered_twice_has_the_same_key(self):
        first, second = blur_events(), blur_events()
        self.assertEqual([e.key for e in first], [e.key for e in second])

    def test_keys_are_unique_across_the_fixture(self):
        keys = [e.key for e in blur_events()]
        self.assertEqual(len(keys), len(set(keys)))

    def test_instruction_index_separates_two_swaps_in_one_transaction(self):
        a = parse_blur_event({"type": "swap", "signature": "S", "pool": "P", "mint": "M", "ix_index": 1})
        b = parse_blur_event({"type": "swap", "signature": "S", "pool": "P", "mint": "M", "ix_index": 2})
        self.assertNotEqual(a.key, b.key)

    def test_event_without_signature_gets_a_content_hash_key(self):
        a = parse_blur_event({"type": "graduation", "mint": "M"})
        b = parse_blur_event({"type": "graduation", "mint": "N"})
        self.assertTrue(a.key.startswith("graduation:h"))
        self.assertNotEqual(a.key, b.key)

    def test_breakout_key_is_mint_trigger_and_window(self):
        a = parse_blur_event({"type": "surge", "mint": "M", "trigger_time": 5, "window_secs": 300})
        self.assertEqual(a.key, "surge:M:5:300")


if __name__ == "__main__":
    unittest.main()
