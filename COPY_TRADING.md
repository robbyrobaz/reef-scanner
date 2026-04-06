# Reef Copy Trader — Design Spec

## Goals
- User picks wallets to copy from the ranked list
- Allocates SOL amount per wallet
- Toggles copy trading on/off per wallet
- Backend monitors target wallets and executes parallel trades for the user
- As fast as possible — sub-second detection + execution

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     USER DASHBOARD                      │
│  (FastAPI + HTMX, port 8891)                           │
│                                                         │
│  • My wallet address + balance                         │
│  • Tracked wallets list (from scanner)                  │
│  • Per-wallet: [x] copy │ 0.1 SOL │ [ON/OFF]         │
│  • Per-wallet P&L + trade history                      │
│  • Total allocation + status                            │
└────────────────────┬────────────────────────────────────┘
                     │ read
                     ▼
┌─────────────────────────────────────────────────────────┐
│                  COPY CONFIG FILE                        │
│              data/copy_config.json                       │
│                                                         │
│  {                                                      │
│    "user_wallet": "SolanaAddr...",                     │
│    "copies": {                                         │
│      "7VbHrtAvCb...": { "enabled": true, "alloc": 0.1},│
│      "EArQZbNCTQ...": { "enabled": false, "alloc": 0.2} │
│    }                                                   │
│  }                                                      │
└────────────────────┬────────────────────────────────────┘
                     │ read (poll or webhook trigger)
                     ▼
┌─────────────────────────────────────────────────────────┐
│              COPY TRADING ENGINE                         │
│              copy_engine.py (async)                     │
│                                                         │
│  • COPY_TRADE_ENABLED flag (global kill switch)         │
│  • Polls target wallet tx history every N seconds      │
│  • On new swap detected → execute scaled trade          │
│  • Proportional sizing: (user_alloc / target_sol) * tx  │
│  • Priority fees for speed                              │
│  • Write to data/copy_trades.csv                       │
└─────────────────────────────────────────────────────────┘
                     │
                     │ execute trades
                     ▼
┌─────────────────────────────────────────────────────────┐
│                  SOLANA RPC / HELIUS                   │
│                                                         │
│  • getSignaturesForAddress (poll)                       │
│  • sendTransaction (execute)                            │
│  • Priority fee in micro Lamports                       │
└─────────────────────────────────────────────────────────┘
```

---

## Copy Config File (data/copy_config.json)

```json
{
  "user_wallet": "SolanaAddress...",
  "global_enabled": false,
  "copies": {
    "WALLET_PUBKEY": {
      "enabled": true,
      "alloc_sol": 0.1,
      "last_sig": "...",
      "last_copy_ts": 1234567890
    }
  }
}
```

---

## Dashboard API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Dashboard HTML (HTMX) |
| GET | `/api/config` | Get copy config |
| POST | `/api/config` | Save copy config |
| GET | `/api/wallets` | Get tracked wallets (from scanner DB) |
| GET | `/api/wallet/:addr/history` | Get swap history for a wallet |
| GET | `/api/copy/history` | Get copy trade history |
| POST | `/api/wallet/:addr/copy` | Toggle copy on/off |
| POST | `/api/global_enable` | Global enable/disable |

---

## Copy Engine Logic

```
every N seconds (configurable, default 5s):
  for each wallet in copies where enabled=true:
    last_sig = copies[wallet].last_sig
    new_sigs = getSignaturesForAddress(wallet, until=last_sig)
    
    for sig in reversed(new_sigs):  # oldest first
      if sig == last_sig: break
      tx = getTransaction(sig)
      swap = parse_transaction_for_swaps(tx)
      if swap:
        execute_copy_trade(swap, copies[wallet].alloc_sol)
        copies[wallet].last_sig = sig
        copies[wallet].last_copy_ts = now
        save_config()
```

### execute_copy_trade(swap, user_alloc_sol):
1. Get swap details: token_mint, action (BUY/SELL), amount_sol, price_sol
2. Scale amount: if target traded `target_sol`, user allocated `user_alloc_sol`
   - scaled_sol = min(user_alloc_sol, user_balance * 0.95)
3. Build swap instruction for pump.fun/raydium
4. Add priority fee (0.001 SOL priority fee for fast execution)
5. Sign and send transaction
6. Log to copy_trades.csv

---

## UI Design

Dark theme, minimal, data-dense.

**Layout:**
- Header: "Reef Copy Trader" | Global [ENABLE] button (red/green)
- Row 1: User wallet | Balance | [Change Wallet] button
- Table: Tracked Wallets
  - Cols: Select | Address | Score | Win% | Trades | Alloc (input) | Status | P&L
  - Each row has ON/OFF toggle button
- Footer: Total allocated: X SOL | Copy engine: RUNNING/STOPPED

**States:**
- Green toggle = enabled (copying)
- Gray toggle = disabled
- Red pulsing = error/connection lost

---

## Implementation Priority

1. **copy_config.json** + reader/writer
2. **copy_engine.py** — polling engine, no execution yet (dry-run mode)
3. **Dashboard API** — FastAPI endpoints for config CRUD
4. **Dashboard UI** — HTMX page with wallet list + toggles
5. **execute_copy_trade()** — actual swap execution via Helius
6. **P&L tracking** — update copy_trades.csv with realized P&L
7. **Helius webhooks** — replace polling with push (faster)

---

## Key Config Params (config.py)

```python
COPY_TRADE_ENABLED = False        # Global kill switch
COPY_ENGINE_INTERVAL_S = 5        # Poll every N seconds
COPY_MIN_ALLOC_SOL = 0.001       # Min SOL per copy trade
COPY_MAX_ALLOC_SOL = 10.0        # Max SOL per copy trade
COPY_PRIORITY_FEE_LAMPORTS = 1_000_000  # 0.001 SOL priority
COPY_CONFIG_FILE = f"{DATA_DIR}/copy_config.json"
COPY_TRADES_FILE = f"{DATA_DIR}/copy_trades.csv"
```
