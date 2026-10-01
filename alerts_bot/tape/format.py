"""Telegram post text for the live Solana tape (HTML parse mode).

House rules, same as the original template (see alerts_bot/formatter.py):
  - every post ends with FOOTER, which says "not financial advice" - on every post, not
    in a pinned message someone may never read;
  - facts only: numbers with their source, trade sides as "bought"/"sold" (what a wallet
    did, never what a reader should do), no scores, no calls;
  - all text that comes from token creators (symbols, names) is HTML-escaped;
  - every fact links to something the reader can check (Solscan tx/account/token page).
"""
import math
import re
import unicodedata
from datetime import datetime

from ..formatter import escape_html, format_usd
from ..solami.events import KNOWN_SYMBOLS
from .flags import short

FOOTER = "On-chain facts, not financial advice. Data: Solami (Blur stream + RPC), Solana mainnet."
FLAG_NOTE = "⚠️ = rule-based flag (README \"Rule-based flags\"), not a verdict."
MAX_POST_CHARS = 3900          # Telegram's hard limit is 4096; leave room for entities

SECTION_TITLES = {
    "authority": "Token authorities",
    "holders": "Top holders",
    "transfers": "Large transfers",
    "liquidity": "Liquidity",
    "trades": "Large trades",
    "pools": "New pools",
    "graduations": "Graduations",
    "launches": "New tokens",
    "breakouts": "Volume breakouts",
}
SECTION_ORDER = list(SECTION_TITLES)


# --- small helpers ---------------------------------------------------------------------------

def clean_label(text, max_len=32):
    """Token names/symbols are chosen by whoever deployed the token. Beyond HTML escaping
    (done at render time), strip what could forge the *layout* of a post: newlines and
    other control characters (a symbol like "X\\n• fake trade" would add a fake line) and
    bidi overrides (which reorder text visually). Long labels are truncated."""
    text = "".join(" " if unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") else ch for ch in text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= max_len else text[:max_len - 1] + "…"


def esc_attr(text):
    return escape_html(text).replace('"', "&quot;")


def link(url, label):
    return '<a href="%s">%s</a>' % (esc_attr(url), escape_html(label))


def tx_link(signature, label="tx"):
    return link("https://solscan.io/tx/%s" % signature, label) if signature else ""


def account_link(address):
    return link("https://solscan.io/account/%s" % address, short(address)) if address else "unknown"


def token_link(mint, symbol):
    label = clean_label(symbol, 24) or KNOWN_SYMBOLS.get(mint) or short(mint)
    return link("https://solscan.io/token/%s" % mint, label) if mint else escape_html(label)


def format_price(value):
    """USD price without exponent notation: $1.92, $0.4512, $0.00002231, $0.0000008672."""
    if value is None or value <= 0 or not math.isfinite(value):
        return "n/a"
    if value >= 1000:
        return "$%s" % format(round(value), ",")
    if value >= 1:
        return "$%.2f" % value
    if value >= 0.01:
        return "$%.4f" % value
    digits = min(14, -int(math.floor(math.log10(value))) + 3)
    return "$" + ("%.*f" % (digits, value)).rstrip("0")


def compact(value):
    """Token amounts: 950 -> "950", 12_345 -> "12.3K", 18_700_000 -> "18.7M"."""
    if value is None:
        return "n/a"
    sign = "-" if value < 0 else ""
    value = abs(value)
    for size, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if value >= size:
            return "%s%.1f%s" % (sign, value / size, suffix)
    if value >= 1 or value == 0:
        return "%s%s" % (sign, ("%.2f" % value).rstrip("0").rstrip("."))
    return "%s%s" % (sign, ("%.6f" % value).rstrip("0").rstrip("."))


def format_pct(value, signed=False):
    if value is None:
        return "n/a"
    digits = 2 if abs(value) < 10 else 1
    return ("%+.*f%%" if signed else "%.*f%%") % (digits, value)


def signed_usd(value):
    return ("+" if value >= 0 else "-") + format_usd(abs(value))


def utc_hm(when):
    return when.strftime("%H:%M") if isinstance(when, datetime) else "?"


def flag_suffix(flags):
    return (" ⚠️ " + escape_html("; ".join(flags))) if flags else ""


# --- item lines ------------------------------------------------------------------------------

def render_item(item, symbol_of):
    """One tape item -> one or two HTML lines. `symbol_of(mint)` returns a symbol or ""."""
    d = item.data

    def quote_label(fallback=""):
        mint = d.get("quote_mint", "")
        return escape_html(clean_label(symbol_of(mint), 24) or fallback or short(mint))

    token = token_link(d.get("mint", ""), symbol_of(d.get("mint", "")))
    section = item.section
    if section == "trades":
        verb = {"buy": "bought", "sell": "sold"}.get(d.get("side"), "traded")
        text = "%s %s %s of %s at %s on %s · %s" % (
            account_link(d.get("trader")), verb, format_usd(d.get("usd")), token, format_price(d.get("price_usd")),
            escape_html(d.get("dex") or "?"), tx_link(d.get("signature")))
    elif section == "liquidity":
        verb = {"add": "added", "remove": "removed"}.get(d.get("kind"), "moved")
        amount = ("≈" if d.get("approx") else "") + format_usd(d["usd"]) if d.get("usd") is not None else \
            escape_html(d.get("legs") or "an unpriced amount")
        preposition = "to" if d.get("kind") == "add" else "from"
        pair = "%s/%s" % (token, quote_label())
        share = ""
        if d.get("pct") is not None:
            share = " (≈%s of the pool's %s side)" % (format_pct(d["pct"]), quote_label("quote"))
        text = "%s %s %s %s %s on %s%s · %s" % (
            account_link(d.get("provider")), verb, amount, preposition, pair, escape_html(d.get("dex") or "?"),
            share, tx_link(d.get("signature")))
    elif section == "pools":
        quote = quote_label()
        text = "New %s/%s pool on %s · pool %s%s · %s" % (
            token, quote, escape_html(d.get("dex") or "?"), account_link(d.get("pool")),
            (" · created by %s" % account_link(d["creator"])) if d.get("creator") else "", tx_link(d.get("signature")))
    elif section == "graduations":
        text = "%s graduated from %s to a %s pool (%s)%s" % (
            token, escape_html(d.get("launchpad") or "a launchpad"), escape_html(d.get("dex") or "DEX"),
            account_link(d.get("pool")), (" · " + tx_link(d["signature"])) if d.get("signature") else "")
    elif section == "launches":
        text = "%s%s launched on %s by %s · %s" % (
            token, (" (%s)" % escape_html(clean_label(d["name"]))) if d.get("name") else "",
            escape_html(d.get("dex") or "?"),
            account_link(d.get("creator")), tx_link(d.get("signature")))
    elif section == "breakouts":
        minutes = int(round((d.get("window_secs") or 0) / 60.0)) or "?"
        base = {"surge": "1-hour", "radar": "6-hour"}.get(d.get("kind"), "baseline")
        parts = ["%s: %s-min volume %s, %.1f× its %s baseline" % (
            token, minutes, format_usd(d.get("volume_usd")), d.get("multiple") or 0.0, base)]
        if d.get("trades") is not None:
            parts.append("%s trades" % format(d["trades"], ","))
        if d.get("traders_est") is not None:
            parts.append("~%s wallets" % format(d["traders_est"], ","))
        if d.get("mcap") is not None:
            parts.append("mcap at trigger %s" % format_usd(d["mcap"]))
        text = " · ".join(parts) + " (Solami %s)" % escape_html(d.get("kind") or "signal")
    elif section == "holders":
        if d.get("entered"):
            text = "%s: %s entered the top 20 holder accounts with %s of supply%s" % (
                token, account_link(d.get("account")), format_pct(d.get("new_pct")), _owner(d))
        else:
            text = "%s: holder account %s%s %s %s of supply (%s), now %s · since %s UTC" % (
                token, account_link(d.get("account")), _owner(d), "gained" if d.get("delta_pct", 0) > 0 else "lost",
                format_pct(abs(d.get("delta_pct") or 0.0)), compact(d.get("delta_ui")), format_pct(d.get("new_pct")),
                escape_html(d.get("since") or "?"))
    elif section == "authority":
        render = _authority if d.get("address") else (lambda v: escape_html(v) if v else "none")
        text = "%s %s changed: %s → %s" % (
            token, escape_html(d.get("what") or "authority"), render(d.get("old")), render(d.get("new")))
    elif section == "transfers":
        kind = d.get("kind")
        amount = "%s of supply (%s)" % (format_pct(d.get("pct")), compact(d.get("amount_ui")))
        if kind == "mint":
            text = "%s: %s minted to %s · %s" % (token, amount, account_link(d.get("dst_owner")),
                                                  tx_link(d.get("signature")))
        elif kind == "burn":
            text = "%s: %s burned by %s · %s" % (token, amount, account_link(d.get("src_owner")),
                                                  tx_link(d.get("signature")))
        else:
            text = "%s: %s moved %s → %s · %s" % (token, amount, account_link(d.get("src_owner")),
                                                   account_link(d.get("dst_owner")), tx_link(d.get("signature")))
    else:
        text = escape_html(str(d))
    # Short flag lists stay on the item's line; long ones (or ones next to a facts line)
    # get their own indented line so the fact itself stays readable.
    inline = len(item.flags) <= 2 and not d.get("facts")
    lines = ["• " + text + (flag_suffix(item.flags) if inline else "")]
    if d.get("facts"):
        lines.append("  " + escape_html(d["facts"]))
    if item.flags and not inline:
        lines.append("  ⚠️ " + escape_html("; ".join(item.flags)))
    return lines


def _owner(d):
    return (" (owner %s)" % account_link(d["owner"])) if d.get("owner") else ""


def _authority(value):
    return account_link(value) if value else "none"


# --- whole posts -----------------------------------------------------------------------------

def header_line(title, now, subtitle=None):
    text = "<b>%s</b> · %s UTC" % (escape_html(title), utc_hm(now))
    return text + (" · " + escape_html(subtitle) if subtitle else "")


def _units(block):
    """A block's lines in pieces a post never breaks inside: an item with its indented
    facts and flag lines, a "+ N more" line with the item before it, and the section title
    with the first item, so a title never ends a post on its own."""
    units = []
    for line in block:
        if units and (line.startswith(("  ", "+ ")) or units == [block[:1]]):
            units[-1].append(line)
        else:
            units.append([line])
    return units


def assemble(header, blocks, footer_lines, max_chars=MAX_POST_CHARS):
    """Blocks of lines -> one or more post texts, each with header and footer, each under
    max_chars. A block is split only if it does not fit whole, and then between items."""
    posts, current = [], [header, ""]
    footer = [""] + list(footer_lines)

    def size(lines):
        return len("\n".join(lines + footer))

    def close():
        while current and current[-1] == "":
            current.pop()
        posts.append("\n".join(current + footer))

    for block in blocks:
        if size(current + block) <= max_chars:
            current.extend(block + [""])
            continue
        for unit in _units(block):
            unit = [line[:max_chars - 200] for line in unit]
            if size(current + unit) > max_chars and len(current) > 2:
                close()
                current[:] = [header + " (cont.)", ""]
            current.extend(unit)
        current.append("")
    if len(current) > 2:
        close()
    return posts


def render_tape(sections, symbol_of, now, subtitle, thresholds_note, max_chars=MAX_POST_CHARS):
    """sections: {section: (items_shown, overflow_count)} -> list of post texts."""
    blocks, any_flags = [], False
    for section in SECTION_ORDER:
        if section not in sections:
            continue
        items, overflow = sections[section]
        title = SECTION_TITLES[section]
        note = thresholds_note.get(section)
        block = ["<b>%s</b>%s" % (title, (" (%s)" % escape_html(note)) if note else "")]
        for item in items:
            block.extend(render_item(item, symbol_of))
            any_flags = any_flags or bool(item.flags) or "⚠️" in (item.data.get("facts") or "")
        if overflow:
            block.append("+ %d more not shown" % overflow)
        blocks.append(block)
    if not blocks:
        return []
    footer = ([FLAG_NOTE] if any_flags else []) + [FOOTER]
    return assemble(header_line("Solana Tape", now, subtitle), blocks, footer, max_chars)


def render_card(mint, symbol, name, info, top_accounts, flags, now, top10_pct=None):
    """'Now watching' card for one token: supply, authorities, extensions, concentration."""
    name = clean_label(name)
    lines = ["<b>Now watching</b> %s%s · %s UTC" % (
        token_link(mint, symbol), (" (%s)" % escape_html(name)) if name and name != symbol else "", utc_hm(now)),
        "Mint <code>%s</code>" % escape_html(mint)]
    if info is None:
        lines.append("Mint account could not be read over RPC right now - authority checks will retry.")
    else:
        lines.append("Supply %s · decimals %s · program %s" % (
            compact(info.ui_supply), info.decimals if info.decimals is not None else "n/a",
            escape_html(info.program or "unknown")))
        lines.append("Mint authority: %s" % _authority(info.mint_authority))
        lines.append("Freeze authority: %s" % _authority(info.freeze_authority))
        if info.extensions:
            lines.append("Token-2022 extensions: %s" % escape_html(", ".join(sorted(info.extensions))))
    if top10_pct is not None and top_accounts:
        largest = top_accounts[0].amount / float(info.supply) * 100.0 if info and info.supply else None
        lines.append("Top-10 holder accounts: %s of supply%s" % (
            format_pct(top10_pct), (" (largest %s)" % format_pct(largest)) if largest is not None else ""))
    for flag in flags:
        lines.append("⚠️ " + escape_html(flag))
    lines.append("Check: %s · %s" % (link("https://solscan.io/token/%s" % mint, "Solscan"),
                                     link("https://dexscreener.com/solana/%s" % mint, "DexScreener chart")))
    footer = ([FLAG_NOTE] if flags else []) + [FOOTER]
    return assemble(lines[0], [lines[1:]], footer)


def render_summary(mint, symbol, stats, now, window_label):
    lines = [
        "Trades: %s (%s buys / %s sells) · %s wallets" % (
            format(stats.trades, ","), format(stats.buys, ","), format(stats.sells, ","),
            format(len(stats.traders), ",")),
        "Volume: %s (buys %s / sells %s)" % (format_usd(stats.volume_usd), format_usd(stats.buy_usd),
                                             format_usd(stats.sell_usd)),
    ]
    if stats.liq_adds or stats.liq_removes:
        net = stats.liq_add_usd - stats.liq_remove_usd
        lines.append("Liquidity: +%s added / -%s removed (net %s, %d events%s)" % (
            format_usd(stats.liq_add_usd), format_usd(stats.liq_remove_usd), signed_usd(net),
            stats.liq_adds + stats.liq_removes,
            ", %d unpriced" % stats.liq_unpriced if stats.liq_unpriced else ""))
    if stats.last_price:
        change = stats.price_change_pct
        lines.append("Last price: %s%s" % (format_price(stats.last_price),
                                            (" (%s over the window)" % format_pct(change, signed=True))
                                            if change is not None else ""))
    if stats.largest_usd:
        lines.append("Largest trade: %s %s · %s" % (format_usd(stats.largest_usd), escape_html(stats.largest_side),
                                                     tx_link(stats.largest_signature)))
    title = "%s · %s" % (token_link(mint, symbol), escape_html(window_label))
    header = "<b>Tape summary</b> %s · %s UTC" % (title, utc_hm(now))
    return assemble(header, [lines], ["Counted from Solami Blur swap/liquidity events received while connected.",
                                      FOOTER])


def render_digest(counters, now, window_label, stream_line=None):
    def top(counter, n=4):
        parts = ["%s %s" % (escape_html(name or "unknown"), format(count, ",")) for name, count in counter.most_common(n)]
        rest = sum(counter.values()) - sum(c for _n, c in counter.most_common(n))
        if rest > 0:
            parts.append("other %s" % format(rest, ","))
        return " · ".join(parts)

    lines = []
    for label, counter in (("New tokens", counters.tokens), ("New pools", counters.pools),
                           ("Graduations", counters.graduations)):
        total = sum(counter.values())
        lines.append("%s: %s%s" % (label, format(total, ","), (" (%s)" % top(counter)) if total else ""))
    if stream_line:
        lines.append(escape_html(stream_line))
    header = "<b>Launch digest</b> · %s · %s UTC" % (escape_html(window_label), utc_hm(now))
    return assemble(header, [lines], [FOOTER])


