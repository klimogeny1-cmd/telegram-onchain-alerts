"""One monitoring cycle: fetch candidate pairs from DexScreener, find new pairs and
volume anomalies, drop what was already posted, format the post(s), send them, and record
what was sent. main.py wires this into a CLI and a sleep loop; nothing in here sleeps or
loops by itself, which is what keeps it unit-testable (see tests/test_runner.py).
"""
import logging
from datetime import datetime, timezone

from . import formatter
from .analysis import (dedupe_by_address, filter_by_chains, filter_by_min_liquidity,
                       find_new_pairs, find_volume_anomalies, parse_pairs)
from .risk_flags import RiskThresholds

log = logging.getLogger("runner")

# DexScreener's public API has no "every new pair on chain X" firehose (see README "How
# pair discovery works") - discovery works by searching for a token symbol that is
# commonly paired against on each chain and filtering the results down to that chain.
# DISCOVERY_QUERIES in .env overrides this for every configured chain at once; these
# per-chain hints are only the fallback used when that setting is left empty.
DEFAULT_CHAIN_QUERY_HINTS = {
    "solana": "SOL",
    "ethereum": "WETH",
    "base": "WETH",
    "arbitrum": "WETH",
    "optimism": "WETH",
    "bsc": "BNB",
    "polygon": "WMATIC",
    "avalanche": "WAVAX",
}
FALLBACK_QUERY = "USDC"  # traded against something on almost every chain DexScreener covers


def discovery_queries_for(chains, configured_queries):
    """Search terms to probe this cycle. An explicit DISCOVERY_QUERIES value is used as-is
    for every chain; otherwise each configured chain gets its own hint so "solana" is not
    searched for using an Ethereum gas-token symbol, or vice versa."""
    if configured_queries:
        return list(dict.fromkeys(configured_queries))  # de-duplicate, keep order
    queries = []
    for chain in chains:
        hint = DEFAULT_CHAIN_QUERY_HINTS.get(chain.strip().lower(), FALLBACK_QUERY)
        if hint not in queries:
            queries.append(hint)
    return queries or [FALLBACK_QUERY]


def collect_candidate_pairs(client, chains, discovery_queries, watchlist):
    """Runs the discovery queries and watchlist lookups against DexScreener and returns
    normalized, chain-filtered, address-deduplicated Pair objects.

    A network error on one query is logged and skipped rather than aborting the whole
    cycle - one bad lookup (a timeout, a single chain's search briefly failing) should
    not silence every other configured chain until the next interval.
    """
    raw = []
    for query in discovery_queries:
        try:
            raw.extend(client.search_pairs(query))
        except Exception:
            log.exception("search_pairs(%r) failed, skipping this query", query)
    for chain_id, token_address in watchlist:
        try:
            raw.extend(client.get_token_pairs(chain_id, token_address))
        except Exception:
            log.exception("get_token_pairs(%s, %s) failed, skipping", chain_id, token_address)

    pairs = parse_pairs(raw)
    pairs = filter_by_chains(pairs, chains)
    return dedupe_by_address(pairs)


def run_cycle(client, bot, dedup_store, config, now=None):
    """Runs one full fetch -> analyze -> post cycle and returns a dict of counters (also
    logged) for visibility. `now` is injectable so tests get deterministic "is this pair
    new" behaviour; production code leaves it as None and gets the real current time."""
    now = now or datetime.now(timezone.utc)

    queries = discovery_queries_for(config.chains, config.discovery_queries)
    candidates = collect_candidate_pairs(client, config.chains, queries, config.watchlist)
    candidates = filter_by_min_liquidity(candidates, config.min_liquidity_usd)

    thresholds = RiskThresholds(
        low_liquidity_usd=config.risk_low_liquidity_usd,
        very_new_pair_min=config.risk_new_pair_min,
        high_vol_to_liquidity_ratio=config.risk_vol_liq_ratio,
    )

    new_pairs = find_new_pairs(candidates, config.new_pair_window_min, now=now)
    new_pairs = dedup_store.filter_unsent("new_pair", new_pairs)

    spikes = find_volume_anomalies(candidates, config.volume_spike_ratio, config.min_volume_usd_for_spike)
    spikes = dedup_store.filter_unsent("volume_spike", spikes)

    posts_sent = 0

    new_pairs_text = formatter.format_new_pairs_post(
        new_pairs, config.new_pair_window_min, generated_at=now, thresholds=thresholds,
        max_items=config.max_items_per_post)
    if new_pairs_text:
        bot.send_message(config.channel_id, new_pairs_text)
        posts_sent += 1
        shown, _overflow = formatter.cap_items(new_pairs, config.max_items_per_post)
        for p in shown:
            dedup_store.mark_sent("new_pair", p.chain_id, p.pair_address)

    spikes_text = formatter.format_volume_alert_post(
        spikes, generated_at=now, thresholds=thresholds, max_items=config.max_items_per_post)
    if spikes_text:
        bot.send_message(config.channel_id, spikes_text)
        posts_sent += 1
        shown, _overflow = formatter.cap_items(spikes, config.max_items_per_post)
        for p in shown:
            dedup_store.mark_sent("volume_spike", p.chain_id, p.pair_address)

    result = {
        "candidates": len(candidates),
        "new_pairs": len(new_pairs),
        "volume_spikes": len(spikes),
        "posts_sent": posts_sent,
    }
    log.info("cycle done: %d candidates, %d new pairs, %d volume spikes, %d post(s) sent",
             result["candidates"], result["new_pairs"], result["volume_spikes"], result["posts_sent"])
    return result
