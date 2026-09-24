#!/usr/bin/env python3
"""Entry point for the on-chain alerts bot.

Usage:
    python3 main.py --check       # validate .env and exit, no network calls at all
    python3 main.py --once        # run a single cycle and exit
    python3 main.py --dry-run     # print posts instead of sending them to Telegram
    python3 main.py               # run forever, one cycle every INTERVAL_MIN minutes

--dry-run and --once combine (a single cycle, printed instead of sent) - that is the
recommended way to try the bot for the first time; see README "Quick start".
"""
import argparse
import logging
import os
import sys
import time

from alerts_bot.config import Config
from alerts_bot.dedup import DedupStore
from alerts_bot.dexscreener import Cache, DexScreenerClient, RateLimiter
from alerts_bot.runner import run_cycle
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
        print("----- end of post -----\n")
        return {"message_id": -1}


def build_arg_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", dest="env_path", default=None, help="path to .env (default: ./.env)")
    parser.add_argument("--check", action="store_true", help="validate settings and exit (no network)")
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit")
    parser.add_argument("--dry-run", action="store_true", help="print posts instead of sending them to Telegram")
    return parser


def setup_logging(level_name, token):
    logging.basicConfig(level=getattr(logging, level_name, logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if token:
        token_filter = TokenFilter(token)
        for handler in logging.getLogger().handlers:
            handler.addFilter(token_filter)


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    config = Config.load(args.env_path)
    setup_logging(config.log_level, config.bot_token)
    log = logging.getLogger("main")

    for key, text in config.problems:
        log.warning("config: %s: %s", key, text)
    if not config.env_found:
        log.warning("no .env file found at %s - copy .env.example to .env and fill it in", config.env_path)
    if not config.token_ok:
        log.error("BOT_TOKEN is missing or does not look like a Telegram bot token")
    if not config.channel_ok:
        log.error("CHANNEL_ID is empty")
    if not config.ready:
        return CONFIG_ERROR_EXIT_CODE

    if args.check:
        log.info("config OK: chains=%s interval=%dmin new_pair_window=%dmin watchlist=%d entries",
                 ",".join(config.chains), config.interval_min, config.new_pair_window_min, len(config.watchlist))
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
