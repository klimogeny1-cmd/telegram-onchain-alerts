"""Turns Pair objects into the Telegram post text this template sends.

Format matches a plain "tape" style: a bold header with a UTC timestamp, a short list,
and a fixed footer that repeats the disclaimer on every single post rather than relying
on a pinned message someone might not have read. Posts use Telegram's HTML parse mode
with a minimal, hand-rolled escaper (see escape_html) - no templating dependency needed
for three tags (<b>, plain text, a bare URL).
"""
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from .analysis import Pair
from .risk_flags import RiskThresholds, compute_risk_flags

FOOTER = "Tape, not advice. Source: DexScreener."


def escape_html(text: str) -> str:
    """Escapes the three characters Telegram's HTML parse mode treats specially.

    This matters because token name/symbol are chosen by whoever deployed the token -
    arbitrary, unmoderated text - and get interpolated into an HTML-parsed message. Left
    unescaped, a symbol like "<b>" or "A & B" would either break the API call (Telegram
    rejects malformed HTML) or let a token creator inject fake formatting into the post.
    """
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_usd(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    value = float(value)
    sign = "-" if value < 0 else ""
    value = abs(value)
    if value >= 1_000_000_000:
        return "%s$%.2fB" % (sign, value / 1_000_000_000)
    if value >= 1_000_000:
        return "%s$%.2fM" % (sign, value / 1_000_000)
    if value >= 1_000:
        return "%s$%.1fK" % (sign, value / 1_000)
    return "%s$%.0f" % (sign, value)


def format_age(age_minutes: Optional[float]) -> str:
    if age_minutes is None:
        return "n/a"
    if age_minutes < 60:
        return "%dm" % round(age_minutes)
    return "%.1fh" % (age_minutes / 60)


def pair_label(pair: Pair) -> str:
    return "%s/%s" % (pair.base_symbol, pair.quote_symbol)


def cap_items(pairs: List[Pair], max_items: int) -> Tuple[List[Pair], int]:
    """(items actually shown, how many were left out). Used by the two format_* functions
    below and by runner.run_cycle, which must mark as "sent" exactly the pairs a post
    actually displayed - never the ones left out of an "+N more" line, or they would be
    silently skipped forever by dedup.DedupStore without ever having been shown."""
    shown = pairs[:max_items]
    return shown, max(0, len(pairs) - len(shown))


def _flag_suffix(pair: Pair, thresholds: Optional[RiskThresholds], now) -> str:
    flags = compute_risk_flags(pair, thresholds, now=now)
    return (" ⚠️ " + "; ".join(flags)) if flags else ""


def format_new_pairs_post(pairs: List[Pair], window_minutes: int, generated_at=None,
                          thresholds: Optional[RiskThresholds] = None, max_items: int = 10) -> str:
    """"New pairs, last N min" post. Returns "" when there is nothing to show - callers
    should skip sending rather than post an empty tape (see runner.run_cycle)."""
    if not pairs:
        return ""
    generated_at = generated_at or datetime.now(timezone.utc)
    shown, overflow = cap_items(pairs, max_items)

    lines = ["<b>New pairs, last %d min</b> · %s UTC" % (window_minutes, generated_at.strftime("%H:%M")), ""]
    for pair in shown:
        age = format_age(pair.age_minutes(now=generated_at))
        lines.append(
            "• <b>%s</b> (%s) — liq %s, vol 1h %s, age %s%s\n  %s" % (
                escape_html(pair_label(pair)), escape_html(pair.chain_id),
                format_usd(pair.liquidity_usd), format_usd(pair.volume_h1), age,
                _flag_suffix(pair, thresholds, generated_at), pair.url or "",
            )
        )
    if overflow:
        lines += ["", "+ %d more not shown" % overflow]
    lines += ["", FOOTER]
    return "\n".join(lines)


def format_volume_alert_post(pairs: List[Pair], generated_at=None,
                             thresholds: Optional[RiskThresholds] = None, max_items: int = 10) -> str:
    """"Volume anomalies, last 1h" post. Returns "" when there is nothing to show."""
    if not pairs:
        return ""
    generated_at = generated_at or datetime.now(timezone.utc)
    shown, overflow = cap_items(pairs, max_items)

    lines = ["<b>Volume anomalies, last 1h</b> · %s UTC" % generated_at.strftime("%H:%M"), ""]
    for pair in shown:
        ratio = pair.volume_spike_ratio
        ratio_text = ("%.1fx" % ratio) if ratio is not None else "n/a"
        lines.append(
            "• <b>%s</b> (%s) — vol 1h %s (%s vs 24h avg), liq %s%s\n  %s" % (
                escape_html(pair_label(pair)), escape_html(pair.chain_id),
                format_usd(pair.volume_h1), ratio_text, format_usd(pair.liquidity_usd),
                _flag_suffix(pair, thresholds, generated_at), pair.url or "",
            )
        )
    if overflow:
        lines += ["", "+ %d more not shown" % overflow]
    lines += ["", FOOTER]
    return "\n".join(lines)
