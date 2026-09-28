"""Tests for alerts_bot.tape.loop - the run loop and publisher: bad events are skipped,
failed posts are retried, shutdown is clean. Replay source + fake bot; no network."""
import os
import tempfile
import threading
import unittest

from alerts_bot.dedup import DedupStore
from alerts_bot.solami.blur import ReplaySource
from alerts_bot.tape.engine import TapeEngine, TapeSettings
from alerts_bot.tape.format import FOOTER
from alerts_bot.tape.loop import Publisher, run_live
from tests.solami_helpers import DEMO, NOW, FakeBot, blur_lines


def settings():
    return TapeSettings(mode="watchlist", watch_mints=(DEMO,), large_trade_usd=5000.0, liq_alert_usd=2500.0,
                        liq_report_adds=True)


class InstantEvent(threading.Event):
    """stop_event whose wait() returns immediately - keeps publisher spacing out of tests."""

    def __init__(self):
        super().__init__()
        self.waits = []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        return self.is_set()


def run(engine, bot, lines=None, stop=None, **kwargs):
    stop = stop or InstantEvent()
    publisher = Publisher(bot, "@test", max_per_min=0, stop_event=stop)
    source = ReplaySource(lines=lines if lines is not None else blur_lines())
    return run_live(source, engine, publisher, stop, stop_when_exhausted=True, now_fn=lambda: NOW, **kwargs)


class LoopTests(unittest.TestCase):
    def test_replay_to_posts(self):
        bot = FakeBot()
        result = run(TapeEngine(settings(), now_fn=lambda: NOW), bot, final_reports=True)
        self.assertEqual(result["events"], 18)
        self.assertEqual(result["posts_sent"], 2)                  # the tape post + the summary
        tape, summary = bot.sent[0][1], bot.sent[1][1]
        self.assertIn("Large trades", tape)
        self.assertIn("Tape summary", summary)
        for _chat, text in bot.sent:
            self.assertTrue(text.endswith(FOOTER))

    def test_one_bad_event_never_stops_the_loop(self):
        class Fragile(TapeEngine):
            def handle(self, event):
                if event.type == "candle":
                    raise ValueError("boom")
                super().handle(event)

        bot = FakeBot()
        result = run(Fragile(settings(), now_fn=lambda: NOW), bot)
        self.assertEqual(result["event_errors"], 1)
        self.assertEqual(result["posts_sent"], 1)

    def test_failed_post_is_retried_on_the_next_flush(self):
        bot = FakeBot(fail_times=1)
        engine = TapeEngine(settings(), now_fn=lambda: NOW)
        result = run(engine, bot)
        self.assertEqual(result["posts_failed"], 1)
        self.assertEqual(len(engine.pending), len({i.key for i in engine.pending}))
        self.assertTrue(engine.pending)                             # re-queued, not lost
        publisher = Publisher(bot, "@test", max_per_min=0)
        for post in engine.flush(NOW):
            engine.confirm(post, publisher.publish(post.text))
        self.assertEqual(len(bot.sent), 1)

    def test_stop_event_ends_the_loop_and_still_flushes(self):
        stop = InstantEvent()
        engine = TapeEngine(settings(), now_fn=lambda: NOW)
        for line in blur_lines():
            from alerts_bot.solami.events import parse_blur_message
            for event in parse_blur_message(line):
                engine.handle(event)
        stop.set()
        bot = FakeBot()
        result = run(engine, bot, lines=[], stop=stop)
        self.assertEqual(result["events"], 0)
        self.assertEqual(len(bot.sent), 1)                          # queued items went out on shutdown

    def test_heartbeat_file_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "heartbeat.json")
            run(TapeEngine(settings(), now_fn=lambda: NOW), FakeBot(), heartbeat_path=path)
            with open(path, encoding="utf-8") as fh:
                content = fh.read()
        self.assertIn('"stopped": true', content)
        self.assertIn('"replay"', content)

    def test_restart_does_not_repost(self):
        store = DedupStore(":memory:", clock=lambda: NOW)
        first, second = FakeBot(), FakeBot()
        run(TapeEngine(settings(), store=store, now_fn=lambda: NOW), first)
        run(TapeEngine(settings(), store=store, now_fn=lambda: NOW), second)
        store.close()
        self.assertEqual(len(first.sent), 1)
        self.assertEqual(second.sent, [])


class SecretScrubbingTests(unittest.TestCase):
    def test_key_in_an_exception_message_is_scrubbed_from_the_traceback(self):
        import logging
        from alerts_bot.telegram import TokenFilter
        record = None
        try:
            raise RuntimeError("failed calling https://rpc.solami.dev/sol?api_key=sk_live_SECRET")
        except RuntimeError:
            import sys
            record = logging.LogRecord("x", logging.ERROR, __file__, 1, "rpc failed", (), sys.exc_info())
        TokenFilter("sk_live_SECRET").filter(record)
        rendered = logging.Formatter().format(record)
        self.assertNotIn("sk_live_SECRET", rendered)
        self.assertIn("api_key=<TOKEN>", rendered)


class PublisherTests(unittest.TestCase):
    def test_posts_are_spaced_to_respect_the_per_minute_cap(self):
        clock = {"t": 100.0}
        stop = InstantEvent()
        publisher = Publisher(FakeBot(), "@c", max_per_min=6, stop_event=stop, clock=lambda: clock["t"])
        publisher.publish("one")
        clock["t"] += 4.0
        publisher.publish("two")
        self.assertEqual(stop.waits, [6.0])                         # 60/6 = 10 s gap, 4 s already passed

    def test_telegram_failure_returns_false(self):
        publisher = Publisher(FakeBot(fail_times=1), "@c", max_per_min=0)
        self.assertFalse(publisher.publish("x"))
        self.assertTrue(publisher.publish("y"))
        self.assertEqual((publisher.sent, publisher.failed), (1, 1))


if __name__ == "__main__":
    unittest.main()
