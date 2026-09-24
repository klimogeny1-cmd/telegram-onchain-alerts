"""Turns raw DexScreener pair dicts into normalized Pair objects, and picks out "new
pairs" and "volume anomalies" from a list of them.

Nothing in this module touches the network - it is plain data in, plain data out, which
is what makes it testable against saved JSON fixtures (see tests/test_analysis.py).
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class Pair:
    chain_id: str
    pair_address: str
    dex_id: str
    base_symbol: str
    base_address: str
    quote_symbol: str
    url: str
    price_usd: Optional[float]
    liquidity_usd: Optional[float]
    volume_h1: Optional[float]
    volume_h24: Optional[float]
    price_change_h1: Optional[float]
    price_change_h24: Optional[float]
    pair_created_at: Optional[datetime]  # UTC; None when DexScreener did not report it
    raw: dict = field(default_factory=dict, repr=False)

    def age_minutes(self, now=None):
        if self.pair_created_at is None:
            return None
        now = now or datetime.now(timezone.utc)
        return (now - self.pair_created_at).total_seconds() / 60.0

    @property
    def avg_hourly_volume_24h(self):
        """24h volume spread evenly over 24 hours - the baseline volume_h1 is compared
        against. None when volume_h24 itself is unknown (never treated as zero)."""
        if self.volume_h24 is None:
            return None
        return self.volume_h24 / 24.0

    @property
    def volume_spike_ratio(self):
        """volume_h1 / avg_hourly_volume_24h, or None when either side is unknown or the
        average is zero (a pair with no 24h volume on record has no meaningful ratio)."""
        avg = self.avg_hourly_volume_24h
        if avg is None or avg <= 0 or self.volume_h1 is None:
            return None
        return self.volume_h1 / avg


def parse_pair(raw):
    """One DexScreener pair dict -> Pair.

    Every field is read defensively: DexScreener omits keys (most often "liquidity" and
    "pairCreatedAt") for very new or very thin pairs instead of sending zeros. A missing
    key must stay None here - it is never guessed as 0, because 0 liquidity and "we don't
    know the liquidity" call for opposite handling downstream (see filter_by_min_liquidity
    and risk_flags.compute_risk_flags).
    """
    base = raw.get("baseToken") or {}
    quote = raw.get("quoteToken") or {}
    liquidity = raw.get("liquidity") or {}
    volume = raw.get("volume") or {}
    price_change = raw.get("priceChange") or {}

    created_at = None
    created_raw = raw.get("pairCreatedAt")
    if isinstance(created_raw, (int, float)) and created_raw > 0:
        try:
            created_at = datetime.fromtimestamp(created_raw / 1000.0, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            created_at = None

    return Pair(
        chain_id=raw.get("chainId") or "",
        pair_address=raw.get("pairAddress") or "",
        dex_id=raw.get("dexId") or "",
        base_symbol=base.get("symbol") or "?",
        base_address=base.get("address") or "",
        quote_symbol=quote.get("symbol") or "?",
        url=raw.get("url") or "",
        price_usd=_to_float(raw.get("priceUsd")),
        liquidity_usd=_to_float(liquidity.get("usd")),
        volume_h1=_to_float(volume.get("h1")),
        volume_h24=_to_float(volume.get("h24")),
        price_change_h1=_to_float(price_change.get("h1")),
        price_change_h24=_to_float(price_change.get("h24")),
        pair_created_at=created_at,
        raw=raw,
    )


def parse_pairs(raw_list):
    return [parse_pair(r) for r in raw_list or []]


def dedupe_by_address(pairs):
    """Keeps the first occurrence of each (chain_id, pair_address). Overlapping discovery
    queries (or a watchlist token that also turns up in a search result) routinely return
    the same pair more than once - later duplicates are dropped, first one wins."""
    seen = set()
    out = []
    for p in pairs:
        key = (p.chain_id, p.pair_address)
        if not p.pair_address or key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def filter_by_chains(pairs, chains):
    allowed = {c.strip().lower() for c in chains if c.strip()}
    if not allowed:
        return list(pairs)
    return [p for p in pairs if p.chain_id.lower() in allowed]


def filter_by_min_liquidity(pairs, min_liquidity_usd):
    """Pairs with unknown liquidity are excluded, not assumed to pass - see Pair's
    parse_pair docstring on why missing is not the same as zero."""
    if min_liquidity_usd is None:
        return list(pairs)
    return [p for p in pairs if p.liquidity_usd is not None and p.liquidity_usd >= min_liquidity_usd]


def find_new_pairs(pairs, window_minutes, now=None):
    """Pairs whose pairCreatedAt falls inside the last `window_minutes`, newest first.

    Pairs with an unknown creation time are skipped (never assumed new or old). `now` is
    injectable so tests are deterministic; production code leaves it as None and gets the
    real current time.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now.timestamp() - window_minutes * 60
    out = [p for p in pairs if p.pair_created_at is not None and p.pair_created_at.timestamp() >= cutoff]
    out.sort(key=lambda p: p.pair_created_at, reverse=True)
    return out


def find_volume_anomalies(pairs, spike_ratio, min_volume_usd=0.0):
    """Pairs where 1h volume is at least `spike_ratio` times the flat 24h hourly average
    (see Pair.volume_spike_ratio), ranked highest ratio first. `min_volume_usd` keeps a
    jump from $5 to $40 on a dead pair from being reported as a "spike"."""
    out = []
    for p in pairs:
        ratio = p.volume_spike_ratio
        if ratio is None or ratio < spike_ratio:
            continue
        if p.volume_h1 is None or p.volume_h1 < min_volume_usd:
            continue
        out.append(p)
    out.sort(key=lambda p: p.volume_spike_ratio, reverse=True)
    return out
