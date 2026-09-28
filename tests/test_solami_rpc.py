"""Tests for alerts_bot.solami.rpc and data_api - recorded JSON-RPC response shapes served
by a fake opener. No network."""
import io
import unittest
import urllib.error

from alerts_bot.solami.data_api import SolamiDataAPI, parse_metadata_response
from alerts_bot.solami.rpc import RateLimiter, SolamiRPC, SolamiRPCError, parse_largest_accounts
from alerts_bot.tape.flags import mint_flags
from tests.solami_helpers import AUTH, DEMO, HOLD_A, HOLD_B, HOLD_C, FakeOpener, load_json

KEY = "sk_test_DO_NOT_LEAK_123"


def make_rpc(*responses, **kwargs):
    opener = FakeOpener(*responses)
    sleeps = []
    rpc = SolamiRPC(KEY, opener=opener, sleep=sleeps.append, rng=lambda: 0.5, max_rps=0, **kwargs)
    return rpc, opener, sleeps


def http_error(code, body=b'{"message":"unauthorized"}'):
    return urllib.error.HTTPError("https://rpc.example/sol", code, "err", hdrs=None, fp=io.BytesIO(body))


class MintInfoTests(unittest.TestCase):
    def test_classic_spl_mint_with_revoked_authorities(self):
        rpc, opener, _ = make_rpc(load_json("rpc_mint_spl.json"))
        info = rpc.get_mint_info(DEMO)
        self.assertEqual((info.program, info.decimals, info.supply), ("spl-token", 6, 1_000_000_000_000_000))
        self.assertIsNone(info.mint_authority)
        self.assertIsNone(info.freeze_authority)
        self.assertEqual(info.ui_supply, 1_000_000_000.0)
        self.assertEqual(mint_flags(info), [])
        request = opener.requests[0]
        self.assertEqual(request["body"]["method"], "getAccountInfo")
        self.assertEqual(request["body"]["params"], [DEMO, {"encoding": "jsonParsed", "commitment": "confirmed"}])
        self.assertIn("api_key=" + KEY, request["url"])

    def test_token_2022_mint_flags_every_documented_rule(self):
        rpc, _opener, _ = make_rpc(load_json("rpc_mint_token2022.json"))
        info = rpc.get_mint_info(DEMO)
        self.assertEqual(info.program, "spl-token-2022")
        self.assertEqual(info.mint_authority, AUTH)
        self.assertIn("transferFeeConfig", info.extensions)
        text = "\n".join(mint_flags(info))
        for expected in ("Mint authority active", "Freeze authority active", "permanent delegate",
                         "transfer fee 2.50%", "transfer hook", "non-transferable"):
            self.assertIn(expected, text)

    def test_not_a_mint_and_missing_account_give_none(self):
        rpc, _opener, _ = make_rpc(load_json("rpc_account_not_mint.json"), load_json("rpc_account_missing.json"))
        self.assertIsNone(rpc.get_mint_info(DEMO))
        self.assertIsNone(rpc.get_mint_info(DEMO))


class LargestAccountsTests(unittest.TestCase):
    def test_v2_shape(self):
        rpc, opener, _ = make_rpc(load_json("rpc_largest_v2.json"))
        top = rpc.get_largest_token_accounts(DEMO)
        self.assertEqual([a.address for a in top], [HOLD_A, HOLD_B, HOLD_C])
        self.assertEqual(top[0].amount, 150_000_000_000_000)
        self.assertEqual(opener.requests[0]["body"]["method"], "getTokenLargestAccountsV2")

    def test_stock_shape_is_accepted_and_sorted(self):
        self.assertEqual([a.address for a in parse_largest_accounts(DEMO, load_json("rpc_largest_stock.json")["result"])],
                         [HOLD_A, HOLD_B, HOLD_C])

    def test_falls_back_to_the_stock_method_only_on_method_not_found(self):
        rpc, opener, _ = make_rpc(load_json("rpc_error_method_not_found.json"), load_json("rpc_largest_stock.json"),
                                  load_json("rpc_largest_stock.json"))
        self.assertEqual(len(rpc.get_largest_token_accounts(DEMO)), 3)
        self.assertEqual(len(rpc.get_largest_token_accounts(DEMO)), 3)
        self.assertEqual([r["body"]["method"] for r in opener.requests],
                         ["getTokenLargestAccountsV2", "getTokenLargestAccounts", "getTokenLargestAccounts"])

    def test_other_errors_are_not_papered_over(self):
        rpc, _opener, _ = make_rpc(load_json("rpc_error_invalid_params.json"))
        with self.assertRaises(SolamiRPCError) as ctx:
            rpc.get_largest_token_accounts(DEMO)
        self.assertEqual(ctx.exception.code, -32602)


class TokenAccountsTests(unittest.TestCase):
    def test_balances_owner_and_closed_accounts(self):
        rpc, _opener, _ = make_rpc(load_json("rpc_multiple_accounts.json"))
        result = rpc.get_token_accounts(["AcctOne", "AcctClosed"])
        self.assertEqual(result["AcctOne"].amount, 1_000_000)
        self.assertEqual(result["AcctOne"].owner, "WaLetD11111111111111111111111111111111111111")
        self.assertTrue(result["AcctClosed"].closed)
        self.assertEqual(result["AcctClosed"].amount, 0)

    def test_chunks_of_100(self):
        addresses = ["A%d" % i for i in range(150)]
        rpc, opener, _ = make_rpc({"jsonrpc": "2.0", "id": 1, "result": {"value": [None] * 100}},
                                  {"jsonrpc": "2.0", "id": 2, "result": {"value": [None] * 50}})
        result = rpc.get_token_accounts(addresses)
        self.assertEqual(len(result), 150)
        self.assertEqual([len(r["body"]["params"][0]) for r in opener.requests], [100, 50])

    def test_length_mismatch_is_an_error_not_a_guess(self):
        rpc, _opener, _ = make_rpc({"jsonrpc": "2.0", "id": 1, "result": {"value": [None]}})
        with self.assertRaises(SolamiRPCError):
            rpc.get_token_accounts(["A", "B"])


class RetryTests(unittest.TestCase):
    def test_rate_limited_body_is_retried_with_backoff(self):
        rpc, opener, sleeps = make_rpc(load_json("rpc_error_rate_limited.json"),
                                       load_json("rpc_error_rate_limited.json"), load_json("rpc_slot.json"))
        self.assertEqual(rpc.get_slot(), 370000123)
        self.assertEqual(len(opener.requests), 3)
        self.assertEqual(sleeps, [0.1, 0.2])            # 0.1 * 2^n * (0.5 + rng 0.5)

    def test_invalid_params_is_not_retried(self):
        rpc, opener, _ = make_rpc(load_json("rpc_error_invalid_params.json"))
        with self.assertRaises(SolamiRPCError):
            rpc.get_slot()
        self.assertEqual(len(opener.requests), 1)

    def test_http_401_is_not_retried_and_never_shows_the_key(self):
        rpc, opener, _ = make_rpc(http_error(401))
        with self.assertRaises(SolamiRPCError) as ctx:
            rpc.get_slot()
        self.assertEqual(ctx.exception.http_status, 401)
        self.assertEqual(len(opener.requests), 1)
        self.assertNotIn(KEY, str(ctx.exception))

    def test_http_503_and_network_errors_are_retried_then_raised(self):
        rpc, opener, _ = make_rpc(http_error(503, b"busy"), urllib.error.URLError("timed out"),
                                  urllib.error.URLError("timed out"), urllib.error.URLError("timed out"))
        with self.assertRaises(SolamiRPCError):
            rpc.get_slot()
        self.assertEqual(len(opener.requests), 4)       # 1 try + 3 retries

    def test_non_json_body_is_an_error(self):
        class BadOpener(FakeOpener):
            def __call__(self, request, timeout=None):
                class R:
                    def read(self):
                        return b"<html>"

                    def __enter__(self):
                        return self

                    def __exit__(self, *exc):
                        return False
                return R()
        rpc = SolamiRPC(KEY, opener=BadOpener(), sleep=lambda s: None, max_rps=0, retries=0)
        with self.assertRaises(SolamiRPCError):
            rpc.get_slot()


class RateLimiterTests(unittest.TestCase):
    def test_spacing(self):
        clock = {"t": 0.0}
        sleeps = []
        limiter = RateLimiter(max_rps=4, clock=lambda: clock["t"], sleep=sleeps.append)
        limiter.wait()
        limiter.wait()
        self.assertEqual(sleeps, [0.25])


class DataApiTests(unittest.TestCase):
    def test_metadata_lookup_sends_the_key_as_header(self):
        opener = FakeOpener(load_json("data_api_metadata.json"))
        api = SolamiDataAPI(KEY, opener=opener, sleep=lambda s: None, max_rps=0)
        self.assertEqual(api.token_metadata(DEMO), {"symbol": "DEMO", "name": "Demo Tape Token", "decimals": 6})
        request = opener.requests[0]
        self.assertIn("/data/token/metadata?address=" + DEMO, request["url"])
        self.assertNotIn(KEY, request["url"])
        self.assertEqual(request["headers"].get("X-api-key"), KEY)

    def test_failures_and_unknown_shapes_return_none(self):
        opener = FakeOpener(urllib.error.URLError("down"), {"unexpected": True})
        api = SolamiDataAPI(KEY, opener=opener, sleep=lambda s: None, max_rps=0)
        self.assertIsNone(api.token_metadata(DEMO))
        self.assertIsNone(api.token_metadata(DEMO))

    def test_nested_and_list_shapes(self):
        self.assertEqual(parse_metadata_response({"data": {"symbol": "X"}})["symbol"], "X")
        self.assertEqual(parse_metadata_response([{"name": "Y"}])["name"], "Y")
        self.assertIsNone(parse_metadata_response([]))


if __name__ == "__main__":
    unittest.main()
