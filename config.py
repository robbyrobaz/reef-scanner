"""
Configuration for Reef Scanner
Loads from .env file
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── API Keys ──────────────────────────────────────────────────────────
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Helius API ────────────────────────────────────────────────────────
HELIUS_BASE_URL = "https://api.helius.xyz/v0"
HELIUS_RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"

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

# Min trades in window to be considered
MIN_TRADES_30D = 3

# Min win rate to qualify (0.0 - 1.0)
MIN_WIN_RATE = 0.50

# Min avg ROI per trade to qualify (0.0 = any profit, 0.1 = 10% avg)
MIN_AVG_ROI = 0.0

# Activity window in days
ACTIVITY_WINDOW_DAYS = 30

# ── Scoring Weights ───────────────────────────────────────────────────
WEIGHT_WIN_RATE = 0.40
WEIGHT_AVG_ROI = 0.30
WEIGHT_TRADE_FREQ = 0.20
WEIGHT_RECENCY = 0.10

# ── Output ────────────────────────────────────────────────────────────
DATA_DIR = "./data"
LOGS_DIR = "./logs"
SIGNAL_OUTPUT_FILE = f"{DATA_DIR}/signals.csv"
WALLET_DB_FILE = f"{DATA_DIR}/wallets.csv"
SCAN_LOG_FILE = f"{LOGS_DIR}/scanner.log"

# ── RPC Fallbacks ─────────────────────────────────────────────────────
PUBLIC_RPC_ENDPOINTS = [
    "https://api.mainnet-beta.solana.com",
    "https://solana-api.projectserum.com",
    "https://rpc.ankr.com/solana",
]
