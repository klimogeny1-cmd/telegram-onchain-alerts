"""SQLite-backed "have we already posted this?" store - the duplicate-protection layer.

Two kinds of alert are tracked separately and with different lifetimes:
  - "new_pair":     posted at most once per (chain, pair address), ever. A pair is only
                    "new" once, so once it has been shown it is never shown again as new.
  - "volume_spike":  posted at most once per (chain, pair address) per UTC clock hour, so
                    a pair that keeps spiking can be reported again later without being
                    repeated on every single cycle inside the same hour.

The live Solami tape (alerts_bot/tape) uses the generic seen()/mark() pair on the same
table (chain_id "solana", pair_address = the event key, bucket chosen by the caller), and
a small kv table for state that must survive a restart - the last authority check and
the last top-holder snapshot per watched token - so a restart never re-announces an
unchanged fact and never misses a change that happened while the bot was down.
"""
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS sent_alerts (
    kind         TEXT NOT NULL,          -- 'new_pair' | 'volume_spike' | live tape kinds
    chain_id     TEXT NOT NULL,
    pair_address TEXT NOT NULL,
    bucket       TEXT NOT NULL,          -- 'once' for new_pair, 'YYYY-MM-DDTHH' for volume_spike
    sent_at      TEXT NOT NULL,
    PRIMARY KEY (kind, chain_id, pair_address, bucket)
);
CREATE TABLE IF NOT EXISTS kv (
    name       TEXT PRIMARY KEY,
    value      TEXT NOT NULL,           -- JSON
    updated_at TEXT NOT NULL
);
"""


class DedupStore:
    def __init__(self, path, clock=None):
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=15)
        with self._lock:
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self):
        self._conn.close()

    @staticmethod
    def _bucket(kind, when):
        return when.strftime("%Y-%m-%dT%H") if kind == "volume_spike" else "once"

    def already_sent(self, kind, chain_id, pair_address):
        bucket = self._bucket(kind, self.clock())
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM sent_alerts WHERE kind=? AND chain_id=? AND pair_address=? AND bucket=?",
                (kind, chain_id, pair_address, bucket)).fetchone()
        return row is not None

    def mark_sent(self, kind, chain_id, pair_address):
        bucket = self._bucket(kind, self.clock())
        now = self.clock().isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO sent_alerts(kind, chain_id, pair_address, bucket, sent_at) "
                "VALUES (?,?,?,?,?)", (kind, chain_id, pair_address, bucket, now))
            self._conn.commit()

    def filter_unsent(self, kind, pairs):
        """Pairs (each with .chain_id / .pair_address) not yet recorded for `kind`, in
        the same order they were given."""
        return [p for p in pairs if not self.already_sent(kind, p.chain_id, p.pair_address)]

    # --- generic API used by the live tape --------------------------------------------------

    def seen(self, kind, key, bucket="once"):
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM sent_alerts WHERE kind=? AND chain_id='solana' AND pair_address=? AND bucket=?",
                (kind, key, bucket)).fetchone()
        return row is not None

    def mark(self, kind, key, bucket="once"):
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO sent_alerts(kind, chain_id, pair_address, bucket, sent_at) "
                "VALUES (?,'solana',?,?,?)", (kind, key, bucket, self.clock().isoformat()))
            self._conn.commit()

    def kv_get(self, name, default=None):
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE name=?", (name,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row[0])
        except ValueError:
            return default

    def kv_set(self, name, value):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO kv(name, value, updated_at) VALUES (?,?,?)",
                (name, json.dumps(value, sort_keys=True), self.clock().isoformat()))
            self._conn.commit()

    def prune(self, older_than_days=30, kinds=("live",)):
        """Deletes live-tape dedup rows older than `older_than_days` (a trade from last
        month will not be re-delivered). Never touches new_pair/volume_spike rows."""
        cutoff = (self.clock() - timedelta(days=older_than_days)).isoformat()
        with self._lock:
            removed = 0
            for kind in kinds:
                removed += self._conn.execute("DELETE FROM sent_alerts WHERE kind=? AND sent_at < ?",
                                              (kind, cutoff)).rowcount
            self._conn.commit()
        return removed
