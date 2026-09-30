# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project intends to follow [Semantic Versioning](https://semver.org/).

## [2.0.0] - unreleased

Solana Tape: the bot now posts a live Solana mainnet tape built on [Solami](https://solami.dev).

### Added

- Solami as the data path: the Blur decoded-event stream (WebSocket) for swaps, liquidity
  adds and removes, new pools, launches, graduations, surge/radar breakouts and transfers,
  filtered on Solami's side; Solami RPC for mint and freeze authority, Token-2022
  extensions, the top-20 holders and balances.
- Two modes: `watchlist` for a project's own token(s) with an hourly summary, and
  `firehose` for the whole market with a launch digest.
- Rule-based flags for top-holder concentration and price impact - documented rules,
  never a buy/sell call.
- `--check-live` to verify a Solami key against live mainnet, `--record` to save raw
  frames, `--replay-file` for an offline demo on synthetic frames.
- A standard-library WebSocket client with reconnects, backoff and liveness checks; a
  heartbeat file and a Docker health check.

- First live run (2026-09-30): real Blur frames of every type in
  `tests/fixtures/solami/live_frames_2026-09-30.jsonl`; liquidity is valued from the
  stream's own `base_usd` / `quote_usd`.

### Security

- No key in a post: links lose `api_key` / `key` / `token` parameters and the bot token
  and the Solami key are replaced (`telegram.without_secrets`) in every post, in
  `--dry-run` output and in `--check-live` examples and `--record` files. Solami puts the
  account key into metadata `image_url`.

### Changed

- `DATA_SOURCE` defaults to `solami`. The original keyless DexScreener tape still works
  unchanged with `DATA_SOURCE=dexscreener`.

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
