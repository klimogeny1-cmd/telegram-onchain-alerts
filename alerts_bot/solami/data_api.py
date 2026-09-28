"""Solami Data API (Blur REST, https://api.solami.dev/data/*) - optional enrichment.

Used for one thing only: a token's name/symbol when the Blur stream has not delivered a
`metadata` event for it yet (the stream sends metadata out of band, usually right after
the first event for a mint, but a post can be due before it arrives).

Auth: API key with the DataApi permission in the `x-api-key` header (docs:
solami.dev/docs/api, "x-api-key or Authorization: Bearer").

ASSUMPTION D1 (verify with a real key): GET /data/token/metadata?address=MINT returns a
JSON object carrying `symbol`/`name` at the top level or under `data`/`metadata`. The
docs mark historical/REST data as beta; any other shape simply returns None and the
post falls back to a shortened mint address. Nothing depends on this call succeeding.
"""
import json
import logging
import time
import urllib.parse
import urllib.request

from .blur import redact
from .rpc import RateLimiter, USER_AGENT

log = logging.getLogger("solami.data_api")

DEFAULT_API_URL = "https://api.solami.dev"


class SolamiDataAPI:
    def __init__(self, api_key, base_url=DEFAULT_API_URL, timeout=8.0, max_rps=2.0, opener=None,
                 sleep=time.sleep):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._urlopen = opener or urllib.request.urlopen
        self._limiter = RateLimiter(max_rps, sleep=sleep)
        self.calls = 0
        self.failures = 0

    def _get(self, path, params):
        self._limiter.wait()
        self.calls += 1
        url = "%s%s?%s" % (self.base_url, path, urllib.parse.urlencode(params))
        request = urllib.request.Request(url, headers={"x-api-key": self.api_key, "Accept": "application/json",
                                                       "User-Agent": USER_AGENT})
        with self._urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def token_metadata(self, mint):
        """{"symbol": ..., "name": ..., "decimals": ...} or None. Never raises."""
        try:
            data = self._get("/data/token/metadata", {"address": mint})
        except Exception as exc:   # optional enrichment: any failure just means "no symbol"
            self.failures += 1
            log.info("data api: metadata for %s unavailable: %s", mint, redact(exc, self.api_key))
            return None
        return parse_metadata_response(data)


def parse_metadata_response(data):
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        return None
    for key in ("data", "metadata", "token"):
        if isinstance(data.get(key), dict):
            data = data[key]
            break
    symbol = data.get("symbol") if isinstance(data.get("symbol"), str) else ""
    name = data.get("name") if isinstance(data.get("name"), str) else ""
    if not symbol and not name:
        return None
    decimals = data.get("decimals")
    return {"symbol": symbol.strip(), "name": name.strip(),
            "decimals": decimals if isinstance(decimals, int) and not isinstance(decimals, bool) else None}
