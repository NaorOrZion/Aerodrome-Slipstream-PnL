"""
USD price resolution via DeFiLlama historical API.

Single Responsibility: convert (token_address, block) or (wei_amount, block)
into a USD value.  All HTTP calls to DeFiLlama happen here.
"""

from decimal import Decimal
from typing import Optional

import requests
from web3 import Web3

from .config import (
    AERO_TOKEN_ADDRESS,
    DEBUG,
    DEFILLAMA_API_KEY,
    DEFILLAMA_CHAIN,
    WETH_BASE_ADDRESS,
)
from .rpc import get_block_timestamp

# ── In-memory caches ─────────────────────────────────────────────────────────
_block_ts_cache: dict[int, int] = {}
_price_cache: dict[tuple[str, int], Optional[tuple[Decimal, int]]] = {}


# ── Internal ─────────────────────────────────────────────────────────────────

def _build_url(timestamp: int, coin_key: str) -> str:
    if DEFILLAMA_API_KEY:
        return (
            f"https://pro-api.llama.fi/{DEFILLAMA_API_KEY}"
            f"/coins/prices/historical/{timestamp}/{coin_key}"
        )
    return f"https://coins.llama.fi/prices/historical/{timestamp}/{coin_key}"


def _fetch_price(
    w3: Web3,
    chain: str,
    token_address: str,
    block: int,
) -> Optional[tuple[Decimal, int]]:
    """
    Query DeFiLlama for (price_usd, decimals) of *token_address* at *block*.
    Results are cached by (coin_key, timestamp).
    """
    ts = get_block_timestamp(w3, block, _block_ts_cache)
    addr = token_address.strip().lower()
    if not addr.startswith("0x"):
        addr = "0x" + addr
    coin_key = f"{chain}:{addr}"
    cache_key = (coin_key, ts)

    if cache_key in _price_cache:
        return _price_cache[cache_key]

    url = _build_url(ts, coin_key)
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        coins = data.get("coins") or {}

        if not coins:
            if DEBUG:
                print(f"[DEBUG] DeFiLlama: no coins in response for {coin_key} ts={ts}")
            _price_cache[cache_key] = None
            return None

        entry = coins.get(coin_key)
        if entry is None:
            for v in coins.values():
                if isinstance(v, dict) and "price" in v:
                    entry = v
                    break

        if not isinstance(entry, dict) or "price" not in entry:
            if DEBUG:
                print(f"[DEBUG] DeFiLlama: no price for {coin_key} ts={ts}")
            _price_cache[cache_key] = None
            return None

        price = Decimal(str(entry["price"]))
        decimals = int(entry.get("decimals", 18))
        if DEBUG:
            print(f"[DEBUG] DeFiLlama: {coin_key} ts={ts} -> ${price} dec={decimals}")
        result = (price, decimals)
        _price_cache[cache_key] = result
        return result

    except Exception as exc:
        if DEBUG:
            print(f"[DEBUG] DeFiLlama FAILED {coin_key} ts={ts}: {exc}")
        _price_cache[cache_key] = None
        return None


# ── Public API ───────────────────────────────────────────────────────────────

def token_amount_to_usd(
    w3: Web3,
    token_address: str,
    block: int,
    amount_wei: int,
    decimals: int = 18,
) -> Decimal:
    """Convert *amount_wei* of a token to USD at *block*."""
    result = _fetch_price(w3, DEFILLAMA_CHAIN, token_address, block)
    if result is None:
        return Decimal("0")
    price, _ = result
    return (Decimal(amount_wei) / Decimal(10**decimals)) * price


def eth_price_usd(w3: Web3, block: int) -> Decimal:
    """1 ETH in USD at *block* (uses WETH on Base)."""
    result = _fetch_price(w3, DEFILLAMA_CHAIN, WETH_BASE_ADDRESS, block)
    if result is None:
        return Decimal("0")
    return result[0]


def aero_price_usd(w3: Web3, block: int) -> Decimal:
    """1 AERO in USD at *block*."""
    result = _fetch_price(w3, DEFILLAMA_CHAIN, AERO_TOKEN_ADDRESS, block)
    if result is None:
        return Decimal("0")
    return result[0]
