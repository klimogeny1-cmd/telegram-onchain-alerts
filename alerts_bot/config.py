"""Settings for the alerts bot.

Values come only from a .env file (next to main.py, or the path passed with --env).
Process environment variables are ignored on purpose: a stray BOT_TOKEN left in a shell
from another project must never be picked up by this one.
"""
import os
import re

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(BASE_DIR)

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
                 log_level=DEFAULT_LOG_LEVEL, env_path=None, env_found=True, problems=None):
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
        """True when the bot has enough to actually run (see main.py --check)."""
        return self.token_ok and self.channel_ok

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

        return cls(
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
