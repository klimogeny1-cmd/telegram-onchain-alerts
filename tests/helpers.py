"""Shared test helpers and constants. Not a test module itself (no test_ prefix, so
unittest discovery skips it)."""
import json
import os
from datetime import datetime, timezone

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

# Every "pairCreatedAt" timestamp in tests/fixtures/*.json was written relative to this
# fixed point in time - NOT to whenever the tests happen to run. Keeping it fixed, and
# passing NOW explicitly as the `now=` argument, is what makes analysis.find_new_pairs()
# and friends deterministic no matter when `python3 -m unittest` is invoked.
BASE_MS = 1735000000000
NOW = datetime.fromtimestamp(BASE_MS / 1000.0, tz=timezone.utc)


def load_fixture(name):
    with open(os.path.join(FIXTURES_DIR, name), encoding="utf-8") as fh:
        return json.load(fh)


class FakeHTTPResponse:
    """Stands in for what urllib.request.urlopen() returns (a context manager with
    .read()), so DexScreenerClient can be tested without any real network call."""

    def __init__(self, payload, status=200):
        self._body = json.dumps(payload).encode("utf-8")
        self.status = status

    def read(self):
        return self._body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def fake_opener(payload):
    """Returns a function with the same call signature as urllib.request.urlopen that
    serves the given JSON-serializable `payload` on every call instead of hitting the
    network - `payload` is returned as-is, so passing a list serves that list (a JSON
    array is a perfectly normal DexScreener response shape, see token_pairs_response.json).

    The returned function also exposes `.calls`, the list of full URLs it was asked for,
    in order - so a test can assert what was actually requested.
    """
    calls = []

    def _opener(request, timeout=None):
        calls.append(request.full_url)
        return FakeHTTPResponse(payload)

    _opener.calls = calls
    return _opener
