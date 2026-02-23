# Aerodrome Slipstream PnL Analyzer

Profit-and-loss tracker for **Aerodrome Slipstream** (concentrated liquidity) positions on **Base**.

Designed for wallets that use bot-driven rebalancing (e.g. Banana Gun), where `tx.from` is a router contract rather than the LP wallet itself.

---

## What It Tracks

| Metric | Description |
|---|---|
| **Gross Deposited** | Total USD value of all `IncreaseLiquidity` events (every deposit, at tx-time price) |
| **Gross Withdrawn** | Total USD value of all `DecreaseLiquidity` events (every withdrawal, at tx-time price) |
| **Net Liquidity** | Gross Deposited - Gross Withdrawn (how much net capital was added/removed) |
| **Rebalances** | Number of withdraw-then-redeposit cycles detected |
| **Trading Fees** | USD earned from `Collect` events minus the principal returned in the same tx |
| **AERO Rewards** | USD value of `ClaimRewards` from the Gauge contract |
| **Gas Fees** | Total gas cost in USD for all related transactions |
| **Net Profit** | Trading Fees + AERO Rewards - Gas Fees |

---

## How It Works

### Strategy Context

Aerodrome Slipstream uses concentrated liquidity (similar to Uniswap V3). Each position is an ERC-721 NFT with a `tokenId`, holding liquidity in a specific price range for a token pair (e.g. WETH/USDC). Positions can be **staked** in a Gauge to earn AERO rewards.

A rebalancing bot periodically:
1. Unstakes the NFT from the Gauge (claims AERO rewards)
2. Withdraws liquidity (`DecreaseLiquidity` + `Collect`)
3. Re-deposits with adjusted price range (`IncreaseLiquidity`)
4. Re-stakes the NFT in the Gauge

### Attribution Logic

Since a bot initiates the transactions, the script cannot simply filter by `tx.from`. Instead it:

1. **Fetches all NFPM events** (`IncreaseLiquidity`, `DecreaseLiquidity`, `Collect`) in the block range
2. **Extracts unique `tokenId`s** from those events
3. **Checks on-chain ownership** at the end block:
   - `ownerOf(tokenId)` — direct ownership
   - `positions(tokenId).operator` — delegated operator
   - If owner is a Gauge address — the NFT is staked
4. **For staked NFTs**, verifies the transaction involves the wallet by checking:
   - `tx.from` matches the wallet
   - `Collect` event recipient matches the wallet
   - An outbound `Transfer` in the receipt originates from the wallet (tokens sent to provide liquidity)
5. **Keeps only events** for tokenIds that belong to the wallet
6. **Filters boundary blocks** — bot rebalancing produces paired transactions per block (exit old range → enter new range). At the edges of the analysis window:
   - `FROM_BLOCK`: only the **last** transaction is kept (the entry into the tracked position)
   - `TO_BLOCK`: only the **first** transaction is kept (the exit from the tracked position)
   - Middle blocks are unaffected

### Fee Calculation

For each `Collect` event, the script subtracts the `DecreaseLiquidity` amounts from the same transaction (which represent returned principal). The remainder is the trading fee earned.

### Price Data

Historical USD prices at each block's timestamp are fetched from the [DeFiLlama API](https://defillama.com/docs/api) (free tier, no key required).

---

## Project Structure

```
slipstream_pnl/
├── __init__.py
├── __main__.py      # Entry point
├── config.py        # All configurable values (addresses, blocks, RPC)
├── abi.py           # Minimal ABI fragments for NFPM, Gauge, ERC20, Pool
├── rpc.py           # Web3 providers, chunked log fetching, tx helpers
├── pricing.py       # DeFiLlama historical price lookups + caching
├── decoder.py       # Raw log → typed tuple decoders
├── ownership.py     # TokenId ownership + tx-involvement checks
├── analyzer.py      # Orchestrates the full pipeline + prints summary
└── lp_simulation.py # LP fee simulator with live on-chain data
```

---

## Setup

### 1. Install dependencies

```bash
python -m venv .venv
.\venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure RPC

Create a `.env` file in the project root:

```env
QUICKNODE_BASE_ENDPOINT=https://your-base-rpc-url.com
```

If omitted, the public `https://mainnet.base.org` endpoint is used (rate-limited).

### 3. Set your wallet and contracts

Edit `config.py`:

```python
# Your LP wallet address
ADDRESS = "0xYourWalletAddress"

# Aerodrome SlipStream Non Fungible Position Manager on Base
NFPM_ADDRESS = "0x827922686190790b37229fd06084350e74485b72"

# Gauge address(es) for your pool(s)
GAUGE_ADDRESSES = ["0xYourGaugeAddress"]

# Block range to analyze
FROM_BLOCK = 42487586    # start block
TO_BLOCK = None          # None = latest block

# Your position's tokenId(s) — HIGHLY RECOMMENDED for large block ranges
TOKEN_IDS = [50564254]
```

#### How to find your Gauge address

1. Go to [BaseScan](https://basescan.org) and search your wallet address
2. Look for transactions with `ClaimRewards` events
3. The contract that emits `ClaimRewards` is your Gauge address

#### Why set TOKEN_IDS?

The NFPM contract is **global** — it handles every Slipstream position across every pool on Base, producing hundreds of events per block. Without `TOKEN_IDS`, the script fetches ALL events and filters client-side, which hits RPC response-size limits (HTTP 413) on any non-trivial block range.

When `TOKEN_IDS` is set, the RPC query filters by `tokenId` at the node level (it's an indexed event parameter), so only your position's events are returned. This makes large ranges (hours, days) feasible.

Find your tokenId in the `Matched Events` output from a short test run, or on BaseScan by inspecting your `IncreaseLiquidity` transactions.

---

## Usage

From the project root:

```bash
python -m Aerodrome-Slipstream-PnL
```

### Debug mode

Set in `.env` to see detailed pipeline output (log counts, tokenIds, filtered events):

```env
TRACE_AERO_DEBUG=1
```

### Optional: DeFiLlama Pro

For higher rate limits, set your API key:

```env
DEFILLAMA_API_KEY=your_key_here
```

---

## Example Output

```
======================================================================
Matched Events
======================================================================
  [1] IncreaseLiquidity      tokenId=50564254  block=42487586
      tx: 70487297d761079f6efadc4c03593294fd884f5edec1de93b0947f6e70f3a176
      amount0=264999999999999999954  amount1=77962613460
  [2] Collect                tokenId=50564254  block=42487586
      tx: 70487297d761079f6efadc4c03593294fd884f5edec1de93b0947f6e70f3a176
      amount0=0  amount1=0  recipient=0xcf979e05c91450e1fb5d98139101f0efcd934d07
  [3] Collect                tokenId=50564254  block=42487587
      tx: 114857f43235d0b2dd5ea000efe6b28bc46f6ac0d7894adaa5854ba3b0746977
      amount0=0  amount1=0  recipient=0xcf979e05c91450e1fb5d98139101f0efcd934d07
  [4] DecreaseLiquidity      tokenId=50564254  block=42487587
      tx: 114857f43235d0b2dd5ea000efe6b28bc46f6ac0d7894adaa5854ba3b0746977
      amount0=264999999999999999953  amount1=77962613459
  [5] Collect                tokenId=50564254  block=42487587
      tx: 114857f43235d0b2dd5ea000efe6b28bc46f6ac0d7894adaa5854ba3b0746977
      amount0=264999999999999999953  amount1=77962613459  recipient=0xcf979e05c91450e1fb5d98139101f0efcd934d07
----------------------------------------------------------------------
  [6] ClaimRewards         block=42487587
      tx: 114857f43235d0b2dd5ea000efe6b28bc46f6ac0d7894adaa5854ba3b0746977
      amount: 1.058016 AERO
======================================================================

============================================================
Aerodrome Slipstream LP PnL Summary
============================================================
Address:     0xCF979E05C91450e1FB5d98139101F0EFcd934d07
Block range: 42487586 -> 42487587
------------------------------------------------------------
1. Gross Deposited (USD):              598,414.20  (1 deposit)
   Gross Withdrawn (USD):              598,403.97  (1 withdrawal)
   Net Liquidity Provided (USD):       10.23
   Rebalances:                         1
2. Total Trading Fees Earned (USD):    0.00
3. Total AERO Rewards Claimed:        1.058016 AERO | USD: 0.34
4. Total Gas Fees Paid (USD):         0.01
------------------------------------------------------------
   Net Profit (Fees + AERO - Gas) USD: 0.33
============================================================
```

---

## LP Simulator (Live On-Chain Data)

A standalone simulator that pulls **real-time pool data** from the blockchain to project LP fee earnings.

### What it fetches automatically

| Data | Source |
|---|---|
| **24h Trading Volume** | Scans all `Swap` events over the last ~43,200 blocks (~24h on Base) |
| **Competing Liquidity** | Reads `pool.liquidity()` + `pool.slot0()` and converts to USD via virtual reserves |
| **Fee Tier** | Reads `pool.fee()` directly from the pool contract |
| **Token Prices** | Current USD prices from DeFiLlama |

### Setup

Set `POOL_ADDRESS` in your `.env` file:

```env
POOL_ADDRESS=0xYourSlipstreamPoolAddress
```

Find pool addresses at [aerodrome.finance](https://aerodrome.finance) or on [BaseScan](https://basescan.org).

### Run

```bash
python lp_simulation.py
```

Edit `MY_INVESTMENT` and `MY_RANGE_PERCENT` at the top of `lp_simulation.py` to match your strategy.

---

## Notes

- Without `TOKEN_IDS`, the script scans **all** NFPM events in the block range, then filters client-side. This is only viable for small ranges (a few blocks). For anything larger, set `TOKEN_IDS` in `config.py` to filter at the RPC level.
- **Boundary block filtering** handles bot rebalancing patterns where each block contains an exit-then-entry pair. At `FROM_BLOCK` and `TO_BLOCK`, only the relevant transaction from the tracked rebalance cycle is kept; events from adjacent cycles are excluded.
- Log fetching uses chunked requests (100 blocks per request) with automatic retry on HTTP 413 errors.
- Price and block-timestamp lookups are cached in memory to avoid redundant API/RPC calls within a single run.
- Gas costs are attributed to your wallet even when a bot pays the gas, since it is an operational cost of the LP strategy.
- The **Matched Events** section is always printed, showing each event's transaction hash, tokenId, block, and raw amounts for easy on-chain verification.
