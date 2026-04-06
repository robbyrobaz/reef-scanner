# 🏄 Reef — Solana DEX Wallet Scanner

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

## Cron / Scheduling

The scanner runs via Hermes MCP cron every 5 minutes:

```python
mcp_cronjob(action="create", 
            name="Reef DEX Scanner",
            schedule="every 5m",
            prompt="Run scanner.py...")
```

To manually trigger:
```python
mcp_cronjob(action="run", job_id="ec5dfb8ad7f6")
```

---

## Limitations

- **Single scan = limited history** — Wallet ROI/win rate requires both buy AND sell. A single block scan may only catch one side of a trade.
- **Accumulate over time** — Running every 5 minutes builds trading history over hours/days.
- **Pump.fun dominant** — Currently ~100% of activity is Pump.fun during memecoin season.
- **No auto-copier yet** — Only wallet discovery, no trade execution.

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
| RPC | Helius (free tier) |
| Async | `asyncio` + `aiohttp` |
| Data | CSV files |
| Scheduling | Hermes MCP cron |

---

## Author

Rob / Reef — 2026
