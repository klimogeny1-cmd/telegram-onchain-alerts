"""Tests for alerts_bot.formatter - pure string building, no network."""
import unittest

from alerts_bot.analysis import Pair, dedupe_by_address, parse_pairs
from alerts_bot.formatter import (FOOTER, cap_items, escape_html, format_new_pairs_post,
                                  format_usd, format_volume_alert_post, pair_label)
from alerts_bot.risk_flags import RiskThresholds
from tests.helpers import NOW, load_fixture


def deduped_pairs():
    return dedupe_by_address(parse_pairs(load_fixture("search_response.json")["pairs"]))


def make_pair(**overrides):
    base = dict(chain_id="solana", pair_address="x", dex_id="d", base_symbol="AAA", base_address="",
               quote_symbol="SOL", url="https://dexscreener.com/solana/x", price_usd=1.0,
               liquidity_usd=100000.0, volume_h1=100.0, volume_h24=2400.0, price_change_h1=None,
               price_change_h24=None, pair_created_at=NOW)
    base.update(overrides)
    return Pair(**base)


class FormatUsdTests(unittest.TestCase):
    def test_scales(self):
        self.assertEqual(format_usd(None), "n/a")
        self.assertEqual(format_usd(0), "$0")
        self.assertEqual(format_usd(950), "$950")
        self.assertEqual(format_usd(1500), "$1.5K")
        self.assertEqual(format_usd(2_500_000), "$2.50M")
        self.assertEqual(format_usd(3_100_000_000), "$3.10B")
        self.assertEqual(format_usd(-2000), "-$2.0K")


class EscapeHtmlTests(unittest.TestCase):
    def test_escapes_special_characters(self):
        self.assertEqual(escape_html("<b>A & B</b>"), "&lt;b&gt;A &amp; B&lt;/b&gt;")

    def test_none_becomes_empty_string(self):
        self.assertEqual(escape_html(None), "")


class CapItemsTests(unittest.TestCase):
    def test_under_limit(self):
        shown, overflow = cap_items([1, 2, 3], 10)
        self.assertEqual(shown, [1, 2, 3])
        self.assertEqual(overflow, 0)

    def test_over_limit(self):
        shown, overflow = cap_items(list(range(15)), 10)
        self.assertEqual(len(shown), 10)
        self.assertEqual(overflow, 5)


class FormatNewPairsPostTests(unittest.TestCase):
    def test_empty_list_returns_empty_string(self):
        self.assertEqual(format_new_pairs_post([], 60, generated_at=NOW), "")

    def test_header_matches_the_house_style(self):
        pairs = [p for p in deduped_pairs() if p.pair_address == "pairAAAA"]
        text = format_new_pairs_post(pairs, 60, generated_at=NOW)
        first_line = text.splitlines()[0]
        self.assertEqual(first_line, "<b>New pairs, last 60 min</b> · %s UTC" % NOW.strftime("%H:%M"))

    def test_footer_is_always_present(self):
        pairs = [p for p in deduped_pairs() if p.pair_address == "pairAAAA"]
        text = format_new_pairs_post(pairs, 60, generated_at=NOW)
        self.assertTrue(text.rstrip().endswith(FOOTER))
        self.assertIn("Tape, not advice. Source: DexScreener.", text)

    def test_malicious_token_symbol_is_escaped(self):
        # base/quote symbols come from on-chain token metadata - arbitrary, unmoderated
        # text - and this post uses Telegram's HTML parse mode, so it must be escaped.
        evil = make_pair(base_symbol="<script>", quote_symbol="A&B")
        text = format_new_pairs_post([evil], 60, generated_at=NOW)
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)
        self.assertIn("A&amp;B", text)

    def test_overflow_line_when_more_than_max_items(self):
        many = [make_pair(pair_address="p%d" % i, base_symbol="T%d" % i) for i in range(12)]
        text = format_new_pairs_post(many, 60, generated_at=NOW, max_items=10)
        self.assertIn("+ 2 more not shown", text)

    def test_risk_flags_shown_inline(self):
        pairs = [p for p in deduped_pairs() if p.pair_address == "pairCCCC"]
        text = format_new_pairs_post(pairs, 60, generated_at=NOW, thresholds=RiskThresholds())
        self.assertIn("Low liquidity", text)
        self.assertIn("Very new pair", text)

    def test_no_flags_means_no_warning_marker(self):
        clean = make_pair(liquidity_usd=100000.0, pair_created_at=NOW.replace(year=NOW.year - 1))
        text = format_new_pairs_post([clean], 60, generated_at=NOW, thresholds=RiskThresholds())
        self.assertNotIn("⚠", text)


class FormatVolumeAlertPostTests(unittest.TestCase):
    def test_empty_list_returns_empty_string(self):
        self.assertEqual(format_volume_alert_post([], generated_at=NOW), "")

    def test_header_and_ratio_shown(self):
        pairs = [p for p in deduped_pairs() if p.pair_address == "pairBBBB"]
        text = format_volume_alert_post(pairs, generated_at=NOW)
        self.assertTrue(text.startswith("<b>Volume anomalies, last 1h</b>"))
        self.assertIn("5.0x", text)
        self.assertTrue(text.rstrip().endswith(FOOTER))


class PairLabelTests(unittest.TestCase):
    def test_label_format(self):
        pairs = [p for p in deduped_pairs() if p.pair_address == "pairAAAA"]
        self.assertEqual(pair_label(pairs[0]), "AAA/SOL")


if __name__ == "__main__":
    unittest.main()
