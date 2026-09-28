"""Settings for the alerts bot.

Values come only from a .env file (next to main.py, or the path passed with --env).
Process environment variables are ignored on purpose: a stray BOT_TOKEN (or
SOLAMI_API_KEY) left in a shell from another project must never be picked up by this one.

Two data sources:
  DATA_SOURCE=solami       (default) the live Solana tape on Solami - Blur stream + RPC.
                           Needs SOLAMI_API_KEY. Settings: the "Solami" block below.
  DATA_SOURCE=dexscreener  the original keyless DexScreener polling tape (CHAINS,
                           INTERVAL_MIN, ...), kept for existing deployments.
"""
import os
import re

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(BASE_DIR)

# --- Solami live tape ------------------------------------------------------------------------
DATA_SOURCES = ("solami", "dexscreener")
DEFAULT_DATA_SOURCE = "solami"
TAPE_MODES = ("auto", "watchlist", "firehose")
DEFAULT_SOLAMI_RPC_URL = "https://rpc.solami.dev/sol"
DEFAULT_SOLAMI_WS_URL = "wss://ws.solami.dev/data/subscribe"
DEFAULT_SOLAMI_API_URL = "https://api.solami.dev"
DEFAULT_SOLAMI_RPC_MAX_RPS = 4.0   # Solami's Free plan allows 5 requests a second
# Thresholds whose sensible value depends on the mode: a project's own token (watchlist)
# vs. the whole Solana market (firehose). An explicit .env value always wins.
MODE_DEFAULTS = {
    "watchlist": {"large_trade_usd": 5000.0, "liq_alert_usd": 2500.0, "liq_report_adds": True},
    "firehose": {"large_trade_usd": 50000.0, "liq_alert_usd": 50000.0, "liq_report_adds": False},
}
DEFAULT_LIQ_ALERT_PCT = 10.0
DEFAULT_SURGE_MIN_MULTIPLE = 5.0
DEFAULT_SURGE_MIN_MCAP_USD = 250000.0
DEFAULT_HOLDER_CHANGE_PCT = 0.5
DEFAULT_TOP10_FLAG_PCT = 50.0
DEFAULT_PRICE_IMPACT_FLAG_PCT = 10.0
DEFAULT_FLUSH_SEC = 60
DEFAULT_MAX_POSTS_PER_MIN = 6
DEFAULT_HOLDERS_POLL_MIN = 5
DEFAULT_AUTHORITY_RECHECK_MIN = 30
DEFAULT_SUMMARY_EVERY_MIN = 60
DEFAULT_DIGEST_EVERY_MIN = 15

MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")   # base58, Solana address length

DEFAULT_CHAINS = "solana"
DEFAULT_INTERVAL_MIN = 60
DEFAULT_MIN_LIQUIDITY_USD = 5000.0
DEFAULT_VOLUME_SPIKE_RATIO = 3.0
DEFAULT_MIN_VOLUME_USD_FOR_SPIKE = 1000.0
DEFAULT_RISK_LOW_LIQUIDITY_USD = 25000.0
DEFAULT_RISK_NEW_PAIR_MIN = 30.0
DEFAULT_RISK_VOL_LIQ_RATIO = 5.0
DEFAULT_MAX_ITEMS_PER_POST = 10
DEFAULT_REQUEST_TIMEOUT_SEC = 15
DEFAULT_MIN_REQUEST_INTERVAL_SEC = 1.2
DEFAULT_CACHE_TTL_SEC = 60.0
DEFAULT_LOG_LEVEL = "INFO"

TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")


def read_env_file(path):
    """KEY=VALUE lines -> dict. Returns None when the file does not exist.

    Supports '#' comments, an optional leading 'export ', and single/double-quoted
    values. This is the entire .env "parser" - no third-party library is needed for it,
    which is the point (see README "Dependencies").
    """
    if not path or not os.path.isfile(path):
        return None
    values = {}
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            key, _, value = line.partition("=")
            value = value.strip()
            if value[:1] in ("'", '"'):
                # Quoted value: take everything up to the matching quote and ignore what
                # follows (usually a trailing comment) - checked before comment-stripping
                # below, otherwise a quoted value with a trailing "# ..." comment would
                # keep its quotes (the closing quote would no longer be the last
                # character once the comment is appended after it).
                quote = value[0]
                end = value.find(quote, 1)
                value = value[1:end] if end != -1 else value[1:]
            else:
                value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
            values[key.strip()] = value
    return values


def _split_list(raw):
    return [item.strip() for item in re.split(r"[,\n]+", raw or "") if item.strip()]


def _number(values, key, default, problems, low=None, high=None, cast=float):
    raw = (values.get(key) or "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError:
        problems.append((key, "must be a number, default %r is used" % (default,)))
        return default
    if (low is not None and value < low) or (high is not None and value > high):
        problems.append((key, "out of range, default %r is used" % (default,)))
        return default
    return value


def _bool(values, key, default, problems):
    raw = (values.get(key) or "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    problems.append((key, "must be true or false, default %r is used" % (default,)))
    return default


def parse_mints(raw):
    """'MINT1, MINT2' -> ([valid base58 addresses, de-duplicated], [invalid entries])."""
    good, bad = [], []
    for part in _split_list(raw):
        if MINT_RE.match(part):
            if part not in good:
                good.append(part)
        else:
            bad.append(part)
    return good, bad


def parse_watchlist(raw):
    """'solana:EPjF...,base:0xabc...' -> ([(chain, address), ...], [bad entries]).

    Optional: a project can list its own token(s) here so they are always checked via
    /token-pairs/v1, in addition to whatever the discovery queries turn up.
    """
    entries, bad = [], []
    for part in _split_list(raw):
        chain, sep, address = part.partition(":")
        chain, address = chain.strip().lower(), address.strip()
        if not sep or not chain or not address:
            bad.append(part)
            continue
        entries.append((chain, address))
    return entries, bad


class Config:
    def __init__(self, bot_token="", channel_id="", chains=None, interval_min=DEFAULT_INTERVAL_MIN,
                 min_liquidity_usd=DEFAULT_MIN_LIQUIDITY_USD, new_pair_window_min=None,
                 volume_spike_ratio=DEFAULT_VOLUME_SPIKE_RATIO,
                 min_volume_usd_for_spike=DEFAULT_MIN_VOLUME_USD_FOR_SPIKE,
                 risk_low_liquidity_usd=DEFAULT_RISK_LOW_LIQUIDITY_USD,
                 risk_new_pair_min=DEFAULT_RISK_NEW_PAIR_MIN,
                 risk_vol_liq_ratio=DEFAULT_RISK_VOL_LIQ_RATIO,
                 discovery_queries=None, watchlist=None,
                 max_items_per_post=DEFAULT_MAX_ITEMS_PER_POST,
                 request_timeout_sec=DEFAULT_REQUEST_TIMEOUT_SEC,
                 min_request_interval_sec=DEFAULT_MIN_REQUEST_INTERVAL_SEC,
                 cache_ttl_sec=DEFAULT_CACHE_TTL_SEC, state_db_path=None,
                 log_level=DEFAULT_LOG_LEVEL, env_path=None, env_found=True, problems=None,
                 data_source=DEFAULT_DATA_SOURCE, solami_api_key="", solami_rpc_url=DEFAULT_SOLAMI_RPC_URL,
                 solami_ws_url=DEFAULT_SOLAMI_WS_URL, solami_api_url=DEFAULT_SOLAMI_API_URL,
                 solami_data_api=True, solami_rpc_max_rps=DEFAULT_SOLAMI_RPC_MAX_RPS, tape_mode="auto",
                 watch_mints=None, large_trade_usd=None, liq_alert_usd=None, liq_alert_pct=DEFAULT_LIQ_ALERT_PCT,
                 liq_report_adds=None, surge_min_multiple=DEFAULT_SURGE_MIN_MULTIPLE,
                 surge_min_mcap_usd=DEFAULT_SURGE_MIN_MCAP_USD, holder_change_pct=DEFAULT_HOLDER_CHANGE_PCT,
                 top10_flag_pct=DEFAULT_TOP10_FLAG_PCT, price_impact_flag_pct=DEFAULT_PRICE_IMPACT_FLAG_PCT,
                 flush_sec=DEFAULT_FLUSH_SEC, max_posts_per_min=DEFAULT_MAX_POSTS_PER_MIN,
                 holders_poll_min=DEFAULT_HOLDERS_POLL_MIN, authority_recheck_min=DEFAULT_AUTHORITY_RECHECK_MIN,
                 summary_every_min=DEFAULT_SUMMARY_EVERY_MIN, digest_every_min=DEFAULT_DIGEST_EVERY_MIN):
        # --- Solami live tape (DATA_SOURCE=solami) ---
        self.data_source = data_source
        self.solami_api_key = (solami_api_key or "").strip()
        self.solami_rpc_url = solami_rpc_url
        self.solami_ws_url = solami_ws_url
        self.solami_api_url = solami_api_url
        self.solami_data_api = solami_data_api
        self.solami_rpc_max_rps = solami_rpc_max_rps
        self.watch_mints = list(watch_mints or [])
        # "auto": a watchlist when WATCH_MINTS is set, otherwise the whole-market firehose
        self.tape_mode = tape_mode if tape_mode in ("watchlist", "firehose") else (
            "watchlist" if self.watch_mints else "firehose")
        mode_defaults = MODE_DEFAULTS[self.tape_mode]
        self.large_trade_usd = large_trade_usd if large_trade_usd is not None else mode_defaults["large_trade_usd"]
        self.liq_alert_usd = liq_alert_usd if liq_alert_usd is not None else mode_defaults["liq_alert_usd"]
        self.liq_report_adds = liq_report_adds if liq_report_adds is not None else mode_defaults["liq_report_adds"]
        self.liq_alert_pct = liq_alert_pct
        self.surge_min_multiple = surge_min_multiple
        self.surge_min_mcap_usd = surge_min_mcap_usd
        self.holder_change_pct = holder_change_pct
        self.top10_flag_pct = top10_flag_pct
        self.price_impact_flag_pct = price_impact_flag_pct
        self.flush_sec = flush_sec
        self.max_posts_per_min = max_posts_per_min
        self.holders_poll_min = holders_poll_min
        self.authority_recheck_min = authority_recheck_min
        self.summary_every_min = summary_every_min
        self.digest_every_min = digest_every_min
        # --- shared / DexScreener tape ---
        self.bot_token = (bot_token or "").strip()
        self.channel_id = (channel_id or "").strip()
        self.chains = chains or _split_list(DEFAULT_CHAINS)
        self.interval_min = interval_min
        self.min_liquidity_usd = min_liquidity_usd
        # NEW_PAIR_WINDOW_MIN defaults to INTERVAL_MIN: with no explicit value, "new" means
        # "appeared since the last cycle", which is what a periodic tape should mean.
        self.new_pair_window_min = new_pair_window_min or interval_min
        self.volume_spike_ratio = volume_spike_ratio
        self.min_volume_usd_for_spike = min_volume_usd_for_spike
        self.risk_low_liquidity_usd = risk_low_liquidity_usd
        self.risk_new_pair_min = risk_new_pair_min
        self.risk_vol_liq_ratio = risk_vol_liq_ratio
        self.discovery_queries = discovery_queries or []
        self.watchlist = watchlist or []
        self.max_items_per_post = max_items_per_post
        self.request_timeout_sec = request_timeout_sec
        self.min_request_interval_sec = min_request_interval_sec
        self.cache_ttl_sec = cache_ttl_sec
        self.state_db_path = state_db_path or os.path.join(REPO_DIR, "state", "seen.db")
        self.log_level = log_level
        self.env_path = env_path
        self.env_found = env_found
        self.problems = list(problems or [])  # [(key, text)] non-fatal problems found while loading

    @property
    def token_ok(self):
        return bool(TOKEN_RE.match(self.bot_token))

    @property
    def channel_ok(self):
        return bool(self.channel_id)

    @property
    def ready(self):
        """True when the bot can post to Telegram (see main.py --check). The Solami tape
        additionally needs live_problems() to be empty."""
        return self.token_ok and self.channel_ok

    def live_problems(self):
        """Blocking problems for DATA_SOURCE=solami (empty list = good to go)."""
        problems = []
        if self.data_source != "solami":
            return problems
        if not self.solami_api_key:
            problems.append("SOLAMI_API_KEY is empty - get a key (README \"Quick start\", step 1), or set "
                            "DATA_SOURCE=dexscreener for the legacy keyless tape")
        if self.tape_mode == "watchlist" and not self.watch_mints:
            problems.append("TAPE_MODE=watchlist needs at least one token mint in WATCH_MINTS")
        return problems

    @property
    def secrets(self):
        """Values that must never appear in a log line (see telegram.TokenFilter)."""
        return [s for s in (self.bot_token, self.solami_api_key) if s]

    @classmethod
    def load(cls, env_path=None):
        env_path = env_path or os.path.join(REPO_DIR, ".env")
        values = read_env_file(env_path)
        found = values is not None
        values = values or {}
        problems = []

        chains = _split_list(values.get("CHAINS") or DEFAULT_CHAINS)
        if not chains:
            problems.append(("CHAINS", "empty, default %r is used" % DEFAULT_CHAINS))
            chains = _split_list(DEFAULT_CHAINS)

        interval_min = _number(values, "INTERVAL_MIN", DEFAULT_INTERVAL_MIN, problems, low=1, high=1440, cast=int)
        new_pair_window_min = None
        if (values.get("NEW_PAIR_WINDOW_MIN") or "").strip():
            new_pair_window_min = _number(values, "NEW_PAIR_WINDOW_MIN", interval_min, problems,
                                          low=1, high=10080, cast=int)

        watchlist, bad_watchlist = parse_watchlist(values.get("WATCHLIST"))
        if bad_watchlist:
            problems.append(("WATCHLIST", "skipped entries not in chain:address form: %s" %
                             ", ".join(bad_watchlist)))

        # --- Solami live tape ---
        data_source = (values.get("DATA_SOURCE") or DEFAULT_DATA_SOURCE).strip().lower()
        if data_source not in DATA_SOURCES:
            problems.append(("DATA_SOURCE", "must be one of %s, default %r is used" %
                             ("/".join(DATA_SOURCES), DEFAULT_DATA_SOURCE)))
            data_source = DEFAULT_DATA_SOURCE
        tape_mode = (values.get("TAPE_MODE") or "auto").strip().lower()
        if tape_mode not in TAPE_MODES:
            problems.append(("TAPE_MODE", "must be one of %s, 'auto' is used" % "/".join(TAPE_MODES)))
            tape_mode = "auto"
        watch_mints, bad_mints = parse_mints(values.get("WATCH_MINTS"))
        if bad_mints:
            problems.append(("WATCH_MINTS", "skipped entries that are not Solana addresses: %s" % ", ".join(bad_mints)))
        if not watch_mints:
            # backwards compatibility: solana entries of the old DexScreener WATCHLIST
            watch_mints = [addr for chain, addr in watchlist if chain == "solana" and MINT_RE.match(addr)]
        api_key = (values.get("SOLAMI_API_KEY") or "").strip()
        if api_key and not api_key.startswith("sk_"):
            problems.append(("SOLAMI_API_KEY", "does not start with 'sk_' - Blur streams need a standard Solami "
                                               "API key (ApiKey) whose role has the DataApi permission"))
        ws_url = (values.get("SOLAMI_WS_URL") or DEFAULT_SOLAMI_WS_URL).strip()
        if not ws_url.startswith(("wss://", "ws://")):
            problems.append(("SOLAMI_WS_URL", "must start with wss://, default is used"))
            ws_url = DEFAULT_SOLAMI_WS_URL
        rpc_url = (values.get("SOLAMI_RPC_URL") or DEFAULT_SOLAMI_RPC_URL).strip()
        if not rpc_url.startswith(("https://", "http://")):
            problems.append(("SOLAMI_RPC_URL", "must start with https://, default is used"))
            rpc_url = DEFAULT_SOLAMI_RPC_URL
        api_url = (values.get("SOLAMI_API_URL") or DEFAULT_SOLAMI_API_URL).strip()
        if not api_url.startswith(("https://", "http://")):
            problems.append(("SOLAMI_API_URL", "must start with https://, default is used"))
            api_url = DEFAULT_SOLAMI_API_URL

        def optional_number(key, low=0):
            if not (values.get(key) or "").strip():
                return None
            return _number(values, key, None, problems, low=low)

        return cls(
            data_source=data_source,
            solami_api_key=api_key,
            solami_rpc_url=rpc_url,
            solami_ws_url=ws_url,
            solami_api_url=api_url,
            solami_data_api=_bool(values, "SOLAMI_DATA_API", True, problems),
            solami_rpc_max_rps=_number(values, "SOLAMI_RPC_MAX_RPS", DEFAULT_SOLAMI_RPC_MAX_RPS, problems,
                                       low=0.1, high=1000),
            tape_mode=tape_mode,
            watch_mints=watch_mints,
            large_trade_usd=optional_number("LARGE_TRADE_USD"),
            liq_alert_usd=optional_number("LIQ_ALERT_USD"),
            liq_alert_pct=_number(values, "LIQ_ALERT_PCT", DEFAULT_LIQ_ALERT_PCT, problems, low=0, high=100),
            liq_report_adds=(_bool(values, "LIQ_REPORT_ADDS", None, problems)
                             if (values.get("LIQ_REPORT_ADDS") or "").strip() else None),
            surge_min_multiple=_number(values, "SURGE_MIN_MULTIPLE", DEFAULT_SURGE_MIN_MULTIPLE, problems, low=0),
            surge_min_mcap_usd=_number(values, "SURGE_MIN_MCAP_USD", DEFAULT_SURGE_MIN_MCAP_USD, problems, low=0),
            holder_change_pct=_number(values, "HOLDER_CHANGE_PCT", DEFAULT_HOLDER_CHANGE_PCT, problems,
                                      low=0.001, high=100),
            top10_flag_pct=_number(values, "TOP10_FLAG_PCT", DEFAULT_TOP10_FLAG_PCT, problems, low=0, high=100),
            price_impact_flag_pct=_number(values, "PRICE_IMPACT_FLAG_PCT", DEFAULT_PRICE_IMPACT_FLAG_PCT, problems,
                                          low=0, high=100),
            flush_sec=_number(values, "FLUSH_SEC", DEFAULT_FLUSH_SEC, problems, low=5, high=3600, cast=int),
            max_posts_per_min=_number(values, "MAX_POSTS_PER_MIN", DEFAULT_MAX_POSTS_PER_MIN, problems,
                                      low=1, high=20, cast=int),
            holders_poll_min=_number(values, "HOLDERS_POLL_MIN", DEFAULT_HOLDERS_POLL_MIN, problems,
                                     low=1, high=1440, cast=int),
            authority_recheck_min=_number(values, "AUTHORITY_RECHECK_MIN", DEFAULT_AUTHORITY_RECHECK_MIN, problems,
                                          low=1, high=1440, cast=int),
            summary_every_min=_number(values, "SUMMARY_EVERY_MIN", DEFAULT_SUMMARY_EVERY_MIN, problems,
                                      low=0, high=1440, cast=int),
            digest_every_min=_number(values, "DIGEST_EVERY_MIN", DEFAULT_DIGEST_EVERY_MIN, problems,
                                     low=0, high=1440, cast=int),
            # --- shared / DexScreener tape ---
            bot_token=values.get("BOT_TOKEN", ""),
            channel_id=values.get("CHANNEL_ID", ""),
            chains=chains,
            interval_min=interval_min,
            min_liquidity_usd=_number(values, "MIN_LIQUIDITY_USD", DEFAULT_MIN_LIQUIDITY_USD, problems, low=0),
            new_pair_window_min=new_pair_window_min,
            volume_spike_ratio=_number(values, "VOLUME_SPIKE_RATIO", DEFAULT_VOLUME_SPIKE_RATIO, problems, low=1),
            min_volume_usd_for_spike=_number(values, "MIN_VOLUME_USD_FOR_SPIKE",
                                             DEFAULT_MIN_VOLUME_USD_FOR_SPIKE, problems, low=0),
            risk_low_liquidity_usd=_number(values, "RISK_LOW_LIQUIDITY_USD",
                                           DEFAULT_RISK_LOW_LIQUIDITY_USD, problems, low=0),
            risk_new_pair_min=_number(values, "RISK_NEW_PAIR_MIN", DEFAULT_RISK_NEW_PAIR_MIN, problems, low=0),
            risk_vol_liq_ratio=_number(values, "RISK_VOL_LIQ_RATIO", DEFAULT_RISK_VOL_LIQ_RATIO, problems, low=0),
            discovery_queries=_split_list(values.get("DISCOVERY_QUERIES")),
            watchlist=watchlist,
            max_items_per_post=_number(values, "MAX_ITEMS_PER_POST", DEFAULT_MAX_ITEMS_PER_POST, problems,
                                       low=1, high=50, cast=int),
            request_timeout_sec=_number(values, "REQUEST_TIMEOUT_SEC", DEFAULT_REQUEST_TIMEOUT_SEC, problems,
                                        low=1, high=120, cast=int),
            min_request_interval_sec=_number(values, "MIN_REQUEST_INTERVAL_SEC",
                                             DEFAULT_MIN_REQUEST_INTERVAL_SEC, problems, low=0),
            cache_ttl_sec=_number(values, "CACHE_TTL_SEC", DEFAULT_CACHE_TTL_SEC, problems, low=0),
            state_db_path=values.get("STATE_DB_PATH") or None,
            log_level=(values.get("LOG_LEVEL") or DEFAULT_LOG_LEVEL).strip().upper(),
            env_path=env_path, env_found=found, problems=problems,
        )
