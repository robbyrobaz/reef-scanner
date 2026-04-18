# 🏄 Reef — Solana DEX Wallet Scanner

## 🎯 CRITICAL STRATEGY FACTS — READ FIRST

**This is a TAIL-DRIVEN whale-copy strategy. NOT grind-positive small-wins.**

- Top 5 live whales (on-chain verified 30d): **437 round-trips, 14 rips ≥10×, 8 rips ≥50×, max 630× ROI**.
- Rate: **~1 rip ≥10× per 31 trips**; avg trip ROI **+93% to +766%** per whale.
- **One 630× rip at 0.05 SOL basis = +31.5 SOL (~$7,500).** One per month = strategy wins.
- Paper bucket's rip counts (32 ≥10×, 10 ≥50×) were **~60% artifacts from wallet 84NXvzQM** — IGNORE, not real.
- Watch bucket's "+0.84 mSOL/sell grind" is the RETAIL-sniper pattern, NOT our whale strategy. Don't confuse them.

**What kills the strategy:**
1. Missing whale signals (WS shard drop, Helius 429, polling gap) → miss the rip → month's profits evaporated
2. Tail-capture ratio too low — source makes 630×, we make 3× because our 4s lag compresses the tail
3. NOT slip — measured 0.1-0.7% on 4 of 5 whales; their deep pools absorb our 0.01-0.05 SOL invisibly

**Key design choices (Apr 18 2026):**
- Proportional alloc: `min(0.05, max(0.01, source_sol × 0.001))` — conviction-weighted, cap at 0.05 SOL
- Slip gate: 5% (correct for these wallets — NEVER raise to "catch more" — random-whale large buys are −19% median)
- Orphan sweep every 30 min: auto-SELL untracked holdings + force-exit positions >6h old
- SELL-guard: skip LIVE SELL if we have no matching open (prevents "no balance" failures)

**Full analysis:** see `analysis/` directory — STRATEGY_REVIEW.md, MOONSHOT_AUDIT.md, LARGE_ORDER_BACKTEST.md.

---

**Automatically find profitable wallets trading on Solana DEXs.**

Scans recent blocks, discovers wallets making money, and saves them for monitoring/copy-trading.

---

## What It Does

1. **Scans blocks** via Helius RPC — finds DEX activity (Pump.fun, Jupiter, Raydium, Orca)
2. **Extracts swaps** — parses buy/sell events with amounts and prices
3. **Discovers wallets** — identifies who's trading profitably
4. **Scores & saves** — ranks wallets by win rate, ROI, and frequency
5. **Auto-purges** — removes wallets inactive for >30 days

---

## Architecture

```
reef-workspace/
├── scanner.py        # Main: block scanner, wallet discovery, orchestration
├── swap_parser.py    # Parse DEX swaps from raw Solana transactions
├── models.py         # WalletMetrics dataclass
├── config.py         # API keys, thresholds, file paths
├── requirements.txt  # Dependencies
├── .env              # API keys (NOT committed)
├── data/             # Output CSVs (created at runtime)
└── venv/             # Python virtual environment
```

**Data Flow:**
```
Blocks (Helius RPC) → scanner.py → swap_parser.py → Wallet metrics → wallets.csv
                                       ↓
                              swaps.csv (raw data)
```

---

## Setup

```bash
cd /home/rob/reef-workspace

# Install dependencies
venv/bin/pip install aiohttp python-dotenv

# Add API key to .env
echo "HELIUS_API_KEY=your_key_here" > .env
```

Get a free Helius API key at https://dev.helius.xyz (100k credits/month)

---

## Run

```bash
# Manual run
venv/bin/python scanner.py

# Or with SCANNER_MODE env var (future)
SCANNER_MODE=discover venv/bin/python scanner.py
```

**Cron job** — runs every 5 minutes via Hermes MCP:
```
mcp_cronjob (Hermes internal scheduler)
```

---

## Output Files

| File | Description |
|------|-------------|
| `data/wallets.csv` | Ranked wallet list with scores, win rates, ROI |
| `data/swaps.csv` | Raw swap history for all discovered trades |

**wallets.csv columns:**
- `address` — Solana wallet address
- `score` — weighted score (win rate + ROI + frequency + recency)
- `total_trades`, `win_rate`, `avg_roi`, `best_roi`, `worst_roi`
- `avg_hold_minutes` — average hold time
- `last_active` — ISO timestamp of most recent swap
- `favorite_token` — most-traded token
- `solscan_link` — link to view wallet on Solscan

---

## Configuration

Edit `config.py` to adjust thresholds:

```python
MIN_TRADES_30D = 3      # Min trades to qualify
MIN_WIN_RATE = 0.50      # Min win rate (50%)
MIN_AVG_ROI = 0.0       # Min avg ROI (0% = any profit)
ACTIVITY_WINDOW_DAYS = 30
```

---

## Supported DEXs

- **Pump.fun** — memecoin bonding curves (primary source)
- **Jupiter** — aggregator
- **Raydium** — AMM and CLMM
- **Orca** — Whirlpool
- **OpenBook** — order book
- **Phoenix** — DEX

---

## Architecture — Two Components

**1. Scanner (`scanner.py`)** — background research, populates `data/reef.db`:
- Runs every 5 minutes via Hermes MCP cron
- Scans recent Solana blocks, extracts swaps, ranks wallets by ROI/WR over 30d
- Used for wallet DISCOVERY + RANKING only (no trade detection, no execution)

**2. Copy engine (`copy_engine.py`)** — always-on live trader, systemd service `reef-copy-engine.service`:
- Subscribes to `logsSubscribe` WebSocket for 171 watched wallets (sharded across 3 connections, 80 per shard to avoid public RPC's 1013 cap)
- Primary RPC: **`wss://solana-rpc.publicnode.com`** (Helius plan exhausted Apr 17, not used anymore)
- PumpPortal WS as secondary source for pump.fun activity
- 5s polling fallback for gaps
- Consensus processor: buffers signals, fires copy within ~300-500ms of source
- Orphan sweep (every 30 min): auto-SELLs untracked holdings + force-exits positions >6h old
- Ghost sweep (every 20s): closes positions that appeared confirmed but orphaned
- Dashboard: `reef-dashboard.service` serves stats at http://omen-claw.tail76e7df.ts.net:8891/

**The scanner does NOT detect live trades — the copy engine's WebSockets do.**

---

## Limitations

- **Single scan = limited history** — Wallet ROI/win rate requires both buy AND sell. A single block scan may only catch one side of a trade.
- **Accumulate over time** — Running every 5 minutes builds trading history over hours/days.
- **Pump.fun dominant** — Currently ~100% of activity is Pump.fun during memecoin season.
- ~~No auto-copier yet~~ — FULLY IMPLEMENTED. Copy engine runs 24/7, 5 whales live, 166 on watch (Apr 18 2026).

---

## Future Phases

- [ ] **Monitor** — Real-time WebSocket alerts when target wallets trade
- [ ] **Signals** — Push to Discord/Telegram
- [ ] **Copier** — Auto-execute trades
- [ ] **Better ROI calc** — Track buy/sell pairs across multiple scanner runs

---

## Tech Stack

| Component | Tool |
|-----------|------|
| RPC (HTTP) | **QuickNode** (trial, 10M credits/mo, 15 req/s) → publicnode → mainnet-beta fallback. Env `QUICKNODE_RPC_URL`. |
| RPC (WebSocket) | `wss://solana-rpc.publicnode.com` — NOT QuickNode (QN rate-limits subscribes at 15/s which would kill our 171-wallet sub flood) |
| Archival tx lookup | QuickNode (publicnode prunes anything >~1h) |
| Async | `asyncio` + `aiohttp` |
| Data | CSV files |
| Scheduling | Hermes MCP cron |

---

## Author

Rob / Reef — 2026
