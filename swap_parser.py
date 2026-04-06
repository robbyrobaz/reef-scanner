"""
Reef Swap Parser — Parse DEX swaps from raw Solana transactions
Handles: Jupiter, Raydium, Pump.fun

Uses base58 decoding for instruction data when base64 fails.
"""

import base64
import struct
from dataclasses import dataclass
from typing import Optional, List, Dict

# ── DEX Program IDs ────────────────────────────────────────────────────
DEX_PROGRAMS = {
    "jupiter": "JUP6LkbZbjS3jtsKSqf5joF4BSrFEh7WEZg3Xs5ycD1c",
    "raydium_amm": "675kPX9MHTjS2zt1qfr1NYHuzeSxPGBY4eNTtRMqDxGD",
    "raydium_clmm": "CAMMCoz5osS1hNYLw9EcD7wK9K9FRGMG6DvVKcy3DyRd",
    "orca": "whirLbMiicVdio4qvUfM5eAgqufWr5Z3WA2L1iLTDj7a",
    "pumpfun": "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",
    "openbook": "srmqPvymJeFKQ4zGz1bHe5PbfwV1hwgENNYNrgT5K6V",
    "phoenix": "Phoenix1aYyKdsgfFeShVD1aT4xTNvGvPUqUyFdN2aD6g9",
}

WRAPPED_SOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"

B58_ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'


@dataclass
class ParsedSwap:
    wallet: str
    signature: str
    dex: str
    token_mint: str
    action: str  # "BUY" or "SELL"
    amount: float
    amount_sol: float
    price_sol: float
    slot: int
    block_time: int
    fee: int


def base58_decode(s: str) -> bytes:
    """Decode a base58 string to bytes"""
    if not s:
        return b''
    num = 0
    for c in s:
        num *= 58
        num += B58_ALPHABET.index(c)
    result = []
    while num > 0:
        result.append(num & 255)
        num >>= 8
    result = bytes(reversed(result))
    leading_zeros = sum(1 for c in s if c == '1')
    return bytes(leading_zeros) + result


def decode_instruction_data(data_str: str) -> bytes:
    """Try base64 first, then base58"""
    if not data_str:
        return b''
    # Try base64 first
    try:
        return base64.b64decode(data_str)
    except:
        pass
    # Try base58
    try:
        return base58_decode(data_str)
    except:
        return b''


def parse_pumpfun_swap(
    wallet: str,
    signature: str,
    instruction: dict,
    accounts: List[dict],
    meta: dict,
    logs: List[str]
) -> Optional[dict]:
    """
    Parse a Pump.fun swap from instruction and logs.
    Uses logs to determine BUY vs SELL, accounts for token mint.
    """
    try:
        # Check log for instruction type
        action = None
        for log in logs:
            if "Instruction: Buy" in log or "buy" in log.lower():
                action = "BUY"
                break
            elif "Instruction: Sell" in log or "sell" in log.lower():
                action = "SELL"
                break

        if not action:
            return None

        # Get token mint from accounts
        # Pump.fun accounts: [bonding_curve, mint, user, ...]
        account_keys = [acc.get("pubkey", "") for acc in accounts]
        token_mint = ""
        if len(account_keys) > 1:
            token_mint = account_keys[1]

        # Try to decode amounts from instruction data
        data_str = instruction.get("data", "")
        decoded = decode_instruction_data(data_str)

        sol_amount = 0.0
        token_amount = 0.0

        if decoded and len(decoded) >= 9:
            # Try to extract u64 values
            if len(decoded) >= 18:
                # Two u64 values
                val1 = struct.unpack("<Q", decoded[1:9])[0] if len(decoded) > 8 else 0
                val2 = struct.unpack("<Q", decoded[10:18])[0] if len(decoded) > 17 else 0

                # Determine which is SOL and which is token
                # SOL amounts are typically larger (in lamports)
                if val2 > 1e15:  # Likely a SOL amount in lamports
                    sol_amount = val2 / 1e9
                    token_amount = val1 / 1e6
                else:
                    sol_amount = val1 / 1e9
                    token_amount = val2 / 1e6
            elif len(decoded) >= 9:
                val = struct.unpack("<Q", decoded[1:9])[0]
                if val > 1e15:
                    sol_amount = val / 1e9
                else:
                    token_amount = val / 1e6

        # Calculate price
        price_sol = 0
        if token_amount > 0 and sol_amount > 0:
            price_sol = sol_amount / token_amount

        return {
            "token_mint": token_mint,
            "action": action,
            "amount": token_amount,
            "sol_amount": sol_amount,
            "price_sol": price_sol,
        }

    except Exception as e:
        return None


def parse_swap_from_transfers(
    wallet: str,
    token_transfers: List[dict],
) -> Optional[dict]:
    """Parse swap from tokenTransfers (Jupiter, Raydium, etc.)"""
    if len(token_transfers) < 2:
        return None

    sol_transfer = None
    token_transfer = None

    for transfer in token_transfers:
        mint = transfer.get("mint", "")
        if mint in [WRAPPED_SOL, USDC, USDT]:
            sol_transfer = transfer
        else:
            token_transfer = transfer

    if not sol_transfer or not token_transfer:
        return None

    sol_amount = abs(float(sol_transfer.get("uiTokenAmount", {}).get("tokenAmount", 0)))
    token_amount = abs(float(token_transfer.get("uiTokenAmount", {}).get("tokenAmount", 0)))
    token_mint = token_transfer.get("mint", "")
    token_symbol = token_transfer.get("symbol", "UNKNOWN")

    if token_amount > 0 and sol_amount > 0:
        price_sol = sol_amount / token_amount
    else:
        price_sol = 0

    action = "BUY" if sol_transfer.get("mint") == WRAPPED_SOL else "SELL"

    return {
        "token_mint": token_mint,
        "token_symbol": token_symbol,
        "action": action,
        "amount": token_amount,
        "sol_amount": sol_amount,
        "price_sol": price_sol,
    }


def parse_transaction_for_swaps(tx: dict) -> List[ParsedSwap]:
    """Parse a transaction and extract all swap events."""
    swaps = []

    try:
        meta = tx.get("meta", {})
        if meta.get("err"):
            return []

        message = tx.get("transaction", {}).get("message", {})
        instructions = message.get("instructions", [])
        accounts = message.get("accountKeys", [])

        signatures = tx.get("transaction", {}).get("signatures", [])
        signature = signatures[0] if signatures else ""

        wallet = accounts[0].get("pubkey", "") if accounts and isinstance(accounts[0], dict) else ""
        if not wallet:
            return []

        slot = tx.get("slot", 0)
        block_time = tx.get("blockTime", 0)
        fee = meta.get("fee", 0)
        logs = meta.get("logMessages", [])

        # Identify involved DEX programs
        dex_programs_found = {}
        for ix in instructions:
            prog = ix.get("programId", "")
            for dex_name, dex_id in DEX_PROGRAMS.items():
                if prog == dex_id:
                    dex_programs_found[dex_name] = ix

        # Try standard tokenTransfers first (works for Jupiter, Raydium)
        token_transfers = meta.get("tokenTransfers", [])
        if len(token_transfers) >= 2:
            parsed = parse_swap_from_transfers(wallet, token_transfers)
            if parsed:
                swaps.append(ParsedSwap(
                    wallet=wallet,
                    signature=signature,
                    dex=list(dex_programs_found.keys())[0] if dex_programs_found else "unknown",
                    token_mint=parsed["token_mint"],
                    action=parsed["action"],
                    amount=parsed["amount"],
                    amount_sol=parsed.get("sol_amount", 0),
                    price_sol=parsed.get("price_sol", 0),
                    slot=slot,
                    block_time=block_time,
                    fee=fee,
                ))
                return swaps

        # Try pump.fun parsing
        if "pumpfun" in dex_programs_found:
            ix = dex_programs_found["pumpfun"]
            parsed = parse_pumpfun_swap(wallet, signature, ix, accounts, meta, logs)
            if parsed:
                swaps.append(ParsedSwap(
                    wallet=wallet,
                    signature=signature,
                    dex="pumpfun",
                    token_mint=parsed["token_mint"],
                    action=parsed["action"],
                    amount=parsed.get("amount", 0),
                    amount_sol=parsed.get("sol_amount", 0),
                    price_sol=parsed.get("price_sol", 0),
                    slot=slot,
                    block_time=block_time,
                    fee=fee,
                ))

    except Exception as e:
        pass

    return swaps


if __name__ == "__main__":
    print("Swap parser loaded")
    print(f"DEX programs: {list(DEX_PROGRAMS.keys())}")
