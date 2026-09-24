# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project intends to follow [Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-09-24

Initial public release.

### Added

- Market tape: posts new trading pairs and 1h volume anomalies to a Telegram channel,
  built from the public [DexScreener](https://dexscreener.com) API on a schedule
  (`INTERVAL_MIN`).
- Rule-based risk flags (liquidity unknown, low liquidity, very new pair, high 24h
  volume vs. liquidity) next to a pair - plain documented rules, never a score or a
  buy/sell call. Every post ends with a `Tape, not advice. Source: DexScreener.`
  footer.
- Public-data-only design: no wallet, no private RPC, no on-chain write access of any
  kind.
- `.env`-based configuration, with `python3 main.py --check` to validate it without a
  single network call.
- `--dry-run` and `--once` flags to try the bot safely (prints instead of posting)
  before running it for real.
- `WATCHLIST` support so a project's own token(s) are always checked directly via
  `/token-pairs/v1`, in addition to the built-in per-chain discovery queries.
- SQLite-backed deduplication (`alerts_bot/dedup.py`) so a restart or a network retry
  never reposts the same new-pair or volume-spike alert.
- Rate limiting and short-lived caching around the DexScreener client
  (`alerts_bot/dexscreener.py`) so a cycle stays well under any reasonable request
  budget.
- `BOT_TOKEN` read only from `.env` (never the process environment) and scrubbed from
  every log line by `alerts_bot/telegram.TokenFilter`.
- 83 unit tests (`python3 -m unittest discover`), all running against saved fixtures in
  `tests/fixtures/` with no network access.
- systemd unit (`deploy/telegram-onchain-alerts.service`) and an optional `Dockerfile`
  for deployment.
