"""In-memory state the tape engine builds up from the Blur stream.

Everything here is derived from events the bot has actually received - nothing is
fetched, estimated from outside data, or carried over from before the process started
(persistent state lives in dedup.DedupStore's kv table instead).
"""
import threading
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from typing import Optional

from ..solami.events import KNOWN_DECIMALS, KNOWN_SYMBOLS, USDC_MINT, USDT_MINT, WSOL_MINT


@dataclass
class TokenMeta:
    symbol: str = ""
    name: str = ""
    decimals: Optional[int] = None


class MetadataCache:
    """mint -> TokenMeta, filled from token_create and metadata events (and, optionally,
    the Data API). Thread-safe: BlurStream reads known_mints() from its reader threads to
    send Blur's {"have": [...]} message after a reconnect."""

    def __init__(self, max_size=20000):
        self._data = OrderedDict()
        self._lock = threading.Lock()
        self.max_size = max_size

    def put(self, mint, symbol="", name="", decimals=None):
        if not mint:
            return
        with self._lock:
            meta = self._data.pop(mint, None) or TokenMeta()
            if symbol:
                meta.symbol = symbol
            if name:
                meta.name = name
            if decimals is not None:
                meta.decimals = decimals
            self._data[mint] = meta
            while len(self._data) > self.max_size:
                self._data.popitem(last=False)

    def get(self, mint):
        with self._lock:
            return self._data.get(mint)

    def symbol(self, mint):
        meta = self.get(mint)
        if meta and (meta.symbol or meta.name):
            return meta.symbol or meta.name
        return KNOWN_SYMBOLS.get(mint, "")

    def decimals(self, mint):
        meta = self.get(mint)
        if meta and meta.decimals is not None:
            return meta.decimals
        return KNOWN_DECIMALS.get(mint)

    def known_mints(self, limit=500):
        with self._lock:
            mints = [m for m, meta in self._data.items() if meta.symbol or meta.name]
        return mints[-limit:]

    def __len__(self):
        return len(self._data)


class PriceBook:
    """Last USD prices seen on the stream: a token's price_usd from its latest swap, and
    each quote token's USD price implied by swaps quoted in it (price_usd / price)."""

    STABLES = {USDC_MINT: 1.0, USDT_MINT: 1.0}
    # Sanity bounds for implied quote prices. price_usd / price is the quote token's USD
    # price only if `price` is quote units per base token (assumption A6 in README); if
    # that ever turned out wrong, these bounds keep a nonsense SOL price out of every
    # liquidity valuation instead of silently scaling them by 10^3.
    PLAUSIBLE = {WSOL_MINT: (1.0, 100000.0), USDC_MINT: (0.8, 1.2), USDT_MINT: (0.8, 1.2)}

    def __init__(self):
        self._token = {}
        self._quote = {}
        self.rejected = 0

    def on_swap(self, swap):
        if swap.mint and swap.price_usd is not None and swap.price_usd > 0 and swap.candle_ok is not False:
            self._token[swap.mint] = swap.price_usd
        implied = swap.quote_price_usd
        if swap.quote_mint and implied is not None and swap.candle_ok is not False:
            low, high = self.PLAUSIBLE.get(swap.quote_mint, (0.0, float("inf")))
            if low <= implied <= high:
                self._quote[swap.quote_mint] = implied
            else:
                self.rejected += 1

    def token_price(self, mint):
        return self._token.get(mint)

    def quote_price(self, mint):
        if mint in self._quote:
            return self._quote[mint]
        return self.STABLES.get(mint)


@dataclass
class PoolState:
    mint: str = ""
    quote_mint: str = ""
    dex: str = ""
    base_reserve: Optional[int] = None
    quote_reserve: Optional[int] = None
    base_decimals: Optional[int] = None
    quote_decimals: Optional[int] = None


class PoolBook:
    """pool address -> latest known reserves, from swap events (which report them)."""

    def __init__(self, max_size=100000):
        self._pools = OrderedDict()
        self.max_size = max_size

    def _entry(self, pool):
        state = self._pools.pop(pool, None) or PoolState()
        self._pools[pool] = state
        while len(self._pools) > self.max_size:
            self._pools.popitem(last=False)
        return state

    def register(self, pool, mint="", quote_mint="", dex=""):
        if not pool:
            return
        state = self._entry(pool)
        state.mint = state.mint or mint
        state.quote_mint = state.quote_mint or quote_mint
        state.dex = state.dex or dex

    def on_swap(self, swap):
        if not swap.pool:
            return
        state = self._entry(swap.pool)
        state.mint, state.quote_mint, state.dex = swap.mint or state.mint, swap.quote_mint or state.quote_mint, \
            swap.dex or state.dex
        if swap.base_reserve is not None and swap.quote_reserve is not None:
            state.base_reserve, state.quote_reserve = swap.base_reserve, swap.quote_reserve
        if swap.base_decimals is not None:
            state.base_decimals = swap.base_decimals
        if swap.quote_decimals is not None:
            state.quote_decimals = swap.quote_decimals

    def on_liquidity(self, event):
        """Keeps reserves roughly current between swaps: exact if the event reports post-
        event reserves, otherwise the previous reserves moved by the event's own amounts."""
        if not event.pool:
            return
        state = self._entry(event.pool)
        state.mint = state.mint or event.mint
        state.quote_mint = state.quote_mint or event.quote_mint
        if event.base_reserve is not None and event.quote_reserve is not None:
            state.base_reserve, state.quote_reserve = event.base_reserve, event.quote_reserve
            return
        sign = 1 if event.kind == "add" else -1 if event.kind == "remove" else 0
        if sign and state.quote_reserve is not None and event.quote_amount is not None:
            state.quote_reserve = max(0, state.quote_reserve + sign * event.quote_amount)
        if sign and state.base_reserve is not None and event.base_amount is not None:
            state.base_reserve = max(0, state.base_reserve + sign * event.base_amount)

    def get(self, pool):
        return self._pools.get(pool)


@dataclass
class WindowStats:
    """One watched token's activity since `started_at` (the summary window)."""
    started_at: object = None
    trades: int = 0
    buys: int = 0
    sells: int = 0
    buy_usd: float = 0.0
    sell_usd: float = 0.0
    traders: set = field(default_factory=set)
    largest_usd: float = 0.0
    largest_side: str = ""
    largest_signature: str = ""
    first_price: Optional[float] = None
    last_price: Optional[float] = None
    liq_adds: int = 0
    liq_removes: int = 0
    liq_add_usd: float = 0.0
    liq_remove_usd: float = 0.0
    liq_unpriced: int = 0

    MAX_TRADERS = 100000

    def on_swap(self, swap):
        self.trades += 1
        usd = swap.volume_usd or 0.0
        if swap.side == "buy":
            self.buys += 1
            self.buy_usd += usd
        elif swap.side == "sell":
            self.sells += 1
            self.sell_usd += usd
        if swap.trader and len(self.traders) < self.MAX_TRADERS:
            self.traders.add(swap.trader)
        if usd > self.largest_usd:
            self.largest_usd, self.largest_side, self.largest_signature = usd, swap.side, swap.signature
        if swap.price_usd and swap.candle_ok is not False:
            if self.first_price is None:
                self.first_price = swap.price_usd
            self.last_price = swap.price_usd

    def on_liquidity(self, kind, usd):
        if kind == "add":
            self.liq_adds += 1
            self.liq_add_usd += usd or 0.0
        elif kind == "remove":
            self.liq_removes += 1
            self.liq_remove_usd += usd or 0.0
        if usd is None:
            self.liq_unpriced += 1

    @property
    def volume_usd(self):
        return self.buy_usd + self.sell_usd

    @property
    def price_change_pct(self):
        if self.first_price and self.last_price:
            return (self.last_price / self.first_price - 1.0) * 100.0
        return None

    @property
    def active(self):
        return self.trades > 0 or self.liq_adds > 0 or self.liq_removes > 0


@dataclass
class LaunchCounters:
    """Firehose digest counters: launches, new pools and graduations since `started_at`."""
    started_at: object = None
    tokens: Counter = field(default_factory=Counter)
    pools: Counter = field(default_factory=Counter)
    graduations: Counter = field(default_factory=Counter)
