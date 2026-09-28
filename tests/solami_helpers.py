"""Shared helpers for the Solami-layer and tape tests (not a test module itself)."""
import json
import os
from datetime import datetime, timezone

from alerts_bot.solami.events import parse_blur_message
from alerts_bot.solami.rpc import MintInfo, TokenAccountBalance, parse_mint_account

SOLAMI_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "solami")
BLUR_FRAMES = os.path.join(SOLAMI_FIXTURES, "blur_frames.jsonl")

DEMO = "DemoTape111111111111111111111111111111111111"
XTRA = "XtraToken11111111111111111111111111111111111"
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
POOL_A = "PairAmm1111111111111111111111111111111111111"
PROVIDER = "Provider111111111111111111111111111111111111"
HOLD_A, HOLD_B, HOLD_C, HOLD_D = ("HoLd%s111111111111111111111111111111111111111" % c for c in "ABCD")
AUTH = "MintAuth111111111111111111111111111111111111"

# Fixed "now" for engine tests: shortly after the fixture frames' block times.
NOW = datetime(2026, 9, 21, 14, 20, 0, tzinfo=timezone.utc)


def load_json(name):
    with open(os.path.join(SOLAMI_FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


def blur_lines():
    with open(BLUR_FRAMES, encoding="utf-8") as fh:
        return [line for line in fh if line.strip() and not line.startswith("#")]


def blur_events():
    events = []
    for line in blur_lines():
        events.extend(parse_blur_message(line))
    return events


def demo_mint_info(mint_authority=None, freeze_authority=None, supply=1_000_000_000_000_000):
    """DEMO: 1,000,000,000 tokens at 6 decimals, both authorities revoked by default."""
    return MintInfo(mint=DEMO, program="spl-token", decimals=6, supply=supply, mint_authority=mint_authority,
                    freeze_authority=freeze_authority)


def fixture_mint_info(name, mint=DEMO):
    return parse_mint_account(mint, load_json(name)["result"])


def holders(*pairs):
    """holders((HOLD_A, 150_000_000), ...) with UI amounts at 6 decimals."""
    return [TokenAccountBalance(address=a, amount=int(ui * 10 ** 6), decimals=6, mint=DEMO) for a, ui in pairs]


class FakeRPC:
    """Scripted stand-in for SolamiRPC. Values may be a single answer or a list of answers
    consumed one per call (the last one repeats)."""

    def __init__(self, infos=None, largest=None, accounts=None, fail=False):
        self.infos = infos or {}          # mint -> MintInfo, or [MintInfo, ...] one per call
        self.largest = largest or {}      # mint -> [balances], or [[balances], ...] one per call
        self.accounts = accounts or {}    # token account -> TokenAccountBalance
        self.fail = fail
        self.calls = []

    def get_mint_info(self, mint):
        self.calls.append(("getAccountInfo", mint))
        if self.fail:
            raise RuntimeError("rpc down")
        value = self.infos.get(mint)
        if isinstance(value, list):
            return value.pop(0) if len(value) > 1 else value[0]
        return value

    def get_largest_token_accounts(self, mint):
        self.calls.append(("getTokenLargestAccountsV2", mint))
        if self.fail:
            raise RuntimeError("rpc down")
        value = self.largest.get(mint, [])
        if value and isinstance(value[0], list):
            return value.pop(0) if len(value) > 1 else value[0]
        return value

    def get_token_accounts(self, addresses):
        self.calls.append(("getMultipleAccounts", tuple(addresses)))
        return {a: self.accounts.get(a) for a in addresses}


class FakeDataAPI:
    def __init__(self, answers=None):
        self.answers = answers or {}
        self.calls = []

    def token_metadata(self, mint):
        self.calls.append(mint)
        return self.answers.get(mint)


class FakeBot:
    def __init__(self, fail_times=0):
        self.sent = []
        self.fail_times = fail_times

    def send_message(self, chat_id, text, **_kwargs):
        from alerts_bot.telegram import TelegramError
        if self.fail_times > 0:
            self.fail_times -= 1
            raise TelegramError("sendMessage", 502, "Bad Gateway")
        self.sent.append((chat_id, text))
        return {"message_id": len(self.sent)}


class FakeOpener:
    """urllib.request.urlopen stand-in for the RPC/Data API clients: serves scripted
    payloads (dict/list -> JSON body; Exception -> raised) and records every request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout=None):
        body = json.loads(request.data.decode("utf-8")) if request.data else None
        self.requests.append({"url": request.full_url, "body": body, "headers": dict(request.header_items())})
        if not self.responses:
            raise AssertionError("unexpected extra request: %s" % (body,))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return _Response(response)


class _Response:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
