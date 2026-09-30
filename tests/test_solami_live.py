"""The first live run of `main.py --check-live 60` (2026-09-30) against the parser, and the rule that a
key never reaches a channel: Solami's `metadata` frames give the token picture as a link with
`?api_key=<the key of the account>`. No network; the key in these tests is made up."""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

from alerts_bot.solami.events import (Graduation, LiquidityChange, OtherEvent, PoolCreated, TokenLaunch,
                                      TokenMetadata, VolumeBreakout, parse_blur_message)
from alerts_bot.telegram import BotAPI, without_secrets
from tests.solami_helpers import SOLAMI_FIXTURES, USDC, WSOL
from tests.test_tape_engine import EngineCase, fire_settings

import main

LIVE = os.path.join(SOLAMI_FIXTURES, "live_frames_2026-09-30.jsonl")
KEY = "sk_test_MADEUPMADEUPMADEUPMADEUPMADEUPMADEUP12"      # the shape of a key, not a key
PICTURE = "https://api.solami.dev/data/token/image/DEW9dSN6QpWyNthphCpMmAbZP1Q4cEKR9xQXAri98WDP"


def live_frames():
    with open(LIVE, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip() and not line.startswith("#")]


def live_events():
    return [event for frame in live_frames() for event in parse_blur_message(json.dumps(frame))]


def of_type(event_type, n=0):
    return [e for e in live_events() if e.type == event_type][n]


class LiveFieldsTests(unittest.TestCase):
    """Assumptions A1, A2, A4, A5 of the README against real frames."""

    def test_every_type_of_the_live_run_is_parsed(self):
        kinds = {e.type: type(e) for e in live_events()}
        self.assertEqual(kinds, {"connected": OtherEvent, "graduation": Graduation, "liquidity": LiquidityChange,
                                 "metadata": TokenMetadata, "pool_create": PoolCreated, "radar": VolumeBreakout,
                                 "surge": VolumeBreakout, "token_create": TokenLaunch})

    def test_liquidity_legs_and_their_usd_values(self):
        remove = of_type("liquidity")
        self.assertEqual((remove.kind, remove.mint, remove.quote_mint, remove.dex), (
            "remove", "6VGac5U864uJnkqzP5wbDEGwE6Tv2Ktk5T9EDHoNMvoJ", WSOL, "fluxbeam"))
        self.assertEqual((remove.base_amount, remove.quote_amount, remove.base_decimals, remove.quote_decimals),
                         (1323893806, 683508, 6, 9))
        self.assertEqual((remove.base_usd, remove.quote_usd), (0.0, 0.08056211148771239))
        self.assertIsNone(remove.value_usd)                  # no single USD field in the live shape
        add = of_type("liquidity", 1)
        self.assertEqual((add.kind, add.quote_mint, add.base_usd, add.quote_usd),
                         ("add", USDC, 43.90856501386503, 43.846724))
        self.assertEqual((add.base_reserve, add.quote_reserve), (2437147742023, 427220318901))

    def test_metadata_takes_the_picture_without_a_key(self):
        meta = of_type("metadata")
        self.assertEqual((meta.mint, meta.symbol, meta.name, meta.decimals),
                         ("DEW9dSN6QpWyNthphCpMmAbZP1Q4cEKR9xQXAri98WDP", "SI", "Super Inu", 6))
        self.assertTrue(meta.logo.startswith("https://axiomtrading."))   # logo_uri, not image_url

    def test_pool_create_graduation_launch_and_breakouts(self):
        pool = of_type("pool_create")
        self.assertEqual((pool.mint, pool.quote_mint, pool.dex), (
            "7sL6aMg3yNMosBH4qTUa2gUXZm1PaPQGPj8fBNpoJxBj", WSOL, "meteora_dbc"))
        grad = of_type("graduation")
        self.assertEqual((grad.launchpad, grad.dex, grad.pool, grad.signature), (
            "meteora_dbc", "meteora_damm2", "4kVsuy9z4bnNHZoFX15cs7g2XoCkxPXNQf8ZPMRG9YeS", ""))
        self.assertTrue(grad.key.startswith("graduation:h"))    # no signature: the frame itself is the identity
        launch = of_type("token_create")
        self.assertEqual((launch.symbol, launch.name, launch.dex, launch.quote_mint), ("AIRPAD", "AirPad", "pumpfun", WSOL))
        surge, radar = of_type("surge"), of_type("radar")
        self.assertEqual((surge.window_secs, radar.window_secs), (300, 1800))
        self.assertAlmostEqual(surge.multiple, 4.886460453412017)
        self.assertEqual(surge.block_time, surge.trigger_time)


class LiveValuationTests(EngineCase):
    def test_both_legs_valued_by_the_stream_are_exact(self):
        engine = self.make(fire_settings())
        self.assertEqual(engine.liquidity_usd(of_type("liquidity", 1), None), (43.90856501386503 + 43.846724, False))

    def test_a_leg_without_a_price_is_not_counted_as_zero(self):
        engine = self.make(fire_settings())
        usd, approx = engine.liquidity_usd(of_type("liquidity"), None)      # base_usd "0": no price for it
        self.assertAlmostEqual(usd, 2 * 0.08056211148771239)
        self.assertTrue(approx)

    def test_a_large_live_shaped_removal_reaches_the_tape_without_any_swap_seen(self):
        frame = dict(live_frames()[3], kind="remove", base_usd="30000.5", quote_usd="29999.5", signature="SigBig")
        engine = self.make(fire_settings(liq_alert_usd=50000.0))
        for event in parse_blur_message(json.dumps(frame)):
            engine.handle(event)
        items = [i for i in engine.pending if i.section == "liquidity"]
        self.assertEqual([(i.data["usd"], i.data["approx"]) for i in items], [(60000.0, False)])


class NoKeyInTheChannelTests(unittest.TestCase):
    def test_a_key_parameter_is_cut_out_of_every_link(self):
        for text, kept in (
                (PICTURE + "?api_key=" + KEY, PICTURE),
                ('<a href="https://ws.example/sub?chain=solana&amp;api_key=%s&amp;type=swap">x</a>' % KEY,
                 '<a href="https://ws.example/sub?chain=solana&amp;type=swap">x</a>'),
                ("wss://ws.solami.dev/data/subscribe?api_key=%s&type=swap" % KEY,
                 "wss://ws.solami.dev/data/subscribe?type=swap"),
                ("https://x.io/a?token=abc&x=1 and https://x.io/b?key=abc", "https://x.io/a?x=1 and https://x.io/b"),
                ("https://x.io/a?Access_Token=abc#top", "https://x.io/a#top"),
                ("https://x.io/a?monkey=1&type=swap", "https://x.io/a?monkey=1&type=swap"),   # not a key
                ("Is it safe? Yes. Price: 1?", "Is it safe? Yes. Price: 1?")):               # no link, no change
            self.assertEqual(without_secrets(text), kept, text)

    def test_a_known_secret_is_hidden_wherever_it_stands(self):
        self.assertEqual(without_secrets("https://x.io/%s/logo.png key %s" % (KEY, KEY), (KEY, "")),
                         "https://x.io/<hidden>/logo.png key <hidden>")

    def test_the_bot_sends_no_key(self):
        sent = []

        class Answer(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def urlopen(request, timeout=None):
            sent.append(json.loads(request.data.decode("utf-8")))
            return Answer(b'{"ok": true, "result": {"message_id": 1}}')

        bot = BotAPI("123456:" + "A" * 35, secrets=(KEY,))
        with mock.patch("urllib.request.urlopen", urlopen):
            bot.send_message("@tape", '<a href="%s?api_key=%s">logo</a> %s' % (PICTURE, KEY, KEY))
        self.assertEqual(sent[0]["text"], '<a href="%s">logo</a> <hidden>' % PICTURE)

    def test_a_dry_run_prints_what_would_be_sent(self):
        bot = main.DryRunBot((KEY,))
        with redirect_stdout(io.StringIO()) as out:
            bot.send_message("@tape", "%s?api_key=%s" % (PICTURE, KEY))
        self.assertNotIn(KEY, out.getvalue())
        self.assertEqual(bot.sent, [("@tape", PICTURE)])

    def test_the_live_check_prints_and_records_no_key(self):
        frame = dict(live_frames()[4], image_url="%s?api_key=%s" % (PICTURE, KEY))
        raw = json.dumps(frame)

        class Stream:
            def __init__(self, api_key, subscriptions, base_url=None, on_raw=None, **_kwargs):
                self.on_raw, self.events = on_raw, parse_blur_message(raw)

            def start(self):
                if self.on_raw:
                    self.on_raw("market", raw)

            def get(self, timeout=None):
                return self.events.pop(0) if self.events else None

            def stop(self):
                pass

            def health_summary(self):
                return {"market": {"connects": 1, "messages": 1, "parse_errors": 0, "last_error": ""}}

        class RPC:
            def __init__(self, *args, **kwargs):
                pass

            def get_slot(self):
                return 451911421

        config = SimpleNamespace(solami_api_key=KEY, solami_rpc_url="https://rpc.example", request_timeout_sec=5,
                                 solami_rpc_max_rps=5, solami_ws_url="wss://ws.example/data/subscribe")
        with tempfile.TemporaryDirectory() as tmp:
            record = os.path.join(tmp, "frames.jsonl")
            with mock.patch("alerts_bot.solami.BlurStream", Stream), mock.patch("alerts_bot.solami.SolamiRPC", RPC), \
                    mock.patch("time.monotonic", side_effect=[0.0, 0.0, 99.0]), redirect_stdout(io.StringIO()) as out:
                code = main.check_live(config, SimpleNamespace(watch_mints=()), [], 5, record, None)
            with open(record, encoding="utf-8") as fh:
                recorded = fh.read()
        self.assertEqual(code, 0)
        self.assertIn("RESULT: OK", out.getvalue())
        self.assertIn(PICTURE, out.getvalue())
        for text in (out.getvalue(), recorded):
            self.assertNotIn(KEY, text)
            self.assertNotIn("api_key", text)
        self.assertEqual(json.loads(recorded)["image_url"], PICTURE)


if __name__ == "__main__":
    unittest.main()
