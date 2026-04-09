"""
Reef Copy Trading Engine

Monitors target wallets and copies their trades in real-time.
Run: python copy_engine.py [--live]

Uses DRY_RUN mode by default. Pass --live to execute real trades.
Requires a keypair file (set KEYPAIR_FILE env or use data/keypair.json).
"""

import asyncio
import csv
import json
import os
import sys
import time
from pathlib import Path
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    HELIUS_API_KEY,
    HELIUS_RPC_URL,
    COPY_TRADE_ENABLED,
    COPY_ENGINE_INTERVAL_S,
    COPY_MIN_ALLOC_SOL,
    COPY_MAX_ALLOC_SOL,
    COPY_CONFIG_FILE,
    COPY_TRADES_FILE,
    COPY_PRIORITY_FEE_LAMPORTS,
    DATA_DIR,
)
from copy_config import load_copy_config, save_copy_config, CopyConfig, CopyEntry
from swap_parser import parse_transaction_for_swaps, ParsedSwap
from swap_executor import execute_swap_legacy, load_solana_keypair, DRY_RUN as EXECUTOR_DRY_RUN, SwapResult
from pumpfun_executor import execute_pumpfun_swap
from positions import (
    load_positions, save_positions, add_position_from_trade, reduce_position,
    get_positions_summary, refresh_positions,
    POSITIONS_FILE,
)

# ── Engine state ────────────────────────────────────────────────────────
DRY_RUN = True  # Default to safe mode
KEYPAIR_LOADED = None
POSITIONS: Dict[str, "Position"] = {}


# ── Paper Position Tracking (for realized PnL) ──────────────────────────────

PAPER_POSITIONS_FILE = Path(DATA_DIR) / "paper_positions.json"

def load_paper_positions() -> Dict[str, dict]:
    """Load open paper positions. Key = f'{source_wallet}:{token_mint}'"""
    if not PAPER_POSITIONS_FILE.exists():
        return {}
    try:
        with open(PAPER_POSITIONS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_paper_positions(positions: Dict[str, dict]) -> None:
    os.makedirs(os.path.dirname(PAPER_POSITIONS_FILE), exist_ok=True)
    with open(PAPER_POSITIONS_FILE, "w") as f:
        json.dump(positions, f)

def record_paper_trade_pnl(trade: "CopyTrade", positions: Dict[str, dict]) -> float:
    """Record a paper trade and compute realized PnL if closing a position.
    
    For BUY: record entry price in positions dict.
    For SELL: compute realized PnL from entry price, clear position.
    Returns realized PnL in SOL (0 if BUY or no position found).
    """
    key = f"{trade.source_wallet}:{trade.token_mint}"
    
    if trade.action == "BUY":
        # Record entry position
        positions[key] = {
            "entry_price": trade.source_price_sol,
            "scaled_amount": trade.scaled_amount_sol,
            "timestamp": trade.timestamp,
        }
        return 0.0
    
    elif trade.action == "SELL":
        if key not in positions:
            return 0.0
        pos = positions[key]
        entry_price = pos["entry_price"]
        scaled = pos["scaled_amount"]
        # Realized PnL: (sell_price - entry_price) * scaled_amount
        pnl = (trade.source_price_sol - entry_price) * scaled
        del positions[key]
        return pnl
    
    return 0.0


# ── Copy Trade Record ────────────────────────────────────────────────────

@dataclass
class CopyTrade:
    timestamp: int
    source_wallet: str
    source_sig: str
    our_wallet: str
    our_sig: str = ""
    action: str = ""          # BUY or SELL
    token_mint: str = ""
    amount_sol: float = 0.0
    scaled_amount_sol: float = 0.0
    source_price_sol: float = 0.0
    our_price_sol: float = 0.0
    status: str = "pending"   # pending, confirmed, failed, dry_run
    error: str = ""
    realized_pnl_sol: float = 0.0  # computed when a SELL closes a paper position


def load_copy_trades() -> List[CopyTrade]:
    trades = []
    if not os.path.exists(COPY_TRADES_FILE):
        return trades
    with open(COPY_TRADES_FILE, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            trades.append(CopyTrade(
                timestamp=int(row["timestamp"]),
                source_wallet=row["source_wallet"],
                source_sig=row["source_sig"],
                our_wallet=row["our_wallet"],
                our_sig=row.get("our_sig", ""),
                action=row["action"],
                token_mint=row["token_mint"],
                amount_sol=float(row["amount_sol"]),
                scaled_amount_sol=float(row["scaled_amount_sol"]),
                source_price_sol=float(row["source_price_sol"]),
                our_price_sol=float(row.get("our_price_sol", 0)),
                status=row["status"],
                error=row.get("error", ""),
                realized_pnl_sol=float(row.get("realized_pnl_sol", 0)),
            ))
    return trades


def save_copy_trade(trade: CopyTrade) -> None:
    file_exists = os.path.exists(COPY_TRADES_FILE)
    with open(COPY_TRADES_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "timestamp", "source_wallet", "source_sig", "our_wallet",
            "our_sig", "action", "token_mint", "amount_sol",
            "scaled_amount_sol", "source_price_sol", "our_price_sol",
            "status", "error", "realized_pnl_sol",
        ])
        if not file_exists:
            writer.writeheader()
        writer.writerow({
            "timestamp": trade.timestamp,
            "source_wallet": trade.source_wallet,
            "source_sig": trade.source_sig,
            "our_wallet": trade.our_wallet,
            "our_sig": trade.our_sig,
            "action": trade.action,
            "token_mint": trade.token_mint,
            "amount_sol": round(trade.amount_sol, 6),
            "scaled_amount_sol": round(trade.scaled_amount_sol, 6),
            "source_price_sol": round(trade.source_price_sol, 9),
            "our_price_sol": round(trade.our_price_sol, 9),
            "status": trade.status,
            "error": trade.error,
            "realized_pnl_sol": round(trade.realized_pnl_sol, 9),
        })


# ── RPC Helpers ──────────────────────────────────────────────────────────

async def get_signatures_for_address(
    address: str,
    before: Optional[str] = None,
    limit: int = 100,
) -> List[dict]:
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            params = {"commitment": "confirmed", "limit": limit}
            if before:
                params["before"] = before
            async with session.post(
                HELIUS_RPC_URL,
                json={
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getSignaturesForAddress",
                    "params": [address, params],
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json()
                return data.get("result", [])
    except Exception as e:
        print(f"    ⚠️  RPC error getting sigs: {e}")
        return []


async def get_transaction(sig: str) -> Optional[dict]:
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.post(
                HELIUS_RPC_URL,
                json={
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getTransaction",
                    "params": [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json()
                return data.get("result")
    except Exception as e:
        print(f"    ⚠️  RPC error getting tx: {e}")
        return None


# ── Swap Execution ───────────────────────────────────────────────────────

async def execute_copy_trade(trade: CopyTrade) -> bool:
    """
    Execute a copy trade via PumpPortal (pumpfun tokens).
    Falls back to Jupiter for graduated tokens.
    Returns True if successful.
    """
    global KEYPAIR_LOADED, POSITIONS
    
    if KEYPAIR_LOADED is None:
        KEYPAIR_LOADED = await load_solana_keypair()
    
    if KEYPAIR_LOADED is None:
        print(f"    ⚠️  No keypair — cannot execute real trades")
        return False
    
    SOL_MINT = "So11111111111111111111111111111111111111112"

    try:
        # Try PumpPortal first (bonding curve tokens)
        result = await execute_pumpfun_swap(
            KEYPAIR_LOADED,
            trade.action.lower(),
            trade.token_mint,
            trade.scaled_amount_sol,
            slippage=15,
            priority_fee=0.005,
            pool="auto",
        )
        
        # If PumpPortal fails (e.g. token graduated off bonding curve), fall back to Jupiter
        if not result.success:
            print(f"    ⚠️  PumpPortal failed ({result.error[:80]}), trying Jupiter...")
            if trade.action == "BUY":
                result = await execute_swap_legacy(
                    KEYPAIR_LOADED, SOL_MINT, trade.token_mint,
                    trade.scaled_amount_sol, slippage_bps=200,
                )
            else:
                result = await execute_swap_legacy(
                    KEYPAIR_LOADED, trade.token_mint, SOL_MINT,
                    trade.scaled_amount_sol, slippage_bps=200,
                )
        
        if result.success:
            trade.our_sig = result.signature
            trade.our_price_sol = result.price_sol if result.price_sol > 0 else trade.source_price_sol
            return True
        else:
            trade.error = result.error
            return False
    
    except Exception as e:
        print(f"    ❌ Copy trade failed: {e}")
        trade.error = str(e)
        return False


# ── Core Engine ──────────────────────────────────────────────────────────

async def check_wallet_for_new_trades(
    wallet_addr: str,
    entry: CopyEntry,
    our_wallet: str,
    paper_positions: Dict[str, dict],
) -> List[CopyTrade]:
    """Check a wallet for new trades since last_sig"""
    global POSITIONS
    
    new_trades = []
    sigs = await get_signatures_for_address(wallet_addr, limit=10)
    if not sigs:
        return []

    # Filter to truly new signatures
    new_sigs = []
    for sig_info in sigs:
        sig = sig_info["signature"]
        if sig == entry.last_sig:
            break
        new_sigs.append(sig_info)

    if not new_sigs:
        return []

    new_sigs = list(reversed(new_sigs))  # Oldest first

    MAX_TRADE_AGE_S = 45  # skip trades older than 45s — pump.fun tokens die fast

    for sig_info in new_sigs:
        sig = sig_info["signature"]

        # Skip stale trades — by the time we detect them, the token is likely dead
        block_time = sig_info.get("blockTime") or 0
        trade_age_s = int(time.time()) - block_time
        if block_time and trade_age_s > MAX_TRADE_AGE_S:
            print(f"    ⏩ Skipping {sig[:20]}... — trade is {trade_age_s}s old (>{MAX_TRADE_AGE_S}s limit)")
            continue

        tx = await get_transaction(sig)
        if not tx:
            continue

        swaps = parse_transaction_for_swaps(tx)
        if not swaps:
            continue

        for swap in swaps:
            # Scale amount based on our allocation
            scale = min(1.0, entry.alloc_sol / max(swap.amount_sol, 0.0001))
            scaled_sol = round(swap.amount_sol * scale, 9)
            scaled_sol = max(COPY_MIN_ALLOC_SOL, min(COPY_MAX_ALLOC_SOL, scaled_sol))

            trade = CopyTrade(
                timestamp=int(time.time()),
                source_wallet=wallet_addr,
                source_sig=sig,
                our_wallet=our_wallet,
                action=swap.action,
                token_mint=swap.token_mint,
                amount_sol=swap.amount_sol,
                scaled_amount_sol=scaled_sol,
                source_price_sol=swap.price_sol,
            )

            # Pure copy: ALWAYS follow source wallet. If they buy, we buy. If they sell, we sell.
            if DRY_RUN:
                # Compute realized PnL for SELLs (closes open BUY position)
                trade.realized_pnl_sol = record_paper_trade_pnl(trade, paper_positions)
                print(f"    🐸 DRY RUN: copy {swap.action} {swap.amount_sol:.4f} SOL "
                      f"(alloc {entry.alloc_sol:.4f}) of {swap.token_mint[:16]}..."
                      + (f" | realized pnl: {trade.realized_pnl_sol:+.9f}" if trade.realized_pnl_sol != 0 else ""))
                trade.status = "dry_run"
                save_copy_trade(trade)
                new_trades.append(trade)
            else:
                success = await execute_copy_trade(trade)
                trade.status = "confirmed" if success else "failed"
                save_copy_trade(trade)
                new_trades.append(trade)

    return new_trades


async def run_engine_cycle(config: CopyConfig) -> int:
    """Run one cycle. Returns number of trades processed."""
    global POSITIONS
    
    if not config.global_enabled:
        return 0

    # Refresh positions from on-chain before checking
    if config.user_wallet:
        POSITIONS = await refresh_positions(POSITIONS, config.user_wallet)

    # Load paper positions for realized PnL tracking
    paper_positions = load_paper_positions()

    enabled_copies = {addr: e for addr, e in config.copies.items() if e.enabled}
    # Filter out self-copy (user's own wallet wouldn't generate new trades anyway,
    # but this also prevents the confusing skip message on every poll cycle)
    if config.user_wallet and config.user_wallet in enabled_copies:
        del enabled_copies[config.user_wallet]
    if not enabled_copies:
        return 0

    total = 0
    sigs_updated = False
    for wallet_addr, entry in enabled_copies.items():
        try:
            trades = await check_wallet_for_new_trades(
                wallet_addr, entry, config.user_wallet, paper_positions
            )
            total += len(trades)

            if trades:
                latest_sig = trades[-1].source_sig
                entry.last_sig = latest_sig
                entry.last_copy_ts = int(time.time())
                sigs_updated = True

        except Exception as e:
            print(f"    ⚠️  Error checking {wallet_addr[:16]}...: {e}")

        # Small delay between wallets to avoid Jupiter rate limits
        await asyncio.sleep(0.5)

    if total > 0 or sigs_updated:
        save_copy_config(config)
        save_paper_positions(paper_positions)

    return total


async def run_engine():
    global DRY_RUN, KEYPAIR_LOADED
    
    print("=" * 60)
    print("🏄 Reef Copy Trading Engine")
    print("=" * 60)
    
    # Load config first to get trade_mode
    config = load_copy_config()
    trade_mode = config.trade_mode
    
    # --live flag overrides config; otherwise use config
    cli_live = "--live" in sys.argv
    DRY_RUN = not cli_live and trade_mode != "live"
    
    # Sync DRY_RUN flag to all executors
    import swap_executor, pumpfun_executor
    swap_executor.DRY_RUN = DRY_RUN
    pumpfun_executor.DRY_RUN = DRY_RUN
    
    print(f"   Mode: {'🐸 DRY RUN (PAPER)' if DRY_RUN else '🔴 LIVE — REAL TRADES'}")
    if cli_live:
        print(f"   (CLI --live flag overrides config trade_mode={trade_mode})")
    print(f"   Poll interval: {COPY_ENGINE_INTERVAL_S}s")
    print()

    # Load positions
    POSITIONS = load_positions()
    print(f"   Loaded {len(POSITIONS)} positions from disk")

    # Try to load keypair from config path or default
    keypair_path = config.keypair_path if config.keypair_path else str(Path(DATA_DIR) / "keypair.json")
    KEYPAIR_LOADED = await load_solana_keypair(keypair_path)
    if KEYPAIR_LOADED:
        print(f"   Keypair: {KEYPAIR_LOADED.pubkey()} ({keypair_path})")
    else:
        print(f"   ⚠️  No keypair at {keypair_path}")
        if not DRY_RUN:
            print(f"   ⚠️  Cannot run LIVE without a keypair — downgrading to PAPER")

    if not config.user_wallet:
        print("   ⚠️  No user wallet set!")
    else:
        print(f"   User wallet: {config.user_wallet[:16]}...")

    enabled_count = sum(1 for e in config.copies.values() if e.enabled)
    print(f"   Enabled copies: {enabled_count}")
    print(f"   Global enabled: {config.global_enabled}")
    print()

    while True:
        try:
            config = load_copy_config()
            if config.global_enabled:
                copied = await run_engine_cycle(config)
                if copied > 0:
                    status = "🐸" if DRY_RUN else "✅"
                    print(f"  {status} {copied} trade(s) processed")
        except Exception as e:
            print(f"  ❌ Engine error: {e}")

        await asyncio.sleep(COPY_ENGINE_INTERVAL_S)


if __name__ == "__main__":
    asyncio.run(run_engine())
