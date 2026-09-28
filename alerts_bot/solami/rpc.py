"""Solami RPC: point reads of Solana state over standard JSON-RPC (HTTPS, stdlib urllib).

"Read state with RPC, react to change with a stream" - that is Solami's own rule of
thumb (solami.dev/docs/which-product), and it is how this bot splits the work: Blur
streams what happens, RPC answers "what is true right now" about a token.

Endpoint and auth (solami.dev/docs/endpoints, read 2026-09-26):
    https://rpc.solami.dev/sol?api_key=KEY     (region-pinned: https://fra.rpc.solami.dev/sol)

Methods used:
  getAccountInfo (jsonParsed)     -> mint: supply, decimals, mint authority, freeze
                                     authority, Token-2022 extensions
  getTokenLargestAccountsV2       -> top-20 holder token accounts of a mint. Solami
                                     retired the stock getTokenLargestAccounts ("use the
                                     V2 form"); we fall back to the stock name only if a
                                     non-Solami RPC answers "method not found".
  getMultipleAccounts (jsonParsed)-> current balance + owner of up to 100 token accounts
  getSlot                          -> connectivity/key check (main.py --check-live)

Error handling follows solami.dev/docs/errors "What to retry": RPC failures mostly come
back as HTTP 200 with an `error` object, so every response body is checked.
  -32005 (rate limited) / HTTP 429          -> retry, exponential backoff + jitter
  HTTP 500/503/408, -32603, network errors  -> retry with backoff
  401/403, -32602, -32600, -32010, ...      -> no retry (nothing changes until the request does)

ASSUMPTION R1 (verify with a real key): getTokenLargestAccountsV2 takes the stock
params [mint, {commitment}] and returns either the stock shape (result.value = [...]) or
the V2 token-method shape (result.value.accounts = [...]). Both are accepted.
"""
import http.client
import json
import logging
import random
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, Optional

from .blur import redact

log = logging.getLogger("solami.rpc")

DEFAULT_RPC_URL = "https://rpc.solami.dev/sol"
USER_AGENT = "telegram-onchain-alerts/2.0 (solana-tape; +https://t.me/gramworks_hub)"

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCC7rPKxkB2"

RETRYABLE_RPC_CODES = {-32005, -32603}
RETRYABLE_HTTP = {408, 429, 500, 502, 503, 504}


class SolamiRPCError(Exception):
    def __init__(self, message, code=None, http_status=None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


@dataclass
class MintInfo:
    """Facts about a mint account, read from chain via getAccountInfo(jsonParsed)."""
    mint: str
    program: str                      # "spl-token" | "spl-token-2022" | other/unknown
    decimals: Optional[int]
    supply: Optional[int]             # raw units
    mint_authority: Optional[str]     # None = no mint authority (supply is fixed)
    freeze_authority: Optional[str]   # None = no freeze authority
    extensions: Dict[str, dict] = field(default_factory=dict)   # Token-2022 only
    slot: Optional[int] = None

    @property
    def ui_supply(self):
        if self.supply is None or self.decimals is None:
            return None
        return self.supply / (10 ** self.decimals)


@dataclass
class TokenAccountBalance:
    address: str                      # the token account (what top-holder lists return)
    amount: int                       # raw units
    decimals: Optional[int] = None
    owner: str = ""                   # wallet/program that owns the token account, if known
    mint: str = ""
    closed: bool = False              # the account no longer exists (balance is then 0)

    @property
    def ui_amount(self):
        if self.decimals is None:
            return None
        return self.amount / (10 ** self.decimals)


class RateLimiter:
    """Keeps calls at least 1/max_rps seconds apart (thread-safe). Solami's free tier is
    10 RPS and Pro 200 RPS; the default of 8 stays inside even the free tier."""

    def __init__(self, max_rps=8.0, clock=time.monotonic, sleep=time.sleep):
        self.interval = 1.0 / max_rps if max_rps and max_rps > 0 else 0.0
        self._clock, self._sleep = clock, sleep
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            now = self._clock()
            delay = self._next - now
            self._next = max(now, self._next) + self.interval
        if delay > 0:
            self._sleep(delay)


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class SolamiRPC:
    def __init__(self, api_key, url=DEFAULT_RPC_URL, timeout=10.0, max_rps=8.0, retries=3,
                 commitment="confirmed", opener=None, sleep=time.sleep, rng=random.random,
                 clock=time.monotonic):
        self.api_key = api_key
        self.base_url = url
        self.timeout = timeout
        self.retries = retries
        self.commitment = commitment
        self._urlopen = opener or urllib.request.urlopen
        self._sleep = sleep
        self._rng = rng
        self._limiter = RateLimiter(max_rps, clock=clock, sleep=sleep)
        self._ids = 0
        self.calls = 0
        self._v2_largest_supported = True

    @property
    def url(self):
        if not self.api_key:
            return self.base_url
        separator = "&" if "?" in self.base_url else "?"
        return self.base_url + separator + urllib.parse.urlencode({"api_key": self.api_key})

    # --- transport -------------------------------------------------------------------------

    def call(self, method, params=None):
        """One JSON-RPC call -> `result`. Raises SolamiRPCError (never with the key in it)."""
        attempt = 0
        while True:
            try:
                return self._call_once(method, params)
            except SolamiRPCError as exc:
                retryable = exc.code in RETRYABLE_RPC_CODES or exc.http_status in RETRYABLE_HTTP or (
                    exc.code is None and exc.http_status is None)
                if not retryable or attempt >= self.retries:
                    raise
                delay = min(8.0, 0.1 * (2 ** attempt)) * (0.5 + self._rng())
                log.info("rpc %s: %s - retry %d/%d in %.2fs", method, exc, attempt + 1, self.retries, delay)
                self._sleep(delay)
                attempt += 1

    def _call_once(self, method, params):
        self._limiter.wait()
        self._ids += 1
        self.calls += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self._ids, "method": method,
                           "params": params or []}).encode("utf-8")
        request = urllib.request.Request(self.url, data=body, headers={
            "Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT})
        try:
            with self._urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            raise SolamiRPCError("HTTP %s from Solami RPC (%s): %s" % (
                exc.code, method, redact(detail, self.api_key)), http_status=exc.code)
        except (urllib.error.URLError, socket.timeout, OSError, http.client.HTTPException) as exc:
            reason = getattr(exc, "reason", exc)
            raise SolamiRPCError("network error calling %s: %s" % (method, redact(reason, self.api_key)))
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise SolamiRPCError("non-JSON response to %s" % method, http_status=200)
        if not isinstance(data, dict):
            raise SolamiRPCError("unexpected response shape for %s" % method, http_status=200)
        error = data.get("error")
        if error:
            code = error.get("code") if isinstance(error, dict) else None
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise SolamiRPCError("%s: RPC error %s %s" % (method, code, redact(message, self.api_key)),
                                 code=code, http_status=200)
        if "result" not in data:
            raise SolamiRPCError("%s: response has neither result nor error" % method, http_status=200)
        return data["result"]

    # --- methods -----------------------------------------------------------------------------

    def get_slot(self):
        return self.call("getSlot", [{"commitment": self.commitment}])

    def get_mint_info(self, mint):
        """MintInfo for `mint`, or None if the account does not exist / is not a mint."""
        result = self.call("getAccountInfo", [mint, {"encoding": "jsonParsed", "commitment": self.commitment}])
        return parse_mint_account(mint, result)

    def get_largest_token_accounts(self, mint):
        """Top holder token accounts of `mint` (up to 20), largest first."""
        params = [mint, {"commitment": self.commitment}]
        if self._v2_largest_supported:
            try:
                return parse_largest_accounts(mint, self.call("getTokenLargestAccountsV2", params))
            except SolamiRPCError as exc:
                if exc.code != -32601:          # only "method not found" means "not Solami"
                    raise
                log.info("getTokenLargestAccountsV2 not available on this RPC, using the stock method")
                self._v2_largest_supported = False
        return parse_largest_accounts(mint, self.call("getTokenLargestAccounts", params))

    def get_token_accounts(self, addresses):
        """{token account address: TokenAccountBalance, or None when the account exists
        but is not a readable token account}. An account that no longer exists comes
        back as TokenAccountBalance(amount=0, closed=True) - a closed account holds 0."""
        out = {}
        addresses = list(dict.fromkeys(a for a in addresses if a))
        for start in range(0, len(addresses), 100):
            chunk = addresses[start:start + 100]
            result = self.call("getMultipleAccounts", [chunk, {"encoding": "jsonParsed",
                                                               "commitment": self.commitment}])
            values = result.get("value") if isinstance(result, dict) else None
            if not isinstance(values, list) or len(values) != len(chunk):
                raise SolamiRPCError("getMultipleAccounts: unexpected response shape", http_status=200)
            for address, account in zip(chunk, values):
                if account is None:
                    out[address] = TokenAccountBalance(address=address, amount=0, closed=True)
                else:
                    out[address] = parse_token_account(address, account)
        return out


# --- response parsers (pure functions, covered by tests with recorded shapes) ----------------

def _parsed(account):
    data = (account or {}).get("data") if isinstance(account, dict) else None
    return data if isinstance(data, dict) else {}


def parse_mint_account(mint, result):
    value = result.get("value") if isinstance(result, dict) else None
    if not isinstance(value, dict):
        return None
    data = _parsed(value)
    parsed = data.get("parsed") if isinstance(data.get("parsed"), dict) else {}
    if parsed.get("type") != "mint":
        return None
    info = parsed.get("info") if isinstance(parsed.get("info"), dict) else {}
    extensions = {}
    for ext in info.get("extensions") or []:
        if isinstance(ext, dict) and ext.get("extension"):
            state = ext.get("state")
            extensions[str(ext["extension"])] = state if isinstance(state, dict) else {}
    program = data.get("program") or ""
    if not program:
        owner = value.get("owner")
        program = {TOKEN_PROGRAM: "spl-token", TOKEN_2022_PROGRAM: "spl-token-2022"}.get(owner, owner or "")
    context = result.get("context") if isinstance(result.get("context"), dict) else {}
    return MintInfo(
        mint=mint,
        program=program,
        decimals=_int(info.get("decimals")),
        supply=_int(info.get("supply")),
        mint_authority=info.get("mintAuthority") or None,
        freeze_authority=info.get("freezeAuthority") or None,
        extensions=extensions,
        slot=_int(context.get("slot")),
    )


def parse_largest_accounts(mint, result):
    value = result.get("value") if isinstance(result, dict) else result
    if isinstance(value, dict):                      # V2 token-method shape (assumption R1)
        value = value.get("accounts")
    if not isinstance(value, list):
        return []
    out = []
    for row in value:
        if not isinstance(row, dict):
            continue
        address = row.get("address") or row.get("pubkey") or ""
        amount = _int(row.get("amount"))
        if not address or amount is None:
            continue
        out.append(TokenAccountBalance(address=address, amount=amount, decimals=_int(row.get("decimals")),
                                       owner=row.get("owner") or "", mint=mint))
    out.sort(key=lambda b: b.amount, reverse=True)
    return out


def parse_token_account(address, account):
    if not isinstance(account, dict):
        return None
    parsed = _parsed(account).get("parsed")
    if not isinstance(parsed, dict) or parsed.get("type") != "account":
        return None
    info = parsed.get("info") if isinstance(parsed.get("info"), dict) else {}
    token_amount = info.get("tokenAmount") if isinstance(info.get("tokenAmount"), dict) else {}
    amount = _int(token_amount.get("amount"))
    if amount is None:
        return None
    return TokenAccountBalance(address=address, amount=amount, decimals=_int(token_amount.get("decimals")),
                               owner=info.get("owner") or "", mint=info.get("mint") or "")
