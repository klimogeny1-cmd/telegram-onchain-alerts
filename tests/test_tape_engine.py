"""Tests for alerts_bot.tape.engine - the rules. Synthetic Blur fixtures + a scripted fake
RPC + an in-memory SQLite dedup store; a fixed clock. No network."""
import unittest
from datetime import timedelta

from alerts_bot.dedup import DedupStore
from alerts_bot.solami.events import LiquidityChange, parse_blur_event
from alerts_bot.tape.engine import TapeEngine, TapeSettings, next_boundary, plan_subscriptions
from alerts_bot.tape.format import FOOTER
from alerts_bot.tape.state import PoolState
from alerts_bot.solami.rpc import TokenAccountBalance
from tests.solami_helpers import (AUTH, DEMO, HOLD_A, HOLD_B, HOLD_C, HOLD_D, NOW, WSOL, XTRA, FakeDataAPI, FakeRPC,
                                  blur_events, demo_mint_info, holders)


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)
        return self.now


def watch_settings(**overrides):
    base = dict(mode="watchlist", watch_mints=(DEMO,), large_trade_usd=5000.0, liq_alert_usd=2500.0,
                liq_report_adds=True, flush_sec=60)
    base.update(overrides)
    return TapeSettings(**base)


def fire_settings(**overrides):
    base = dict(mode="firehose", large_trade_usd=50000.0, liq_alert_usd=50000.0, liq_report_adds=False)
    base.update(overrides)
    return TapeSettings(**base)


class EngineCase(unittest.TestCase):
    def make(self, settings, rpc=None, data_api=None, store=True):
        self.clock = Clock()
        self.store = DedupStore(":memory:", clock=self.clock) if store else None
        if self.store:
            self.addCleanup(self.store.close)
        engine = TapeEngine(settings, rpc=rpc, data_api=data_api, store=self.store, now_fn=self.clock)
        return engine

    @staticmethod
    def feed(engine, events=None):
        for event in events if events is not None else blur_events():
            engine.handle(event)

    @staticmethod
    def sections(engine):
        return sorted({i.section for i in engine.pending})


class WatchlistRuleTests(EngineCase):
    def test_what_qualifies_from_the_fixture(self):
        engine = self.make(watch_settings())
        self.feed(engine)
        self.assertEqual(self.sections(engine), ["breakouts", "graduations", "liquidity", "pools", "trades"])
        trades = [i for i in engine.pending if i.section == "trades"]
        self.assertEqual(sorted(i.data["usd"] for i in trades), [6100.0, 7500.0])   # $119 sell stays out
        outlier = next(i for i in trades if i.data["usd"] == 6100.0)
        self.assertTrue(any("Price impact 23.5%" in f for f in outlier.flags))
        self.assertTrue(any("Outlier print" in f for f in outlier.flags))
        liquidity = [i for i in engine.pending if i.section == "liquidity"]
        self.assertEqual(len(liquidity), 1)                                         # small add stays out
        removal = liquidity[0].data
        self.assertEqual(removal["kind"], "remove")
        self.assertTrue(removal["approx"])
        self.assertAlmostEqual(removal["usd"], 22500.0 + 30_000_000 * 0.0007455, places=2)
        self.assertAlmostEqual(removal["pct"], 150 / 499.204 * 100, places=3)
        self.assertIn("Removed 30% of the pool's SOL side", liquidity[0].flags)

    def test_other_tokens_are_ignored_but_counted(self):
        engine = self.make(watch_settings())
        self.feed(engine)
        self.assertFalse(any(i.data.get("mint") == XTRA for i in engine.pending))
        self.assertEqual(engine.counts["swap"], 4)
        self.assertEqual(engine.meta.symbol(XTRA), "XTRA")                          # metadata still cached

    def test_large_burn_needs_supply_from_rpc(self):
        engine = self.make(watch_settings(), rpc=FakeRPC(infos={DEMO: demo_mint_info()}))
        engine.mint_info(DEMO)                                                      # cached, as startup does
        self.feed(engine)
        burn = next(i for i in engine.pending if i.section == "transfers")
        self.assertEqual(burn.data["kind"], "burn")
        self.assertAlmostEqual(burn.data["pct"], 2.0)
        self.assertEqual(burn.data["amount_ui"], 20_000_000)

    def test_flush_builds_one_post_with_footer_and_dedups_after_confirm(self):
        engine = self.make(watch_settings())
        self.feed(engine)
        posts = engine.flush(self.clock())
        self.assertEqual(len(posts), 1)
        text = posts[0].text
        self.assertTrue(text.startswith("<b>Solana Tape</b>"))
        self.assertTrue(text.endswith(FOOTER))
        self.assertIn("not financial advice", text)
        self.assertIn("watching DEMO", text)
        engine.confirm(posts[0], True)
        self.feed(engine)                         # the same events delivered again (e.g. a reconnect)
        self.assertEqual(engine.pending, [])
        self.assertEqual(engine.flush(self.clock()), [])

    def test_failed_send_requeues_then_gives_up(self):
        engine = self.make(watch_settings())
        self.feed(engine)
        count = len(engine.pending)
        for attempt in range(3):
            posts = engine.flush(self.clock())
            self.assertEqual(len(posts), 1, attempt)
            engine.confirm(posts[0], False)
        self.assertEqual(engine.pending, [])
        self.assertEqual(engine.items_dropped, count)

    def test_stale_items_are_dropped_at_flush(self):
        engine = self.make(watch_settings(item_max_age_sec=60))
        self.feed(engine)
        self.clock.advance(minutes=5)
        self.assertEqual(engine.flush(self.clock()), [])

    def test_liquidity_add_reporting_can_be_switched_off(self):
        engine = self.make(watch_settings(liq_report_adds=False, liq_alert_usd=1.0))
        self.feed(engine)
        kinds = [i.data["kind"] for i in engine.pending if i.section == "liquidity"]
        self.assertEqual(kinds, ["remove"])

    def test_pending_queue_is_capped(self):
        engine = self.make(watch_settings(max_pending=2))
        self.feed(engine)
        self.assertEqual(len(engine.pending), 2)
        self.assertGreater(engine.items_dropped, 0)


class FirehoseRuleTests(EngineCase):
    def test_whale_trade_and_graduation_with_rpc_facts(self):
        rpc = FakeRPC(infos={DEMO: demo_mint_info(mint_authority=AUTH)}, largest={DEMO: holders((HOLD_A, 150e6))})
        engine = self.make(fire_settings(), rpc=rpc)
        self.feed(engine)
        self.assertEqual(self.sections(engine), ["graduations", "trades"])
        trade = next(i for i in engine.pending if i.section == "trades")
        self.assertEqual(trade.data["mint"], XTRA)                                   # $60K >= $50K; DEMO $7.5K is not
        posts = engine.flush(self.clock())
        text = posts[0].text
        self.assertIn("mint authority: active ⚠️ · freeze authority: none · top-10 accounts 15.0% of supply", text)
        self.assertIn("rule-based flag", text)                                        # footnote for the ⚠️
        self.assertIn("firehose", text.splitlines()[0])

    def test_breakout_thresholds(self):
        engine = self.make(fire_settings(surge_min_multiple=4.0, surge_min_mcap_usd=250000.0))
        self.feed(engine)
        self.assertNotIn("breakouts", self.sections(engine))                          # mcap $75K < $250K
        engine = self.make(fire_settings(surge_min_multiple=4.0, surge_min_mcap_usd=50000.0))
        self.feed(engine)
        self.assertIn("breakouts", self.sections(engine))

    def test_liquidity_threshold_and_removals_only(self):
        engine = self.make(fire_settings(liq_alert_usd=40000.0))
        self.feed(engine)
        self.assertEqual([i.data["kind"] for i in engine.pending if i.section == "liquidity"], ["remove"])

    def test_rpc_budget_per_flush(self):
        engine = self.make(fire_settings(rpc_lookups_per_flush=0), rpc=FakeRPC(infos={DEMO: demo_mint_info()}))
        self.feed(engine)
        text = engine.flush(self.clock())[0].text
        self.assertIn("authorities: not checked this round", text)

    @staticmethod
    def graduations(count):
        return [parse_blur_event({"type": "graduation", "signature": "SigGrad%d" % n, "slot": 370000100 + n,
                                  "block_time": 1790000100 + n, "mint": "Grad%040d" % n, "launchpad": "pumpfun",
                                  "dex": "pumpswap", "pool": "Pool%040d" % n}) for n in range(count)]

    def test_the_busiest_window_of_the_first_live_night_is_checked_in_full(self):
        events = self.graduations(10)               # 20 lookups: the old budget of 12 checked six of them
        rpc = FakeRPC(infos={e.mint: demo_mint_info() for e in events})
        engine = self.make(fire_settings(max_items_per_section=10), rpc=rpc)
        self.feed(engine, events)
        text = "\n".join(p.text for p in engine.flush(self.clock()))
        self.assertEqual(text.count("mint authority: none · freeze authority: none"), 10)
        self.assertNotIn("not checked this round", text)

    def test_rpc_lookups_stop_after_the_time_budget_when_rpc_is_slow(self):
        events = self.graduations(10)
        clock_holder = {}

        class SlowRPC(FakeRPC):
            def get_mint_info(self, mint):
                clock_holder["clock"].advance(seconds=8)
                return FakeRPC.get_mint_info(self, mint)

        rpc = SlowRPC(infos={e.mint: demo_mint_info() for e in events})
        engine = self.make(fire_settings(max_items_per_section=10), rpc=rpc)
        clock_holder["clock"] = self.clock
        self.feed(engine, events)
        text = "\n".join(p.text for p in engine.flush(self.clock()))
        self.assertEqual(len([c for c in rpc.calls if c[0] == "getAccountInfo"]), 3)   # at 0, 8 and 16 s
        self.assertEqual(text.count("authorities: not checked this round (RPC budget)"), 7)

    def test_digest_counts_launches_pools_graduations(self):
        engine = self.make(fire_settings(), store=False)
        engine.startup()
        self.feed(engine)
        text = engine.digest(self.clock.advance(minutes=15))[0].text
        self.assertIn("New tokens: 1 (pumpfun 1)", text)
        self.assertIn("New pools: 1 (meteora_dlmm 1)", text)
        self.assertIn("Graduations: 1 (pumpfun 1)", text)
        self.assertTrue(text.endswith(FOOTER))
        self.assertEqual(engine.digest(self.clock.advance(minutes=15)), [])            # counters were reset

    def test_symbols_come_from_the_data_api_when_the_stream_has_none(self):
        api = FakeDataAPI({XTRA: {"symbol": "XTR2", "name": "", "decimals": 6}})
        engine = self.make(fire_settings(), data_api=api)
        whale = [e for e in blur_events() if e.signature == "SigSwapXtraWhale"]
        self.feed(engine, whale)
        text = engine.flush(self.clock())[0].text
        self.assertIn(">XTR2</a>", text)


class RpcFactsTests(EngineCase):
    def test_startup_card_then_authority_change_is_reported(self):
        rpc = FakeRPC(infos={DEMO: [demo_mint_info(mint_authority=AUTH), demo_mint_info(mint_authority=None)]},
                      largest={DEMO: holders((HOLD_A, 150e6), (HOLD_B, 90e6), (HOLD_C, 60e6))})
        engine = self.make(watch_settings(), rpc=rpc)
        cards = engine.startup()
        self.assertEqual(len(cards), 1)
        card = cards[0].text
        self.assertIn("Now watching", card)
        self.assertIn("Mint authority: <a", card)                                   # active -> linked address
        self.assertIn("Freeze authority: none", card)
        self.assertIn("Top-10 holder accounts: 30.0% of supply (largest 15.0%)", card)
        self.assertIn("⚠️ Mint authority active", card)
        self.assertTrue(card.endswith(FOOTER))
        engine.confirm(cards[0], True)
        engine.check_authorities(self.clock.advance(minutes=30))
        change = next(i for i in engine.pending if i.section == "authority")
        self.assertEqual((change.data["what"], change.data["old"], change.data["new"]), ("mint authority", AUTH, None))
        self.assertIn("mint authority changed", engine.flush(self.clock())[0].text)

    def test_card_is_once_per_day_unless_forced(self):
        rpc = FakeRPC(infos={DEMO: demo_mint_info()}, largest={DEMO: holders((HOLD_A, 10e6))})
        engine = self.make(watch_settings(), rpc=rpc)
        engine.confirm(engine.startup()[0], True)
        self.assertEqual(engine.startup(), [])
        self.assertEqual(len(engine.startup(force_cards=True)), 1)

    def test_restart_with_unchanged_state_reports_nothing(self):
        rpc = FakeRPC(infos={DEMO: demo_mint_info()}, largest={DEMO: holders((HOLD_A, 10e6))})
        engine = self.make(watch_settings(), rpc=rpc)
        engine.startup()
        again = TapeEngine(watch_settings(), rpc=rpc, store=self.store, now_fn=self.clock)
        again.startup()
        self.assertEqual(again.pending, [])

    def test_holder_changes_including_accounts_that_left_the_top_20(self):
        before = holders((HOLD_A, 150e6), (HOLD_B, 90e6), (HOLD_C, 60e6))
        after = holders((HOLD_A, 120e6), (HOLD_B, 90e6), (HOLD_D, 70e6))            # C dropped out, D is new
        rpc = FakeRPC(infos={DEMO: demo_mint_info()}, largest={DEMO: [before, after]},
                      accounts={HOLD_C: TokenAccountBalance(HOLD_C, 0, closed=True),
                                HOLD_A: TokenAccountBalance(HOLD_A, 120 * 10 ** 12, 6, owner="WhaLeA1")})
        engine = self.make(watch_settings(), rpc=rpc)
        engine.startup()
        self.assertEqual(engine.pending, [])                                         # first snapshot = baseline
        engine.poll_holders(self.clock.advance(minutes=5))
        changes = {i.data["account"]: i.data for i in engine.pending if i.section == "holders"}
        self.assertEqual(set(changes), {HOLD_A, HOLD_C, HOLD_D})                     # B unchanged
        self.assertAlmostEqual(changes[HOLD_A]["delta_pct"], -3.0)
        self.assertEqual(changes[HOLD_A]["owner"], "WhaLeA1")
        self.assertAlmostEqual(changes[HOLD_C]["delta_pct"], -6.0)
        self.assertTrue(changes[HOLD_D]["entered"])
        text = engine.flush(self.clock())[0].text
        self.assertIn("lost 3.00% of supply (-30.0M), now 12.0%", text)
        self.assertIn("entered the top 20 holder accounts with 7.00% of supply", text)

    def test_rpc_outage_never_breaks_the_engine(self):
        engine = self.make(watch_settings(), rpc=FakeRPC(fail=True))
        engine.startup()
        engine.tick(self.clock.advance(hours=1))
        self.feed(engine)
        self.assertTrue(engine.flush(self.clock()))


class ScheduleTests(EngineCase):
    def test_tick_runs_flush_and_summary_on_schedule(self):
        engine = self.make(watch_settings(summary_every_sec=3600, flush_sec=60), store=False)
        engine.startup()
        self.feed(engine)
        self.assertEqual(engine.tick(self.clock.advance(seconds=30)), [])            # nothing due yet
        tape = engine.tick(self.clock.advance(seconds=31))
        self.assertEqual([p.kind for p in tape], ["tape"])
        boundary = next_boundary(NOW, 3600)
        self.clock.now = boundary
        summary = [p for p in engine.tick(boundary) if p.kind == "summary"]
        self.assertEqual(len(summary), 1)
        text = summary[0].text
        self.assertIn("Trades: 3 (2 buys / 1 sells) · 3 wallets", text)
        self.assertIn("Liquidity: +", text)
        self.assertIn("since 14:20 UTC", text)                                       # partial first window
        self.assertTrue(text.endswith(FOOTER))

    def test_dedup_rows_are_pruned_daily(self):
        engine = self.make(watch_settings())
        engine.startup()
        self.store.mark("live", "old-item")
        pruned = []
        self.store.prune = lambda days: pruned.append(days)
        engine.tick(self.clock.advance(hours=25))
        self.assertEqual(pruned, [30])

    def test_next_boundary(self):
        self.assertEqual(next_boundary(NOW, 3600), NOW.replace(hour=15, minute=0))
        self.assertEqual(next_boundary(NOW, 900), NOW.replace(minute=30))


class ValuationTests(EngineCase):
    def liq(self, **fields):
        base = {"type": "liquidity", "kind": "remove", "signature": "S", "pool": "P", "base_mint": DEMO,
                "quote_mint": WSOL, "quote_amount": 10 * 10 ** 9, "quote_decimals": 9}
        base.update(fields)
        return parse_blur_event(base)

    def test_reported_usd_value_wins(self):
        engine = self.make(watch_settings())
        usd, approx = engine.liquidity_usd(self.liq(value_usd="1234.5"), None)
        self.assertEqual((usd, approx), (1234.5, False))

    def test_single_priced_leg_is_doubled_and_marked_approximate(self):
        engine = self.make(watch_settings())
        engine.prices._quote[WSOL] = 150.0
        usd, approx = engine.liquidity_usd(self.liq(), None)
        self.assertEqual((usd, approx), (3000.0, True))

    def test_unpriced_event_stays_unpriced(self):
        engine = self.make(watch_settings())
        self.assertEqual(engine.liquidity_usd(self.liq(), None), (None, False))

    def test_implausible_implied_sol_price_is_ignored(self):
        engine = self.make(watch_settings())
        swap = parse_blur_event({"type": "swap", "mint": DEMO, "quote_mint": WSOL, "price": "0.000000005",
                                 "price_usd": "0.00075", "candle_ok": True})    # implies SOL = $150,000
        engine.handle(swap)
        self.assertIsNone(engine.prices.quote_price(WSOL))
        self.assertEqual(engine.prices.rejected, 1)

    def test_pool_share_from_reported_post_event_reserve(self):
        event = self.liq(quote_reserve=30 * 10 ** 9)                  # 10 removed, 30 left -> 25%
        self.assertEqual(TapeEngine.pool_share_pct(event, None), 25.0)
        self.assertEqual(TapeEngine.pool_share_pct(event, PoolState(quote_reserve=1)), 25.0)
        self.assertIsNone(TapeEngine.pool_share_pct(self.liq(), None))
        self.assertIsInstance(event, LiquidityChange)


class PlanTests(unittest.TestCase):
    def test_watchlist_is_one_filtered_socket(self):
        subs = plan_subscriptions(watch_settings())
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0].mints, (DEMO,))
        self.assertIn("transfer", subs[0].types)

    def test_firehose_splits_whale_swaps_into_their_own_socket(self):
        market, whales = plan_subscriptions(fire_settings(large_trade_usd=75000.0))
        self.assertNotIn("swap", market.types)
        self.assertEqual((whales.types, whales.min_volume_usd), (("swap",), 75000.0))


if __name__ == "__main__":
    unittest.main()
