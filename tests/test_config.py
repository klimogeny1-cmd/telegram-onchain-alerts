"""Tests for alerts_bot.config - a temp .env file on disk, never the real process env."""
import os
import tempfile
import unittest

from alerts_bot.config import Config, parse_watchlist, read_env_file


def write_env(tmpdir, text):
    path = os.path.join(tmpdir, ".env")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


class ReadEnvFileTests(unittest.TestCase):
    def test_missing_file_returns_none(self):
        self.assertIsNone(read_env_file("/does/not/exist/.env"))

    def test_parses_key_value_lines_comments_and_quotes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_env(tmp, "\n".join([
                "# a comment",
                "BOT_TOKEN=123:abc",
                "CHANNEL_ID='@mychannel'",
                'CHAINS="solana,base"  # inline comment',
                "export INTERVAL_MIN=30",
                "",
                "not a valid line without equals",
            ]))
            values = read_env_file(path)
        self.assertEqual(values["BOT_TOKEN"], "123:abc")
        self.assertEqual(values["CHANNEL_ID"], "@mychannel")
        self.assertEqual(values["CHAINS"], "solana,base")
        self.assertEqual(values["INTERVAL_MIN"], "30")


class ParseWatchlistTests(unittest.TestCase):
    def test_valid_entries(self):
        entries, bad = parse_watchlist("solana:AAA, base:0xBBB")
        self.assertEqual(entries, [("solana", "AAA"), ("base", "0xBBB")])
        self.assertEqual(bad, [])

    def test_bad_entries_are_reported_not_raised(self):
        entries, bad = parse_watchlist("no-colon-here, :missing-chain, solana:")
        self.assertEqual(entries, [])
        self.assertEqual(bad, ["no-colon-here", ":missing-chain", "solana:"])


class ConfigLoadTests(unittest.TestCase):
    def test_no_env_file_uses_defaults_and_is_not_ready(self):
        config = Config.load(env_path="/does/not/exist/.env")
        self.assertFalse(config.env_found)
        self.assertFalse(config.ready)
        self.assertEqual(config.chains, ["solana"])
        self.assertEqual(config.interval_min, 60)

    def test_full_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_env(tmp, "\n".join([
                "BOT_TOKEN=123456789:AAEfakeTokenFakeTokenFakeTokenFak",
                "CHANNEL_ID=@mychannel",
                "CHAINS=solana, base",
                "INTERVAL_MIN=30",
                "MIN_LIQUIDITY_USD=10000",
                "WATCHLIST=solana:AAA,base:BBB",
            ]))
            config = Config.load(path)
        self.assertTrue(config.env_found)
        self.assertTrue(config.token_ok)
        self.assertTrue(config.channel_ok)
        self.assertTrue(config.ready)
        self.assertEqual(config.chains, ["solana", "base"])
        self.assertEqual(config.interval_min, 30)
        self.assertEqual(config.new_pair_window_min, 30)  # falls back to interval_min
        self.assertEqual(config.min_liquidity_usd, 10000.0)
        self.assertEqual(config.watchlist, [("solana", "AAA"), ("base", "BBB")])
        self.assertEqual(config.problems, [])

    def test_bad_numeric_value_is_reported_and_falls_back_to_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_env(tmp, "INTERVAL_MIN=not-a-number\n")
            config = Config.load(path)
        self.assertEqual(config.interval_min, 60)
        self.assertTrue(any(key == "INTERVAL_MIN" for key, _ in config.problems))

    def test_token_ok_rejects_malformed_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_env(tmp, "BOT_TOKEN=not-a-real-token\nCHANNEL_ID=@x\n")
            config = Config.load(path)
        self.assertFalse(config.token_ok)
        self.assertFalse(config.ready)


if __name__ == "__main__":
    unittest.main()
