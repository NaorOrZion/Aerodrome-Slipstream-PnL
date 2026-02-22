"""
Event log decoders for NFPM and Gauge contracts.

Single Responsibility: turn raw log dicts into typed Python tuples.
No network calls, no filtering — pure data transformation.
"""

from typing import Any, Optional

from web3 import Web3


# ── Topic hashes (computed once) ─────────────────────────────────────────────

def _topic_hex(signature: str) -> str:
    return "0x" + Web3.keccak(text=signature).hex()


INCREASE_LIQUIDITY_TOPIC = _topic_hex("IncreaseLiquidity(uint256,uint128,uint256,uint256)")
DECREASE_LIQUIDITY_TOPIC = _topic_hex("DecreaseLiquidity(uint256,uint128,uint256,uint256)")
COLLECT_TOPIC = _topic_hex("Collect(uint256,address,uint256,uint256)")
CLAIM_REWARDS_TOPIC = _topic_hex("ClaimRewards(address,uint256)")
TRANSFER_TOPIC = _topic_hex("Transfer(address,address,uint256)")


# ── Helpers ──────────────────────────────────────────────────────────────────

def normalize_topic(t: Any) -> str:
    """Return topic as a '0x'-prefixed hex string for safe comparison."""
    if t is None:
        return ""
    h = t.hex() if hasattr(t, "hex") else str(t)
    return h if h.startswith("0x") else "0x" + h


def topic_match(log_topic: Any, expected_hex: str) -> bool:
    return normalize_topic(log_topic).lower() == expected_hex.lower()


def address_to_topic(addr: str) -> str:
    """Pad an address to a 32-byte indexed topic."""
    return "0x" + addr.lower().replace("0x", "").zfill(64)


def _raw_data(log: dict) -> bytes:
    data = log.get("data") or b""
    if isinstance(data, str) and data.startswith("0x"):
        data = bytes.fromhex(data[2:])
    return data


# ── NFPM decoder ─────────────────────────────────────────────────────────────

# Return types:
#   IncreaseLiquidity / DecreaseLiquidity -> (event_name, tokenId, amount0, amount1, blockNumber)
#   Collect -> (event_name, tokenId, amount0, amount1, blockNumber, recipient)

NfpmEvent = tuple  # lightweight — keeps the module dependency-free


def decode_nfpm_log(log: dict) -> Optional[NfpmEvent]:
    """Decode a single NFPM log into a named tuple-like plain tuple."""
    topics = log.get("topics") or []
    if not topics:
        return None

    topic0 = normalize_topic(topics[0])
    data = _raw_data(log)
    block = log["blockNumber"]

    if topic0.lower() == INCREASE_LIQUIDITY_TOPIC.lower():
        token_id = int(topics[1].hex(), 16)
        # ABI: each non-indexed param occupies a 32-byte slot
        amount0 = int.from_bytes(data[32:64], "big")
        amount1 = int.from_bytes(data[64:96], "big")
        return ("IncreaseLiquidity", token_id, amount0, amount1, block)

    if topic0.lower() == DECREASE_LIQUIDITY_TOPIC.lower():
        token_id = int(topics[1].hex(), 16)
        amount0 = int.from_bytes(data[32:64], "big")
        amount1 = int.from_bytes(data[64:96], "big")
        return ("DecreaseLiquidity", token_id, amount0, amount1, block)

    if topic0.lower() == COLLECT_TOPIC.lower():
        token_id = int(topics[1].hex(), 16)
        recipient = "0x" + data[12:32].hex()[-40:]
        amount0 = int.from_bytes(data[32:64], "big")
        amount1 = int.from_bytes(data[64:96], "big")
        return ("Collect", token_id, amount0, amount1, block, recipient)

    return None


# ── Gauge decoder ────────────────────────────────────────────────────────────

GaugeEvent = tuple  # (user, amount, block_number, tx_hash)


def decode_gauge_log(log: dict) -> Optional[GaugeEvent]:
    """Decode a Gauge ClaimRewards log."""
    topics = log.get("topics") or []
    if len(topics) < 2:
        return None

    user = "0x" + topics[1].hex()[-40:]
    data = _raw_data(log)
    amount = int.from_bytes(data[:32], "big") if len(data) >= 32 else 0
    tx_hash = log.get("transactionHash")
    if isinstance(tx_hash, bytes):
        tx_hash = tx_hash.hex()
    return (user, amount, log["blockNumber"], tx_hash)
