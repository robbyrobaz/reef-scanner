"""
Reef PumpSwap AMM Executor — executes trades on pump.fun's graduated AMM.

Uses the AL-THE-BOT-FATHER/pump_swap_py SDK (vendored locally) to build and
send Solana transactions directly to the pAMMBay6... program.

This handles tokens that have graduated from the bonding curve to pump.fun's
native AMM — these are NOT supported by PumpPortal /api/trade-local or Jupiter.

Runs sync SDK calls in a thread-pool executor to avoid blocking the async loop.
"""

import asyncio
import os
import sys
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import HELIUS_RPC_URL
from solders.keypair import Keypair

# Helius key exhausted Apr 17 — use publicnode as primary RPC
RPC_URL = "https://solana.publicnode.com"

DRY_RUN = True

# Cache pool addresses: mint → pair_address (avoid repeated getProgramAccounts calls)
_pool_cache: dict[str, str] = {}


@dataclass
class PumpSwapResult:
    success: bool
    signature: str = ""
    error: str = ""
    dex: str = "pumpswap"


def _get_sync_client():
    """Build a synchronous solana.rpc.api.Client using the Helius RPC URL."""
    from solana.rpc.api import Client
    return Client(RPC_URL)


def _find_pool_sync(mint: str) -> Optional[str]:
    """Synchronous pool lookup — runs in thread pool."""
    if mint in _pool_cache:
        return _pool_cache[mint]
    try:
        from pool_utils import fetch_pair_from_rpc
        client = _get_sync_client()
        pair = fetch_pair_from_rpc(client, mint)
        if pair:
            _pool_cache[mint] = pair
        return pair
    except Exception as e:
        print(f"    ⚠️  PumpSwap pool lookup failed for {mint[:16]}...: {e}")
        return None


def _buy_sync(keypair: Keypair, pair_address: str, sol_in: float, slippage: int) -> bool:
    """Synchronous buy — runs in thread pool."""
    from pump_swap import buy
    client = _get_sync_client()
    return buy(
        client=client,
        payer_keypair=keypair,
        pair_address=pair_address,
        sol_in=sol_in,
        slippage=slippage,
        unit_budget=200_000,
        unit_price=500_000,
    )


def _sell_sync(keypair: Keypair, pair_address: str, percentage: int, slippage: int) -> bool:
    """Synchronous sell — runs in thread pool."""
    from pump_swap import sell
    client = _get_sync_client()
    return sell(
        client=client,
        payer_keypair=keypair,
        pair_address=pair_address,
        percentage=percentage,
        slippage=slippage,
        unit_budget=200_000,
        unit_price=500_000,
    )


async def execute_pumpswap(
    keypair: Keypair,
    action: str,        # "buy" or "sell"
    token_mint: str,
    amount_sol: float,
    slippage: int = 15,
    pool_address: str = "",  # known pool address from tx parsing (skips getProgramAccounts)
) -> PumpSwapResult:
    """
    Execute a pump-amm swap asynchronously (runs sync SDK in thread pool).

    For BUY:  spends amount_sol SOL to buy token_mint.
    For SELL: sells 100% of held token_mint (amount_sol ignored).

    pool_address: if provided, used directly (extracted from source wallet's tx).
                  if empty, falls back to getProgramAccounts lookup (slow, may fail).
    """
    if DRY_RUN:
        print(f"    🐸 DRY RUN (PumpSwap): would {action} {amount_sol:.4f} SOL of {token_mint[:16]}...")
        return PumpSwapResult(success=True, signature="DRY_RUN")

    loop = asyncio.get_event_loop()

    # Step 1: find pool address (use passed address if available, else look it up)
    if pool_address:
        pair = pool_address
        _pool_cache[token_mint] = pair  # cache for future sells
    else:
        pair = await loop.run_in_executor(None, _find_pool_sync, token_mint)
    if not pair:
        return PumpSwapResult(success=False, error=f"No pump-amm pool found for {token_mint[:16]}...")

    print(f"    🔵 PumpSwap {action.upper()} {token_mint[:16]}... pool={pair[:16]}...")

    # Step 2: execute swap — retry up to 3 times. Public RPC intermittently
    # fails on get_account_info (pool keys) and get_token_accounts_by_owner
    # (creator vault); the SDK silently returns None → aborts. Retrying recovers.
    try:
        ok = False
        last_err = ""
        for attempt in range(3):
            if action.lower() == "buy":
                ok = await asyncio.wait_for(
                    loop.run_in_executor(None, _buy_sync, keypair, pair, amount_sol, slippage),
                    timeout=60,
                )
            else:
                ok = await asyncio.wait_for(
                    loop.run_in_executor(None, _sell_sync, keypair, pair, 100, slippage),
                    timeout=60,
                )
            if ok:
                break
            last_err = f"SDK returned False on attempt {attempt+1}/3"
            if attempt < 2:
                print(f"    🔁 PumpSwap retry {attempt+1}/3 after silent SDK abort")
                await asyncio.sleep(1.0)

        if ok:
            return PumpSwapResult(success=True, signature="confirmed")
        else:
            return PumpSwapResult(success=False, error=last_err or "PumpSwap tx failed")

    except asyncio.TimeoutError:
        return PumpSwapResult(success=False, error="PumpSwap tx timed out after 60s")
    except Exception as e:
        return PumpSwapResult(success=False, error=str(e))
