"""Tests for the Solami settings in alerts_bot.config - temp .env files, never the real
process environment or a real key."""
import os
import tempfile
import unittest

from alerts_bot.config import Config, parse_mints
from tests.solami_helpers import DEMO, XTRA

TOKEN = "123456789:AAEfakeTokenFakeTokenFakeTokenFak"


def load(text):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, ".env")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return Config.load(path)


class SolamiConfigTests(unittest.TestCase):
    def test_defaults_are_the_solami_firehose(self):
        config = load("BOT_TOKEN=%s\nCHANNEL_ID=@c\n" % TOKEN)
        self.assertEqual(config.data_source, "solami")
        self.assertEqual(config.tape_mode, "firehose")
        self.assertEqual((config.large_trade_usd, config.liq_alert_usd, config.liq_report_adds),
                         (50000.0, 50000.0, False))
        self.assertEqual(config.solami_rpc_url, "https://rpc.solami.dev/sol")
        self.assertEqual(config.solami_ws_url, "wss://ws.solami.dev/data/subscribe")
        self.assertTrue(config.live_problems()[0].startswith("SOLAMI_API_KEY is empty"))

    def test_watch_mints_switch_auto_mode_to_watchlist_with_its_defaults(self):
        config = load("SOLAMI_API_KEY=sk_test\nWATCH_MINTS=%s, %s\n" % (DEMO, XTRA))
        self.assertEqual(config.tape_mode, "watchlist")
        self.assertEqual(config.watch_mints, [DEMO, XTRA])
        self.assertEqual((config.large_trade_usd, config.liq_alert_usd, config.liq_report_adds),
                         (5000.0, 2500.0, True))
        self.assertEqual(config.live_problems(), [])

    def test_explicit_values_beat_mode_defaults(self):
        config = load("WATCH_MINTS=%s\nLARGE_TRADE_USD=1000\nLIQ_REPORT_ADDS=false\nFLUSH_SEC=20\n" % DEMO)
        self.assertEqual((config.large_trade_usd, config.liq_report_adds, config.flush_sec), (1000.0, False, 20))

    def test_bad_values_are_reported_and_fall_back(self):
        config = load("DATA_SOURCE=carrier-pigeon\nTAPE_MODE=everything\nWATCH_MINTS=not-a-mint,%s\n"
                      "SOLAMI_WS_URL=http://nope\nFLUSH_SEC=1\nLIQ_REPORT_ADDS=maybe\n" % DEMO)
        keys = {key for key, _text in config.problems}
        self.assertTrue({"DATA_SOURCE", "TAPE_MODE", "WATCH_MINTS", "SOLAMI_WS_URL", "FLUSH_SEC",
                         "LIQ_REPORT_ADDS"} <= keys)
        self.assertEqual(config.data_source, "solami")
        self.assertEqual(config.watch_mints, [DEMO])
        self.assertEqual(config.solami_ws_url, "wss://ws.solami.dev/data/subscribe")
        self.assertEqual(config.flush_sec, 60)

    def test_explicit_watchlist_without_mints_is_a_blocking_problem(self):
        config = load("SOLAMI_API_KEY=sk_test\nTAPE_MODE=watchlist\n")
        self.assertIn("WATCH_MINTS", config.live_problems()[0])

    def test_key_type_hint_and_secrets(self):
        config = load("SOLAMI_API_KEY=rpc_abc\nBOT_TOKEN=%s\n" % TOKEN)
        self.assertTrue(any(key == "SOLAMI_API_KEY" for key, _ in config.problems))
        self.assertEqual(config.secrets, [TOKEN, "rpc_abc"])

    def test_old_solana_watchlist_entries_are_reused(self):
        config = load("WATCHLIST=solana:%s,base:0xabc\n" % DEMO)
        self.assertEqual(config.watch_mints, [DEMO])

    def test_dexscreener_source_needs_no_key(self):
        config = load("DATA_SOURCE=dexscreener\n")
        self.assertEqual(config.live_problems(), [])

    def test_parse_mints(self):
        self.assertEqual(parse_mints("%s,%s, bad0O" % (DEMO, DEMO)), ([DEMO], ["bad0O"]))


if __name__ == "__main__":
    unittest.main()
