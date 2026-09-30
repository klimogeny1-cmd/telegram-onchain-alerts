"""The tape engine: Solami events in, Telegram posts out, by documented rules.

Two modes (README "Modes"):

  watchlist  A project's own token(s), WATCH_MINTS. One Blur subscription filtered to
             those mints. Posted: large trades, liquidity adds/removes (by USD size or
             share of the pool), new pools, graduations, Solami surge/radar breakouts,
             large transfers/mints/burns, top-20 holder balance changes (RPC poll), mint/
             freeze authority and Token-2022 changes (RPC re-check), an hourly summary
             (trades, buy vs sell volume, wallets, liquidity in/out, price), and a
             "now watching" card on start.
  firehose   The whole market, with thresholds. Two Blur subscriptions (see
             plan_subscriptions). Posted: whale trades, large liquidity removals,
             graduations (with mint/freeze authority read over RPC), big breakouts, and
             a launch digest (new tokens / pools / graduations by venue).

Real-time items are batched: the engine queues them and flush() turns everything queued
in the last FLUSH_SEC into one post, grouped by section and capped per section. Posting
is confirmed back via confirm() - only then is an item recorded as sent, so a failed
send is retried instead of silently lost, and a restart never reposts what went out.

Nothing in here touches the network directly: RPC and Data API clients are passed in
(and can be None), which is what makes the whole engine testable offline.
"""
import logging
import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from ..solami.blur import BlurSubscription
from ..solami.events import (Graduation, LiquidityChange, PoolCreated, StreamNotice, Swap, TokenLaunch,
                             TokenMetadata, TokenTransfer, VolumeBreakout)
from . import format as fmt
from .flags import (concentration_flag, fee_bps, liquidity_flags, mint_flags, short, swap_flags,
                    top10_share_pct)
from .state import LaunchCounters, MetadataCache, PoolBook, PriceBook, WindowStats

log = logging.getLogger("tape")

WATCHLIST_TYPES = ("swap", "liquidity", "pool_create", "token_create", "graduation", "surge", "radar", "transfer")
FIREHOSE_MARKET_TYPES = ("liquidity", "pool_create", "token_create", "graduation", "surge", "radar")
MINT_INFO_TTL = timedelta(minutes=30)
MINT_INFO_CACHE_MAX = 5000
PRUNE_EVERY = timedelta(days=1)


@dataclass
class TapeSettings:
    mode: str = "firehose"                 # "watchlist" | "firehose"
    watch_mints: Tuple[str, ...] = ()
    large_trade_usd: float = 50000.0
    liq_alert_usd: float = 50000.0
    liq_alert_pct: float = 10.0
    liq_report_adds: bool = False
    surge_min_multiple: float = 5.0
    surge_min_mcap_usd: float = 250000.0
    holder_change_pct: float = 0.5
    top10_flag_pct: float = 50.0
    price_impact_flag_pct: float = 10.0
    flush_sec: int = 60
    max_items_per_section: int = 8
    holders_poll_sec: int = 300
    authority_recheck_sec: int = 1800
    summary_every_sec: int = 3600
    digest_every_sec: int = 900
    rpc_lookups_per_flush: int = 12
    symbol_lookups_per_flush: int = 5
    item_max_age_sec: int = 900
    max_pending: int = 2000

    @property
    def watchlist(self):
        return self.mode == "watchlist"

    @classmethod
    def from_config(cls, config):
        return cls(
            mode=config.tape_mode, watch_mints=tuple(config.watch_mints),
            large_trade_usd=config.large_trade_usd, liq_alert_usd=config.liq_alert_usd,
            liq_alert_pct=config.liq_alert_pct, liq_report_adds=config.liq_report_adds,
            surge_min_multiple=config.surge_min_multiple, surge_min_mcap_usd=config.surge_min_mcap_usd,
            holder_change_pct=config.holder_change_pct, top10_flag_pct=config.top10_flag_pct,
            price_impact_flag_pct=config.price_impact_flag_pct, flush_sec=config.flush_sec,
            max_items_per_section=config.max_items_per_post, holders_poll_sec=config.holders_poll_min * 60,
            authority_recheck_sec=config.authority_recheck_min * 60,
            summary_every_sec=config.summary_every_min * 60, digest_every_sec=config.digest_every_min * 60,
        )


def plan_subscriptions(settings):
    """Which Blur sockets to open. Blur applies a type-specific filter (like
    min_volume_usd) as "swaps only" (docs), so the firehose needs two sockets: one broad
    one for launches/pools/liquidity/breakouts, one for swaps above the whale threshold
    filtered server-side - instead of pulling every swap on Solana and dropping 99%."""
    if settings.watchlist:
        return [BlurSubscription(name="watchlist", types=WATCHLIST_TYPES, mints=tuple(settings.watch_mints))]
    return [BlurSubscription(name="market", types=FIREHOSE_MARKET_TYPES),
            BlurSubscription(name="large-trades", types=("swap",), min_volume_usd=settings.large_trade_usd)]


@dataclass
class TapeItem:
    section: str
    key: str
    data: dict
    flags: List[str] = field(default_factory=list)
    sort_value: float = 0.0
    created_at: Optional[datetime] = None
    needs_mint_info: bool = False
    attempts: int = 0


@dataclass
class Post:
    text: str
    kind: str                                   # "tape" | "card" | "summary" | "digest"
    items: List[TapeItem] = field(default_factory=list)
    marks: List[Tuple[str, str, str]] = field(default_factory=list)   # (kind, key, bucket) once sent


def next_boundary(now, period_sec):
    """Next wall-clock multiple of period_sec after `now` (e.g. the next :00 for 3600)."""
    epoch = now.timestamp()
    return datetime.fromtimestamp((math.floor(epoch / period_sec) + 1) * period_sec, tz=timezone.utc)


def _utcnow():
    return datetime.now(timezone.utc)


class TapeEngine:
    def __init__(self, settings, rpc=None, data_api=None, store=None, health_fn=None, now_fn=_utcnow):
        self.s = settings
        self.rpc = rpc
        self.data_api = data_api
        self.store = store
        self.health_fn = health_fn               # () -> {subscription: {messages, connects, ...}}
        self._now = now_fn
        self.meta = MetadataCache()
        self.prices = PriceBook()
        self.pools = PoolBook()
        self.watch = set(settings.watch_mints)
        self.stats: Dict[str, WindowStats] = {}
        self.launches = LaunchCounters()
        self.pending: List[TapeItem] = []
        self._pending_keys = set()
        self._mint_info = {}                     # mint -> (MintInfo or None, fetched_at)
        self._holders_mem = {}                   # snapshots when running without a store
        self._authority_mem = {}
        self._next = {}
        self._health_mark = (0, 0)               # (messages, connects) at the last digest
        self.counts = Counter()
        self.posts_confirmed = 0
        self.items_dropped = 0

    # === intake ================================================================================

    def handle(self, event):
        """Routes one normalized event. Raises only on programming errors; the live loop
        catches those per event, so one bad event never stops the tape."""
        self.counts[event.type] += 1
        if isinstance(event, TokenMetadata):
            self.meta.put(event.mint, event.symbol, event.name, event.decimals)
        elif isinstance(event, Swap):
            self._on_swap(event)
        elif isinstance(event, LiquidityChange):
            self._on_liquidity(event)
        elif isinstance(event, PoolCreated):
            self._on_pool(event)
        elif isinstance(event, TokenLaunch):
            self._on_launch(event)
        elif isinstance(event, Graduation):
            self._on_graduation(event)
        elif isinstance(event, VolumeBreakout):
            self._on_breakout(event)
        elif isinstance(event, TokenTransfer):
            self._on_transfer(event)
        elif isinstance(event, StreamNotice):
            log.debug("stream notice %s: %s", event.type, event.message)   # blur.py already logged errors

    def _watched(self, *mints):
        return any(m in self.watch for m in mints if m)

    def _stats(self, mint):
        if mint not in self.stats:
            self.stats[mint] = WindowStats(started_at=self.launches.started_at or self._now())
        return self.stats[mint]

    def _queue(self, item):
        if item.key in self._pending_keys:
            return
        if self.store is not None and self.store.seen("live", item.key):
            return
        if len(self.pending) >= self.s.max_pending:
            self.items_dropped += 1
            return
        item.created_at = item.created_at or self._now()
        self.pending.append(item)
        self._pending_keys.add(item.key)

    def _on_swap(self, ev):
        self.prices.on_swap(ev)
        self.pools.on_swap(ev)
        if ev.base_decimals is not None:
            self.meta.put(ev.mint, decimals=ev.base_decimals)
        if self.s.watchlist:
            if not self._watched(ev.mint):
                return
            self._stats(ev.mint).on_swap(ev)
        if ev.volume_usd is not None and ev.volume_usd >= self.s.large_trade_usd:
            self._queue(TapeItem(
                "trades", "trade:" + ev.key, sort_value=ev.volume_usd, flags=swap_flags(ev, self.s.price_impact_flag_pct),
                data=dict(mint=ev.mint, side=ev.side, usd=ev.volume_usd, price_usd=ev.price_usd, dex=ev.dex,
                          trader=ev.trader, signature=ev.signature)))

    def liquidity_usd(self, ev, pool):
        """(USD value, is_estimate) of a liquidity event. Uses the stream's own USD field
        if it has one (assumption A1), then the stream's USD value of each leg (live frames:
        base_usd / quote_usd), else values the legs at the last prices seen on the stream.
        With only one leg priced it doubles it - exact for constant-product pools, an
        estimate elsewhere - and marks the number with "≈"."""
        if ev.value_usd is not None:
            return ev.value_usd, False
        stream_base = ev.base_usd if ev.base_usd is not None and ev.base_usd > 0 else None
        stream_quote = ev.quote_usd if ev.quote_usd is not None and ev.quote_usd > 0 else None
        if stream_base is not None and stream_quote is not None:
            return stream_base + stream_quote, False
        quote_mint = ev.quote_mint or (pool.quote_mint if pool else "")
        qdec = _first_not_none(ev.quote_decimals, pool.quote_decimals if pool else None, self.meta.decimals(quote_mint))
        bdec = _first_not_none(ev.base_decimals, pool.base_decimals if pool else None, self.meta.decimals(ev.mint))
        q_price, b_price = self.prices.quote_price(quote_mint), self.prices.token_price(ev.mint)
        quote_leg = ev.quote_amount / 10 ** qdec * q_price if None not in (ev.quote_amount, qdec, q_price) else None
        base_leg = ev.base_amount / 10 ** bdec * b_price if None not in (ev.base_amount, bdec, b_price) else None
        quote_leg = stream_quote if stream_quote is not None else quote_leg
        base_leg = stream_base if stream_base is not None else base_leg
        if quote_leg is not None and base_leg is not None:
            return quote_leg + base_leg, True
        if quote_leg is not None:
            return 2 * quote_leg, True
        if base_leg is not None:
            return 2 * base_leg, True
        return None, False

    @staticmethod
    def pool_share_pct(ev, pool):
        """The event's quote amount as % of the pool's quote reserve before the event."""
        amount = ev.quote_amount
        if amount is None or amount <= 0:
            return None
        if ev.quote_reserve is not None:          # post-event reserve reported by the stream
            before = ev.quote_reserve + amount if ev.kind == "remove" else ev.quote_reserve - amount
        elif pool is not None and pool.quote_reserve:
            before = pool.quote_reserve
        else:
            return None
        if before <= 0:
            return None
        share = amount / float(before) * 100.0
        return min(100.0, share) if ev.kind == "remove" else share

    def _legs_text(self, ev, quote_mint):
        parts = []
        for amount, mint, decimals in ((ev.quote_amount, quote_mint, ev.quote_decimals),
                                       (ev.base_amount, ev.mint, ev.base_decimals)):
            decimals = _first_not_none(decimals, self.meta.decimals(mint))
            if amount is not None and decimals is not None:
                parts.append("%s %s" % (fmt.compact(abs(amount) / 10 ** decimals),
                                        fmt.clean_label(self.meta.symbol(mint), 16) or short(mint)))
        return " + ".join(parts)

    def _on_liquidity(self, ev):
        pool = self.pools.get(ev.pool)
        usd, approx = self.liquidity_usd(ev, pool)
        pct = self.pool_share_pct(ev, pool)
        quote_mint = ev.quote_mint or (pool.quote_mint if pool else "")
        self.pools.on_liquidity(ev)
        if self.s.watchlist:
            if not self._watched(ev.mint):
                return
            self._stats(ev.mint).on_liquidity(ev.kind, usd)
        if ev.kind not in ("add", "remove") or (ev.kind == "add" and not self.s.liq_report_adds):
            return
        qualifies = usd is not None and usd >= self.s.liq_alert_usd
        if not qualifies and self.s.watchlist and pct is not None and pct >= self.s.liq_alert_pct:
            # a big share of a small pool matters for a project's own token - but not dust
            qualifies = usd is None or usd >= min(250.0, self.s.liq_alert_usd)
        if not qualifies:
            return
        quote_symbol = fmt.clean_label(self.meta.symbol(quote_mint), 16)
        self._queue(TapeItem(
            "liquidity", "liq:" + ev.key, sort_value=usd or 0.0,
            flags=liquidity_flags(ev.kind, pct, quote_symbol, self.s.liq_alert_pct),
            data=dict(mint=ev.mint, quote_mint=quote_mint, kind=ev.kind, usd=usd, approx=approx, pct=pct, dex=ev.dex,
                      provider=ev.provider, signature=ev.signature,
                      legs=self._legs_text(ev, quote_mint) if usd is None else "")))

    def _on_pool(self, ev):
        self.launches.pools[ev.dex or "unknown"] += 1
        self.pools.register(ev.pool, ev.mint, ev.quote_mint, ev.dex)
        if self.s.watchlist and self._watched(ev.mint, ev.quote_mint):
            mint, quote = (ev.mint, ev.quote_mint) if self._watched(ev.mint) else (ev.quote_mint, ev.mint)
            self._queue(TapeItem("pools", "pool:" + ev.key, data=dict(
                mint=mint, quote_mint=quote, dex=ev.dex, pool=ev.pool, creator=ev.creator, signature=ev.signature)))

    def _on_launch(self, ev):
        self.meta.put(ev.mint, ev.symbol, ev.name)
        self.launches.tokens[ev.dex or "unknown"] += 1
        if self.s.watchlist and self._watched(ev.mint):
            self._queue(TapeItem("launches", "launch:" + ev.key, needs_mint_info=True, data=dict(
                mint=ev.mint, name=ev.name, dex=ev.dex, creator=ev.creator, signature=ev.signature)))

    def _on_graduation(self, ev):
        self.launches.graduations[ev.launchpad or "unknown"] += 1
        if self.s.watchlist and not self._watched(ev.mint):
            return
        self._queue(TapeItem("graduations", "grad:" + ev.key, needs_mint_info=True, data=dict(
            mint=ev.mint, launchpad=ev.launchpad, dex=ev.dex, pool=ev.pool, signature=ev.signature)))

    def _on_breakout(self, ev):
        if self.s.watchlist:
            if not self._watched(ev.mint):
                return
        elif (ev.multiple or 0.0) < self.s.surge_min_multiple or (ev.mcap_at_trigger or 0.0) < self.s.surge_min_mcap_usd:
            return
        self._queue(TapeItem("breakouts", "brk:" + ev.key, sort_value=ev.multiple or 0.0,
                             needs_mint_info=not self.s.watchlist, data=dict(
                                 mint=ev.mint, kind=ev.type, multiple=ev.multiple, volume_usd=ev.volume_window_usd,
                                 window_secs=ev.window_secs, trades=ev.trades, traders_est=ev.traders_est,
                                 mcap=ev.mcap_at_trigger)))

    def _on_transfer(self, ev):
        if not self.s.watchlist or not self._watched(ev.mint) or ev.amount is None:
            return
        info = self.cached_mint_info(ev.mint)
        if info is None or not info.supply:
            return
        pct = ev.amount / float(info.supply) * 100.0
        if pct < self.s.holder_change_pct:
            return
        decimals = _first_not_none(ev.decimals, info.decimals)
        self._queue(TapeItem("transfers", "xfer:" + ev.key, sort_value=pct, data=dict(
            mint=ev.mint, kind=ev.kind, pct=pct, amount_ui=ev.amount / 10 ** decimals if decimals is not None else None,
            src_owner=ev.src_owner, dst_owner=ev.dst_owner, signature=ev.signature)))

    # === RPC-backed facts ======================================================================

    def cached_mint_info(self, mint):
        entry = self._mint_info.get(mint)
        return entry[0] if entry else None

    def mint_info(self, mint, max_age=MINT_INFO_TTL, fetch=True):
        entry = self._mint_info.get(mint)
        now = self._now()
        if entry and now - entry[1] < max_age:
            return entry[0]
        if not fetch or self.rpc is None:
            return entry[0] if entry else None
        try:
            info = self.rpc.get_mint_info(mint)
        except Exception as exc:
            log.warning("mint info for %s unavailable: %s", mint, exc)
            return entry[0] if entry else None
        self._mint_info.pop(mint, None)
        self._mint_info[mint] = (info, now)
        while len(self._mint_info) > MINT_INFO_CACHE_MAX:          # oldest first (insertion order)
            self._mint_info.pop(next(iter(self._mint_info)))
        if info is not None and info.decimals is not None:
            self.meta.put(mint, decimals=info.decimals)
        return info

    def _top_accounts(self, mint):
        if self.rpc is None:
            return []
        try:
            return self.rpc.get_largest_token_accounts(mint)
        except Exception as exc:
            log.warning("top holders for %s unavailable: %s", mint, exc)
            return []

    @staticmethod
    def authority_state(info):
        ext = info.extensions or {}
        fee = fee_bps(ext["transferFeeConfig"]) if "transferFeeConfig" in ext else None
        return {
            "mint authority": info.mint_authority,
            "freeze authority": info.freeze_authority,
            "permanent delegate": (ext.get("permanentDelegate") or {}).get("delegate"),
            "transfer hook program": (ext.get("transferHook") or {}).get("programId"),
            "transfer fee": ("%.2f%%" % (fee / 100.0)) if fee is not None else None,
            "paused": "yes" if (ext.get("pausableConfig") or {}).get("paused") is True else None,
        }

    def _authority_changes(self, mint, info, now):
        """Compares the mint's authorities with the last stored check; queues one item per
        change and stores the new state. The first check ever just records a baseline."""
        if info is None:
            return
        state = self.authority_state(info)
        name = "authority:%s" % mint
        previous = self.store.kv_get(name) if self.store is not None else self._authority_mem.get(mint)
        if previous is not None:
            for what in state:
                old, new = previous.get(what), state[what]
                if old == new:
                    continue
                is_address = what in ("mint authority", "freeze authority", "permanent delegate",
                                      "transfer hook program")
                self._queue(TapeItem("authority", "auth:%s:%s:%s>%s:%s" % (mint, what, old, new, now.date()),
                                     sort_value=10.0, data=dict(mint=mint, what=what, old=old, new=new,
                                                                address=is_address)))
        if self.store is not None:
            self.store.kv_set(name, state)
        else:
            self._authority_mem[mint] = state

    def _holder_changes(self, mint, info, top, now):
        """Compares the current top-20 holder accounts with the last stored snapshot.
        Accounts that dropped out of the top 20 are re-read, so a holder that sold down
        (or closed the account) is still reported. Changes below HOLDER_CHANGE_PCT of
        supply are ignored. The first snapshot ever just records a baseline."""
        if info is None or not info.supply or not top:
            return
        name = "holders:%s" % mint
        previous = self.store.kv_get(name) if self.store is not None else self._holders_mem.get(mint)
        current = {a.address: a.amount for a in top}
        owners = {a.address: a.owner for a in top if a.owner}
        if previous and isinstance(previous.get("accounts"), dict):
            old_accounts = previous["accounts"]
            since = _since_label(previous.get("taken_at"), now)
            dropped = [a for a in old_accounts if a not in current]
            balances = {}
            if dropped and self.rpc is not None:
                try:
                    balances = self.rpc.get_token_accounts(dropped)
                except Exception as exc:
                    log.warning("balances of accounts that left the top 20 unavailable: %s", exc)
            changes = []
            for address in set(old_accounts) | set(current):
                old = old_accounts.get(address)
                if address in current:
                    new = current[address]
                elif balances.get(address) is not None:
                    new = balances[address].amount
                    owners.setdefault(address, balances[address].owner)
                else:
                    continue
                if old is None:
                    new_pct = new / float(info.supply) * 100.0
                    if new_pct >= self.s.holder_change_pct:
                        changes.append((address, None, new))
                    continue
                if abs(new - old) / float(info.supply) * 100.0 >= self.s.holder_change_pct:
                    changes.append((address, old, new))
            missing_owners = [a for a, _o, _n in changes if not owners.get(a)]
            if missing_owners and self.rpc is not None:
                try:
                    for address, bal in self.rpc.get_token_accounts(missing_owners).items():
                        if bal is not None and bal.owner:
                            owners[address] = bal.owner
                except Exception as exc:
                    log.info("owner lookup failed: %s", exc)
            decimals = info.decimals
            for address, old, new in changes:
                new_pct = new / float(info.supply) * 100.0
                if old is None:
                    data = dict(mint=mint, account=address, owner=owners.get(address, ""), entered=True, new_pct=new_pct)
                    sort_value = new_pct
                else:
                    delta = new - old
                    data = dict(mint=mint, account=address, owner=owners.get(address, ""), entered=False,
                                delta_pct=delta / float(info.supply) * 100.0, new_pct=new_pct, since=since,
                                delta_ui=delta / 10 ** decimals if decimals is not None else None)
                    sort_value = abs(data["delta_pct"])
                self._queue(TapeItem("holders", "holder:%s:%s:%s>%s" % (mint, address, old, new),
                                     sort_value=sort_value, data=data))
        snapshot = {"taken_at": now.isoformat(), "accounts": current}
        if self.store is not None:
            self.store.kv_set(name, snapshot)
        else:
            self._holders_mem[mint] = snapshot

    def check_authorities(self, now):
        for mint in self.s.watch_mints:
            self._authority_changes(mint, self.mint_info(mint, max_age=timedelta(0)), now)

    def poll_holders(self, now):
        for mint in self.s.watch_mints:
            info = self.mint_info(mint)
            self._holder_changes(mint, info, self._top_accounts(mint), now)

    # === scheduling ============================================================================

    def _schedule(self, now):
        self.launches = LaunchCounters(started_at=now)
        self._next = {
            "flush": now + timedelta(seconds=self.s.flush_sec),
            "authority": now + timedelta(seconds=self.s.authority_recheck_sec),
            "holders": now + timedelta(seconds=self.s.holders_poll_sec),
            "summary": next_boundary(now, self.s.summary_every_sec) if self.s.summary_every_sec else None,
            "digest": next_boundary(now, self.s.digest_every_sec) if self.s.digest_every_sec else None,
            "prune": now + PRUNE_EVERY,
        }

    def startup(self, force_cards=False):
        """Called once after the stream starts. Watchlist mode: reads each token's mint
        account and top holders over RPC, reports anything that changed while the bot was
        down, and returns a "now watching" card per token (once per UTC day, unless
        `force_cards`)."""
        now = self._now()
        self._schedule(now)
        if not self.s.watchlist:
            return []
        if self.rpc is None:
            log.info("no RPC client: authority checks, holder tracking and 'now watching' cards are off")
            return []
        posts = []
        for mint in self.s.watch_mints:
            if self.data_api is not None and not self.meta.symbol(mint):
                meta = self.data_api.token_metadata(mint)
                if meta:
                    self.meta.put(mint, meta.get("symbol", ""), meta.get("name", ""), meta.get("decimals"))
            info = self.mint_info(mint, max_age=timedelta(0))
            top = self._top_accounts(mint)
            self._authority_changes(mint, info, now)
            self._holder_changes(mint, info, top, now)
            bucket = now.strftime("%Y-%m-%d")
            if not force_cards and self.store is not None and self.store.seen("card", mint, bucket):
                continue
            flags = mint_flags(info)
            top10 = top10_share_pct(top, info.supply) if info else None
            concentration = concentration_flag(top, info.supply, self.s.top10_flag_pct) if info else None
            if concentration:
                flags.append(concentration)
            meta = self.meta.get(mint)
            for text in fmt.render_card(mint, self.meta.symbol(mint), meta.name if meta else "", info, top, flags, now,
                                        top10_pct=top10):
                posts.append(Post(text, "card", marks=[("card", mint, bucket)]))
        return posts

    def tick(self, now=None):
        """Runs whatever periodic work is due and returns the posts it produced."""
        now = now or self._now()
        if not self._next:
            self._schedule(now)
        posts = []
        if self.s.watchlist:
            if now >= self._next["authority"]:
                self._next["authority"] = now + timedelta(seconds=self.s.authority_recheck_sec)
                self._safely(self.check_authorities, now)
            if now >= self._next["holders"]:
                self._next["holders"] = now + timedelta(seconds=self.s.holders_poll_sec)
                self._safely(self.poll_holders, now)
            if self._next["summary"] and now >= self._next["summary"]:
                self._next["summary"] = next_boundary(now, self.s.summary_every_sec)
                posts += self.summaries(now)
        elif self._next["digest"] and now >= self._next["digest"]:
            self._next["digest"] = next_boundary(now, self.s.digest_every_sec)
            posts += self.digest(now)
        if now >= self._next["flush"]:
            posts += self.flush(now)
        if now >= self._next["prune"]:
            self._next["prune"] = now + PRUNE_EVERY
            if self.store is not None:
                self._safely(self.store.prune, 30)
        return posts

    @staticmethod
    def _safely(fn, *args):
        try:
            fn(*args)
        except Exception:
            log.exception("%s failed, will retry on the next schedule", getattr(fn, "__name__", "task"))

    # === posts =================================================================================

    def _enrich(self, items):
        """RPC facts for items that need them (graduations, firehose breakouts, launches),
        within a per-flush budget; then symbols from the Data API for mints the stream
        has not named yet. Items beyond a budget are posted without the extra fact."""
        lookups = 0
        for item in sorted(items, key=lambda i: i.sort_value, reverse=True):
            if not item.needs_mint_info or self.rpc is None:
                continue
            mint = item.data.get("mint", "")
            cached = self._mint_info.get(mint)
            fresh = cached is not None and self._now() - cached[1] < MINT_INFO_TTL
            if not fresh and lookups >= self.s.rpc_lookups_per_flush:
                item.data["facts"] = "authorities: not checked this round (RPC budget)"
                continue
            if not fresh:
                lookups += 1
            info = self.mint_info(mint)
            if info is None:
                item.data["facts"] = "mint account not readable over RPC right now"
                continue
            facts = ["mint authority: %s" % ("active ⚠️" if info.mint_authority else "none"),
                     "freeze authority: %s" % ("active ⚠️" if info.freeze_authority else "none")]
            if item.section == "graduations" and lookups < self.s.rpc_lookups_per_flush:
                lookups += 1
                share = top10_share_pct(self._top_accounts(mint), info.supply)
                if share is not None:
                    facts.append("top-10 accounts %s of supply" % fmt.format_pct(share))
            item.data["facts"] = " · ".join(facts)
            # the facts line already states both authorities; keep the other rule flags
            item.flags = item.flags + [f for f in mint_flags(info)
                                       if not f.startswith(("Mint authority", "Freeze authority"))]
        if self.data_api is None:
            return
        missing = []
        for item in items:
            for mint in (item.data.get("mint"), item.data.get("quote_mint")):
                if mint and not self.meta.symbol(mint) and mint not in missing:
                    missing.append(mint)
        for mint in missing[:self.s.symbol_lookups_per_flush]:
            meta = self.data_api.token_metadata(mint)
            if meta:
                self.meta.put(mint, meta.get("symbol", ""), meta.get("name", ""), meta.get("decimals"))

    def _subtitle(self):
        if not self.s.watchlist:
            return "firehose"
        names = [fmt.clean_label(self.meta.symbol(m), 16) or short(m) for m in self.s.watch_mints[:3]]
        extra = len(self.s.watch_mints) - len(names)
        return "watching " + ", ".join(names) + (" +%d" % extra if extra > 0 else "")

    def _threshold_notes(self):
        usd = fmt.format_usd
        notes = {"trades": "≥ %s" % usd(self.s.large_trade_usd),
                 "holders": "changes ≥ %s of supply" % fmt.format_pct(self.s.holder_change_pct),
                 "transfers": "≥ %s of supply" % fmt.format_pct(self.s.holder_change_pct)}
        if self.s.watchlist:
            notes["liquidity"] = "≥ %s or ≥ %s of a pool" % (usd(self.s.liq_alert_usd), fmt.format_pct(self.s.liq_alert_pct))
        else:
            notes["liquidity"] = ("≥ %s" if self.s.liq_report_adds else "removals ≥ %s") % usd(self.s.liq_alert_usd)
            notes["breakouts"] = "≥ %.0f× baseline, mcap ≥ %s" % (self.s.surge_min_multiple, usd(self.s.surge_min_mcap_usd))
        return notes

    def flush(self, now=None):
        """Everything queued since the last flush -> tape post(s). Items already sent (per
        the dedup store) or older than item_max_age_sec are dropped here."""
        now = now or self._now()
        if self._next:
            self._next["flush"] = now + timedelta(seconds=self.s.flush_sec)
        items, self.pending, self._pending_keys = self.pending, [], set()
        max_age = timedelta(seconds=self.s.item_max_age_sec)
        fresh = [i for i in items if now - (i.created_at or now) <= max_age]
        if len(fresh) < len(items):
            log.info("dropped %d tape items older than %ds", len(items) - len(fresh), self.s.item_max_age_sec)
        if self.store is not None:
            fresh = [i for i in fresh if not self.store.seen("live", i.key)]
        if not fresh:
            return []
        self._enrich(fresh)
        by_section = {}
        for item in fresh:
            by_section.setdefault(item.section, []).append(item)
        sections, shown_items = {}, []
        for section, section_items in by_section.items():
            section_items.sort(key=lambda i: (i.sort_value, i.created_at), reverse=True)
            shown = section_items[:self.s.max_items_per_section]
            sections[section] = (shown, len(section_items) - len(shown))
            shown_items.extend(shown)
        texts = fmt.render_tape(sections, self.meta.symbol, now, self._subtitle(), self._threshold_notes())
        if not texts:
            return []
        # All marks ride on the first post: if it fails, everything is retried; later
        # continuation posts (only for very busy windows) are best effort.
        posts = [Post(texts[0], "tape", items=shown_items, marks=[("live", i.key, "once") for i in shown_items])]
        posts += [Post(text, "tape") for text in texts[1:]]
        return posts

    def summaries(self, now):
        posts = []
        bucket = now.strftime("%Y-%m-%dT%H:%M")
        for mint in self.s.watch_mints:
            stats = self.stats.get(mint)
            if stats is not None and stats.active:
                started = stats.started_at or now
                minutes = int(round((now - started).total_seconds() / 60.0))
                full = self.s.summary_every_sec and minutes >= self.s.summary_every_sec / 60.0 - 1
                label = ("last %d min" % minutes) if full else ("since %s UTC" % fmt.utc_hm(started))
                for text in fmt.render_summary(mint, self.meta.symbol(mint), stats, now, label):
                    posts.append(Post(text, "summary", marks=[("summary", mint, bucket)]))
            self.stats[mint] = WindowStats(started_at=now)
        return posts

    def digest(self, now):
        counters = self.launches
        self.launches = LaunchCounters(started_at=now)
        started = counters.started_at or now
        minutes = max(1, int(round((now - started).total_seconds() / 60.0)))
        total = sum(counters.tokens.values()) + sum(counters.pools.values()) + sum(counters.graduations.values())
        if not total:
            return []
        stream_line = None
        if self.health_fn is not None:
            try:
                health = self.health_fn() or {}
                messages = sum(h.get("messages", 0) for h in health.values())
                connects = sum(h.get("connects", 0) for h in health.values())
                last_messages, last_connects = self._health_mark
                self._health_mark = (messages, connects)
                reconnects = max(0, connects - last_connects - (len(health) if last_connects == 0 else 0))
                stream_line = "Stream: %s events in this window, %d reconnect%s" % (
                    format(messages - last_messages, ","), reconnects, "" if reconnects == 1 else "s")
            except Exception:
                stream_line = None
        return [Post(text, "digest", marks=[("digest", "firehose", now.strftime("%Y-%m-%dT%H:%M"))])
                for text in fmt.render_digest(counters, now, "last %d min" % minutes, stream_line)]

    def confirm(self, post, sent):
        """Records a post's items as sent - or re-queues them (up to 3 attempts) if the
        send failed, so a Telegram hiccup delays a fact instead of losing it."""
        if sent:
            self.posts_confirmed += 1
            if self.store is not None:
                for kind, key, bucket in post.marks:
                    self.store.mark(kind, key, bucket)
            return
        for item in post.items:
            item.attempts += 1
            if item.attempts < 3:
                if item.key not in self._pending_keys:
                    self.pending.append(item)
                    self._pending_keys.add(item.key)
            else:
                self.items_dropped += 1
                log.warning("giving up on tape item %s after %d failed sends", item.key, item.attempts)


def _first_not_none(*values):
    for value in values:
        if value is not None:
            return value
    return None


def _since_label(taken_at, now):
    try:
        then = datetime.fromisoformat(taken_at)
    except (TypeError, ValueError):
        return "?"
    return then.strftime("%H:%M") if then.date() == now.date() else then.strftime("%m-%d %H:%M")
