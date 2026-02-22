"""
Aerodrome Slipstream LP Simulator — live on-chain edition.

Pulls pool daily volume and competing liquidity directly from the blockchain
via Web3.py, then runs a fee-share projection.

Usage:
    python lp_simulation.py
"""

import math
import os
import sys

import requests
from dotenv import load_dotenv
from web3 import Web3

load_dotenv()

# ═════════════════════════════════════════════════════════════════════════════
# Configuration
# ═════════════════════════════════════════════════════════════════════════════

RPC_URL = os.getenv("QUICKNODE_BASE_ENDPOINT") or "https://mainnet.base.org"

# Set this to the Slipstream CL pool you want to simulate.
# Find pool addresses at https://aerodrome.finance or on BaseScan.
POOL_ADDRESS = os.getenv("POOL_ADDRESS", "")

# Your strategy parameters
MY_INVESTMENT = 1000.0       # Real USD you are depositing
MY_RANGE_PERCENT = 5.0       # Price range (+/- % from current price)

# Base chain: ~2s block time → ~43,200 blocks per day
BLOCKS_PER_DAY = 43_200
_LOG_CHUNK_SIZE = 5_000

DEFILLAMA_CHAIN = "base"

# ═════════════════════════════════════════════════════════════════════════════
# ABIs (minimal fragments — also available in abi.py for package use)
# ═════════════════════════════════════════════════════════════════════════════

POOL_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "sender", "type": "address"},
            {"indexed": True, "name": "recipient", "type": "address"},
            {"indexed": False, "name": "amount0", "type": "int256"},
            {"indexed": False, "name": "amount1", "type": "int256"},
            {"indexed": False, "name": "sqrtPriceX96", "type": "uint160"},
            {"indexed": False, "name": "liquidity", "type": "uint128"},
            {"indexed": False, "name": "tick", "type": "int24"},
        ],
        "name": "Swap",
        "type": "event",
    },
    {
        "inputs": [],
        "name": "liquidity",
        "outputs": [{"name": "", "type": "uint128"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "slot0",
        "outputs": [
            {"name": "sqrtPriceX96", "type": "uint160"},
            {"name": "tick", "type": "int24"},
            {"name": "observationIndex", "type": "uint16"},
            {"name": "observationCardinality", "type": "uint16"},
            {"name": "observationCardinalityNext", "type": "uint16"},
            {"name": "feeProtocol", "type": "uint8"},
            {"name": "unlocked", "type": "bool"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "token0",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "token1",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "fee",
        "outputs": [{"name": "", "type": "uint24"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "tickSpacing",
        "outputs": [{"name": "", "type": "int24"}],
        "stateMutability": "view",
        "type": "function",
    },
]

ERC20_ABI = [
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"type": "uint8"}],
        "type": "function",
    },
    {
        "inputs": [],
        "name": "symbol",
        "outputs": [{"type": "string"}],
        "type": "function",
    },
]


# ═════════════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════════════

def _get_token_decimals(w3: Web3, token_addr: str) -> int:
    try:
        c = w3.eth.contract(
            address=Web3.to_checksum_address(token_addr), abi=ERC20_ABI
        )
        return c.functions.decimals().call()
    except Exception:
        return 18


def _get_token_symbol(w3: Web3, token_addr: str) -> str:
    try:
        c = w3.eth.contract(
            address=Web3.to_checksum_address(token_addr), abi=ERC20_ABI
        )
        return c.functions.symbol().call()
    except Exception:
        return token_addr[:10]


def _get_current_price_usd(token_addr: str) -> float:
    """Fetch current USD price from DeFiLlama."""
    addr = token_addr.strip().lower()
    if not addr.startswith("0x"):
        addr = "0x" + addr
    coin_key = f"{DEFILLAMA_CHAIN}:{addr}"
    url = f"https://coins.llama.fi/prices/current/{coin_key}"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        entry = data.get("coins", {}).get(coin_key)
        if entry and "price" in entry:
            return float(entry["price"])
    except Exception as e:
        print(f"  [!] Failed to fetch price for {coin_key}: {e}")
    return 0.0


# ═════════════════════════════════════════════════════════════════════════════
# On-chain data fetchers
# ═════════════════════════════════════════════════════════════════════════════

def _fetch_swap_logs(w3: Web3, pool_addr: str, from_block: int, to_block: int) -> list:
    """Fetch all Swap event logs from the pool in chunked requests."""
    swap_topic = w3.keccak(
        text="Swap(address,address,int256,int256,uint160,uint128,int24)"
    ).hex()
    all_logs = []
    current = from_block
    chunk = _LOG_CHUNK_SIZE

    while current <= to_block:
        end = min(current + chunk - 1, to_block)
        try:
            logs = w3.eth.get_logs({
                "address": Web3.to_checksum_address(pool_addr),
                "topics": [swap_topic],
                "fromBlock": hex(current),
                "toBlock": hex(end),
            })
            all_logs.extend(logs)
        except Exception as e:
            msg = str(e).lower()
            if "413" in msg or "too large" in msg or "too many" in msg:
                chunk = max(500, chunk // 2)
                continue
            raise
        current = end + 1

    return all_logs


def fetch_pool_daily_volume(
    w3: Web3,
    pool_addr: str,
    d0: int,
    d1: int,
    price0: float,
    price1: float,
) -> float:
    """
    Sum the USD value of all Swap events in the pool over the last 24h.

    Uses one side of each swap (whichever token has a non-zero price)
    to avoid double-counting.
    """
    latest = w3.eth.block_number
    from_block = max(0, latest - BLOCKS_PER_DAY)

    print(f"  Scanning swaps from block {from_block:,} to {latest:,} "
          f"({latest - from_block:,} blocks) ...")

    logs = _fetch_swap_logs(w3, pool_addr, from_block, latest)
    print(f"  Found {len(logs):,} Swap events in last 24h")

    total_volume = 0.0
    for log in logs:
        data = log["data"]
        if isinstance(data, bytes):
            raw = data
        else:
            raw = bytes.fromhex(data[2:] if data.startswith("0x") else data)

        amount0 = int.from_bytes(raw[0:32], "big", signed=True)
        amount1 = int.from_bytes(raw[32:64], "big", signed=True)

        if price0 > 0:
            total_volume += abs(amount0) / 10**d0 * price0
        elif price1 > 0:
            total_volume += abs(amount1) / 10**d1 * price1

    return total_volume


def fetch_competing_liquidity_usd(
    w3: Web3,
    pool_addr: str,
    d0: int,
    d1: int,
    price0: float,
    price1: float,
) -> float:
    """
    Read pool.liquidity() and slot0(), then convert the active liquidity
    at the current tick to a USD value via virtual reserves.

    pool.liquidity() already reflects concentration: a tight-range LP
    contributes more L per dollar than a wide-range LP.
    """
    pool = w3.eth.contract(
        address=Web3.to_checksum_address(pool_addr), abi=POOL_ABI
    )
    L = pool.functions.liquidity().call()
    slot0 = pool.functions.slot0().call()
    sqrt_price_x96 = slot0[0]

    if sqrt_price_x96 == 0 or L == 0:
        return 0.0

    # Virtual reserves at the current price point
    sqrt_price = sqrt_price_x96 / (2**96)
    virtual_amount0 = L / sqrt_price          # raw units of token0
    virtual_amount1 = L * sqrt_price          # raw units of token1

    amount0_human = virtual_amount0 / 10**d0
    amount1_human = virtual_amount1 / 10**d1

    return amount0_human * price0 + amount1_human * price1


def fetch_pool_fee_percent(pool_contract) -> float:
    """Read the pool's fee and return it as a percentage (e.g. 0.05)."""
    try:
        fee_raw = pool_contract.functions.fee().call()
        return fee_raw / 10_000  # 500 → 0.05%, 3000 → 0.30%
    except Exception:
        print("  [!] Could not read fee() from pool, defaulting to 0.05%")
        return 0.05


# ═════════════════════════════════════════════════════════════════════════════
# Simulation logic
# ═════════════════════════════════════════════════════════════════════════════

def calculate_multiplier(range_percent: float) -> float:
    """
    Capital efficiency multiplier for a symmetric range around the current price.
    A ±5% range ≈ 10x capital efficiency vs full-range V2 liquidity.
    """
    x = range_percent / 100.0
    if x >= 1.0:
        return 1.0
    return 1.0 / (1.0 - math.sqrt((1.0 - x) / (1.0 + x)))


def run_simulation(
    investment: float,
    range_percent: float,
    daily_volume: float,
    fee_tier_percent: float,
    competing_liquidity: float,
    token0_symbol: str = "token0",
    token1_symbol: str = "token1",
) -> None:
    print("\n" + "=" * 60)
    print(" Aerodrome Slipstream LP Simulator (Live Data)")
    print("=" * 60)

    multiplier = calculate_multiplier(range_percent)
    effective_liquidity = investment * multiplier

    total_active_liquidity = effective_liquidity + competing_liquidity
    our_share = effective_liquidity / total_active_liquidity if total_active_liquidity > 0 else 0

    daily_total_fees = daily_volume * (fee_tier_percent / 100.0)
    daily_profit = daily_total_fees * our_share

    print(f"  Pool:                      {token0_symbol}/{token1_symbol}")
    print(f"  Fee Tier:                  {fee_tier_percent}%")
    print("-" * 60)
    print("INVESTMENT DATA:")
    print(f"  Actual Investment:         ${investment:,.2f}")
    print(f"  Target Price Range:        +/-{range_percent}%")
    print(f"  Efficiency Multiplier:     {multiplier:.2f}x")
    print(f"  Effective Liquidity:       ${effective_liquidity:,.2f} (V2 Equivalent Power)")
    print("-" * 60)
    print("MARKET DATA (LIVE FROM CHAIN):")
    print(f"  Pool Daily Volume (24h):   ${daily_volume:,.2f}")
    print(f"  Total Fees Generated:      ${daily_total_fees:,.2f}")
    print(f"  Competing Liquidity:       ${competing_liquidity:,.2f}")
    print(f"  Your Share of Range:       {our_share * 100:.4f}%")
    print("-" * 60)
    print("EARNINGS PROJECTION:")
    print(f"  Daily Profit:              ${daily_profit:,.2f}")
    print(f"  Monthly Profit (est.):     ${(daily_profit * 30):,.2f}")
    print(f"  Yearly Profit (est.):      ${(daily_profit * 365):,.2f}")

    apr = ((daily_profit * 365) / investment) * 100 if investment > 0 else 0
    print(f"  Annual Percentage Rate:    {apr:,.2f}% APR")
    print("=" * 60 + "\n")


# ═════════════════════════════════════════════════════════════════════════════
# Entry point
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    if not POOL_ADDRESS:
        print("ERROR: Set POOL_ADDRESS in .env or at the top of this file.")
        print("  Find pool addresses at https://aerodrome.finance or on BaseScan.")
        sys.exit(1)

    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    if not w3.is_connected():
        print(f"ERROR: Cannot connect to RPC at {RPC_URL}")
        sys.exit(1)

    pool = w3.eth.contract(
        address=Web3.to_checksum_address(POOL_ADDRESS), abi=POOL_ABI
    )

    # ── Read pool metadata ────────────────────────────────────────────────
    print("Connecting to pool ...")
    token0_addr = pool.functions.token0().call()
    token1_addr = pool.functions.token1().call()
    d0 = _get_token_decimals(w3, token0_addr)
    d1 = _get_token_decimals(w3, token1_addr)
    sym0 = _get_token_symbol(w3, token0_addr)
    sym1 = _get_token_symbol(w3, token1_addr)
    print(f"  Pool: {sym0}/{sym1}  (decimals: {d0}/{d1})")

    # ── Fetch current token prices ────────────────────────────────────────
    print("Fetching token prices from DeFiLlama ...")
    price0 = _get_current_price_usd(token0_addr)
    price1 = _get_current_price_usd(token1_addr)
    print(f"  {sym0}: ${price0:,.4f}")
    print(f"  {sym1}: ${price1:,.4f}")

    if price0 == 0 and price1 == 0:
        print("ERROR: Could not fetch prices for either token. Cannot compute USD values.")
        sys.exit(1)

    # ── Fee tier ──────────────────────────────────────────────────────────
    fee_pct = fetch_pool_fee_percent(pool)
    print(f"  Fee tier: {fee_pct}%")

    # ── 24h trading volume ────────────────────────────────────────────────
    print("Fetching 24h trading volume ...")
    daily_volume = fetch_pool_daily_volume(w3, POOL_ADDRESS, d0, d1, price0, price1)

    # ── Competing liquidity ───────────────────────────────────────────────
    print("Reading active liquidity from pool ...")
    competing_liq = fetch_competing_liquidity_usd(w3, POOL_ADDRESS, d0, d1, price0, price1)
    print(f"  Active liquidity (USD): ${competing_liq:,.2f}")

    # ── Run simulation ────────────────────────────────────────────────────
    run_simulation(
        investment=MY_INVESTMENT,
        range_percent=MY_RANGE_PERCENT,
        daily_volume=daily_volume,
        fee_tier_percent=fee_pct,
        competing_liquidity=competing_liq,
        token0_symbol=sym0,
        token1_symbol=sym1,
    )
