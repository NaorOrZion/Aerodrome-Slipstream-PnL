"""
Token-ID ownership resolution and transaction-involvement checks.

Single Responsibility: decide whether a given NFPM event belongs to the
wallet being analyzed.  Two strategies are combined:

  1. **Direct ownership** — ownerOf(tokenId) == ADDRESS or
     positions(tokenId).operator == ADDRESS at to_block.
  2. **Staked ownership** — ownerOf returns a known Gauge address, AND
     the transaction itself involves ADDRESS (tx.from, Collect recipient,
     or an ERC20/ERC721 Transfer to/from ADDRESS in the receipt).
"""

from typing import Any, Optional, Set

from web3 import Web3

from .decoder import TRANSFER_TOPIC, normalize_topic, topic_match


# ── ownerOf / operator resolution ────────────────────────────────────────────

def resolve_token_ownership(
    nfpm_contract: Any,
    token_ids: set,
    block: int,
    address: str,
    gauge_addresses: list[str],
) -> tuple[set, set]:
    """
    For each *token_id*, call ownerOf and positions().operator at *block*.

    Returns
    -------
    (owned_or_operated, staked)
        owned_or_operated : tokenIds directly owned or operated by ADDRESS.
        staked            : tokenIds whose owner is a Gauge address.
    """
    addr_lower = address.lower()
    gauge_lower = _normalize_address_set(gauge_addresses)

    owned_or_operated: Set[int] = set()
    staked: Set[int] = set()

    for tid in token_ids:
        try:
            owner = nfpm_contract.functions.ownerOf(tid).call(block_identifier=block)
            owner_lower = (owner or "").lower()

            if owner_lower == addr_lower:
                owned_or_operated.add(tid)
                continue

            if owner_lower in gauge_lower:
                staked.add(tid)
                continue

            pos = nfpm_contract.functions.positions(tid).call(block_identifier=block)
            operator = (pos[1] or "").lower()
            if operator == addr_lower:
                owned_or_operated.add(tid)
        except Exception:
            pass

    return owned_or_operated, staked


# ── Transaction involvement ──────────────────────────────────────────────────

def tx_involves_address(
    receipt: Optional[dict],
    tx: Optional[dict],
    address_lower: str,
    collect_recipients_in_tx: set,
) -> bool:
    """
    Return True if the transaction interacts with *address_lower*:

    * tx.from == address
    * A Collect event in that tx has recipient == address
    * Any ERC20/ERC721 Transfer in the receipt has from/to == address
    """
    if tx and (tx.get("from") or "").lower() == address_lower:
        return True

    if collect_recipients_in_tx and address_lower in {
        r.lower() for r in collect_recipients_in_tx
    }:
        return True

    if not receipt or not receipt.get("logs"):
        return False

    for log in receipt["logs"]:
        topics = log.get("topics") or []
        if len(topics) < 3:
            continue
        if not topic_match(topics[0], TRANSFER_TOPIC):
            continue
        from_addr = _topic_to_address(topics[1])
        if from_addr == address_lower:
            return True

    return False


# ── Helpers (private) ────────────────────────────────────────────────────────

def _normalize_address_set(addresses: list[str]) -> set[str]:
    out: set[str] = set()
    for a in addresses:
        if not a:
            continue
        a = a.strip().lower()
        if not a.startswith("0x"):
            a = "0x" + a
        out.add(a)
    return out


def _topic_to_address(t: Any) -> str:
    h = t.hex() if hasattr(t, "hex") else str(t)
    if h.startswith("0x"):
        h = h[2:]
    return ("0x" + h[-40:]).lower()
