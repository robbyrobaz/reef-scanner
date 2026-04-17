"""
Configuration for Reef Scanner
Loads from .env file
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── API Keys ──────────────────────────────────────────────────────────
HELIUS_API_KEY=os.getenv("HELIUS_API_KEY", "")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
TELEGRAM_BOT_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Helius API ────────────────────────────────────────────────────────
HELIUS_BASE_URL = "https://api.helius.xyz/v0"
HELIUS_RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"

# ── Output ────────────────────────────────────────────────────────────
DATA_DIR = "./data"
LOGS_DIR = "./logs"
SIGNAL_OUTPUT_FILE = f"{DATA_DIR}/signals.csv"
WALLET_DB_FILE = f"{DATA_DIR}/wallets.csv"
SCAN_LOG_FILE = f"{LOGS_DIR}/scanner.log"

# ── Copy Trading Settings ────────────────────────────────────────────
COPY_TRADE_ENABLED = False        # Global kill switch (BE CAREFUL)
COPY_ENGINE_INTERVAL_S = 5       # Poll every N seconds
COPY_MIN_ALLOC_SOL = 0.001       # Min SOL per copy trade
COPY_MAX_ALLOC_SOL = 10.0        # Max SOL per copy trade
COPY_PRIORITY_FEE_LAMPORTS = 50_000  # 0.00005 SOL — minimum to actually land txs (zero-fee txs were dropped from mempool Apr 17)
COPY_CONFIG_FILE = f"{DATA_DIR}/copy_config.json"
COPY_TRADES_FILE = f"{DATA_DIR}/copy_trades.csv"
KEYPAIR_FILE = f"{DATA_DIR}/keypair.json"  # User's trading wallet keypair

# ── Scanner Settings ───────────────────────────────────────────────────
# Wallets to scan (add your seed list here)
# Get wallets from: GMGN.ai, Solscan, Birdeye, or any DeFi aggregator
# Format: "SolanaAddressAsBase58String"
WALLETS_TO_SCAN = [
    # Solana Foundation wallet (active on-chain)
    "CxD7ATxbP6uGxkNTypPNc5M2CoVpoVmVnAqGt8TtBta",
    # Raydium Treasury wallet
    "4N6bQ9oGynYWNZjhD6aQXpXcSLNUHNAGaD75gFoJTE85",
]

# ── Ranking Filters ─────────────────────────────────────────────────
# Min total trades to qualify for ranking (prevents 1-2 trade flukes)
MIN_TRADES = 5

# Min win rate to qualify (0.0 - 1.0)
# 60% = solid winner, 80% = exceptional
MIN_WIN_RATE = 0.60

# Min timespan in hours (first to last swap) to qualify
# Filters out same-session flukes; 6h = half a day of activity
MIN_SPAN_HOURS = 6.0

# Min avg ROI per trade to qualify (0.0 = any profit, 0.1 = 10% avg)
MIN_AVG_ROI = 0.0

# Bot detection: avg gap < this many seconds → flag as potential bot
# 0 = disabled, 5 = very fast bot, 60 = any sub-minute auto-trader
BOT_GAP_THRESHOLD_S = 5

# Activity window in days (for recency scoring only)
ACTIVITY_WINDOW_DAYS = 30

# Min trades in window to be considered (legacy, still used for discovery)
MIN_TRADES_30D = 2

# ── Scoring Weights ───────────────────────────────────────────────────
WEIGHT_WIN_RATE = 0.30
WEIGHT_AVG_ROI = 0.10
WEIGHT_TRADE_FREQ = 0.15
WEIGHT_RECENCY = 0.10
WEIGHT_PF = 0.20   # profit factor — heavily weighted

# ── Minimum Thresholds ───────────────────────────────────────────────
MIN_PF = 2.0       # minimum profit factor — filters out losing wallets

# ── RPC Fallbacks ─────────────────────────────────────────────────────
# Used when Helius RPC is rate-limited (HTTP 429).  Tested 2026-04-13.
# publicnode is listed first: returns getTransaction data ~300ms after a
# processed WS notification; mainnet-beta sometimes returns null at that point.
PUBLIC_RPC_ENDPOINTS = [
    "https://solana.publicnode.com",
    "https://api.mainnet-beta.solana.com",
]
