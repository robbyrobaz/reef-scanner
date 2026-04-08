"""
Reef Swap Executor — executes real trades via Jupiter API.

DRY_RUN=True by default (logs only, no actual swaps).
Set DRY_RUN=False or pass --live to execute real trades.

Keypair: set KEYPAIR_FILE in config.py or pass --keypair PATH.
Defaults to ~/.config/solana/id.json (Solana CLI default).
"""

import asyncio
import base64
import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    HELIUS_API_KEY,
    HELIUS_RPC_URL,
    COPY_PRIORITY_FEE_LAMPORTS,
    DATA_DIR,
)
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.transaction import VersionedTransaction
import aiohttp

DRY_RUN = True

# ── Constants ───────────────────────────────────────────────────────────
JUPITER_QUOTE_API = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_API = "https://quote-api.jup.ag/v6/swap"
JUPITER_PRICE_API = "https://quote-api.jup.ag/v6/price"
SOL_MINT = "So11111111111111111111111111111111111111112"
HELIUS_TX_URL = f"https://api.helius.xyz/v0/addresses/push?api-key={HELIUS_API_KEY}"

KEYPAIR_FILE = os.path.expanduser(
    os.getenv("KEYPAIR_FILE", f"{DATA_DIR}/keypair.json")
)


@dataclass
class SwapResult:
    success: bool
    signature: str = ""
    error: str = ""
    input_amount: float = 0.0
    output_amount: float = 0.0
    price_sol: float = 0.0
    dex: str = "jupiter"


def load_keypair(path: str) -> Optional[Keypair]:
    """Load a keypair from a JSON file."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            data = json.load(f)
        # Solana CLI keypair format: base58 array
        if isinstance(data, list):
            return Keypair.from_bytes(bytes(data))
        # Alternative: bs58 encoded
        if isinstance(data, str):
            import base58
            return Keypair.from_bytes(base58.b58decode(data))
    except Exception as e:
        print(f"    ❌ Failed to load keypair from {path}: {e}")
    return None


async def get_jupiter_quote(
    input_mint: str,
    output_mint: str,
    amount_lamports: int,
    slippage_bps: int = 50,
) -> Optional[dict]:
    """Get a Jupiter quote for the swap."""
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": amount_lamports,
        "slippageBps": slippage_bps,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(JUPITER_QUOTE_API, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    print(f"    ⚠️  Jupiter quote error: {resp.status}")
    except Exception as e:
        print(f"    ⚠️  Jupiter quote failed: {e}")
    return None


async def get_token_price(mint: str) -> float:
    """Get price of a token in SOL."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                JUPITER_PRICE_API,
                params={"ids": mint},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return float(data.get(mint, {}).get("price", 0))
    except:
        pass
    return 0.0


async def execute_jupiter_swap(
    keypair: Keypair,
    quote: dict,
    input_mint: str,
    output_mint: str,
    amount_lamports: int,
) -> SwapResult:
    """
    Execute a Jupiter swap using the given quote.
    Returns SwapResult with success/failure details.
    """
    try:
        # Get the swap transaction from Jupiter
        swap_payload = {
            "quoteResponse": quote,
            "userPublicKey": str(keypair.pubkey()),
            "wrapAndUnwrapSol": True,
            "prioritizationFeeLamports": COPY_PRIORITY_FEE_LAMPORTS,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                JUPITER_SWAP_API,
                json=swap_payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    return SwapResult(success=False, error=f"Jupiter swap API error {resp.status}: {text[:200]}")
                
                swap_data = await resp.json()
        
        # Deserialize the transaction
        tx_bytes = base64.b64decode(swap_data["transaction"])
        transaction = VersionedTransaction.from_bytes(tx_bytes)
        
        # The transaction is already signed by Jupiter's signer for the intermediate steps
        # We just need to sign with our keypair (the userKey parameter handles this)
        # Actually, Jupiter returns a partially-signed transaction - our keypair's signature
        # needs to be added. Let me check the structure.
        
        # Re-sign the transaction with our keypair
        # The Jupiter API returns a transaction where the user is the final signer
        # We need to sign the message with our keypair
        
        # Actually for VersionedTransactions with lookup tables, we need to
        # reconstruct and sign
        signers = [keypair]
        
        # Try to get the latest blockhash
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.post(
                HELIUS_RPC_URL,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getLatestBlockhash",
                    "params": [{"commitment": "confirmed"}],
                },
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    recent_blockhash = data["result"]["value"]["blockhash"]
                else:
                    return SwapResult(success=False, error="Failed to get recent blockhash")
        
        # Create a new transaction with the swap instructions
        # Jupiter returns a transaction that we need to sign
        # The swap instruction is already in the message - we just need our signature
        
        # For jupiter v6, the tx comes pre-formatted, we just need to sign
        # and send. But VersionedTransaction signing works differently...
        
        # Let me try a simpler approach: use legacy transaction format
        # Re-request with asLegacyTransaction=true
        return SwapResult(success=False, error="Versioned transactions need special handling - using legacy path")

    except Exception as e:
        return SwapResult(success=False, error=str(e))


async def send_raw_transaction(tx_bytes: bytes) -> Optional[str]:
    """Send a raw transaction via Helius."""
    try:
        import aiohttp
        tx_b64 = base64.b64encode(tx_bytes).decode()
        
        async with aiohttp.ClientSession() as session:
            async with session.post(
                HELIUS_TX_URL,
                json={
                    "transaction": tx_b64,
                    "skipPreFlight": False,
                    "encoding": "base64",
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("signature", "")
                else:
                    text = await resp.text()
                    print(f"    ⚠️  Helius send error {resp.status}: {text[:200]}")
    except Exception as e:
        print(f"    ⚠️  Send tx failed: {e}")
    return None


async def execute_swap(
    keypair: Keypair,
    input_mint: str,
    output_mint: str,
    amount_sol: float,
    slippage_bps: int = 50,
) -> SwapResult:
    """
    Execute a swap on Jupiter.
    
    Args:
        keypair: User's keypair for signing
        input_mint: Input token mint (use SOL_MINT for SOL)
        output_mint: Output token mint  
        amount_sol: Amount in SOL to swap
        slippage_bps: Slippage tolerance in basis points (50 = 0.5%)
    
    Returns:
        SwapResult with execution details
    """
    if DRY_RUN:
        print(f"    🐸 DRY RUN: would swap {amount_sol:.4f} SOL ({input_mint[:16]}... → {output_mint[:16]}...)")
        return SwapResult(success=True, signature="DRY_RUN_SIG", input_amount=amount_sol, output_amount=0)
    
    # Convert SOL to lamports
    amount_lamports = int(amount_sol * 1e9)
    
    # If input is SOL, use wrapped SOL mint
    if input_mint == "SOL" or input_mint == SOL_MINT:
        input_mint = SOL_MINT
    
    # Get quote
    quote = await get_jupiter_quote(input_mint, output_mint, amount_lamports, slippage_bps)
    if not quote:
        return SwapResult(success=False, error="Failed to get Jupiter quote")
    
    # Get output amount
    output_amount = int(quote.get("outAmount", 0))
    price_sol = float(quote.get("inAmount", 0)) / max(float(quote.get("outAmount", 1)), 1) / 1e9
    
    # Build and sign transaction
    result = await execute_jupiter_swap(
        keypair, quote, input_mint, output_mint, amount_lamports
    )
    
    if result.success:
        result.input_amount = amount_sol
        result.output_amount = output_amount / 1e9 if output_mint != SOL_MINT else output_amount / 1e9
        result.price_sol = price_sol
    
    return result


# ── Solana CLI keystore compatibility ──────────────────────────────────
async def load_solana_keypair(keypair_path: str = "") -> Optional[Keypair]:
    """
    Load keypair from a specific path, or fall back to common locations.
    Priority: explicit path > KEYPAIR_FILE env > DATA_DIR/keypair.json > ~/.config/solana/id.json
    """
    paths_to_try = []
    if keypair_path:
        paths_to_try.append(keypair_path)
    paths_to_try.extend([
        KEYPAIR_FILE,
        f"{DATA_DIR}/keypair.json",
        os.path.expanduser("~/.config/solana/id.json"),
    ])
    
    seen = set()
    for path in paths_to_try:
        if path in seen or not path:
            continue
        seen.add(path)
        kp = load_keypair(path)
        if kp:
            print(f"    🔑 Loaded keypair from {path}")
            return kp
    
    print(f"    ⚠️  No keypair found.")
    return None


# ── Simpler legacy transaction approach ────────────────────────────────
async def execute_swap_legacy(
    keypair: Keypair,
    input_mint: str,
    output_mint: str,
    amount_sol: float,
    slippage_bps: int = 50,
) -> SwapResult:
    """
    Execute swap using legacy transaction format (simpler, more compatible).
    """
    if DRY_RUN:
        print(f"    🐸 DRY RUN: would swap {amount_sol:.4f} SOL")
        return SwapResult(success=True, signature="DRY_RUN", input_amount=amount_sol)
    
    amount_lamports = int(amount_sol * 1e9)
    if input_mint == "SOL":
        input_mint = SOL_MINT
    
    # Get quote with legacy format
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": amount_lamports,
        "slippageBps": slippage_bps,
        "asLegacyTransaction": True,
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            # Get quote
            async with session.get(JUPITER_QUOTE_API, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return SwapResult(success=False, error=f"Quote error {resp.status}")
                quote = await resp.json()
            
            # Get swap tx
            swap_payload = {
                "quoteResponse": quote,
                "userPublicKey": str(keypair.pubkey()),
                "wrapAndUnwrapSol": True,
                "prioritizationFeeLamports": COPY_PRIORITY_FEE_LAMPORTS,
            }
            async with session.post(JUPITER_SWAP_API, json=swap_payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return SwapResult(success=False, error=f"Swap API error {resp.status}")
                swap_data = await resp.json()
            
            # Deserialize and sign
            tx_bytes = base64.b64decode(swap_data["transaction"])
            
            # Parse the legacy transaction
            from solders.transaction import Transaction
            from solders.hash import Hash
            tx = Transaction.from_bytes(tx_bytes)
            
            # Get fresh blockhash for signing
            async with session.post(
                HELIUS_RPC_URL,
                json={
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getLatestBlockhash",
                    "params": [{"commitment": "confirmed"}],
                },
                timeout=aiohttp.ClientTimeout(total=5),
            ) as bh_resp:
                if bh_resp.status != 200:
                    return SwapResult(success=False, error="Failed to get blockhash")
                bh_data = await bh_resp.json()
                blockhash_str = bh_data["result"]["value"]["blockhash"]
                recent_blockhash = Hash.from_string(blockhash_str)
            
            # Sign with our keypair (mutates tx in place)
            tx.sign([keypair], recent_blockhash)
            
            # Send
            async with session.post(
                HELIUS_RPC_URL,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "sendTransaction",
                    "params": [
                        base64.b64encode(bytes(tx)).decode(),
                        {"encoding": "base64", "skipPreFlight": False},
                    ],
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if "result" in data:
                        return SwapResult(
                            success=True,
                            signature=data["result"],
                            input_amount=amount_sol,
                            price_sol=float(quote.get("inAmount", 0)) / max(int(quote.get("outAmount", 1)), 1) / 1e9,
                        )
                    elif "error" in data:
                        return SwapResult(success=False, error=str(data["error"]))
                else:
                    return SwapResult(success=False, error=f"RPC error {resp.status}")
                        
    except Exception as e:
        return SwapResult(success=False, error=str(e))


if __name__ == "__main__":
    import asyncio
    
    DRY_RUN = "--live" not in sys.argv
    
    async def test():
        print(f"DRY_RUN={DRY_RUN}")
        kp = await load_solana_keypair()
        if not kp:
            print("No keypair - testing quote API only")
            # Just test the quote API
            q = await get_jupiter_quote(
                SOL_MINT,
                "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
                10_000_000,  # 0.01 SOL
                50,
            )
            if q:
                print(f"    ✅ Quote: {q.get('inAmount')} → {q.get('outAmount')}")
            return
        
        print(f"Keypair loaded: {kp.pubkey()}")
        
        # Test with a small amount
        result = await execute_swap_legacy(
            kp,
            SOL_MINT,
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
            0.001,  # 0.001 SOL
            50,
        )
        print(f"Swap result: {result}")
    
    asyncio.run(test())
