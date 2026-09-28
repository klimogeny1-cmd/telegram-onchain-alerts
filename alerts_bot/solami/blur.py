"""Solami Blur: decoded Solana market events over one WebSocket per subscription.

This is the bot's primary data path. Blur already decodes every major DEX into typed
events (swap, liquidity, token_create, pool_create, graduation, surge, ...) with USD
values computed, so the bot never parses a single program instruction.

Endpoint and auth (solami.dev/docs/endpoints and /docs/blur, read 2026-09-26):
    wss://ws.solami.dev/data/subscribe?chain=solana&api_key=KEY[&filters...]
    region-pinned: wss://fra.ws.solami.dev/data/subscribe?...
The key must be a *standard* API key (ApiKey, usually "sk_...") whose role grants the
`DataApi` permission - RPC-only and gRPC-only keys are rejected for Blur.

Filters (query params at connect time; JSON text frame {"filter": {...}} to change them
live, keys plural there): type, address (token mints), pool, trader, dex, side,
min_base, min_quote, min_volume_usd, min_progress/max_progress, min_mcap_at_trigger/
max_mcap_at_trigger, min_multiple, metadata=false, backfill=N.
IMPORTANT (from the docs): "A filter that only applies to one type excludes the others
when you set it" - e.g. min_volume_usd returns swaps only. That is why the firehose mode
opens two subscriptions (see tape.plan_subscriptions) instead of one.

Close codes worth knowing: 4002 = streaming bandwidth and balance both empty,
4029 = concurrent stream limit, 1001 = node restart (reconnect), 1006 before the upgrade
= key rejected / wrong key type / no bandwidth.
"""
import json
import logging
import queue
import random
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional, Tuple

from . import websocket
from .events import DATA_TYPES, parse_blur_message

log = logging.getLogger("solami.blur")

DEFAULT_BLUR_URL = "wss://ws.solami.dev/data/subscribe"


def redact(text, secret):
    """Replaces the API key (and anything after `api_key=`) so URLs can be logged."""
    text = str(text)
    if secret:
        text = text.replace(secret, "<SOLAMI_API_KEY>")
        text = text.replace(urllib.parse.quote(secret, safe=""), "<SOLAMI_API_KEY>")
    return text


@dataclass(frozen=True)
class BlurSubscription:
    """One Blur socket's filter. `name` is only for logs and health output."""
    name: str
    types: Tuple[str, ...] = ()
    mints: Tuple[str, ...] = ()
    pools: Tuple[str, ...] = ()
    traders: Tuple[str, ...] = ()
    dexes: Tuple[str, ...] = ()
    side: Optional[str] = None
    min_volume_usd: Optional[float] = None
    min_multiple: Optional[float] = None
    min_mcap_at_trigger: Optional[float] = None
    metadata: bool = True
    backfill: int = 0

    def query_params(self, api_key):
        params = [("chain", "solana"), ("api_key", api_key)]
        for name, values in (("type", self.types), ("address", self.mints), ("pool", self.pools),
                             ("trader", self.traders), ("dex", self.dexes)):
            if values:
                params.append((name, ",".join(values)))
        if self.side:
            params.append(("side", self.side))
        for name, value in (("min_volume_usd", self.min_volume_usd), ("min_multiple", self.min_multiple),
                            ("min_mcap_at_trigger", self.min_mcap_at_trigger)):
            if value is not None:
                params.append((name, _plain_number(value)))
        if not self.metadata:
            params.append(("metadata", "false"))
        if self.backfill:
            params.append(("backfill", str(int(self.backfill))))
        return params

    def url(self, base_url, api_key):
        separator = "&" if "?" in base_url else "?"
        return base_url + separator + urllib.parse.urlencode(self.query_params(api_key), safe=",")

    def filter_message(self):
        """The same filter as a live-update text frame (docs: keys are plural there)."""
        body = {}
        for name, values in (("types", self.types), ("mints", self.mints), ("pools", self.pools),
                             ("traders", self.traders), ("dexes", self.dexes)):
            if values:
                body[name] = list(values)
        if self.side:
            body["side"] = self.side
        for name, value in (("min_volume_usd", self.min_volume_usd), ("min_multiple", self.min_multiple),
                            ("min_mcap_at_trigger", self.min_mcap_at_trigger)):
            if value is not None:
                body[name] = float(value)
        return json.dumps({"filter": body}, separators=(",", ":"))


def _plain_number(value):
    """1000.0 -> "1000", 2.5 -> "2.5" - never exponent notation in a query string."""
    value = float(value)
    return str(int(value)) if value.is_integer() else ("%.6f" % value).rstrip("0").rstrip(".")


class Backoff:
    """Exponential backoff with full jitter (what Solami's troubleshooting page asks for:
    "always back off between attempts"). next() -> seconds to wait; reset() after a
    connection has proven stable."""

    def __init__(self, base=1.0, cap=60.0, minimum=0.5, rng=random.random):
        self.base, self.cap, self.minimum, self._rng = base, cap, minimum, rng
        self.attempt = 0

    def next(self):
        ceiling = min(self.cap, self.base * (2 ** self.attempt))
        self.attempt = min(self.attempt + 1, 30)
        return max(self.minimum, ceiling * self._rng())

    def reset(self):
        self.attempt = 0


@dataclass
class StreamHealth:
    name: str
    connected: bool = False
    connects: int = 0
    disconnects: int = 0
    messages: int = 0
    parse_errors: int = 0
    last_message_at: Optional[float] = None
    last_error: str = ""
    by_type: dict = field(default_factory=dict)


# How long a close code keeps us away before the next attempt (seconds). Codes not listed
# use the normal exponential backoff.
SLOW_RETRY_SECONDS = {
    4002: 300.0,   # streaming bandwidth and balance both empty - retrying fast won't help
    4029: 60.0,    # concurrent stream limit for the plan
}
AUTH_RETRY_SECONDS = 300.0   # HTTP 401/403 at the handshake: the key is wrong or lacks DataApi


class BlurStream:
    """Runs one reader thread per BlurSubscription and funnels every parsed event into a
    single queue that the main loop drains with get().

    Each thread owns its connection end to end (see websocket.py on why) and reconnects
    on its own with exponential backoff; one failing subscription never blocks another.
    `connect` and `sleep` are injectable for tests.
    """

    def __init__(self, api_key, subscriptions, base_url=DEFAULT_BLUR_URL, connect=None,
                 queue_size=50000, connect_timeout=15.0, ping_after=20.0, dead_after=90.0,
                 stable_after=60.0, have_provider=None, on_raw=None, clock=time.monotonic, rng=random.random):
        if not subscriptions:
            raise ValueError("BlurStream needs at least one subscription")
        self.api_key = api_key
        self.subscriptions = list(subscriptions)
        self.base_url = base_url
        self._connect = connect or websocket.connect
        self._queue = queue.Queue(maxsize=queue_size)
        self._stop = threading.Event()
        self._threads = []
        self.connect_timeout = connect_timeout
        self.ping_after = ping_after
        self.dead_after = dead_after
        self.stable_after = stable_after
        self._have_provider = have_provider      # () -> list of mints we already hold metadata for
        self._on_raw = on_raw                    # (subscription name, raw text) -> None, e.g. a recorder
        self._clock = clock
        self._rng = rng
        self.health = {s.name: StreamHealth(s.name) for s in self.subscriptions}
        self.dropped = 0
        self._last_drop_log = 0.0

    # --- lifecycle -----------------------------------------------------------------------

    def start(self):
        for sub in self.subscriptions:
            thread = threading.Thread(target=self._run, args=(sub,), name="blur-%s" % sub.name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self, timeout=5.0):
        self._stop.set()
        deadline = time.monotonic() + timeout
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))

    @property
    def stopping(self):
        return self._stop.is_set()

    def get(self, timeout=1.0):
        """Next normalized event, or None if none arrived within `timeout` seconds."""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def qsize(self):
        return self._queue.qsize()

    def health_summary(self):
        return {name: dict(connected=h.connected, connects=h.connects, disconnects=h.disconnects,
                           messages=h.messages, parse_errors=h.parse_errors, last_error=h.last_error,
                           by_type=dict(h.by_type))
                for name, h in self.health.items()}

    # --- per-subscription worker ------------------------------------------------------------

    def _run(self, sub):
        backoff = Backoff(rng=self._rng)
        health = self.health[sub.name]
        while not self._stop.is_set():
            delay = self._session(sub, health, backoff)
            if self._stop.is_set():
                break
            log.info("blur[%s]: reconnecting in %.1fs", sub.name, delay)
            self._stop.wait(delay)

    def _session(self, sub, health, backoff):
        """One connect -> read -> disconnect cycle. Returns how long to wait before the
        next attempt. Never raises: every failure is logged and turned into a delay."""
        url = sub.url(self.base_url, self.api_key)
        try:
            conn = self._connect(url, timeout=self.connect_timeout)
        except websocket.HandshakeError as exc:
            health.last_error = redact(exc, self.api_key)
            if exc.status in (401, 403):
                log.error("blur[%s]: Solami rejected the key (HTTP %s: %s). Blur needs a standard API key "
                          "(ApiKey, sk_...) whose role has the DataApi permission, and streaming bandwidth or "
                          "balance. Retrying in %ds.", sub.name, exc.status, redact(exc.body, self.api_key)[:200],
                          AUTH_RETRY_SECONDS)
                return AUTH_RETRY_SECONDS
            log.warning("blur[%s]: %s", sub.name, health.last_error)
            return backoff.next()
        except Exception as exc:  # DNS, TCP, TLS, timeouts
            health.last_error = redact(exc, self.api_key)
            log.warning("blur[%s]: connect failed: %s", sub.name, health.last_error)
            return backoff.next()

        health.connected = True
        health.connects += 1
        connected_at = self._clock()
        last_ping = connected_at
        log.info("blur[%s]: connected (%s)", sub.name, redact(url, self.api_key))
        try:
            self._send_have(conn, sub)
            while not self._stop.is_set():
                message = conn.recv(timeout=1.0)
                now = self._clock()
                if message is None:
                    idle = now - conn.last_received
                    if idle > self.dead_after:
                        raise websocket.ConnectionClosed(1006, "no data or pong for %ds" % int(idle))
                    if idle > self.ping_after and now - last_ping > self.ping_after:
                        conn.ping(b"keepalive")
                        last_ping = now
                    continue
                if now - connected_at > self.stable_after:
                    backoff.reset()
                self._on_message(sub, health, message)
            conn.close(1000, "client shutting down")
            return 0.0
        except websocket.ConnectionClosed as exc:
            health.last_error = "closed %s %s" % (exc.code, exc.reason)
            return self._delay_after_close(sub, exc, backoff)
        except Exception as exc:
            health.last_error = redact(exc, self.api_key)
            log.exception("blur[%s]: stream error", sub.name)
            return backoff.next()
        finally:
            health.connected = False
            health.disconnects += 1
            try:
                conn.close(1000, "", timeout=0.5)
            except Exception:
                pass

    def _delay_after_close(self, sub, exc, backoff):
        if exc.code == 4002:
            log.error("blur[%s]: closed 4002 - Solami streaming bandwidth and balance are both empty "
                      "(buy bandwidth or top up). Retrying in %ds.", sub.name, SLOW_RETRY_SECONDS[4002])
            return SLOW_RETRY_SECONDS[4002]
        if exc.code == 4029:
            log.error("blur[%s]: closed 4029 - concurrent stream limit reached for this plan. Retrying in "
                      "%ds; reduce subscriptions (see README) if this repeats.", sub.name, SLOW_RETRY_SECONDS[4029])
            return max(SLOW_RETRY_SECONDS[4029], backoff.next())
        if exc.code == 1001:
            log.info("blur[%s]: server restarting a node (1001), reconnecting", sub.name)
            return 1.0
        log.warning("blur[%s]: disconnected (%s %s)", sub.name, exc.code, exc.reason)
        return backoff.next()

    def _send_have(self, conn, sub):
        """Tells Blur which mints we already hold metadata for, so it does not resend
        them after a reconnect (docs: {"have": ["mint1", ...]}). Best effort."""
        if not sub.metadata or self._have_provider is None:
            return
        try:
            mints = list(self._have_provider() or [])[:500]
        except Exception:
            return
        if mints:
            conn.send_text(json.dumps({"have": mints}, separators=(",", ":")))

    def _on_message(self, sub, health, message):
        health.messages += 1
        health.last_message_at = self._clock()
        if isinstance(message, bytes):
            # Blur documents JSON text frames; accept JSON sent as a binary frame too.
            try:
                message = message.decode("utf-8")
            except UnicodeDecodeError:
                health.parse_errors += 1
                return
        if self._on_raw is not None:
            try:
                self._on_raw(sub.name, message)
            except Exception:
                log.exception("raw frame hook failed")
        events = parse_blur_message(message)
        if not events:
            health.parse_errors += 1
            return
        for event in events:
            health.by_type[event.type] = health.by_type.get(event.type, 0) + 1
            if event.type == "error":
                log.warning("blur[%s]: server error event: %s", sub.name, event.message or event.raw)
            try:
                self._queue.put_nowait(event)
            except queue.Full:
                self.dropped += 1
                now = self._clock()
                if now - self._last_drop_log > 60:
                    self._last_drop_log = now
                    log.warning("blur: event queue full, %d events dropped so far (main loop too slow?)",
                                self.dropped)


class ReplaySource:
    """Same get()/start()/stop() surface as BlurStream, fed from a JSONL file of raw Blur
    frames (one JSON object per line) - used by tests and by `main.py --replay-file` to
    try the whole pipeline offline, with no key and no network."""

    def __init__(self, path=None, lines=None):
        self._lines = list(lines) if lines is not None else self._read(path)
        self._events = []
        self._pos = 0
        self.exhausted = False
        self.health = {"replay": StreamHealth("replay", connected=True)}
        self.dropped = 0

    @staticmethod
    def _read(path):
        with open(path, encoding="utf-8") as fh:
            return [line for line in fh if line.strip() and not line.lstrip().startswith("#")]

    def start(self):
        for line in self._lines:
            self._events.extend(parse_blur_message(line))

    def stop(self, timeout=0.0):
        pass

    @property
    def stopping(self):
        return False

    def get(self, timeout=0.0):
        if self._pos >= len(self._events):
            self.exhausted = True
            return None
        event = self._events[self._pos]
        self._pos += 1
        health = self.health["replay"]
        health.messages += 1
        health.by_type[event.type] = health.by_type.get(event.type, 0) + 1
        return event

    def qsize(self):
        return len(self._events) - self._pos

    def health_summary(self):
        h = self.health["replay"]
        return {"replay": dict(connected=True, connects=1, disconnects=0, messages=h.messages,
                               parse_errors=0, last_error="", by_type=dict(h.by_type))}


def subscription_types(types):
    """Validates a type list against the documented Blur event types."""
    unknown = [t for t in types if t not in DATA_TYPES]
    if unknown:
        raise ValueError("unknown Blur event type(s): %s" % ", ".join(unknown))
    return tuple(types)
