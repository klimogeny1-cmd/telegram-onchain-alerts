"""Minimal WebSocket client (RFC 6455), standard library only.

Why hand-rolled: this project promises "clone, configure, run - nothing to pip install"
(see README "Dependencies"), and the Solami Blur stream only needs the client side of
the protocol: one TLS connection, JSON text frames in, a few small frames out. That is
a few hundred lines, and every branch below is exercised by
tests/test_solami_websocket.py against a scripted fake server on a socketpair.

Supported: ws:// and wss:// (TLS with SNI and normal certificate verification via
ssl.create_default_context()), the HTTP/1.1 upgrade handshake with Sec-WebSocket-Accept
verification, text and binary messages, fragmented messages (continuation frames),
16- and 64-bit payload lengths, ping/pong (server pings are answered automatically),
and the close handshake with a close code and reason.

Not supported, on purpose: extensions such as permessage-deflate (never offered in the
handshake, so a compliant server never compresses), subprotocols, HTTP proxies and
redirects. None of them are needed to read the Blur stream.

Threading rule: use one connection from one thread only. An ssl.SSLSocket must not be
read and written from two threads at once, so blur.BlurStream keeps every connection -
reads, pings, filter messages and the final close - inside that connection's own reader
thread.
"""
import base64
import hashlib
import os
import socket
import ssl
import time
import urllib.parse

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
DEFAULT_USER_AGENT = "telegram-onchain-alerts/2.0 (solana-tape; +https://t.me/gramworks_hub)"
DEFAULT_MAX_MESSAGE_BYTES = 16 * 1024 * 1024

OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

# Close codes an endpoint may put on the wire (RFC 6455 section 7.4). 1005/1006/1015 are
# reserved for "no code"/"abnormal"/"TLS failure" and must never be sent.
_SENDABLE_CLOSE_CODES = set(range(1000, 1004)) | set(range(1007, 1012)) | set(range(3000, 5000))


class WebSocketError(Exception):
    """Base class for everything this module raises on purpose."""


class HandshakeError(WebSocketError):
    """The HTTP upgrade did not succeed. `status` is the HTTP status (0 when the server
    never sent a parseable status line) and `body` a short, decoded snippet of the
    response body - e.g. {"message":"unauthorized"} for a bad API key. The request URL
    is deliberately NOT part of the message: it carries the API key."""

    def __init__(self, status, reason="", body=""):
        text = "WebSocket handshake failed: HTTP %s %s" % (status, reason)
        if body:
            text += " - %s" % body[:300]
        super().__init__(text.strip())
        self.status = status
        self.reason = reason
        self.body = body


class ConnectionClosed(WebSocketError):
    """The connection is gone. `code` is the WebSocket close code (1006 when the TCP/TLS
    connection dropped without a close frame) and `reason` the server's reason text."""

    def __init__(self, code=1006, reason=""):
        super().__init__("WebSocket closed: %s %s" % (code, reason))
        self.code = code
        self.reason = reason


def parse_url(url):
    """'wss://host[:port]/path?query' -> (secure, host, port, resource)."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("ws", "wss"):
        raise ValueError("WebSocket URL must start with ws:// or wss://")
    if not parts.hostname:
        raise ValueError("WebSocket URL has no host")
    secure = parts.scheme == "wss"
    port = parts.port or (443 if secure else 80)
    resource = parts.path or "/"
    if parts.query:
        resource += "?" + parts.query
    return secure, parts.hostname, port, resource


def accept_key(client_key):
    """The Sec-WebSocket-Accept value a server must answer `client_key` with."""
    digest = hashlib.sha1((client_key + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def apply_mask(data, mask):
    """XOR `data` with the 4-byte `mask`, repeated. Used for every client->server frame
    (RFC 6455 requires clients to mask) and for the rare server frame that is masked."""
    if not data:
        return b""
    n = len(data)
    repeated = (bytes(mask) * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(repeated, "big")).to_bytes(n, "big")


def encode_frame(opcode, payload, fin=True, mask=None):
    """One client frame. `mask` is 4 bytes (random if None) - exposed for tests."""
    mask = mask if mask is not None else os.urandom(4)
    header = bytearray([(0x80 if fin else 0x00) | opcode])
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header += length.to_bytes(2, "big")
    else:
        header.append(0x80 | 127)
        header += length.to_bytes(8, "big")
    header += mask
    return bytes(header) + apply_mask(payload, mask)


def _read_response_head(sock, limit=65536):
    data = bytearray()
    while b"\r\n\r\n" not in data:
        if len(data) > limit:
            raise HandshakeError(0, "response headers too large")
        chunk = sock.recv(4096)
        if not chunk:
            raise HandshakeError(0, "connection closed during the handshake")
        data += chunk
    head, _sep, rest = bytes(data).partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status_parts = lines[0].split(" ", 2)
    if len(status_parts) < 2 or not status_parts[0].startswith("HTTP/"):
        raise HandshakeError(0, "malformed status line")
    try:
        status = int(status_parts[1])
    except ValueError:
        raise HandshakeError(0, "malformed status line")
    reason = status_parts[2] if len(status_parts) == 3 else ""
    headers = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    return status, reason, headers, rest


def _read_error_body(sock, headers, rest, limit=2048):
    body = bytearray(rest[:limit])
    try:
        wanted = min(int(headers.get("content-length", "0") or 0), limit)
    except ValueError:
        wanted = 0
    try:
        sock.settimeout(2.0)
        while len(body) < wanted:
            chunk = sock.recv(wanted - len(body))
            if not chunk:
                break
            body += chunk
    except OSError:
        pass
    return bytes(body).decode("utf-8", "replace").strip()


def _close_quietly(sock):
    try:
        sock.close()
    except OSError:
        pass


def connect(url, timeout=15.0, headers=None, ssl_context=None, create_connection=None,
            user_agent=DEFAULT_USER_AGENT, max_message_bytes=DEFAULT_MAX_MESSAGE_BYTES,
            clock=time.monotonic):
    """Opens a WebSocket and returns a WebSocketConnection.

    Raises HandshakeError when the server answers the upgrade with anything but a valid
    101, and OSError (socket.timeout, ssl.SSLError, ConnectionRefusedError, ...) for
    transport failures. `create_connection` is injectable so tests can hand in one end
    of a socketpair instead of touching the network.
    """
    secure, host, port, resource = parse_url(url)
    create_connection = create_connection or socket.create_connection
    raw = create_connection((host, port), timeout)
    sock = raw
    try:
        if secure:
            context = ssl_context or ssl.create_default_context()
            sock = context.wrap_socket(raw, server_hostname=host)
        sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        default_port = 443 if secure else 80
        host_header = ("[%s]" % host) if ":" in host else host
        if port != default_port:
            host_header += ":%d" % port
        lines = [
            "GET %s HTTP/1.1" % resource,
            "Host: %s" % host_header,
            "Upgrade: websocket",
            "Connection: Upgrade",
            "Sec-WebSocket-Key: %s" % key,
            "Sec-WebSocket-Version: 13",
            "User-Agent: %s" % user_agent,
        ]
        for name, value in (headers or {}).items():
            lines.append("%s: %s" % (name, value))
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))

        status, reason, response_headers, rest = _read_response_head(sock)
        if status != 101:
            raise HandshakeError(status, reason, _read_error_body(sock, response_headers, rest))
        if response_headers.get("upgrade", "").lower() != "websocket":
            raise HandshakeError(status, "server did not upgrade to websocket")
        if response_headers.get("sec-websocket-accept") != accept_key(key):
            raise HandshakeError(status, "bad Sec-WebSocket-Accept")
        if response_headers.get("sec-websocket-extensions"):
            # We never offer an extension, so a server that turns one on is broken.
            raise HandshakeError(status, "server enabled an extension that was not offered")
        return WebSocketConnection(sock, initial=rest, max_message_bytes=max_message_bytes, clock=clock)
    except BaseException:
        _close_quietly(sock)
        if sock is not raw:
            _close_quietly(raw)
        raise


class WebSocketConnection:
    """An open client connection. See the module docstring for the one-thread rule."""

    def __init__(self, sock, initial=b"", max_message_bytes=DEFAULT_MAX_MESSAGE_BYTES, clock=time.monotonic):
        self._sock = sock
        self._buf = bytearray(initial)
        self._max = max_message_bytes
        self._clock = clock
        self._frag_opcode = None
        self._frag_parts = []
        self._frag_size = 0
        self._close_sent = False
        self.closed = False
        self.close_code = None
        self.close_reason = ""
        self.last_received = clock()   # any frame, data or control - used for liveness checks

    # --- receiving -------------------------------------------------------------------

    def recv(self, timeout=None):
        """Next complete data message: str for text, bytes for binary. Returns None when
        `timeout` seconds pass without a complete message (a partial frame stays
        buffered, nothing is lost). Server pings are answered here. Raises
        ConnectionClosed once the connection is gone."""
        if self.closed:
            raise ConnectionClosed(self.close_code or 1006, self.close_reason or "connection already closed")
        deadline = None if timeout is None else self._clock() + timeout
        while True:
            frame = self._parse_frame()
            if frame is None:
                remaining = None if deadline is None else deadline - self._clock()
                if remaining is not None and remaining <= 0:
                    return None
                if not self._read_more(remaining):
                    return None
                continue
            message = self._handle_frame(*frame)
            if message is not None:
                return message

    def _read_more(self, timeout):
        try:
            self._sock.settimeout(None if timeout is None else max(timeout, 0.001))
            chunk = self._sock.recv(65536)
        except socket.timeout:
            return False
        except OSError as exc:  # ConnectionResetError, ssl.SSLError, ...
            self._shutdown()
            raise ConnectionClosed(1006, "connection lost: %s" % exc)
        if not chunk:
            self._shutdown()
            raise ConnectionClosed(1006, "server closed the connection without a close frame")
        self._buf += chunk
        return True

    def _parse_frame(self):
        buf = self._buf
        if len(buf) < 2:
            return None
        first, second = buf[0], buf[1]
        fin = bool(first & 0x80)
        rsv = first & 0x70
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        pos = 2
        if length == 126:
            if len(buf) < 4:
                return None
            length = int.from_bytes(buf[2:4], "big")
            pos = 4
        elif length == 127:
            if len(buf) < 10:
                return None
            length = int.from_bytes(buf[2:10], "big")
            pos = 10
        if length > self._max:
            self._fail(1009, "frame larger than %d bytes" % self._max)
        mask = None
        if masked:
            if len(buf) < pos + 4:
                return None
            mask = bytes(buf[pos:pos + 4])
            pos += 4
        if len(buf) < pos + length:
            return None
        payload = bytes(buf[pos:pos + length])
        del buf[:pos + length]
        if mask is not None:
            # RFC 6455 says servers never mask; be lenient and unmask instead of failing.
            payload = apply_mask(payload, mask)
        if rsv:
            self._fail(1002, "RSV bits set but no extension was negotiated")
        return fin, opcode, payload

    def _handle_frame(self, fin, opcode, payload):
        self.last_received = self._clock()
        if opcode >= 0x8:
            if not fin or len(payload) > 125:
                self._fail(1002, "malformed control frame")
            if opcode == OP_CLOSE:
                self._on_close_frame(payload)
            elif opcode == OP_PING:
                self._send_frame(OP_PONG, payload)
            elif opcode == OP_PONG:
                pass
            else:
                self._fail(1002, "unknown control opcode %d" % opcode)
            return None

        if opcode == OP_CONTINUATION:
            if self._frag_opcode is None:
                self._fail(1002, "continuation frame without a message to continue")
            self._frag_parts.append(payload)
            self._frag_size += len(payload)
            if self._frag_size > self._max:
                self._fail(1009, "message larger than %d bytes" % self._max)
            if not fin:
                return None
            data = b"".join(self._frag_parts)
            opcode = self._frag_opcode
            self._frag_opcode, self._frag_parts, self._frag_size = None, [], 0
            return self._decode(opcode, data)

        if opcode not in (OP_TEXT, OP_BINARY):
            self._fail(1002, "unknown data opcode %d" % opcode)
        if self._frag_opcode is not None:
            self._fail(1002, "new message started inside a fragmented one")
        if fin:
            return self._decode(opcode, payload)
        self._frag_opcode, self._frag_parts, self._frag_size = opcode, [payload], len(payload)
        return None

    @staticmethod
    def _decode(opcode, data):
        return data.decode("utf-8", "replace") if opcode == OP_TEXT else data

    def _on_close_frame(self, payload):
        code = int.from_bytes(payload[:2], "big") if len(payload) >= 2 else 1005
        reason = payload[2:].decode("utf-8", "replace")
        self.close_code, self.close_reason = code, reason
        if not self._close_sent:
            try:
                self._send_close(code if code in _SENDABLE_CLOSE_CODES else 1000, "")
            except OSError:
                pass
        self._shutdown()
        raise ConnectionClosed(code, reason)

    # --- sending ---------------------------------------------------------------------

    def send_text(self, text):
        self._send_frame(OP_TEXT, text.encode("utf-8"))

    def send_binary(self, data):
        self._send_frame(OP_BINARY, bytes(data))

    def ping(self, payload=b""):
        self._send_frame(OP_PING, payload)

    def _send_frame(self, opcode, payload):
        if self.closed:
            raise ConnectionClosed(self.close_code or 1006, "connection already closed")
        try:
            self._sock.sendall(encode_frame(opcode, payload))
        except OSError as exc:
            self._shutdown()
            raise ConnectionClosed(1006, "send failed: %s" % exc)

    def _send_close(self, code, reason):
        payload = int(code).to_bytes(2, "big") + reason.encode("utf-8")[:123]
        self._close_sent = True
        self._sock.sendall(encode_frame(OP_CLOSE, payload))

    # --- closing ---------------------------------------------------------------------

    def close(self, code=1000, reason="", timeout=1.0):
        """Starts the close handshake and waits up to `timeout` seconds for the server's
        close frame, then drops the socket either way. Safe to call more than once."""
        if self.closed:
            return
        try:
            if not self._close_sent:
                self._send_close(code, reason)
            deadline = self._clock() + timeout
            while not self.closed:
                remaining = deadline - self._clock()
                if remaining <= 0 or self.recv(timeout=remaining) is None:
                    break
        except (ConnectionClosed, OSError):
            pass
        finally:
            self._shutdown()

    def _fail(self, code, reason):
        """Protocol error: tell the server why (best effort), drop the socket, raise."""
        self.close_code, self.close_reason = code, reason
        if not self._close_sent:
            try:
                self._send_close(code, reason)
            except OSError:
                pass
        self._shutdown()
        raise ConnectionClosed(code, reason)

    def _shutdown(self):
        if not self.closed:
            self.closed = True
            _close_quietly(self._sock)
