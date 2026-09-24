"""Rule-based risk flags computed only from public DexScreener metrics.

These are short factual observations about publicly visible numbers - liquidity, pair
age, 24h volume vs. liquidity - not a score, not a rating, and not a recommendation. They
do not add up to a verdict, and an empty flag list is not a claim that a pair is "safe".
Every place these flags reach a reader (see formatter.py) must keep the "rule-based, not
advice" framing next to them; that pairing is part of the contract of this module, not
just a suggestion for callers.
"""
from dataclasses import dataclass
from typing import List

from .analysis import Pair


@dataclass
class RiskThresholds:
    low_liquidity_usd: float = 25_000.0
    very_new_pair_min: float = 30.0
    high_vol_to_liquidity_ratio: float = 5.0


def compute_risk_flags(pair: Pair, thresholds: RiskThresholds = None, now=None) -> List[str]:
    """Short factual flag strings, most notable first. An empty list means none of the
    rules below fired - it does not mean the pair passed any kind of check."""
    thresholds = thresholds or RiskThresholds()
    flags = []

    if pair.liquidity_usd is None:
        flags.append("Liquidity unknown (not reported by DexScreener)")
    elif pair.liquidity_usd < thresholds.low_liquidity_usd:
        flags.append("Low liquidity (${:,.0f})".format(pair.liquidity_usd))

    age = pair.age_minutes(now=now)
    if age is not None and age < thresholds.very_new_pair_min:
        flags.append("Very new pair ({:.0f} min old)".format(age))

    if pair.liquidity_usd and pair.volume_h24 is not None:
        ratio = pair.volume_h24 / pair.liquidity_usd
        if ratio >= thresholds.high_vol_to_liquidity_ratio:
            flags.append("High 24h volume vs liquidity ({:.1f}x)".format(ratio))

    return flags
