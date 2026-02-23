"""
Aerodrome Slipstream LP PnL Analyzer — orchestration layer.

Coordinates every step of the analysis pipeline:
  1. Fetch NFPM & Gauge logs
  2. Resolve token-ID ownership
  3. Filter events to the target wallet
  4. Compute USD-denominated PnL metrics
  5. Print summary
"""

from collections import defaultdict
from decimal import Decimal
from typing import Optional

from web3 import Web3

from .abi import ERC20_DECIMALS_ABI, GAUGE_ABI, NFPM_ABI
from .config import (
    ADDRESS,
    DEBUG,
    FROM_BLOCK,
    GAUGE_ADDRESSES,
    NFPM_ADDRESS,
    TO_BLOCK,
    TOKEN_IDS,
)
from .decoder import (
    CLAIM_REWARDS_TOPIC,
    COLLECT_TOPIC,
    DECREASE_LIQUIDITY_TOPIC,
    INCREASE_LIQUIDITY_TOPIC,
    decode_gauge_log,
    decode_nfpm_log,
)
from .ownership import resolve_token_ownership, tx_involves_address
from .pricing import aero_price_usd, eth_price_usd, token_amount_to_usd
from .rpc import (
    create_providers,
    get_logs_chunked,
    get_transaction,
    get_transaction_receipt,
)


# ═════════════════════════════════════════════════════════════════════════════
# Log fetchers (thin wrappers that bind contract addresses / topics)
# ═════════════════════════════════════════════════════════════════════════════

def _token_id_topic(tid: int) -> str:
    """Encode a tokenId as a 32-byte hex topic for eth_getLogs filtering."""
    return "0x" + hex(tid)[2:].zfill(64)


def _fetch_nfpm_logs(
    w3: Web3, from_block: int, to_block: int, fallback: Optional[Web3]
) -> list:
    event_types = [INCREASE_LIQUIDITY_TOPIC, DECREASE_LIQUIDITY_TOPIC, COLLECT_TOPIC]

    if TOKEN_IDS:
        token_id_topics = [_token_id_topic(tid) for tid in TOKEN_IDS]
        topics_combined = [event_types, token_id_topics]
        retry_single = [
            [[et], token_id_topics] for et in event_types
        ]
    else:
        topics_combined = [event_types]
        retry_single = [[et] for et in event_types]

    logs = list(
        get_logs_chunked(
            w3,
            NFPM_ADDRESS,
            topics_combined,
            from_block,
            to_block,
            fallback,
            retry_single_topics=retry_single,
        )
    )
    logs.sort(key=lambda l: (l.get("blockNumber", 0), l.get("logIndex", 0)))
    return logs


def _fetch_gauge_logs(
    w3: Web3, from_block: int, to_block: int, fallback: Optional[Web3]
) -> list:
    out: list = []
    for addr in GAUGE_ADDRESSES:
        out.extend(
            get_logs_chunked(
                w3,
                Web3.to_checksum_address(addr),
                [[CLAIM_REWARDS_TOPIC]],
                from_block,
                to_block,
                fallback,
            )
        )
    return out


# ═════════════════════════════════════════════════════════════════════════════
# Token-ID helpers
# ═════════════════════════════════════════════════════════════════════════════

def _extract_token_ids(logs: list) -> set[int]:
    ids: set[int] = set()
    for log in logs:
        decoded = decode_nfpm_log(log)
        if decoded and len(decoded) >= 2:
            ids.add(decoded[1])
    return ids


def _resolve_token_pair(
    nfpm_contract, token_id: int, block: int
) -> tuple[str, str]:
    """Return (token0, token1) addresses for *token_id*."""
    try:
        pos = nfpm_contract.functions.positions(token_id).call(block_identifier=block)
        return (pos[2], pos[3])
    except Exception:
        return ("", "")


def _token_decimals(w3: Web3, token_addr: str, block: int) -> int:
    if not token_addr:
        return 18
    try:
        c = w3.eth.contract(
            address=Web3.to_checksum_address(token_addr),
            abi=ERC20_DECIMALS_ABI,
        )
        return c.functions.decimals().call(block_identifier=block)
    except Exception:
        return 18


# ═════════════════════════════════════════════════════════════════════════════
# Filtering pipeline
# ═════════════════════════════════════════════════════════════════════════════

def _build_tx_involvement_map(
    w3: Web3,
    logs_by_tx: dict[str, list],
    address_lower: str,
) -> dict[str, bool]:
    """For each unique tx, determine if it involves ADDRESS."""
    collect_recipients_by_tx: dict[str, set] = defaultdict(set)
    for tx_hash, logs in logs_by_tx.items():
        for log in logs:
            decoded = decode_nfpm_log(log)
            if decoded and decoded[0] == "Collect" and len(decoded) > 5:
                collect_recipients_by_tx[tx_hash].add(decoded[5])

    result: dict[str, bool] = {}
    for tx_hash in logs_by_tx:
        receipt = get_transaction_receipt(w3, tx_hash)
        tx_obj = get_transaction(w3, tx_hash)
        result[tx_hash] = tx_involves_address(
            receipt, tx_obj, address_lower, collect_recipients_by_tx.get(tx_hash, set())
        )
    return result


def _filter_owned_nfpm_logs(
    nfpm_logs: list,
    owned_or_operated: set[int],
    staked: set[int],
    involvement_map: dict[str, bool],
) -> tuple[list, set[int]]:
    """
    Keep only NFPM logs whose tokenId belongs to ADDRESS.

    Returns (filtered_logs, set_of_seen_token_ids).
    """
    filtered: list = []
    seen_ids: set[int] = set()

    for log in nfpm_logs:
        decoded = decode_nfpm_log(log)
        if not decoded or len(decoded) < 2:
            continue
        token_id = decoded[1]
        tx_hash = log["transactionHash"]
        tx_hash = tx_hash.hex() if isinstance(tx_hash, bytes) else tx_hash

        if token_id in owned_or_operated:
            filtered.append(log)
            seen_ids.add(token_id)
        elif token_id in staked and involvement_map.get(tx_hash, False):
            filtered.append(log)
            seen_ids.add(token_id)

    return filtered, seen_ids


def _compute_excluded_boundary_txs(
    logs: list,
    from_block: int,
    to_block: int,
) -> set[str]:
    """
    Identify tx hashes at boundary blocks that belong to adjacent
    rebalance cycles and should be excluded.

    Bot-driven rebalancing produces paired transactions in each block:
    the EXIT from the old range followed by the ENTRY into the new range.
    At boundary blocks only one of the two belongs to the tracked period:

    - FROM_BLOCK: the LAST tx is the entry into the tracked position,
      earlier txs are exits/entries from a previous cycle.
    - TO_BLOCK:   the FIRST tx is the exit from the tracked position,
      later txs are exits/entries from a subsequent cycle.
    """
    if from_block == to_block:
        return set()

    block_tx_order: dict[int, list[str]] = {}
    for log in logs:
        bn = log["blockNumber"]
        h = log["transactionHash"]
        h = h.hex() if isinstance(h, bytes) else h
        if bn not in block_tx_order:
            block_tx_order[bn] = []
        if h not in block_tx_order[bn]:
            block_tx_order[bn].append(h)

    excluded: set[str] = set()
    for bn, txs in block_tx_order.items():
        if len(txs) <= 1:
            continue
        if bn == from_block:
            excluded.update(txs[:-1])
        elif bn == to_block:
            excluded.update(txs[1:])
    return excluded


# ═════════════════════════════════════════════════════════════════════════════
# PnL computation
# ═════════════════════════════════════════════════════════════════════════════

class PnLResult:
    """Data container for all computed PnL metrics."""

    def __init__(self) -> None:
        self.net_liquidity_usd = Decimal("0")
        self.gross_deposited_usd = Decimal("0")
        self.gross_withdrawn_usd = Decimal("0")
        self.deposit_count = 0
        self.withdraw_count = 0
        self.fee_earned_usd = Decimal("0")
        self.aero_claimed_wei = 0
        self.aero_claimed_usd = Decimal("0")
        self.gas_cost_usd = Decimal("0")

    @property
    def rebalance_count(self) -> int:
        return min(self.deposit_count, self.withdraw_count)

    @property
    def net_profit_usd(self) -> Decimal:
        return self.fee_earned_usd + self.aero_claimed_usd - self.gas_cost_usd


class LiquidityResult:
    """Intermediate container for liquidity + fee computation."""

    def __init__(self) -> None:
        self.net_liquidity_usd = Decimal("0")
        self.gross_deposited_usd = Decimal("0")
        self.gross_withdrawn_usd = Decimal("0")
        self.deposit_count = 0
        self.withdraw_count = 0
        self.fee_usd = Decimal("0")


def _compute_liquidity_and_fees(
    w3: Web3,
    our_nfpm_logs: list,
    token_pairs: dict[int, tuple[str, str]],
    to_block: int,
) -> LiquidityResult:
    """
    Walk filtered NFPM logs and compute liquidity flows + fees.

    * Gross deposited = SUM of all IncreaseLiquidity in USD.
    * Gross withdrawn = SUM of all DecreaseLiquidity in USD.
    * Net liquidity   = gross_deposited − gross_withdrawn.
    * Fees            = Collect amounts minus same-tx DecreaseLiquidity (principal).
    """
    deposit_events: list[tuple[int, int, int, int]] = []
    withdraw_events: list[tuple[int, int, int, int]] = []
    tx_decrease: dict[str, dict[int, tuple[int, int]]] = defaultdict(dict)
    tx_collects: dict[str, list[tuple[int, int, int, int]]] = defaultdict(list)

    for log in our_nfpm_logs:
        decoded = decode_nfpm_log(log)
        if not decoded:
            continue
        tx_hash = log["transactionHash"]
        tx_hash = tx_hash.hex() if isinstance(tx_hash, bytes) else tx_hash

        name = decoded[0]
        if name == "IncreaseLiquidity":
            _, tid, a0, a1, blk = decoded
            deposit_events.append((tid, a0, a1, blk))
        elif name == "DecreaseLiquidity":
            _, tid, a0, a1, blk = decoded
            withdraw_events.append((tid, a0, a1, blk))
            tx_decrease[tx_hash][tid] = (a0, a1)
        elif name == "Collect":
            _, tid, a0, a1, blk, _ = decoded
            tx_collects[tx_hash].append((tid, a0, a1, blk))

    # Fee = Collect − same-tx Decrease (principal)
    fee_events: list[tuple[int, int, int, int]] = []
    for tx_hash, collects in tx_collects.items():
        dec_map = tx_decrease.get(tx_hash, {})
        for tid, a0, a1, blk in collects:
            p0, p1 = dec_map.get(tid, (0, 0))
            f0 = a0 - p0 if a0 >= p0 else 0
            f1 = a1 - p1 if a1 >= p1 else 0
            fee_events.append((tid, f0, f1, blk))

    def _value_events(events: list[tuple[int, int, int, int]], sign: int) -> Decimal:
        total = Decimal("0")
        for tid, a0, a1, blk in events:
            t0, t1 = token_pairs.get(tid, ("", ""))
            if t0:
                total += sign * token_amount_to_usd(w3, t0, blk, a0, _token_decimals(w3, t0, to_block))
            if t1:
                total += sign * token_amount_to_usd(w3, t1, blk, a1, _token_decimals(w3, t1, to_block))
        return total

    result = LiquidityResult()
    result.gross_deposited_usd = _value_events(deposit_events, 1)
    result.gross_withdrawn_usd = _value_events(withdraw_events, 1)  # positive value
    result.deposit_count = len(deposit_events)
    result.withdraw_count = len(withdraw_events)
    result.net_liquidity_usd = result.gross_deposited_usd - result.gross_withdrawn_usd
    result.fee_usd = _value_events(fee_events, 1)
    return result


def _compute_aero_rewards(
    w3: Web3,
    claim_events: list[tuple],
) -> tuple[int, Decimal]:
    """Return (total_wei, total_usd) for AERO ClaimRewards."""
    total_wei = sum(e[1] for e in claim_events)
    total_usd = Decimal("0")
    for _, amount, block, _ in claim_events:
        total_usd += aero_price_usd(w3, block) * (Decimal(amount) / Decimal(10**18))
    return total_wei, total_usd


def _compute_gas_costs(
    w3: Web3,
    our_nfpm_logs: list,
    claim_events: list[tuple],
    to_block: int,
) -> Decimal:
    """Sum gas for every unique tx that contains our NFPM or Gauge events."""
    tx_hashes: set[str] = set()
    for log in our_nfpm_logs:
        h = log["transactionHash"]
        tx_hashes.add(h.hex() if isinstance(h, bytes) else h)
    for _, _, _, tx_hash in claim_events:
        tx_hashes.add(tx_hash)

    gas_wei = 0
    for tx_hash in tx_hashes:
        receipt = get_transaction_receipt(w3, tx_hash)
        if not receipt:
            continue
        tx = get_transaction(w3, tx_hash)
        gas_used = receipt.get("gasUsed") or 0
        eff = receipt.get("effectiveGasPrice") or (tx or {}).get("gasPrice") or 0
        gas_wei += int(gas_used) * int(eff)

    return (Decimal(gas_wei) / Decimal(10**18)) * eth_price_usd(w3, to_block)


# ═════════════════════════════════════════════════════════════════════════════
# Output
# ═════════════════════════════════════════════════════════════════════════════

def _print_event_details(
    our_nfpm_logs: list,
    our_claims: list[tuple],
) -> None:
    """Print detailed per-event breakdown with transaction hashes."""
    print("\n" + "=" * 70)
    print("Matched Events")
    print("=" * 70)

    idx = 1
    for log in our_nfpm_logs:
        decoded = decode_nfpm_log(log)
        if not decoded:
            continue
        tx_hash = log["transactionHash"]
        tx_hash = tx_hash.hex() if isinstance(tx_hash, bytes) else tx_hash
        block = log["blockNumber"]

        name = decoded[0]
        tid = decoded[1]
        a0 = decoded[2]
        a1 = decoded[3]

        print(f"  [{idx}] {name:<22} tokenId={tid}  block={block}")
        print(f"      tx: {tx_hash}")
        detail = f"      amount0={a0}  amount1={a1}"
        if name == "Collect" and len(decoded) > 5:
            detail += f"  recipient={decoded[5]}"
        print(detail)
        idx += 1

    if our_claims:
        print("-" * 70)
        for claim in our_claims:
            user, amount, block, tx_hash = claim
            aero_human = Decimal(amount) / Decimal(10**18)
            print(f"  [{idx}] ClaimRewards         block={block}")
            print(f"      tx: {tx_hash}")
            print(f"      amount: {aero_human:.6f} AERO")
            idx += 1

    print("=" * 70)


def _print_summary(
    from_block: int,
    to_block: int,
    result: PnLResult,
) -> None:
    aero_tokens = Decimal(result.aero_claimed_wei) / Decimal(10**18)

    print("\n" + "=" * 60)
    print("Aerodrome Slipstream LP PnL Summary")
    print("=" * 60)
    print(f"Address:     {ADDRESS}")
    print(f"Block range: {from_block} -> {to_block}")
    print("-" * 60)
    print(f"1. Gross Deposited (USD):              {result.gross_deposited_usd:,.2f}  ({result.deposit_count} deposits)")
    print(f"   Gross Withdrawn (USD):              {result.gross_withdrawn_usd:,.2f}  ({result.withdraw_count} withdrawals)")
    print(f"   Net Liquidity Provided (USD):       {result.net_liquidity_usd:,.2f}")
    print(f"   Rebalances:                         {result.rebalance_count}")
    print(f"2. Total Trading Fees Earned (USD):    {result.fee_earned_usd:,.2f}")
    print(f"3. Total AERO Rewards Claimed:        {aero_tokens:.6f} AERO | USD: {result.aero_claimed_usd:,.2f}")
    print(f"4. Total Gas Fees Paid (USD):         {result.gas_cost_usd:,.2f}")
    print("-" * 60)
    print(f"   Net Profit (Fees + AERO - Gas) USD: {result.net_profit_usd:,.2f}")
    print("=" * 60 + "\n")


# ═════════════════════════════════════════════════════════════════════════════
# Main entry point
# ═════════════════════════════════════════════════════════════════════════════

def run_analysis() -> None:
    w3, fallback = create_providers()

    address = Web3.to_checksum_address(ADDRESS)
    address_lower = address.lower()
    from_block = FROM_BLOCK or 0
    to_block = TO_BLOCK or w3.eth.block_number

    nfpm = w3.eth.contract(
        address=Web3.to_checksum_address(NFPM_ADDRESS), abi=NFPM_ABI
    )

    # ── Step 1: Fetch raw logs ───────────────────────────────────────────
    nfpm_logs = _fetch_nfpm_logs(w3, from_block, to_block, fallback)
    gauge_logs = _fetch_gauge_logs(w3, from_block, to_block, fallback)

    # ── Step 2: Resolve token-ID ownership ───────────────────────────────
    unique_ids = _extract_token_ids(nfpm_logs)
    owned_or_operated, staked = resolve_token_ownership(
        nfpm, unique_ids, to_block, address, GAUGE_ADDRESSES
    )

    # ── Step 3: Determine per-tx involvement ─────────────────────────────
    logs_by_tx: dict[str, list] = defaultdict(list)
    for log in nfpm_logs:
        h = log["transactionHash"]
        h = h.hex() if isinstance(h, bytes) else h
        logs_by_tx[h].append(log)

    involvement_map = _build_tx_involvement_map(w3, logs_by_tx, address_lower)

    # ── Step 4: Filter NFPM logs to owned tokenIds ───────────────────────
    our_nfpm_logs, our_ids = _filter_owned_nfpm_logs(
        nfpm_logs, owned_or_operated, staked, involvement_map
    )

    if DEBUG:
        print("[DEBUG] NFPM logs in range:", len(nfpm_logs))
        print("[DEBUG] Unique tokenIds from those logs:", sorted(unique_ids))
        print("[DEBUG] TokenIds owned_or_operated:", sorted(owned_or_operated))
        print("[DEBUG] TokenIds staked (owner = Gauge):", sorted(staked))
        print("[DEBUG] Our NFPM logs (after filter):", len(our_nfpm_logs), "for tokenIds:", sorted(our_ids))
        for log in our_nfpm_logs:
            d = decode_nfpm_log(log)
            tx_h = log["transactionHash"]
            tx_h = tx_h.hex() if isinstance(tx_h, bytes) else tx_h
            if d:
                print(f"[DEBUG]   {d[0]} tokenId={d[1]} amt0={d[2]} amt1={d[3]} block={log['blockNumber']} tx={tx_h}")

    # ── Step 4b: Exclude boundary-block txs from adjacent cycles ────────
    excluded_txs = _compute_excluded_boundary_txs(
        our_nfpm_logs, from_block, to_block
    )
    if excluded_txs:
        our_nfpm_logs = [
            log for log in our_nfpm_logs
            if (log["transactionHash"].hex()
                if isinstance(log["transactionHash"], bytes)
                else log["transactionHash"])
            not in excluded_txs
        ]

    # ── Step 5: Filter Gauge ClaimRewards ────────────────────────────────
    our_claims = [
        decoded
        for log in gauge_logs
        if (decoded := decode_gauge_log(log))
        and decoded[0]
        and decoded[0].lower() == address_lower
        and decoded[3] not in excluded_txs
    ]

    # ── Step 6: Resolve token pairs for USD valuation ────────────────────
    all_ids_for_pricing: set[int] = set()
    for log in our_nfpm_logs:
        d = decode_nfpm_log(log)
        if d and len(d) >= 2:
            all_ids_for_pricing.add(d[1])

    token_pairs: dict[int, tuple[str, str]] = {}
    for tid in all_ids_for_pricing:
        token_pairs[tid] = _resolve_token_pair(nfpm, tid, to_block)

    # ── Step 7: Compute PnL metrics ──────────────────────────────────────
    pnl = PnLResult()
    liq = _compute_liquidity_and_fees(w3, our_nfpm_logs, token_pairs, to_block)
    pnl.net_liquidity_usd = liq.net_liquidity_usd
    pnl.gross_deposited_usd = liq.gross_deposited_usd
    pnl.gross_withdrawn_usd = liq.gross_withdrawn_usd
    pnl.deposit_count = liq.deposit_count
    pnl.withdraw_count = liq.withdraw_count
    pnl.fee_earned_usd = liq.fee_usd
    pnl.aero_claimed_wei, pnl.aero_claimed_usd = _compute_aero_rewards(w3, our_claims)
    pnl.gas_cost_usd = _compute_gas_costs(w3, our_nfpm_logs, our_claims, to_block)

    # ── Step 8: Print details and summary ────────────────────────────────
    _print_event_details(our_nfpm_logs, our_claims)
    _print_summary(from_block, to_block, pnl)
