"""
Reef Copy Trading Engine

Monitors target wallets and copies their trades.
Run: python copy_engine.py

Uses polling mode (no webhooks yet) for simplicity.
"""

import asyncio
import csv
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

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
    DATA_DIR,
)
from copy_config import load_copy_config, save_copy_config, CopyConfig, CopyEntry
from swap_parser import parse_transaction_for_swaps, ParsedSwap


DRY_RUN = True  # True = log only, no actual trades


# ── RPC Helpers ──────────────────────────────────────────────────────────

async def get_signatures_for_address(
    address: str,
    before: Optional[str] = None,
    limit: int = 100,
) -> List[dict]:
    """Get signatures for a wallet address"""
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            params = {"commitment": "confirmed", "limit": limit}
            if before:
                params["before"] = before
            async with session.post(
                HELIUS_RPC_URL,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
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
    """Get a parsed transaction"""
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.post(
                HELIUS_RPC_URL,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getTransaction",
                    "params": [
                        sig,
                        {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0},
                    ],
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json()
                return data.get("result")
    except Exception as e:
        print(f"    ⚠️  RPC error getting tx: {e}")
        return None


# ── Copy Trade Log ────────────────────────────────────────────────────────

@dataclass
class CopyTrade:
    """Record of a copied trade"""
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
    status: str = "pending"  # pending, confirmed, failed
    error: str = ""


def load_copy_trades() -> List[CopyTrade]:
    """Load copy trade history from CSV"""
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
            ))
    return trades


def save_copy_trade(trade: CopyTrade) -> None:
    """Append a copy trade to CSV"""
    file_exists = os.path.exists(COPY_TRADES_FILE)
    with open(COPY_TRADES_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "timestamp", "source_wallet", "source_sig", "our_wallet",
            "our_sig", "action", "token_mint", "amount_sol",
            "scaled_amount_sol", "source_price_sol", "our_price_sol",
            "status", "error",
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
        })


# ── Core Engine ──────────────────────────────────────────────────────────

async def check_wallet_for_new_trades(
    wallet_addr: str,
    entry: CopyEntry,
    our_wallet: str,
) -> List[CopyTrade]:
    """Check a wallet for new trades since last_sig"""
    new_trades = []

    # Get signatures newer than last_sig
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

    # Process oldest first
    new_sigs = list(reversed(new_sigs))

    for sig_info in new_sigs:
        sig = sig_info["signature"]
        tx = await get_transaction(sig)
        if not tx:
            continue

        # Parse for DEX swaps
        swaps = parse_transaction_for_swaps(tx)
        if not swaps:
            continue

        for swap in swaps:
            trade = CopyTrade(
                timestamp=int(time.time()),
                source_wallet=wallet_addr,
                source_sig=sig,
                our_wallet=our_wallet,
                action=swap.action,
                token_mint=swap.token_mint,
                amount_sol=swap.amount_sol,
                scaled_amount_sol=min(entry.alloc_sol, swap.amount_sol),
                source_price_sol=swap.price_sol,
            )

            if DRY_RUN:
                print(f"    🐸 DRY RUN: would copy {swap.action} {swap.amount_sol:.4f} SOL "
                      f"({entry.alloc_sol:.4f} SOL allocated) of token {swap.token_mint[:16]}...")
                trade.status = "dry_run"
            else:
                # Real execution
                result = await execute_copy_trade(trade)
                if result:
                    trade.status = "confirmed"
                else:
                    trade.status = "failed"

            save_copy_trade(trade)
            new_trades.append(trade)

    return new_trades


async def execute_copy_trade(trade: CopyTrade) -> bool:
    """
    Execute a copy trade.
    Returns True if successful, False otherwise.
    
    This is the critical path — needs to be FAST.
    """
    try:
        import aiohttp

        # TODO: Build and sign the actual swap transaction
        # For pump.fun: use the swap instruction from pump.fun program
        # For raydium/orca: use their swap instruction
        #
        # Key steps:
        # 1. Get user's wallet keypair from environment/keystore
        # 2. Build swap instruction with scaled amount
        # 3. Add priority fee
        # 4. Send transaction via Helius
        # 5. Wait for confirmation

        print(f"    ⚠️  execute_copy_trade() not yet implemented — "
              f"need user keypair + swap instruction builder")
        return False

    except Exception as e:
        print(f"    ❌ Copy trade failed: {e}")
        trade.error = str(e)
        return False


async def run_engine_cycle(config: CopyConfig) -> int:
    """Run one cycle of the copy engine. Returns number of trades copied."""
    if not config.global_enabled:
        return 0

    enabled_copies = {addr: e for addr, e in config.copies.items() if e.enabled}
    if not enabled_copies:
        return 0

    total_copied = 0

    for wallet_addr, entry in enabled_copies.items():
        try:
            trades = await check_wallet_for_new_trades(
                wallet_addr, entry, config.user_wallet
            )
            total_copied += len(trades)

            # Update last_sig for this wallet
            if trades:
                latest_sig = trades[-1].source_sig
                entry.last_sig = latest_sig
                entry.last_copy_ts = int(time.time())

        except Exception as e:
            print(f"    ⚠️  Error checking {wallet_addr[:16]}...: {e}")

    if total_copied > 0:
        save_copy_config(config)

    return total_copied


async def run_engine():
    """Main loop — run engine cycle every N seconds"""
    print("=" * 60)
    print("🏄 Reef Copy Trading Engine")
    print("=" * 60)
    print(f"   Dry run: {DRY_RUN}")
    print(f"   Poll interval: {COPY_ENGINE_INTERVAL_S}s")
    print(f"   Config: {COPY_CONFIG_FILE}")
    print()

    # Check config
    config = load_copy_config()
    if not config.user_wallet:
        print("⚠️  No user wallet set! Run: python copy_config.py --set-wallet <ADDRESS>")
        print("   Or use the dashboard at http://localhost:8891")
    else:
        print(f"   User wallet: {config.user_wallet[:16]}...")

    enabled_count = sum(1 for e in config.copies.values() if e.enabled)
    print(f"   Enabled copies: {enabled_count}")
    print(f"   Global enabled: {config.global_enabled}")
    print()

    while True:
        try:
            config = load_copy_config()
            if config.global_enabled and enabled_count > 0:
                copied = await run_engine_cycle(config)
                if copied > 0:
                    print(f"  ✅ Copied {copied} trade(s)")
            else:
                if not config.global_enabled:
                    pass  # Silent idle
        except Exception as e:
            print(f"  ❌ Engine error: {e}")

        await asyncio.sleep(COPY_ENGINE_INTERVAL_S)


if __name__ == "__main__":
    if "--dry-run" not in sys.argv:
        DRY_RUN = True

    if "--live" in sys.argv:
        DRY_RUN = False
        print("⚠️  LIVE MODE — real trades will be executed!")

    asyncio.run(run_engine())
