"""
Centralized configuration for the Slipstream PnL analyzer.

All user-configurable values (addresses, block range, API settings) live here.
"""

import os
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

# ── RPC ──────────────────────────────────────────────────────────────────────
RPC_URL: str = os.getenv("QUICKNODE_BASE_ENDPOINT") or "https://mainnet.base.org"
FALLBACK_RPC_URL: str = os.getenv("FALLBACK_RPC_URL") or "https://mainnet.base.org"

# ── Wallet ───────────────────────────────────────────────────────────────────
ADDRESS: str = "0xCF979E05C91450e1FB5d98139101F0EFcd934d07"

# ── Contracts ────────────────────────────────────────────────────────────────
NFPM_ADDRESS: str = "0x827922686190790b37229fd06084350e74485b72"
GAUGE_ADDRESSES: list[str] = [
    "0xF33a96b5932D9E9B9A0eDA447AbD8C9d48d2e0c8",
]
AERO_TOKEN_ADDRESS: str = "0x940181a94A35A4569E4529A3CDfB74e38FD98631"
WETH_BASE_ADDRESS: str = "0x4200000000000000000000000000000000000006"

# ── Block range ──────────────────────────────────────────────────────────────
FROM_BLOCK: Optional[int] = 42487585
TO_BLOCK: Optional[int] = 42487587 	  # None = latest

# ── Log fetching ─────────────────────────────────────────────────────────────
LOG_CHUNK_SIZE: int = 100

# ── DeFiLlama pricing ───────────────────────────────────────────────────────
DEFILLAMA_API_KEY: Optional[str] = os.getenv("DEFILLAMA_API_KEY")
DEFILLAMA_CHAIN: str = "base"

# ── Simulation ────────────────────────────────────────────────────────────────
POOL_ADDRESS: str = os.getenv("POOL_ADDRESS", "")

# ── Debug ────────────────────────────────────────────────────────────────────
DEBUG: bool = os.getenv("TRACE_AERO_DEBUG", "").strip().lower() in ("1", "true", "yes")
