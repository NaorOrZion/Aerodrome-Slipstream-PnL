"""
RPC helpers: Web3 connections, chunked log fetching with 413 fallback.

Single Responsibility: all raw blockchain I/O (getLogs, getTransaction,
getTransactionReceipt, getBlock) lives here.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, List, Optional

from web3 import Web3

from .config import FALLBACK_RPC_URL, LOG_CHUNK_SIZE, RPC_URL


# ── Web3 providers ───────────────────────────────────────────────────────────

def create_web3(url: str) -> Web3:
    return Web3(Web3.HTTPProvider(url))


def create_providers() -> tuple[Web3, Optional[Web3]]:
    """Return (primary_w3, fallback_w3 | None)."""
    w3 = create_web3(RPC_URL)
    if not w3.is_connected():
        raise RuntimeError(f"Cannot connect to primary RPC: {RPC_URL}")

    fallback: Optional[Web3] = None
    if FALLBACK_RPC_URL and FALLBACK_RPC_URL.strip() and FALLBACK_RPC_URL != RPC_URL:
        fb = create_web3(FALLBACK_RPC_URL)
        if fb.is_connected():
            fallback = fb

    return w3, fallback


# ── Helpers ──────────────────────────────────────────────────────────────────

def block_hex(n: int) -> str:
    return hex(n)


def _is_413(e: Exception) -> bool:
    if e is None:
        return False
    if (
        hasattr(e, "response")
        and e.response is not None
        and getattr(e.response, "status_code", None) == 413
    ):
        return True
    msg = (str(e) + str(getattr(e, "__cause__", ""))).lower()
    return "413" in msg or "request entity too large" in msg or "entity too large" in msg


# ── Single request ───────────────────────────────────────────────────────────

def _get_logs_single(
    w3: Web3, params: dict, fallback_w3: Optional[Web3] = None
) -> list:
    try:
        return w3.eth.get_logs(params)
    except Exception as e:
        if not _is_413(e) or fallback_w3 is None:
            raise
        return fallback_w3.eth.get_logs(params)


def _fetch_chunk(
    w3: Web3,
    address: str,
    topics: list,
    from_b: int,
    to_b: int,
    fallback_w3: Optional[Web3] = None,
) -> list:
    params = {
        "fromBlock": block_hex(from_b),
        "toBlock": block_hex(to_b),
        "address": Web3.to_checksum_address(address),
        "topics": topics,
    }
    return _get_logs_single(w3, params, fallback_w3)


# ── Chunked log fetcher ─────────────────────────────────────────────────────

def get_logs_chunked(
    w3: Web3,
    address: str,
    topics: list,
    from_block: int,
    to_block: int,
    fallback_w3: Optional[Web3] = None,
    retry_single_topics: Optional[List[list]] = None,
):
    """
    Yield logs in chunks of LOG_CHUNK_SIZE blocks.

    On HTTP 413:
      1. If retry_single_topics is provided, retry the same chunk with one
         request per topic in parallel, then merge results.
      2. Otherwise halve the chunk size and retry.
    """
    current = from_block
    chunk = LOG_CHUNK_SIZE
    addr = Web3.to_checksum_address(address)

    while current <= to_block:
        end = min(current + chunk - 1, to_block)
        try:
            logs = _fetch_chunk(w3, addr, topics, current, end, fallback_w3)
        except Exception as e:
            if not _is_413(e):
                raise
            if retry_single_topics and len(retry_single_topics) > 1:
                all_logs: List[list] = []
                with ThreadPoolExecutor(max_workers=len(retry_single_topics)) as ex:
                    futures = {
                        ex.submit(
                            _fetch_chunk, w3, addr, st, current, end, fallback_w3
                        ): st
                        for st in retry_single_topics
                    }
                    for fut in as_completed(futures):
                        all_logs.append(fut.result())
                logs = []
                for batch in all_logs:
                    logs.extend(batch)
                logs.sort(key=lambda l: (l.get("blockNumber", 0), l.get("logIndex", 0)))
            elif chunk > 50:
                chunk = max(50, chunk // 2)
                continue
            else:
                raise

        yield from logs
        chunk = LOG_CHUNK_SIZE
        current = end + 1


# ── Transaction helpers ──────────────────────────────────────────────────────

def get_transaction(w3: Web3, tx_hash: Any) -> Optional[dict]:
    try:
        return w3.eth.get_transaction(tx_hash)
    except Exception:
        return None


def get_transaction_receipt(w3: Web3, tx_hash: Any) -> Optional[dict]:
    try:
        return w3.eth.get_transaction_receipt(tx_hash)
    except Exception:
        return None


def get_block_timestamp(w3: Web3, block_number: int, cache: dict[int, int]) -> int:
    """Return Unix timestamp for *block_number*; results are cached in *cache*."""
    if block_number not in cache:
        block = w3.eth.get_block(block_number)
        cache[block_number] = block["timestamp"]
    return cache[block_number]
