"""Tests for alerts_bot.solami.blur - subscriptions, reconnect/backoff decisions and the
reader threads, with fake connections instead of sockets. No network."""
import json
import time
import unittest

from alerts_bot.solami import websocket
from alerts_bot.solami.blur import (AUTH_RETRY_SECONDS, SLOW_RETRY_SECONDS, Backoff, BlurStream, BlurSubscription,
                                    ReplaySource, redact)
from tests.solami_helpers import BLUR_FRAMES, DEMO, blur_lines

KEY = "sk_test_DO_NOT_LEAK_123"


class FakeConn:
    """Serves `messages`, then either raises ConnectionClosed(close_code) or idles."""

    def __init__(self, messages=(), close_code=None, close_reason="", idle_forever=False):
        self.messages = list(messages)
        self.close_code = close_code
        self.close_reason = close_reason
        self.idle_forever = idle_forever
        self.sent = []
        self.pings = 0
        self.closed = False
        self.last_received = time.monotonic()

    def recv(self, timeout=None):
        if self.messages:
            self.last_received = time.monotonic()
            return self.messages.pop(0)
        if self.close_code is not None and not self.idle_forever:
            raise websocket.ConnectionClosed(self.close_code, self.close_reason)
        time.sleep(0.005)
        return None

    def send_text(self, text):
        self.sent.append(text)

    def ping(self, payload=b""):
        self.pings += 1

    def close(self, code=1000, reason="", timeout=1.0):
        self.closed = True


class FakeConnector:
    def __init__(self, *sessions):
        self.sessions = list(sessions)
        self.urls = []

    def __call__(self, url, timeout=None):
        self.urls.append(url)
        if not self.sessions:
            return FakeConn(idle_forever=True)
        session = self.sessions.pop(0)
        if isinstance(session, Exception):
            raise session
        return session


def make_stream(*sessions, subs=None, **kwargs):
    subs = subs or [BlurSubscription(name="test", types=("swap",))]
    connector = FakeConnector(*sessions)
    stream = BlurStream(KEY, subs, base_url="wss://ws.example.test/data/subscribe", connect=connector,
                        rng=lambda: 1.0, **kwargs)
    return stream, connector


class SubscriptionTests(unittest.TestCase):
    def test_url_carries_filters_as_documented(self):
        sub = BlurSubscription(name="w", types=("swap", "liquidity"), mints=(DEMO,), min_volume_usd=50000.0,
                               metadata=False)
        url = sub.url("wss://ws.solami.dev/data/subscribe", KEY)
        self.assertTrue(url.startswith("wss://ws.solami.dev/data/subscribe?chain=solana&api_key=" + KEY))
        self.assertIn("type=swap,liquidity", url)             # comma lists stay readable
        self.assertIn("address=" + DEMO, url)
        self.assertIn("min_volume_usd=50000&", url)           # no "50000.0", no exponent
        self.assertTrue(url.endswith("metadata=false"))

    def test_filter_message_uses_plural_keys(self):
        sub = BlurSubscription(name="w", types=("swap",), mints=(DEMO,), dexes=("pumpswap",), min_volume_usd=500)
        self.assertEqual(json.loads(sub.filter_message()),
                         {"filter": {"types": ["swap"], "mints": [DEMO], "dexes": ["pumpswap"],
                                     "min_volume_usd": 500.0}})

    def test_redact_hides_the_key(self):
        self.assertEqual(redact("wss://x/?api_key=%s&type=swap" % KEY, KEY),
                         "wss://x/?api_key=<SOLAMI_API_KEY>&type=swap")
        self.assertEqual(redact("nothing secret", ""), "nothing secret")


class BackoffTests(unittest.TestCase):
    def test_exponential_with_cap_and_reset(self):
        backoff = Backoff(base=1.0, cap=8.0, rng=lambda: 1.0)
        self.assertEqual([backoff.next() for _ in range(5)], [1.0, 2.0, 4.0, 8.0, 8.0])
        backoff.reset()
        self.assertEqual(backoff.next(), 1.0)

    def test_jitter_never_goes_below_the_minimum(self):
        backoff = Backoff(base=1.0, cap=60.0, minimum=0.5, rng=lambda: 0.0)
        self.assertEqual(backoff.next(), 0.5)


class SessionTests(unittest.TestCase):
    """_session() is one connect->read->disconnect cycle; it returns the delay before the
    next attempt, which is what these tests pin down."""

    def run_session(self, stream):
        sub = stream.subscriptions[0]
        return stream._session(sub, stream.health[sub.name], Backoff(rng=lambda: 1.0))

    def test_events_are_parsed_and_queued(self):
        conn = FakeConn(messages=blur_lines()[:3], close_code=1006)
        stream, _ = make_stream(conn)
        self.run_session(stream)
        types = []
        while True:
            event = stream.get(timeout=0.01)
            if event is None:
                break
            types.append(event.type)
        self.assertEqual(types, ["metadata", "swap", "swap"])
        health = stream.health_summary()["test"]
        self.assertEqual(health["messages"], 3)
        self.assertEqual(health["by_type"], {"metadata": 1, "swap": 2})
        self.assertTrue(conn.closed)

    def test_bandwidth_exhausted_4002_waits_long(self):
        stream, _ = make_stream(FakeConn(close_code=4002, close_reason="balance exhausted"))
        self.assertEqual(self.run_session(stream), SLOW_RETRY_SECONDS[4002])

    def test_stream_limit_4029_waits(self):
        stream, _ = make_stream(FakeConn(close_code=4029))
        self.assertGreaterEqual(self.run_session(stream), SLOW_RETRY_SECONDS[4029])

    def test_node_restart_1001_reconnects_quickly(self):
        stream, _ = make_stream(FakeConn(close_code=1001))
        self.assertEqual(self.run_session(stream), 1.0)

    def test_rejected_key_waits_long_and_never_logs_the_key(self):
        stream, _ = make_stream(websocket.HandshakeError(401, "Unauthorized", '{"message":"missing api key"}'))
        self.assertEqual(self.run_session(stream), AUTH_RETRY_SECONDS)
        self.assertNotIn(KEY, stream.health["test"].last_error)

    def test_network_error_uses_backoff(self):
        stream, _ = make_stream(OSError("connection refused"))
        self.assertEqual(self.run_session(stream), 1.0)      # first backoff step with rng=1.0

    def test_bad_frames_are_counted_not_fatal(self):
        conn = FakeConn(messages=["not json", b"\xff\xfe binary", blur_lines()[1]], close_code=1006)
        stream, _ = make_stream(conn)
        self.run_session(stream)
        self.assertEqual(stream.health["test"].parse_errors, 2)
        self.assertEqual(stream.get(timeout=0.01).type, "swap")

    def test_json_in_a_binary_frame_is_accepted(self):
        conn = FakeConn(messages=[blur_lines()[1].encode("utf-8")], close_code=1006)
        stream, _ = make_stream(conn)
        self.run_session(stream)
        self.assertEqual(stream.get(timeout=0.01).type, "swap")

    def test_have_message_lists_known_mints(self):
        conn = FakeConn(close_code=1006)
        stream, _ = make_stream(conn, have_provider=lambda: [DEMO])
        self.run_session(stream)
        self.assertEqual(json.loads(conn.sent[0]), {"have": [DEMO]})

    def test_silent_connection_is_pinged_then_dropped(self):
        conn = FakeConn(idle_forever=True, close_code=0)
        clock = {"t": 1000.0}
        stream, _ = make_stream(conn, ping_after=10.0, dead_after=30.0, clock=lambda: clock["t"])
        conn.last_received = 1000.0

        def advancing_recv(timeout=None):
            clock["t"] += 5.0
            return None
        conn.recv = advancing_recv
        self.run_session(stream)
        self.assertGreaterEqual(conn.pings, 1)
        self.assertIn("no data", stream.health["test"].last_error)

    def test_raw_hook_sees_every_text_frame(self):
        seen = []
        stream, _ = make_stream(FakeConn(messages=blur_lines()[:2], close_code=1006),
                                on_raw=lambda name, raw: seen.append(name))
        self.run_session(stream)
        self.assertEqual(seen, ["test", "test"])


class ThreadTests(unittest.TestCase):
    def test_start_get_stop_across_a_reconnect(self):
        first = FakeConn(messages=blur_lines()[:2], close_code=1001)      # node restart -> reconnect in 1s
        second = FakeConn(messages=blur_lines()[2:4], idle_forever=True)
        stream, connector = make_stream(first, second)
        stream.start()
        received = []
        deadline = time.monotonic() + 5
        while len(received) < 4 and time.monotonic() < deadline:
            event = stream.get(timeout=0.1)
            if event is not None:
                received.append(event.type)
        stream.stop(timeout=3)
        self.assertEqual(len(received), 4)
        self.assertEqual(len(connector.urls), 2)
        self.assertTrue(second.closed)                 # stop() closes the live connection
        self.assertEqual(stream.health["test"].connects, 2)

    def test_two_subscriptions_run_independently(self):
        subs = [BlurSubscription(name="a", types=("swap",)), BlurSubscription(name="b", types=("liquidity",))]
        stream, connector = make_stream(websocket.HandshakeError(403, "Forbidden"),
                                        FakeConn(messages=blur_lines()[1:2], idle_forever=True), subs=subs)
        stream.start()
        event = stream.get(timeout=3)
        stream.stop(timeout=3)
        self.assertIsNotNone(event)                    # one broken subscription does not block the other


class ReplaySourceTests(unittest.TestCase):
    def test_reads_the_fixture_and_reports_exhaustion(self):
        source = ReplaySource(BLUR_FRAMES)
        source.start()
        events = []
        while True:
            event = source.get()
            if event is None:
                break
            events.append(event)
        self.assertTrue(source.exhausted)
        self.assertEqual(len(events), 18)
        self.assertEqual(events[0].mint, DEMO)
        self.assertEqual(source.health_summary()["replay"]["messages"], 18)


if __name__ == "__main__":
    unittest.main()
