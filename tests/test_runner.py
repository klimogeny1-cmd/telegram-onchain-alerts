"""Integration-style tests for alerts_bot.runner: a fake DexScreener client (serves fixed
fixture data, no network) plus a fake bot (records what would be sent, no Telegram) drive
the whole fetch -> analyze -> post -> dedup pipeline in one call."""
import unittest

from alerts_bot.config import Config
from alerts_bot.dedup import DedupStore
from alerts_bot.runner import discovery_queries_for, run_cycle
from tests.helpers import NOW, load_fixture


class FakeClient:
    """Serves the shared fixtures regardless of query text - good enough for exercising
    the full pipeline without caring which discovery query "found" which pair."""

    def __init__(self):
        self.search_calls = []
        self.token_pairs_calls = []

    def search_pairs(self, query):
        self.search_calls.append(query)
        return load_fixture("search_response.json")["pairs"]

    def get_token_pairs(self, chain_id, token_address):
        self.token_pairs_calls.append((chain_id, token_address))
        return load_fixture("token_pairs_response.json")


class FakeBot:
    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text, **_kwargs):
        self.sent.append((chat_id, text))
        return {"message_id": len(self.sent)}


def make_config(**overrides):
    kwargs = dict(bot_token="123456789:AAEfakeTokenFakeTokenFakeTokenFak", channel_id="@mychannel",
                 chains=["solana"], interval_min=60, min_liquidity_usd=5000.0)
    kwargs.update(overrides)
    return Config(**kwargs)


class DiscoveryQueriesForTests(unittest.TestCase):
    def test_explicit_queries_override_hints(self):
        self.assertEqual(discovery_queries_for(["solana", "base"], ["FOO"]), ["FOO"])

    def test_default_hints_per_chain_are_deduplicated(self):
        result = discovery_queries_for(["base", "arbitrum"], [])
        self.assertEqual(result, ["WETH"])  # both chains share the same hint

    def test_unknown_chain_falls_back_to_usdc(self):
        self.assertEqual(discovery_queries_for(["some-new-chain"], []), ["USDC"])

    def test_solana_hint(self):
        self.assertEqual(discovery_queries_for(["solana"], []), ["SOL"])


class RunCycleTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.bot = FakeBot()
        self.dedup_store = DedupStore(":memory:", clock=lambda: NOW)
        self.config = make_config()

    def tearDown(self):
        self.dedup_store.close()

    def test_first_cycle_sends_new_pairs_and_volume_alerts(self):
        result = run_cycle(self.client, self.bot, self.dedup_store, self.config, now=NOW)
        self.assertEqual(result["posts_sent"], 2)  # one "new pairs" post, one "volume anomalies" post
        self.assertEqual(len(self.bot.sent), 2)
        new_pairs_post = next(text for _chat, text in self.bot.sent if "New pairs" in text)
        volume_post = next(text for _chat, text in self.bot.sent if "Volume anomalies" in text)
        self.assertIn("AAA/SOL", new_pairs_post)
        self.assertIn("BBB/SOL", volume_post)
        # pairCCCC (liquidity $800) must never reach a post - it's below MIN_LIQUIDITY_USD
        self.assertNotIn("CCC/SOL", new_pairs_post)

    def test_second_cycle_does_not_repeat_the_same_alerts(self):
        run_cycle(self.client, self.bot, self.dedup_store, self.config, now=NOW)
        self.bot.sent.clear()
        result = run_cycle(self.client, self.bot, self.dedup_store, self.config, now=NOW)
        self.assertEqual(result["posts_sent"], 0)
        self.assertEqual(self.bot.sent, [])

    def test_watchlist_pair_is_included_via_token_pairs_endpoint(self):
        config = make_config(chains=["base"], watchlist=[("base", "tokenWATCH")], new_pair_window_min=999999)
        result = run_cycle(self.client, self.bot, self.dedup_store, config, now=NOW)
        self.assertEqual(self.client.token_pairs_calls, [("base", "tokenWATCH")])
        self.assertGreaterEqual(result["candidates"], 1)

    def test_network_error_on_one_query_does_not_abort_the_cycle(self):
        class FlakyClient(FakeClient):
            def search_pairs(self, query):
                if query == "SOL":
                    raise RuntimeError("simulated network failure")
                return super().search_pairs(query)

        config = make_config(discovery_queries=["SOL", "USDC"])
        result = run_cycle(FlakyClient(), self.bot, self.dedup_store, config, now=NOW)
        self.assertGreaterEqual(result["candidates"], 1)  # the USDC query still went through

    def test_low_liquidity_floor_is_applied_before_analysis(self):
        config = make_config(min_liquidity_usd=1_000_000.0)  # above every fixture pair
        result = run_cycle(self.client, self.bot, self.dedup_store, config, now=NOW)
        self.assertEqual(result["candidates"], 0)
        self.assertEqual(result["posts_sent"], 0)


if __name__ == "__main__":
    unittest.main()
