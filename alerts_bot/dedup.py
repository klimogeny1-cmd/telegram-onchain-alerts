"""SQLite-backed "have we already posted this?" store - the duplicate-protection layer.

Two kinds of alert are tracked separately and with different lifetimes:
  - "new_pair":     posted at most once per (chain, pair address), ever. A pair is only
                    "new" once, so once it has been shown it is never shown again as new.
  - "volume_spike":  posted at most once per (chain, pair address) per UTC clock hour, so
                    a pair that keeps spiking can be reported again later without being
                    repeated on every single cycle inside the same hour.
"""
import sqlite3
import threading
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS sent_alerts (
    kind         TEXT NOT NULL,          -- 'new_pair' | 'volume_spike'
    chain_id     TEXT NOT NULL,
    pair_address TEXT NOT NULL,
    bucket       TEXT NOT NULL,          -- 'once' for new_pair, 'YYYY-MM-DDTHH' for volume_spike
    sent_at      TEXT NOT NULL,
    PRIMARY KEY (kind, chain_id, pair_address, bucket)
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
