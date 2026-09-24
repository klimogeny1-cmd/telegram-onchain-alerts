"""Minimal client for the public DexScreener API (https://docs.dexscreener.com).

Standard-library only (urllib) - see README "Dependencies" for why. No API key: as of
the check on 2026-09-24, calling these endpoints directly (curl, no headers beyond a
User-Agent) returned normal 200 responses, so none of them require authentication.

Endpoints used by this bot, and the two different response shapes they return - both
confirmed by calling the live API directly, not assumed from memory:

  GET /latest/dex/search?q=<text>
      -> {"schemaVersion": "1.0.0", "pairs": [ {...}, ... ]}   (object, "pairs" key)

  GET /token-pairs/v1/{chainId}/{tokenAddress}
      -> [ {...}, ... ]                                        (bare JSON array)

Rate limits: the docs (docs.dexscreener.com/api/reference, checked 2026-09-24) state
"60 requests per minute" for a neighbouring family of endpoints (/token-profiles/*,
/ads/latest/v1, /metas/*). No explicit number is published for /latest/dex/search or
/token-pairs/v1 specifically, and five back-to-back requests during the check returned
plain 200s with no rate-limit headers. In the absence of a published number for the
endpoints this bot actually calls, RateLimiter below stays well under that documented
neighbour figure (a default of 1.2s between calls is ~50/min) instead of assuming no
limit applies. See README "Data source & rate limits" for the full write-up.
"""
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("dexscreener")

API_BASE = "https://api.dexscreener.com"
USER_AGENT = "telegram-onchain-alerts/1.0 (open-source template; https://t.me/gramworks_hub)"


class DexScreenerError(Exception):
    """Raised for network failures or responses that can't be parsed as JSON."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class RateLimiter:
    """Sleeps as needed so calls through it are spaced at least `min_interval` seconds
    apart. See the module docstring for why this exists even though DexScreener does not
    publish a number for the specific endpoints used here."""

    def __init__(self, min_interval=1.2, clock=time.monotonic, sleep=time.sleep):
        self.min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._last_call = None

    def wait(self):
        if self._last_call is not None:
            remaining = self.min_interval - (self._clock() - self._last_call)
            if remaining > 0:
                self._sleep(remaining)
        self._last_call = self._clock()


class Cache:
    """Tiny in-memory TTL cache keyed by the full request URL.

    One monitoring cycle can ask for the same query more than once (e.g. two configured
    chains sharing a discovery query, or a watchlist token that also turns up in search
    results); this keeps that from becoming two network calls instead of one.
    """

    def __init__(self, ttl=60.0, clock=time.monotonic):
        self.ttl = ttl
        self._clock = clock
        self._store = {}  # url -> (expires_at, value)

    def get(self, key):
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if self._clock() >= expires_at:
            del self._store[key]
            return None
        return value

    def set(self, key, value):
        if self.ttl > 0:
            self._store[key] = (self._clock() + self.ttl, value)

    def clear(self):
        self._store.clear()


class DexScreenerClient:
    def __init__(self, base_url=API_BASE, timeout=15, min_request_interval=1.2, cache_ttl=60.0,
                 rate_limiter=None, cache=None, opener=None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._rate_limiter = rate_limiter if rate_limiter is not None else RateLimiter(min_request_interval)
        self._cache = cache if cache is not None else Cache(cache_ttl)
        self._urlopen = opener or urllib.request.urlopen

    def _get(self, path, params=None, use_cache=True):
        query = ("?" + urllib.parse.urlencode(params)) if params else ""
        url = self.base_url + path + query

        if use_cache:
            cached = self._cache.get(url)
            if cached is not None:
                return cached

        self._rate_limiter.wait()
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with self._urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise DexScreenerError("HTTP %s from %s" % (exc.code, path), status=exc.code) from exc
        except urllib.error.URLError as exc:
            raise DexScreenerError("network error calling %s: %s" % (path, exc.reason)) from exc
        except (TimeoutError, OSError) as exc:
            raise DexScreenerError("network error calling %s: %s" % (path, exc)) from exc

        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise DexScreenerError("could not parse JSON from %s: %s" % (path, exc)) from exc

        if use_cache:
            self._cache.set(url, data)
        return data

    def search_pairs(self, query):
        """GET /latest/dex/search?q=<query> -> list of raw pair dicts (possibly empty)."""
        data = self._get("/latest/dex/search", {"q": query})
        if isinstance(data, dict):
            return data.get("pairs") or []
        log.warning("search_pairs(%r): unexpected response shape %s", query, type(data).__name__)
        return []

    def get_token_pairs(self, chain_id, token_address):
        """GET /token-pairs/v1/{chainId}/{tokenAddress} -> list of raw pair dicts.

        Returns a bare JSON array (no {"pairs": [...]} envelope) - see module docstring.
        """
        path = "/token-pairs/v1/%s/%s" % (urllib.parse.quote(chain_id), urllib.parse.quote(token_address))
        data = self._get(path)
        if isinstance(data, list):
            return data
        log.warning("get_token_pairs(%s, %s): unexpected response shape %s",
                   chain_id, token_address, type(data).__name__)
        return []
