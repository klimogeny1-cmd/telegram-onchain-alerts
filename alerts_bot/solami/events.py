"""Normalized events from the Solami Blur stream (wss://ws.solami.dev/data/subscribe).

Blur sends one JSON object per WebSocket message, tagged with a `type` field (docs:
solami.dev/docs/blur, read 2026-09-26). Documented types: swap, liquidity, token_create,
pool_create, transfer, candle, stats, meme, graduation, surge, radar, metadata - plus
stream notices (error, replay_end, backfill_end).

Number conventions, straight from the Blur docs:
  - every FRACTIONAL value (prices, USD amounts, percentages, ratios) is a JSON *string*
    holding a plain decimal, e.g. "price_usd": "0.0000008672";
  - integers (slots, unix times, counts, decimals) are JSON numbers;
  - raw token `amount`s may be strings (a u64 can exceed 2^53);
  - a value JSON cannot represent (NaN, infinity) arrives as null.
The helpers below accept both strings and numbers for every field, because being wrong
about which one a field uses must never crash the bot or silently turn a value into 0.

Parsing contract: parse_blur_event() never raises. Unknown or malformed input gives
None or an OtherEvent, and a missing field stays None/"" - it is never guessed.

ASSUMPTIONS (to verify against real frames with `python3 main.py --check-live`; also
listed in README "Assumptions to verify with a real key"):
  A1. `liquidity`: the docs say "an add or remove, with the provider and both legs" and
      the docs playground reads `kind`, `base_mint`, `dex`. Field names for the legs are
      assumed to mirror `swap` (base_amount / quote_amount / *_decimals, quote_mint,
      pool, provider). A USD value is used only if a field such as `value_usd` exists.
  A2. `pool_create`: the docs call it "same shape as token_create with the name fields
      blank" (so `mint`), while the playground reads `base_mint`. Both are accepted.
  A3. `transfer`: `kind` (transfer|mint|burn), `mint`, `src_owner`, `dst_owner` are in
      the docs playground; `amount` (raw units) and `decimals` are assumed.
  A4. `metadata`: `mint`, `name`, `symbol` are in the playground; `decimals` and `logo`
      are named in the docs text ("name, symbol, logo, decimals, and socials").
  A5. `graduation`: "carrying the launchpad it came from and the pool/dex it landed on".
"""
import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Optional

WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"

# Decimals of the common quote tokens, used only when an event does not carry its own.
KNOWN_DECIMALS = {WSOL_MINT: 9, USDC_MINT: 6, USDT_MINT: 6}
KNOWN_SYMBOLS = {WSOL_MINT: "SOL", USDC_MINT: "USDC", USDT_MINT: "USDT"}

DATA_TYPES = ("swap", "liquidity", "token_create", "pool_create", "transfer", "candle", "stats",
              "meme", "graduation", "surge", "radar", "metadata")
NOTICE_TYPES = ("error", "replay_end", "backfill_end")


# --- tolerant field readers -------------------------------------------------------------

def to_float(value):
    """"0.0000412" / 1284.55 / 3 -> float; None, "", "abc", NaN, inf, bool -> None."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def to_int(value):
    """Raw integer amounts: 1500 / "1500" / 1500.0 -> 1500; anything fractional or
    non-numeric -> None (an integer field that isn't an integer is not trusted)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and value.is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        if text.lstrip("-").isdigit():
            return int(text)
        number = to_float(text)
        return int(number) if number is not None and number.is_integer() else None
    return None


def to_abs_int(value):
    """Amounts whose direction is carried by another field (liquidity `kind`, transfer
    `kind`): a stream that reports a removal as a negative delta still means the same
    size, so the sign is dropped here and the direction is taken from `kind`."""
    number = to_int(value)
    return abs(number) if number is not None else None


def to_str(value):
    return value.strip() if isinstance(value, str) else ""


def to_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return None


def _first(raw, *keys):
    for key in keys:
        value = raw.get(key)
        if value is not None and value != "":
            return value
    return None


# --- event model ------------------------------------------------------------------------

@dataclass
class BlurEvent:
    type: str = ""
    mint: str = ""
    signature: str = ""
    slot: Optional[int] = None
    block_time: Optional[int] = None
    dex: str = ""
    pool: str = ""
    replay: bool = False
    raw: dict = field(default_factory=dict, repr=False, compare=False)

    @property
    def key(self):
        """Stable identity for de-duplication: one on-chain event -> one key, however many
        times it is delivered (reconnects, backfill, a restart)."""
        if self.signature:
            return "%s:%s:%s:%s:%s" % (self.type, self.signature, self.pool, self.mint,
                                       self._key_suffix())
        blob = json.dumps(self.raw, sort_keys=True, default=str).encode("utf-8")
        return "%s:h%s" % (self.type, hashlib.sha1(blob).hexdigest()[:20])

    def _key_suffix(self):
        return ""


@dataclass
class Swap(BlurEvent):
    quote_mint: str = ""
    trader: str = ""
    side: str = ""                              # "buy" | "sell"
    base_amount: Optional[int] = None           # raw units of `mint`
    quote_amount: Optional[int] = None          # raw units of `quote_mint`
    base_decimals: Optional[int] = None
    quote_decimals: Optional[int] = None
    base_reserve: Optional[int] = None          # pool reserves as reported with the trade
    quote_reserve: Optional[int] = None
    price: Optional[float] = None               # in quote-token units per base token
    price_usd: Optional[float] = None
    volume_usd: Optional[float] = None
    price_impact_pct: Optional[float] = None
    fee_pct: Optional[float] = None
    candle_ok: Optional[bool] = None            # False = Solami's price guard judged it an outlier
    ix_index: Optional[int] = None

    def _key_suffix(self):
        if self.ix_index is not None:
            return str(self.ix_index)
        return "%s:%s" % (self.side, self.base_amount)

    @property
    def quote_price_usd(self):
        """USD price of the quote token implied by this trade (price_usd / price)."""
        if self.price and self.price > 0 and self.price_usd is not None and self.price_usd > 0:
            return self.price_usd / self.price
        return None


@dataclass
class LiquidityChange(BlurEvent):
    kind: str = ""                              # "add" | "remove"
    provider: str = ""
    quote_mint: str = ""
    base_amount: Optional[int] = None
    quote_amount: Optional[int] = None
    base_decimals: Optional[int] = None
    quote_decimals: Optional[int] = None
    base_reserve: Optional[int] = None          # only if the stream reports post-event reserves
    quote_reserve: Optional[int] = None
    value_usd: Optional[float] = None           # only if the stream reports a USD value (A1)

    def _key_suffix(self):
        return "%s:%s" % (self.kind, self.quote_amount)


@dataclass
class TokenLaunch(BlurEvent):                   # token_create
    quote_mint: str = ""
    name: str = ""
    symbol: str = ""
    uri: str = ""
    creator: str = ""


@dataclass
class PoolCreated(BlurEvent):                   # pool_create
    quote_mint: str = ""
    creator: str = ""


@dataclass
class Graduation(BlurEvent):
    launchpad: str = ""


@dataclass
class VolumeBreakout(BlurEvent):                # surge (5m vs 1h) / radar (30m vs 6h)
    trigger_time: Optional[int] = None
    mcap_at_trigger: Optional[float] = None
    price_at_trigger: Optional[float] = None
    volume_window_usd: Optional[float] = None
    baseline_usd: Optional[float] = None
    multiple: Optional[float] = None
    trades: Optional[int] = None
    traders_est: Optional[int] = None
    window_secs: Optional[int] = None

    @property
    def key(self):
        return "%s:%s:%s:%s" % (self.type, self.mint, self.trigger_time, self.window_secs)


@dataclass
class TokenMetadata(BlurEvent):
    name: str = ""
    symbol: str = ""
    decimals: Optional[int] = None
    logo: str = ""


@dataclass
class TokenTransfer(BlurEvent):
    kind: str = ""                              # "transfer" | "mint" | "burn"
    src_owner: str = ""
    dst_owner: str = ""
    amount: Optional[int] = None                # raw units (A3)
    decimals: Optional[int] = None

    def _key_suffix(self):
        return "%s:%s:%s:%s" % (self.kind, self.src_owner, self.dst_owner, self.amount)


@dataclass
class StreamNotice(BlurEvent):                  # error / replay_end / backfill_end
    message: str = ""


@dataclass
class OtherEvent(BlurEvent):
    """candle / stats / meme / anything new: counted for stream health, not posted."""


def _common(raw, event_type):
    return dict(
        type=event_type,
        mint=to_str(_first(raw, "mint", "base_mint", "address")),
        signature=to_str(raw.get("signature")),
        slot=to_int(raw.get("slot")),
        block_time=to_int(_first(raw, "block_time", "time")),
        dex=to_str(raw.get("dex")),
        pool=to_str(raw.get("pool")),
        replay=to_bool(raw.get("replay")) is True,
        raw=raw,
    )


def _parse_swap(raw):
    return Swap(
        quote_mint=to_str(raw.get("quote_mint")),
        trader=to_str(raw.get("trader")),
        side=to_str(raw.get("side")).lower(),
        base_amount=to_int(raw.get("base_amount")),
        quote_amount=to_int(raw.get("quote_amount")),
        base_decimals=to_int(raw.get("base_decimals")),
        quote_decimals=to_int(raw.get("quote_decimals")),
        base_reserve=to_int(raw.get("base_reserve")),
        quote_reserve=to_int(raw.get("quote_reserve")),
        price=to_float(raw.get("price")),
        price_usd=to_float(raw.get("price_usd")),
        volume_usd=to_float(raw.get("volume_usd")),
        price_impact_pct=to_float(raw.get("price_impact_pct")),
        fee_pct=to_float(raw.get("fee_pct")),
        candle_ok=to_bool(raw.get("candle_ok")),
        ix_index=to_int(raw.get("ix_index")),
        **_common(raw, "swap"))


def _parse_liquidity(raw):
    return LiquidityChange(
        kind=to_str(raw.get("kind")).lower(),
        provider=to_str(_first(raw, "provider", "owner", "trader")),
        quote_mint=to_str(raw.get("quote_mint")),
        base_amount=to_abs_int(_first(raw, "base_amount", "base_delta")),
        quote_amount=to_abs_int(_first(raw, "quote_amount", "quote_delta")),
        base_decimals=to_int(raw.get("base_decimals")),
        quote_decimals=to_int(raw.get("quote_decimals")),
        base_reserve=to_int(raw.get("base_reserve")),
        quote_reserve=to_int(raw.get("quote_reserve")),
        value_usd=to_float(_first(raw, "value_usd", "volume_usd", "amount_usd", "usd")),
        **_common(raw, "liquidity"))


def _parse_breakout(raw, event_type):
    event = VolumeBreakout(
        trigger_time=to_int(raw.get("trigger_time")),
        mcap_at_trigger=to_float(raw.get("mcap_at_trigger")),
        price_at_trigger=to_float(raw.get("price_at_trigger")),
        volume_window_usd=to_float(raw.get("volume_window_usd")),
        baseline_usd=to_float(raw.get("baseline_usd")),
        multiple=to_float(raw.get("multiple")),
        trades=to_int(raw.get("trades")),
        traders_est=to_int(raw.get("traders_est")),
        window_secs=to_int(raw.get("window_secs")),
        **_common(raw, event_type))
    if event.block_time is None:
        event.block_time = event.trigger_time
    return event


def _parse_metadata(raw):
    nested = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    source = nested or raw
    return TokenMetadata(
        name=to_str(source.get("name")),
        symbol=to_str(source.get("symbol")),
        decimals=to_int(source.get("decimals")),
        logo=to_str(_first(source, "logo", "image", "logo_uri")),
        **_common(raw, "metadata"))


def parse_blur_event(raw):
    """One decoded Blur frame (a dict) -> a BlurEvent subclass, or None if `raw` is not
    an object at all. Never raises: unknown types become OtherEvent so a new event type
    on Solami's side can never break this bot (the docs ask clients to do exactly this)."""
    if not isinstance(raw, dict):
        return None
    event_type = to_str(raw.get("type")).lower() or "unknown"
    try:
        if event_type == "swap":
            return _parse_swap(raw)
        if event_type == "liquidity":
            return _parse_liquidity(raw)
        if event_type == "token_create":
            return TokenLaunch(quote_mint=to_str(raw.get("quote_mint")), name=to_str(raw.get("name")),
                               symbol=to_str(raw.get("symbol")), uri=to_str(raw.get("uri")),
                               creator=to_str(raw.get("creator")), **_common(raw, event_type))
        if event_type == "pool_create":
            return PoolCreated(quote_mint=to_str(raw.get("quote_mint")),
                               creator=to_str(_first(raw, "creator", "trader")), **_common(raw, event_type))
        if event_type == "graduation":
            return Graduation(launchpad=to_str(raw.get("launchpad")), **_common(raw, event_type))
        if event_type in ("surge", "radar"):
            return _parse_breakout(raw, event_type)
        if event_type == "metadata":
            return _parse_metadata(raw)
        if event_type == "transfer":
            return TokenTransfer(kind=to_str(raw.get("kind")).lower(), src_owner=to_str(raw.get("src_owner")),
                                 dst_owner=to_str(raw.get("dst_owner")), amount=to_abs_int(raw.get("amount")),
                                 decimals=to_int(raw.get("decimals")), **_common(raw, event_type))
        if event_type in NOTICE_TYPES:
            return StreamNotice(message=to_str(_first(raw, "message", "error", "reason")),
                                **_common(raw, event_type))
        return OtherEvent(**_common(raw, event_type))
    except Exception:  # defensive: a malformed frame must never take the stream down
        return OtherEvent(type="malformed", raw=raw)


def parse_blur_message(text):
    """One WebSocket text message -> list of events. Blur documents one event per
    message; a JSON array is accepted too, just in case. Bad JSON -> []."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return []
    items = data if isinstance(data, list) else [data]
    events = []
    for item in items:
        event = parse_blur_event(item)
        if event is not None:
            events.append(event)
    return events
