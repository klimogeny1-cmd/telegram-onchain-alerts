"""Tests for alerts_bot.tape.format - post text, escaping, length limits. Pure functions."""
import unittest

from alerts_bot.tape.engine import TapeItem
from alerts_bot.tape.format import (FOOTER, MAX_POST_CHARS, assemble, clean_label, compact, format_pct,
                                    format_price, render_item, render_tape)
from tests.solami_helpers import DEMO, NOW


def item(section, **data):
    data.setdefault("mint", DEMO)
    return TapeItem(section, "k-%s-%s" % (section, len(data)), data=data)


class NumberFormatTests(unittest.TestCase):
    def test_prices_never_use_exponent_notation(self):
        self.assertEqual(format_price(1.92), "$1.92")
        self.assertEqual(format_price(0.4512), "$0.4512")
        self.assertEqual(format_price(0.00002231), "$0.00002231")
        self.assertEqual(format_price(0.0000008672), "$0.0000008672")
        self.assertEqual(format_price(64250.4), "$64,250")
        for bad in (None, 0, -1.0, float("nan")):
            self.assertEqual(format_price(bad), "n/a")

    def test_compact_amounts(self):
        self.assertEqual(compact(950), "950")
        self.assertEqual(compact(12345), "12.3K")
        self.assertEqual(compact(-18_700_000), "-18.7M")
        self.assertEqual(compact(0.5), "0.5")
        self.assertEqual(compact(None), "n/a")

    def test_percentages(self):
        self.assertEqual(format_pct(2.5), "2.50%")
        self.assertEqual(format_pct(30.04), "30.0%")
        self.assertEqual(format_pct(-3.2, signed=True), "-3.20%")


class ItemTests(unittest.TestCase):
    def test_trade_wording_is_descriptive_not_a_call(self):
        line = render_item(item("trades", side="sell", usd=52300.0, price_usd=0.0000223, dex="pumpswap",
                                trader="TraderA1111111111111111111111111111111111111", signature="SIG"),
                           lambda mint: "DEMO")[0]
        self.assertIn("sold $52.3K of", line)
        self.assertIn('<a href="https://solscan.io/tx/SIG">tx</a>', line)
        for call_word in ("BUY", "SELL", "entry", "target", "moon"):
            self.assertNotIn(call_word, line)

    def test_creator_controlled_text_is_escaped(self):
        lines = render_item(item("launches", name='<a href="x">evil</a> & co', dex="pumpfun", creator="C",
                                 signature="S"), lambda mint: "<script>")
        text = "\n".join(lines)
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)
        self.assertIn('&lt;a href="x"&gt;evil&lt;/a&gt; &amp; co', text)   # inert text, not a link

    def test_symbols_cannot_forge_extra_lines_or_reorder_text(self):
        self.assertEqual(clean_label("DEMO\n• bought $9M of X"), "DEMO • bought $9M of X")
        self.assertEqual(clean_label("A\u202eB\u2066C"), "A B C")         # bidi overrides become spaces
        self.assertNotIn("\u202e", clean_label("evil\u202etxt.exe"))
        self.assertEqual(len(clean_label("X" * 100, 24)), 24)
        line = render_item(item("trades", side="buy", usd=6000.0, price_usd=1.0, dex="d", trader="T", signature="S"),
                           lambda mint: "FAKE\n• line")[0]
        self.assertNotIn("\n", line)

    def test_unpriced_liquidity_shows_raw_legs(self):
        line = render_item(item("liquidity", kind="remove", usd=None, legs="150 SOL + 30.0M DEMO", pct=None,
                                quote_mint="So11111111111111111111111111111111111111112", dex="pumpswap",
                                provider="P", signature="S"), lambda mint: "")[0]
        self.assertIn("removed 150 SOL + 30.0M DEMO from", line)

    def test_flags_are_appended_with_marker(self):
        entry = item("trades", side="buy", usd=6100.0, price_usd=0.00087, dex="raydium", trader="T", signature="S")
        entry.flags = ["Price impact 23.5%"]
        self.assertTrue(render_item(entry, lambda m: "DEMO")[0].endswith("⚠️ Price impact 23.5%"))


class PostTests(unittest.TestCase):
    def test_tape_post_has_header_sections_and_footer(self):
        trades = [item("trades", side="buy", usd=7500.0, price_usd=0.00075, dex="pumpswap", trader="T", signature="S")]
        posts = render_tape({"trades": (trades, 2)}, lambda m: "DEMO", NOW, "watching DEMO", {"trades": "≥ $5.0K"})
        self.assertEqual(len(posts), 1)
        lines = posts[0].splitlines()
        self.assertEqual(lines[0], "<b>Solana Tape</b> · 14:20 UTC · watching DEMO")
        self.assertIn("<b>Large trades</b> (≥ $5.0K)", lines)
        self.assertIn("+ 2 more not shown", lines)
        self.assertEqual(lines[-1], FOOTER)
        self.assertIn("not financial advice", FOOTER)

    def test_empty_sections_give_no_post(self):
        self.assertEqual(render_tape({}, lambda m: "", NOW, "firehose", {}), [])

    def test_long_content_is_split_and_every_part_keeps_header_and_footer(self):
        blocks = [["<b>Section %d</b>" % n] + ["• line %d-%d %s" % (n, i, "x" * 150) for i in range(12)]
                  for n in range(6)]
        posts = assemble("<b>Solana Tape</b> · 14:20 UTC", blocks, [FOOTER], max_chars=2000)
        self.assertGreater(len(posts), 1)
        for post in posts:
            self.assertLessEqual(len(post), 2000)
            self.assertTrue(post.startswith("<b>Solana Tape</b>"))
            self.assertTrue(post.endswith(FOOTER))
        self.assertLess(MAX_POST_CHARS, 4096)


if __name__ == "__main__":
    unittest.main()
