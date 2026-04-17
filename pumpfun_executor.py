"""
Reef PumpFun Swap Executor — executes trades via PumpPortal Local Transaction API.

Uses pumpportal.fun/api/trade-local → sign with solders → send via Helius RPC.
No API key needed. Supports pump, raydium, pump-amm, auto pools.
"""

import asyncio
import base64
import json
import os
import sys
from dataclasses import dataclass
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import HELIUS_API_KEY, HELIUS_RPC_URL
# Helius key exhausted Apr 17 — use publicnode as primary RPC for tx submission.
# Restore to HELIUS_RPC_URL after topping up Helius.
RPC_URL = "https://solana.publicnode.com"
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
import aiohttp

DRY_RUN = True

PUMPPORTAL_API = "https://pumpportal.fun/api/trade-local"
SOL_MINT = "So11111111111111111111111111111111111111112"


@dataclass
class SwapResult:
    success: bool
    signature: str = ""
    error: str = ""
    input_amount: float = 0.0
    output_amount: float = 0.0
    price_sol: float = 0.0
    dex: str = "pumpfun"


async def execute_pumpfun_swap(
    keypair: Keypair,
    action: str,           # "buy" or "sell"
    token_mint: str,
    amount_sol: float,
    slippage: int = 15,
    priority_fee: float = 0.00005,
    pool: str = "auto",
) -> SwapResult:
    """Execute a swap via PumpPortal. Returns SwapResult."""
    if DRY_RUN:
        print(f"    🐸 DRY RUN: would {action} {amount_sol:.4f} SOL of {token_mint[:16]}...")
        return SwapResult(success=True, signature="DRY_RUN", input_amount=amount_sol, dex="pumpfun")

    try:
        async with aiohttp.ClientSession() as session:
            # Step 1: Get transaction from PumpPortal
            async with session.post(
                PUMPPORTAL_API,
                json={
                    "publicKey": str(keypair.pubkey()),
                    "action": action.lower(),
                    "mint": token_mint,
                    "amount": amount_sol,
                    "denominatedInSol": "true",
                    "slippage": slippage,
                    "priorityFee": priority_fee,
                    "pool": pool,
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    return SwapResult(success=False, error=f"PumpPortal {resp.status}: {text[:100]}")
                tx_bytes = await resp.read()

            if not tx_bytes or len(tx_bytes) < 50:
                return SwapResult(success=False, error="Empty tx from PumpPortal")

            # Step 2: Sign immediately (blockhash has ~60s lifetime)
            signed_tx = VersionedTransaction(
                VersionedTransaction.from_bytes(tx_bytes).message, [keypair]
            )

            # Step 3: Send via Helius RPC (processed commitment for speed)
            async with session.post(
                RPC_URL,
                json={
                    "jsonrpc": "2.0", "id": 1,
                    "method": "sendTransaction",
                    "params": [
                        base64.b64encode(bytes(signed_tx)).decode(),
                        {
                            "encoding": "base64",
                            "skipPreFlight": True,
                            "preflightCommitment": "processed",
                            "maxRetries": 5,
                        },
                    ],
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if "result" in data:
                        sig = data["result"]
                        print(f"    📤 {action.upper()} submitted: {amount_sol:.4f} SOL | {token_mint[:16]}... | {sig[:20]}...")
                        return SwapResult(success=True, signature=sig, input_amount=amount_sol, dex="pumpfun")
                    elif "error" in data:
                        err = data["error"]
                        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                        return SwapResult(success=False, error=msg[:200])
                else:
                    return SwapResult(success=False, error=f"RPC HTTP {resp.status}")

    except Exception as e:
        return SwapResult(success=False, error=str(e)[:200])

    return SwapResult(success=False, error="Unknown error")
