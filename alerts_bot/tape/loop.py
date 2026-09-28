"""The live loop: event source -> engine -> Telegram, until told to stop.

Reliability rules this loop enforces (README "Reliability"):
  - one bad event never stops the loop: handle() errors are logged per event and skipped;
  - one failed post never loses its facts: the engine re-queues items of a failed send;
  - Telegram is never flooded: at most MAX_POSTS_PER_MIN posts, spaced evenly;
  - graceful shutdown: on SIGTERM/SIGINT (stop_event) the loop stops reading, closes the
    Blur sockets with a proper close frame, flushes what is already queued, and returns;
  - a heartbeat file (state/heartbeat.json) is rewritten every 30 s with stream health,
    so Docker/systemd/an operator can tell "running" from "hung".
"""
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

from ..telegram import TelegramError

log = logging.getLogger("tape.loop")


class Publisher:
    """Sends post texts to the channel, spaced so no more than `max_per_min` go out per
    minute. Returns True/False per post; never raises for a Telegram failure."""

    def __init__(self, bot, channel_id, max_per_min=6, stop_event=None, clock=time.monotonic):
        self.bot = bot
        self.channel_id = channel_id
        self.min_gap = 60.0 / max_per_min if max_per_min and max_per_min > 0 else 0.0
        self._stop = stop_event or threading.Event()
        self._clock = clock
        self._last = None
        self.sent = 0
        self.failed = 0

    def publish(self, text):
        if self._last is not None and self.min_gap:
            wait = self.min_gap - (self._clock() - self._last)
            if wait > 0:
                self._stop.wait(wait)      # interruptible: shutdown does not wait out the gap
        try:
            self.bot.send_message(self.channel_id, text)
        except TelegramError as exc:
            self.failed += 1
            log.error("telegram: post failed (%s), its items will be retried", exc)
            return False
        except Exception:
            self.failed += 1
            log.exception("telegram: unexpected error while posting")
            return False
        finally:
            self._last = self._clock()
        self.sent += 1
        return True


def write_heartbeat(path, payload):
    """Atomic write (tmp + rename) so a reader never sees half a file."""
    if not path:
        return
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True)
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("heartbeat not written: %s", exc)


def _deliver(engine, publisher, posts):
    for post in posts:
        try:
            ok = publisher.publish(post.text)
        except Exception:
            log.exception("publisher crashed on a post")
            ok = False
        engine.confirm(post, ok)


def run_live(source, engine, publisher, stop_event, duration=None, heartbeat_path=None, force_cards=False,
             stop_when_exhausted=False, final_reports=False, health_every=60.0, clock=time.monotonic,
             now_fn=None):
    """Runs until stop_event is set, `duration` seconds pass, or (replay) the source is
    exhausted. `final_reports` also posts the summary/digest for the partial window on
    the way out (used by the offline replay; a real service leaves it off so a restart
    does not post a partial summary). Returns a dict of counters for the caller to log."""
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    started = clock()
    events = handled_errors = 0
    last_health, last_beat = started, started - 30.0      # first heartbeat right away
    source.start()
    try:
        _deliver(engine, publisher, engine.startup(force_cards=force_cards))
    except Exception:
        log.exception("startup checks failed; continuing with the stream only")
    try:
        while not stop_event.is_set():
            if duration is not None and clock() - started >= duration:
                log.info("--duration reached, stopping")
                break
            try:
                event = source.get(timeout=0.5)
            except Exception:
                log.exception("event source error")
                event = None
            if event is not None:
                events += 1
                try:
                    engine.handle(event)
                except Exception:
                    handled_errors += 1
                    log.exception("skipped one %s event that could not be handled", getattr(event, "type", "?"))
            elif stop_when_exhausted and getattr(source, "exhausted", False):
                log.info("replay finished")
                break
            try:
                _deliver(engine, publisher, engine.tick(now_fn()))
            except Exception:
                log.exception("periodic tasks failed, will retry")
            now = clock()
            if now - last_beat >= 30.0:
                last_beat = now
                write_heartbeat(heartbeat_path, _health_payload(source, engine, publisher, events))
            if now - last_health >= health_every:
                last_health = now
                _log_health(source, engine, publisher, events)
    finally:
        log.info("shutting down: closing streams and flushing queued items")
        try:
            source.stop()
        except Exception:
            log.exception("error while stopping the stream")
        try:
            _deliver(engine, publisher, engine.flush(now_fn()))
            if final_reports:
                now = now_fn()
                _deliver(engine, publisher, engine.summaries(now) if engine.s.watchlist else engine.digest(now))
        except Exception:
            log.exception("final flush failed")
        write_heartbeat(heartbeat_path, dict(_health_payload(source, engine, publisher, events), stopped=True))
    return {"events": events, "event_errors": handled_errors, "posts_sent": publisher.sent,
            "posts_failed": publisher.failed, "items_dropped": engine.items_dropped}


def _health_payload(source, engine, publisher, events):
    try:
        streams = source.health_summary()
    except Exception:
        streams = {}
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "events_handled": events,
        "queue": source.qsize() if hasattr(source, "qsize") else None,
        "dropped_by_stream": getattr(source, "dropped", 0),
        "pending_items": len(engine.pending),
        "posts_sent": publisher.sent,
        "posts_failed": publisher.failed,
        "streams": streams,
    }


def _log_health(source, engine, publisher, events):
    payload = _health_payload(source, engine, publisher, events)
    parts = []
    for name, h in payload["streams"].items():
        parts.append("%s:%s msgs=%d connects=%d%s" % (
            name, "up" if h.get("connected") else "DOWN", h.get("messages", 0), h.get("connects", 0),
            (" last_error=%s" % h["last_error"]) if h.get("last_error") and not h.get("connected") else ""))
    log.info("health: %s | handled=%d queue=%s pending=%d posts=%d failed=%d", "; ".join(parts) or "no streams",
             events, payload["queue"], payload["pending_items"], publisher.sent, publisher.failed)
