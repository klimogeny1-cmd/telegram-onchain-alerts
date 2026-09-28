#!/usr/bin/env python3
"""Entry point for Solana Tape (telegram-onchain-alerts).

Usage (DATA_SOURCE=solami, the default - the live tape on Solami Blur + RPC):
    python3 main.py --check                 # validate .env and exit, no network calls at all
    python3 main.py --check-live            # verify the Solami key: one RPC call + 20 s of the Blur stream
    python3 main.py --dry-run --duration 300  # live mainnet data, posts printed instead of sent
    python3 main.py                         # run until stopped (SIGTERM/Ctrl-C shut down cleanly)
    python3 main.py --env examples/offline-demo.conf --dry-run --replay-file tests/fixtures/solami/blur_frames.jsonl
                                            # offline: replay synthetic frames, no key, no network

Legacy (DATA_SOURCE=dexscreener - the original keyless polling tape):
    python3 main.py --once [--dry-run]      # a single DexScreener cycle
    python3 main.py                         # one cycle every INTERVAL_MIN minutes

See README "Quick start".
"""
import argparse
import json
import logging
import os
import signal
import sys
import threading
import time

from alerts_bot.config import Config
from alerts_bot.dedup import DedupStore
from alerts_bot.telegram import BotAPI, TokenFilter

CONFIG_ERROR_EXIT_CODE = 2  # matches deploy/telegram-onchain-alerts.service RestartPreventExitStatus=2


class DryRunBot:
    """Drop-in replacement for telegram.BotAPI: prints what would be sent instead of
    calling the Telegram API. A real BotAPI is never constructed in --dry-run mode, so no
    token - real or fake - is ever used to contact Telegram."""

    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text, **_kwargs):
        self.sent.append((chat_id, text))
        print("----- DRY RUN: would send to %s -----" % chat_id)
        print(text)
        print("----- end of post -----\n", flush=True)
        return {"message_id": -1}


def build_arg_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", dest="env_path", default=None, help="path to .env (default: ./.env)")
    parser.add_argument("--check", action="store_true", help="validate settings and exit (no network)")
    parser.add_argument("--check-live", nargs="?", type=int, const=20, default=None, metavar="SECONDS",
                        help="Solami only: verify the key with one RPC call and SECONDS (default 20) of the "
                             "Blur stream, print what arrived, and exit")
    parser.add_argument("--record", metavar="FILE", default=None,
                        help="with --check-live: also save every raw Blur frame to FILE (JSONL)")
    parser.add_argument("--once", action="store_true", help="DexScreener only: run a single cycle and exit")
    parser.add_argument("--dry-run", action="store_true", help="print posts instead of sending them to Telegram")
    parser.add_argument("--duration", type=int, default=None, metavar="SECONDS",
                        help="Solami only: stop cleanly after SECONDS (handy for demos and trials)")
    parser.add_argument("--replay-file", metavar="FILE", default=None,
                        help="Solami only: read Blur frames from a JSONL file instead of the live stream "
                             "(offline; no key needed)")
    parser.add_argument("--announce", action="store_true",
                        help="Solami watchlist: post the 'now watching' card even if it was already posted today")
    return parser


def setup_logging(level_name, secrets):
    logging.basicConfig(level=getattr(logging, level_name, logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    secrets = [s for s in secrets if s]
    if secrets:
        token_filter = TokenFilter(*secrets)
        for handler in logging.getLogger().handlers:
            handler.addFilter(token_filter)


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    config = Config.load(args.env_path)
    setup_logging(config.log_level, config.secrets)
    log = logging.getLogger("main")

    for key, text in config.problems:
        log.warning("config: %s: %s", key, text)
    if not config.env_found:
        log.warning("no .env file found at %s - copy .env.example to .env and fill it in", config.env_path)
    if config.data_source == "dexscreener":
        return run_dexscreener(args, config, log)
    return run_solami(args, config, log)


# === Solami live tape ===========================================================================

def _telegram_ok(config, log):
    ok = True
    if not config.token_ok:
        log.error("BOT_TOKEN is missing or does not look like a Telegram bot token")
        ok = False
    if not config.channel_ok:
        log.error("CHANNEL_ID is empty")
        ok = False
    return ok


def run_solami(args, config, log):
    from alerts_bot.solami import BlurStream, ReplaySource, SolamiDataAPI, SolamiRPC
    from alerts_bot.tape import Publisher, TapeEngine, TapeSettings, plan_subscriptions, run_live

    offline = bool(args.replay_file)
    live_problems = [] if offline else config.live_problems()
    if offline and config.tape_mode == "watchlist" and not config.watch_mints:
        live_problems.append("TAPE_MODE=watchlist needs at least one token mint in WATCH_MINTS")
    for problem in live_problems:
        log.error(problem)
    needs_telegram = not (args.dry_run or args.check_live is not None)
    telegram_ok = _telegram_ok(config, log) if (needs_telegram or args.check) else True
    if live_problems or not telegram_ok:
        return CONFIG_ERROR_EXIT_CODE

    settings = TapeSettings.from_config(config)
    subscriptions = plan_subscriptions(settings)
    if args.check:
        log.info("config OK: data source solami, mode %s, %d watched mint(s), %d Blur subscription(s): %s",
                 settings.mode, len(settings.watch_mints), len(subscriptions),
                 "; ".join("%s types=%s%s" % (s.name, ",".join(s.types),
                                               " min_volume_usd=%g" % s.min_volume_usd if s.min_volume_usd else "")
                           for s in subscriptions))
        return 0
    if args.check_live is not None:
        return check_live(config, settings, subscriptions, max(5, args.check_live), args.record, log)

    rpc = data_api = None
    if config.solami_api_key:
        rpc = SolamiRPC(config.solami_api_key, url=config.solami_rpc_url, timeout=config.request_timeout_sec,
                        max_rps=config.solami_rpc_max_rps)
        if config.solami_data_api:
            data_api = SolamiDataAPI(config.solami_api_key, base_url=config.solami_api_url)
    elif offline:
        log.info("offline replay without SOLAMI_API_KEY: RPC facts (authorities, holders) are skipped")

    state_dir = os.path.dirname(config.state_db_path)
    os.makedirs(state_dir, exist_ok=True)
    # --dry-run keeps its own dedup/state file, so a trial run never marks real posts as
    # "already sent"; the offline replay keeps nothing at all.
    db_path = ":memory:" if offline else config.state_db_path + (".dry-run" if args.dry_run else "")
    store = DedupStore(db_path)
    removed = store.prune(older_than_days=30)
    if removed:
        log.info("pruned %d dedup rows older than 30 days", removed)

    engine_ref = {}
    if offline:
        source = ReplaySource(args.replay_file)
    else:
        source = BlurStream(config.solami_api_key, subscriptions, base_url=config.solami_ws_url,
                            have_provider=lambda: engine_ref["engine"].meta.known_mints())
    engine = TapeEngine(settings, rpc=rpc, data_api=data_api, store=store, health_fn=source.health_summary)
    engine_ref["engine"] = engine

    stop_event = threading.Event()
    _install_signal_handlers(stop_event, log)
    bot = DryRunBot() if args.dry_run else BotAPI(config.bot_token, timeout=config.request_timeout_sec)
    if args.dry_run:
        log.info("--dry-run: posts will be printed here, not sent to Telegram")
    channel = config.channel_id or "@dry_run_channel"
    publisher = Publisher(bot, channel, max_per_min=0 if (args.dry_run or offline) else config.max_posts_per_min,
                          stop_event=stop_event)
    log.info("Solana Tape starting: mode %s, %s", settings.mode,
             ", ".join(s.name for s in subscriptions) if not offline else "replay " + args.replay_file)
    try:
        result = run_live(source, engine, publisher, stop_event, duration=args.duration,
                          heartbeat_path=None if offline else os.path.join(state_dir, "heartbeat.json"),
                          force_cards=args.announce, stop_when_exhausted=offline, final_reports=offline)
    finally:
        store.close()
    log.info("stopped: %s", json.dumps(result, sort_keys=True))
    return 0


def _install_signal_handlers(stop_event, log):
    def handler(signum, _frame):
        if not stop_event.is_set():
            log.info("signal %s received, shutting down cleanly", signum)
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):   # not in the main thread (e.g. under a test runner)
            pass


def check_live(config, settings, subscriptions, seconds, record_path, log):
    """Talks to Solami for real and prints what came back - the first thing to run once a
    key exists (it answers every "assumption to verify" in README with real frames)."""
    from alerts_bot.solami import BlurStream, SolamiRPC, SolamiRPCError
    from alerts_bot.tape.flags import mint_flags, top10_share_pct

    ok = True
    rpc = SolamiRPC(config.solami_api_key, url=config.solami_rpc_url, timeout=config.request_timeout_sec,
                    max_rps=config.solami_rpc_max_rps, retries=1)
    try:
        print("RPC  %s: OK, slot %s" % (config.solami_rpc_url, rpc.get_slot()))
    except SolamiRPCError as exc:
        ok = False
        print("RPC  %s: FAILED - %s" % (config.solami_rpc_url, exc))
    for mint in settings.watch_mints[:5]:
        try:
            info = rpc.get_mint_info(mint)
            top = rpc.get_largest_token_accounts(mint)
            if info is None:
                print("RPC  mint %s: not a mint account" % mint)
                continue
            share = top10_share_pct(top, info.supply)
            print("RPC  mint %s: program=%s decimals=%s supply=%s mint_authority=%s freeze_authority=%s "
                  "extensions=%s top_accounts=%d top10=%s" % (
                      mint, info.program, info.decimals, info.supply, info.mint_authority or "none",
                      info.freeze_authority or "none", ",".join(sorted(info.extensions)) or "-", len(top),
                      ("%.2f%%" % share) if share is not None else "n/a"))
            for flag in mint_flags(info):
                print("     flag: %s" % flag)
        except SolamiRPCError as exc:
            ok = False
            print("RPC  mint %s: FAILED - %s" % (mint, exc))

    record = open(record_path, "a", encoding="utf-8") if record_path else None
    record_lock = threading.Lock()

    def write_raw(_name, raw):            # called from the reader threads
        with record_lock:
            record.write(raw.rstrip("\n") + "\n")

    try:
        stream = BlurStream(config.solami_api_key, subscriptions, base_url=config.solami_ws_url,
                            on_raw=write_raw if record else None)
        print("BLUR %s: listening for %ds on %d subscription(s)..." % (config.solami_ws_url, seconds, len(subscriptions)))
        stream.start()
        examples, counts, deadline = {}, {}, time.monotonic() + seconds
        while time.monotonic() < deadline:
            event = stream.get(timeout=0.5)
            if event is not None:
                counts[event.type] = counts.get(event.type, 0) + 1
                examples.setdefault(event.type, event.raw)
        stream.stop()
    finally:
        if record:
            record.close()
    for name, health in stream.health_summary().items():
        print("BLUR [%s] connects=%d messages=%d parse_errors=%d%s" % (
            name, health["connects"], health["messages"], health["parse_errors"],
            (" last_error=" + health["last_error"]) if health["last_error"] else ""))
    total = sum(counts.values())
    print("BLUR events by type: %s" % (", ".join("%s=%d" % kv for kv in sorted(counts.items())) or "none"))
    for event_type, raw in sorted(examples.items()):
        text = json.dumps(raw, sort_keys=True)
        print("  example %s: %s" % (event_type, text if len(text) <= 900 else text[:900] + " ..."))
    if record_path:
        print("raw frames appended to %s" % record_path)
    if not total:
        ok = False
        print("BLUR: no events received - check the key's DataApi permission and streaming bandwidth "
              "(README \"Troubleshooting\")")
    print("RESULT: %s" % ("OK" if ok else "PROBLEMS FOUND"))
    return 0 if ok else 1


# === Legacy DexScreener tape =====================================================================

def run_dexscreener(args, config, log):
    from alerts_bot.dexscreener import Cache, DexScreenerClient, RateLimiter
    from alerts_bot.runner import run_cycle

    if not _telegram_ok(config, log) or not config.ready:
        return CONFIG_ERROR_EXIT_CODE
    if args.check:
        log.info("config OK: data source dexscreener, chains=%s interval=%dmin new_pair_window=%dmin "
                 "watchlist=%d entries", ",".join(config.chains), config.interval_min, config.new_pair_window_min,
                 len(config.watchlist))
        return 0

    os.makedirs(os.path.dirname(config.state_db_path), exist_ok=True)
    client = DexScreenerClient(
        timeout=config.request_timeout_sec,
        rate_limiter=RateLimiter(config.min_request_interval_sec),
        cache=Cache(config.cache_ttl_sec),
    )
    dedup_store = DedupStore(config.state_db_path)
    bot = DryRunBot() if args.dry_run else BotAPI(config.bot_token, timeout=config.request_timeout_sec)
    if args.dry_run:
        log.info("--dry-run: posts will be printed here, not sent to Telegram")

    try:
        while True:
            try:
                run_cycle(client, bot, dedup_store, config)
            except Exception:
                log.exception("cycle failed, will retry next interval")
            if args.once:
                return 0
            time.sleep(config.interval_min * 60)
    except KeyboardInterrupt:
        log.info("stopped by keyboard interrupt")
        return 0
    finally:
        dedup_store.close()


if __name__ == "__main__":
    sys.exit(main())
