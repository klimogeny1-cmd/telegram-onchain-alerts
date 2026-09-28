"""Tests for alerts_bot.solami.websocket - the stdlib RFC 6455 client - against the scripted
fake server in tests/ws_helpers.py (a socketpair: no ports, no network)."""
import unittest

from alerts_bot.solami import websocket as ws
from tests.ws_helpers import FakeServer, close_frame, server_frame, text

URL = "ws://example.test:8080/data/subscribe?chain=solana&api_key=sk_test_SECRET"


def open_conn(server, **kwargs):
    return ws.connect(URL, timeout=2.0, create_connection=server.create_connection, **kwargs)


class HandshakeTests(unittest.TestCase):
    def test_upgrade_request_and_first_message(self):
        server = FakeServer(script=[text('{"type":"swap"}')])
        conn = open_conn(server)
        try:
            self.assertEqual(conn.recv(timeout=2), '{"type":"swap"}')
            self.assertEqual(server.address, ("example.test", 8080))
            self.assertIn("GET /data/subscribe?chain=solana&api_key=sk_test_SECRET HTTP/1.1\r\n", server.request)
            self.assertIn("Host: example.test:8080\r\n", server.request)
            self.assertIn("Upgrade: websocket\r\n", server.request)
            self.assertIn("Sec-WebSocket-Version: 13\r\n", server.request)
            self.assertNotIn("Sec-WebSocket-Extensions", server.request)   # nothing is offered
        finally:
            conn.close()
            server.close()

    def test_frames_sent_together_with_the_handshake_are_not_lost(self):
        server = FakeServer(with_handshake=text("first") + text("second"))
        conn = open_conn(server)
        try:
            self.assertEqual(conn.recv(timeout=2), "first")
            self.assertEqual(conn.recv(timeout=2), "second")
        finally:
            conn.close()
            server.close()

    def test_http_error_raises_handshake_error_with_body_but_without_the_key(self):
        server = FakeServer(status=401, body=b'{"message":"missing api key"}')
        with self.assertRaises(ws.HandshakeError) as ctx:
            open_conn(server)
        server.close()
        self.assertEqual(ctx.exception.status, 401)
        self.assertIn("missing api key", ctx.exception.body)
        self.assertNotIn("sk_test_SECRET", str(ctx.exception))

    def test_wrong_accept_key_is_rejected(self):
        server = FakeServer(accept_ok=False)
        with self.assertRaises(ws.HandshakeError):
            open_conn(server)
        server.close()

    def test_unrequested_extension_is_rejected(self):
        server = FakeServer(extra_headers="Sec-WebSocket-Extensions: permessage-deflate\r\n")
        with self.assertRaises(ws.HandshakeError):
            open_conn(server)
        server.close()


class FramingTests(unittest.TestCase):
    def run_script(self, script, **kwargs):
        server = FakeServer(script=script)
        conn = open_conn(server, **kwargs)
        self.addCleanup(server.close)
        self.addCleanup(conn.close)
        return server, conn

    def test_fragmented_message_with_interleaved_ping(self):
        server, conn = self.run_script([
            text("hel", fin=False),
            server_frame(ws.OP_PING, b"are-you-there"),
            server_frame(ws.OP_CONTINUATION, b"lo", fin=True),
        ])
        self.assertEqual(conn.recv(timeout=2), "hello")
        conn.close()
        server.join()
        pongs = [f for f in server.client_frames if f[0] == ws.OP_PONG]
        self.assertEqual(pongs[0][1], b"are-you-there")

    def test_16_and_64_bit_payload_lengths(self):
        medium, large = "m" * 300, "L" * 70000
        _server, conn = self.run_script([text(medium), text(large)])
        self.assertEqual(conn.recv(timeout=2), medium)
        self.assertEqual(conn.recv(timeout=2), large)

    def test_binary_message_comes_back_as_bytes(self):
        _server, conn = self.run_script([server_frame(ws.OP_BINARY, b"\x00\x01\x02")])
        self.assertEqual(conn.recv(timeout=2), b"\x00\x01\x02")

    def test_masked_server_frame_is_unmasked_leniently(self):
        _server, conn = self.run_script([server_frame(ws.OP_TEXT, b"masked text", masked=True)])
        self.assertEqual(conn.recv(timeout=2), "masked text")

    def test_timeout_returns_none_and_keeps_the_partial_frame(self):
        frame = text("split across reads")
        _server, conn = self.run_script([frame[:5], 0.3, frame[5:]])
        self.assertIsNone(conn.recv(timeout=0.1))
        self.assertEqual(conn.recv(timeout=2), "split across reads")

    def test_client_frames_are_masked(self):
        server, conn = self.run_script(["read"])
        conn.send_text('{"filter":{"types":["swap"]}}')
        conn.close()
        server.join()
        opcode, payload, masked = server.client_frames[0]
        self.assertEqual(opcode, ws.OP_TEXT)
        self.assertTrue(masked)
        self.assertEqual(payload, b'{"filter":{"types":["swap"]}}')


class CloseTests(unittest.TestCase):
    def test_server_close_raises_with_code_and_reason_and_is_echoed(self):
        server = FakeServer(script=[close_frame(4002, "StreamingBandwidth exhausted; buy more")])
        conn = open_conn(server)
        with self.assertRaises(ws.ConnectionClosed) as ctx:
            conn.recv(timeout=2)
        server.join()
        server.close()
        self.assertEqual(ctx.exception.code, 4002)
        self.assertIn("StreamingBandwidth", ctx.exception.reason)
        echoed = [f for f in server.client_frames if f[0] == ws.OP_CLOSE]
        self.assertEqual(int.from_bytes(echoed[0][1][:2], "big"), 4002)
        self.assertTrue(conn.closed)
        with self.assertRaises(ws.ConnectionClosed):   # further reads fail fast
            conn.recv(timeout=0.1)

    def test_drop_without_close_frame_is_1006(self):
        server = FakeServer(script=["hangup"])
        conn = open_conn(server)
        with self.assertRaises(ws.ConnectionClosed) as ctx:
            conn.recv(timeout=2)
        server.close()
        self.assertEqual(ctx.exception.code, 1006)

    def test_rsv_bits_without_extension_fail_with_1002(self):
        server = FakeServer(script=[server_frame(ws.OP_TEXT, b"x", rsv=0x40)])
        conn = open_conn(server)
        with self.assertRaises(ws.ConnectionClosed) as ctx:
            conn.recv(timeout=2)
        server.close()
        self.assertEqual(ctx.exception.code, 1002)

    def test_oversized_message_fails_with_1009(self):
        server = FakeServer(script=[text("x" * 5000)])
        conn = open_conn(server, max_message_bytes=1000)
        with self.assertRaises(ws.ConnectionClosed) as ctx:
            conn.recv(timeout=2)
        server.close()
        self.assertEqual(ctx.exception.code, 1009)

    def test_client_close_sends_close_frame_and_is_idempotent(self):
        server = FakeServer(script=["read", close_frame(1000)])
        conn = open_conn(server)
        conn.close(1000, "bye")
        conn.close()                     # second call must not raise
        server.join()
        server.close()
        opcode, payload, _masked = server.client_frames[0]
        self.assertEqual(opcode, ws.OP_CLOSE)
        self.assertEqual(int.from_bytes(payload[:2], "big"), 1000)
        self.assertEqual(payload[2:], b"bye")


class HelperTests(unittest.TestCase):
    def test_parse_url(self):
        self.assertEqual(ws.parse_url("wss://ws.solami.dev/data/subscribe?chain=solana"),
                         (True, "ws.solami.dev", 443, "/data/subscribe?chain=solana"))
        self.assertEqual(ws.parse_url("ws://localhost"), (False, "localhost", 80, "/"))
        with self.assertRaises(ValueError):
            ws.parse_url("https://ws.solami.dev/")

    def test_mask_round_trip(self):
        data = bytes(range(256)) * 3
        self.assertEqual(ws.apply_mask(ws.apply_mask(data, b"abcd"), b"abcd"), data)
        self.assertEqual(ws.apply_mask(b"", b"abcd"), b"")

    def test_accept_key_matches_rfc_example(self):
        # RFC 6455 section 1.3 worked example
        self.assertEqual(ws.accept_key("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")


if __name__ == "__main__":
    unittest.main()
