# Reef API Batching Strategy

How we batch and optimize API calls for maximum efficiency across all Reef components.

## APIs We Hit

| API | Endpoint | Used By | Rate Limits |
|-----|----------|---------|-------------|
| **Helius RPC** | `mainnet.helius-rpc.com` | Scanner, Copy Engine, Retro Scan | Paid plan, generous |
| **Helius Enhanced** | `api.helius.xyz/v0` | Retro Scan (batch tx fetch) | Paid plan |
| **Jupiter v1** | `api.jup.ag/swap/v1` | Swap Executor (quote + swap) | Public, moderate |
| **Jupiter Price** | `api.jup.ag/price/v2` | Swap Executor (token prices) | Public, generous |
| **PumpPortal** | `pumpportal.fun/api/trade-local` | PumpFun Executor | Public, generous |
| **Public RPCs** | `api.mainnet-beta.solana.com` etc. | Retro Scan (fallback) | Very limited |

## Batching Techniques

### 1. Helius Batch Transaction API (retro_scan.py)

**The big win.** Instead of fetching transactions one-by-one with `getTransaction`, we use Helius's batch endpoint which accepts up to 100 tx signatures per call and returns enriched (pre-parsed) data.

```
POST https://api.helius.xyz/v0/transactions?api-key=KEY
Body: {"transactions": ["sig1", "sig2", ..., "sig100"]}
```

**Impact:** Scanning 3000 transactions goes from ~3000 RPC calls to ~30 batch calls. That's a 100x reduction in HTTP round trips.

**Chunking:** We split signature lists into chunks of 100, with a 50ms sleep between chunks to avoid hammering the endpoint.

```python
for i in range(0, len(sigs), 100):
    batch = sigs[i:i+100]
    resp = await session.post(url, json={"transactions": batch})
    await asyncio.sleep(0.05)
```

**Bonus:** Helius enriched format gives us pre-parsed `tokenTransfers`, `nativeTransfers`, `type` (SWAP, TRANSFER, etc.), and `feePayer` — so we skip raw instruction parsing entirely.

### 2. Block-Level Scanning (scanner.py)

Instead of checking individual wallets, the scanner fetches entire blocks with `getBlock` and extracts ALL swap activity in a single pass.

```
getBlock(slot, {encoding: "jsonParsed", transactionDetails: "full"})
```

**Why this is efficient:**
- One RPC call per block gets ALL transactions (hundreds or thousands)
- We filter for DEX program IDs locally (zero extra API calls)
- Wallets are discovered as a side effect, not a separate scan

**Flow:**
1. `getSlot` → current finalized slot (1 call)
2. `getBlock` × 30 blocks = 30 calls
3. Parse locally → extract wallets + swaps (0 calls)

Total: ~31 RPC calls to discover hundreds of wallets and their swaps.

### 3. Signature Pagination (retro_scan.py, copy_engine.py)

`getSignaturesForAddress` returns up to 1000 sigs per call. We paginate using the `before` cursor:

```python
while sigs_collected < max_sigs:
    sigs = await get_signatures_for_address(program_id, before=cursor, limit=1000)
    cursor = sigs[-1]["signature"]  # oldest = next page cursor
```

**Efficiency:** 1000 sigs per call means 3 calls to get 3000 signatures. Combined with batch tx fetch (technique #1), a full wallet deep-scan uses ~33 total API calls instead of ~3003.

### 4. Copy Engine Polling (copy_engine.py)

The copy engine polls each tracked wallet every 5 seconds with a small signature window:

```python
sigs = await get_signatures_for_address(wallet_addr, limit=10)
```

**Why limit=10:** We only need to detect NEW trades since last check. Most polls return 0-2 new sigs. This keeps each poll to 1 RPC call per wallet.

**Sequential with delay:** Wallets are checked one at a time with 500ms gaps to stay under Jupiter rate limits when executing copy trades:

```python
for wallet_addr, entry in enabled_copies.items():
    trades = await check_wallet_for_new_trades(wallet_addr, entry, ...)
    await asyncio.sleep(0.5)
```

### 5. Swap Execution Pipeline (swap_executor.py, pumpfun_executor.py)

Each swap requires a fixed sequence — can't be parallelized because each step depends on the previous:

**PumpPortal path (3 calls):**
1. `POST pumpportal.fun/api/trade-local` → get unsigned tx
2. Sign locally (0 calls)
3. `POST helius RPC sendTransaction` → submit signed tx

**Jupiter path (4 calls):**
1. `GET jupiter/quote` → get quote
2. `POST jupiter/swap` → get unsigned tx
3. `POST helius RPC getLatestBlockhash` → fresh blockhash
4. `POST helius RPC sendTransaction` → submit signed tx

**Fallback strategy:** Try PumpPortal first (fewer calls, works for bonding curve tokens). If it 400s (graduated token), fall back to Jupiter.

### 6. RPC Fallback Chain (retro_scan.py)

For non-Helius calls, we cascade through public RPCs:

```python
PUBLIC_RPC_ENDPOINTS = [
    "https://api.mainnet-beta.solana.com",
    "https://solana-api.projectserum.com",
    "https://rpc.ankr.com/solana",
]
```

Each call tries Helius first, then falls through public endpoints. This gives us resilience without extra cost.

## Rate Limiting

| Where | Strategy | Delay |
|-------|----------|-------|
| Block scanning | Sleep between blocks | 50ms |
| Batch tx fetch | Sleep between 100-tx chunks | 50ms |
| Signature pagination | Sleep between pages | 50ms |
| Copy engine wallets | Sleep between wallet checks | 500ms |
| Retro scan phases | Sleep between phases | 100-500ms |

All sleeps use `asyncio.sleep()` — non-blocking, keeps the event loop responsive.

## Call Budget Per Cycle

### Scanner (every 5 min via cron)
- `getSlot`: 1 call
- `getBlock` × 30: 30 calls
- Local parsing: 0 calls
- DB writes: local only
- **Total: ~31 RPC calls per scan**

### Copy Engine (every 5 seconds)
- Per wallet: 1 `getSignaturesForAddress` + 0-2 `getTransaction`
- Per trade execution: 3-4 calls (PumpPortal or Jupiter)
- With 5 tracked wallets, quiet cycle: 5 calls
- With 5 tracked wallets, active cycle: ~15-25 calls
- **Total: 5-25 RPC calls per cycle**

### Retro Scan (on-demand)
- Signature collection: ~3 calls per 3000 sigs
- Batch tx fetch: ~30 calls per 3000 txs
- **Total: ~33 RPC calls per 3000 transactions**

## Key Design Decisions

1. **Helius batch API is the single biggest optimization.** Going from 1-call-per-tx to 100-tx-per-call makes deep scanning viable on a paid-but-not-enterprise plan.

2. **Block scanning > wallet scanning for discovery.** One `getBlock` call gets ALL activity vs. calling `getSignaturesForAddress` per wallet.

3. **PumpPortal before Jupiter** saves 1 API call per swap and works better for new tokens still on bonding curves.

4. **Enriched Helius format** saves us from parsing raw instructions — `type: "SWAP"` and `tokenTransfers[]` are pre-extracted.

5. **Small polling windows** (limit=10) for copy trading keeps the per-cycle cost constant regardless of how many historical trades a wallet has.

6. **DuckDB for local state** means zero external database calls — all analytics, ranking, and history queries happen locally with zero network overhead.
