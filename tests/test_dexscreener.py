"""Tests for alerts_bot.dexscreener - a fake urllib opener stands in for the network, so
these never make a real HTTP call."""
import unittest
import urllib.error

from alerts_bot.dexscreener import Cache, DexScreenerClient, DexScreenerError, RateLimiter
from tests.helpers import fake_opener, load_fixture


class SearchPairsTests(unittest.TestCase):
    def test_unwraps_pairs_key(self):
        payload = {"schemaVersion": "1.0.0", "pairs": [{"chainId": "solana", "pairAddress": "x"}]}
        opener = fake_opener(payload)
        client = DexScreenerClient(opener=opener, min_request_interval=0)
        result = client.search_pairs("SOL")
        self.assertEqual(result, payload["pairs"])
        self.assertEqual(len(opener.calls), 1)
        self.assertIn("/latest/dex/search", opener.calls[0])
        self.assertIn("q=SOL", opener.calls[0])

    def test_real_fixture_shape(self):
        payload = load_fixture("search_response.json")
        client = DexScreenerClient(opener=fake_opener(payload), min_request_interval=0)
        result = client.search_pairs("SOL")
        self.assertEqual(len(result), 6)

    def test_unexpected_shape_returns_empty_list_not_crash(self):
        client = DexScreenerClient(opener=fake_opener(["not", "a", "dict"]), min_request_interval=0)
        self.assertEqual(client.search_pairs("x"), [])


class GetTokenPairsTests(unittest.TestCase):
    def test_bare_array_shape(self):
        payload = load_fixture("token_pairs_response.json")
        opener = fake_opener(payload)
        client = DexScreenerClient(opener=opener, min_request_interval=0)
        result = client.get_token_pairs("base", "tokenWATCH")
        self.assertEqual(result, payload)
        self.assertIn("/token-pairs/v1/base/tokenWATCH", opener.calls[0])

    def test_unexpected_shape_returns_empty_list_not_crash(self):
        client = DexScreenerClient(opener=fake_opener({"not": "a list"}), min_request_interval=0)
        self.assertEqual(client.get_token_pairs("base", "x"), [])


class CachingTests(unittest.TestCase):
    def test_identical_request_hits_network_once(self):
        opener = fake_opener({"schemaVersion": "1.0.0", "pairs": []})
        client = DexScreenerClient(opener=opener, min_request_interval=0, cache_ttl=60)
        client.search_pairs("SOL")
        client.search_pairs("SOL")
        self.assertEqual(len(opener.calls), 1)

    def test_different_queries_are_not_shared(self):
        opener = fake_opener({"schemaVersion": "1.0.0", "pairs": []})
        client = DexScreenerClient(opener=opener, min_request_interval=0, cache_ttl=60)
        client.search_pairs("SOL")
        client.search_pairs("BASE")
        self.assertEqual(len(opener.calls), 2)

    def test_cache_expiry(self):
        clock = {"t": 0.0}
        cache = Cache(ttl=5.0, clock=lambda: clock["t"])
        opener = fake_opener({"schemaVersion": "1.0.0", "pairs": []})
        client = DexScreenerClient(opener=opener, min_request_interval=0, cache=cache)
        client.search_pairs("SOL")
        clock["t"] = 10.0  # past the 5s TTL
        client.search_pairs("SOL")
        self.assertEqual(len(opener.calls), 2)


class RateLimiterTests(unittest.TestCase):
    def test_waits_between_calls(self):
        clock = {"t": 0.0}
        sleeps = []
        limiter = RateLimiter(min_interval=2.0, clock=lambda: clock["t"], sleep=sleeps.append)
        limiter.wait()
        clock["t"] = 0.5
        limiter.wait()
        self.assertEqual(sleeps, [1.5])

    def test_no_wait_once_interval_has_passed(self):
        clock = {"t": 0.0}
        sleeps = []
        limiter = RateLimiter(min_interval=2.0, clock=lambda: clock["t"], sleep=sleeps.append)
        limiter.wait()
        clock["t"] = 5.0
        limiter.wait()
        self.assertEqual(sleeps, [])


class ErrorHandlingTests(unittest.TestCase):
    def test_http_error_raises_dexscreener_error(self):
        def opener(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 500, "boom", hdrs=None, fp=None)
        client = DexScreenerClient(opener=opener, min_request_interval=0)
        with self.assertRaises(DexScreenerError):
            client.search_pairs("SOL")

    def test_bad_json_raises_dexscreener_error(self):
        class BadResponse:
            def read(self):
                return b"not json"

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        client = DexScreenerClient(opener=lambda request, timeout=None: BadResponse(), min_request_interval=0)
        with self.assertRaises(DexScreenerError):
            client.search_pairs("SOL")


if __name__ == "__main__":
    unittest.main()
