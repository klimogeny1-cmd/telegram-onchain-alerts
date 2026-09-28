"""Solami data layer - the bot's primary (and, in the default mode, only) data path.

Which Solami product does what, and why (details in README "Why Solami"):

  Blur WebSocket (blur.py)   wss://ws.solami.dev/data/subscribe
      Decoded, typed market events as they confirm: swap, liquidity, token_create,
      pool_create, graduation, surge/radar, transfer, metadata. It replaces running an
      indexer and writing DEX instruction parsers.
  RPC (rpc.py)               https://rpc.solami.dev/sol
      Point reads of chain state: mint/freeze authority and Token-2022 extensions
      (getAccountInfo jsonParsed), top-20 holders (Solami's getTokenLargestAccountsV2),
      current balances/owners (getMultipleAccounts).
  Data API (data_api.py)     https://api.solami.dev/data/token/metadata
      Optional: a token's symbol when the stream has not sent its metadata yet.

The rest of the bot only sees the small interface below: an event source with
start()/get()/stop(), normalized event dataclasses, and an RPC reader.
"""
from .blur import BlurStream, BlurSubscription, ReplaySource, redact
from .data_api import SolamiDataAPI
from .events import (BlurEvent, Graduation, LiquidityChange, OtherEvent, PoolCreated, StreamNotice, Swap,
                     TokenLaunch, TokenMetadata, TokenTransfer, VolumeBreakout, parse_blur_event,
                     parse_blur_message)
from .rpc import MintInfo, SolamiRPC, SolamiRPCError, TokenAccountBalance

__all__ = [
    "BlurStream", "BlurSubscription", "ReplaySource", "redact", "SolamiDataAPI", "BlurEvent", "Graduation",
    "LiquidityChange", "OtherEvent", "PoolCreated", "StreamNotice", "Swap", "TokenLaunch", "TokenMetadata",
    "TokenTransfer", "VolumeBreakout", "parse_blur_event", "parse_blur_message", "MintInfo", "SolamiRPC",
    "SolamiRPCError", "TokenAccountBalance",
]
