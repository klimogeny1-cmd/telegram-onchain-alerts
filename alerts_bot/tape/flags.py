"""Rule-based flags for the live Solana tape - short, factual, documented, never a score.

Each rule below checks one publicly verifiable fact and, if it holds, returns one line
of text. The lines do not add up to a rating, and an empty list is NOT a claim that a
token is safe: it only means none of these specific rules fired. formatter/format code
always prints them next to the "not financial advice" footer. (README "Rule-based
flags" lists every rule with its threshold and where the fact comes from.)

  Source: Solami RPC getAccountInfo(jsonParsed) on the mint
    MINT_AUTHORITY      mint authority is set -> more supply can be minted
    FREEZE_AUTHORITY    freeze authority is set -> token accounts can be frozen
    PERMANENT_DELEGATE  Token-2022 permanent delegate -> can move/burn anyone's tokens
    TRANSFER_FEE        Token-2022 transfer fee > 0
    TRANSFER_HOOK       Token-2022 transfer hook program set
    DEFAULT_FROZEN      Token-2022 default account state = frozen
    PAUSED              Token-2022 pausable config with paused = true
    NON_TRANSFERABLE    Token-2022 non-transferable
  Source: Solami RPC getTokenLargestAccountsV2 + supply
    TOP10_CONCENTRATION top-10 holder accounts >= TOP10_FLAG_PCT of supply
  Source: Blur swap event
    PRICE_IMPACT        price_impact_pct >= PRICE_IMPACT_FLAG_PCT
    OUTLIER_PRINT       candle_ok == false (Solami's per-token price guard)
  Source: Blur liquidity event + pool reserves seen on swaps
    LARGE_REMOVAL       a removal >= LIQ_ALERT_PCT of the pool's quote-side reserve
"""
from typing import List, Optional


def short(address, head=4, tail=4):
    address = address or ""
    return address if len(address) <= head + tail + 1 else "%s…%s" % (address[:head], address[-tail:])


def fee_bps(state):
    """Highest transfer fee (basis points) in a Token-2022 transferFeeConfig state."""
    best = None
    for key in ("newerTransferFee", "olderTransferFee"):
        fee = state.get(key) if isinstance(state.get(key), dict) else {}
        bps = fee.get("transferFeeBasisPoints")
        if isinstance(bps, int) and not isinstance(bps, bool):
            best = bps if best is None else max(best, bps)
    return best


def mint_flags(info) -> List[str]:
    """Flags from a MintInfo (see solami/rpc.py). Order: most consequential first."""
    if info is None:
        return []
    flags = []
    if info.mint_authority:
        flags.append("Mint authority active (%s) - supply can still be increased" % short(info.mint_authority))
    if info.freeze_authority:
        flags.append("Freeze authority active (%s) - token accounts can be frozen" % short(info.freeze_authority))
    ext = info.extensions or {}
    delegate = (ext.get("permanentDelegate") or {}).get("delegate")
    if delegate:
        flags.append("Token-2022 permanent delegate (%s) - can transfer or burn any holder's tokens" % short(delegate))
    if "transferFeeConfig" in ext:
        bps = fee_bps(ext["transferFeeConfig"])
        if bps:
            flags.append("Token-2022 transfer fee %.2f%%" % (bps / 100.0))
    hook = (ext.get("transferHook") or {}).get("programId")
    if hook:
        flags.append("Token-2022 transfer hook program set (%s)" % short(hook))
    if (ext.get("defaultAccountState") or {}).get("accountState") == "frozen":
        flags.append("Token-2022: new token accounts start frozen")
    if (ext.get("pausableConfig") or {}).get("paused") is True:
        flags.append("Token-2022: transfers are paused")
    if "nonTransferable" in ext:
        flags.append("Token-2022: non-transferable")
    return flags


def top10_share_pct(top_accounts, supply) -> Optional[float]:
    if not top_accounts or not supply:
        return None
    return sum(a.amount for a in top_accounts[:10]) / float(supply) * 100.0


def concentration_flag(top_accounts, supply, threshold_pct) -> Optional[str]:
    share = top10_share_pct(top_accounts, supply)
    if share is None or share < threshold_pct:
        return None
    return "Top-10 holder accounts hold %.1f%% of supply (can include pool and exchange accounts)" % share


def swap_flags(swap, price_impact_flag_pct) -> List[str]:
    flags = []
    if swap.price_impact_pct is not None and swap.price_impact_pct >= price_impact_flag_pct:
        flags.append("Price impact %.1f%%" % swap.price_impact_pct)
    if swap.candle_ok is False:
        flags.append("Outlier print (kept out of candles by Solami's price guard)")
    return flags


def liquidity_flags(kind, pct_of_pool, quote_symbol, threshold_pct) -> List[str]:
    if kind == "remove" and pct_of_pool is not None and pct_of_pool >= threshold_pct:
        return ["Removed %.0f%% of the pool's %s side" % (pct_of_pool, quote_symbol or "quote")]
    return []
