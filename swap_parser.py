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
    fee: int = 0            # default for historical loads
    pool_address: str = ""  # pump-amm pool address (only for dex="pumpfun")


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
    Parse a pump-amm swap from instruction and logs.

    For pump-amm, instruction accounts layout:
      [0] pool (amm)
      [1] payer (wallet)
      [2] GLOBAL_CONFIG
      [3] base_mint  (may be WSOL for inverted pools)
      [4] quote_mint (may be the token for inverted pools)
      ...

    We pick whichever of [3],[4] is NOT WSOL as the token mint.
    Amounts come from pre/post token balance changes.
    """
    try:
        # Determine action from logs
        action = None
        for log in logs:
            if "Instruction: Buy" in log:
                action = "BUY"
                break
            elif "Instruction: Sell" in log:
                action = "SELL"
                break
        if not action:
            return None

        # Extract token mint from instruction accounts [3] and [4]
        ix_accounts = instruction.get("accounts", [])
        token_mint = ""
        if len(ix_accounts) >= 5:
            candidate_a = ix_accounts[3]  # base_mint
            candidate_b = ix_accounts[4]  # quote_mint
            # Pick whichever is not WSOL
            if candidate_a != WRAPPED_SOL:
                token_mint = candidate_a
            elif candidate_b != WRAPPED_SOL:
                token_mint = candidate_b

        # Fall back to accountKeys[1] if ix_accounts unavailable (old format)
        if not token_mint:
            account_keys = [acc.get("pubkey", "") if isinstance(acc, dict) else acc for acc in accounts]
            if len(account_keys) > 1:
                token_mint = account_keys[1]

        if not token_mint:
            return None

        # Compute SOL and token amounts from pre/post token balances
        sol_amount = 0.0
        token_amount = 0.0
        pre_balances = {b["accountIndex"]: b for b in meta.get("preTokenBalances", [])}
        post_balances = {b["accountIndex"]: b for b in meta.get("postTokenBalances", [])}

        # Find the wallet's index in accountKeys
        account_keys_list = [
            acc.get("pubkey", "") if isinstance(acc, dict) else acc
            for acc in accounts
        ]
        wallet_idx = None
        for i, k in enumerate(account_keys_list):
            if k == wallet:
                wallet_idx = i
                break

        # Token amount: change in wallet's balance of token_mint
        for idx, post in post_balances.items():
            if post.get("mint") == token_mint:
                pre = pre_balances.get(idx, {})
                pre_amt = int(pre.get("uiTokenAmount", {}).get("amount", 0) or 0)
                post_amt = int(post.get("uiTokenAmount", {}).get("amount", 0) or 0)
                delta = abs(post_amt - pre_amt)
                decimals = int(post.get("uiTokenAmount", {}).get("decimals", 6) or 6)
                if delta > token_amount * (10 ** decimals):
                    token_amount = delta / (10 ** decimals)

        # SOL amount: use actual native balance delta (pre/post) — instruction data contains
        # slippage-adjusted ceilings (max_quote_amount_in / min_quote_amount_out), not real amounts.
        pre_sol = meta.get("preBalances", [])
        post_sol = meta.get("postBalances", [])
        if wallet_idx is not None and wallet_idx < len(pre_sol) and wallet_idx < len(post_sol):
            fee = meta.get("fee", 0) / 1e9
            raw_delta = (post_sol[wallet_idx] - pre_sol[wallet_idx]) / 1e9
            sol_amount = abs(raw_delta) - fee
            sol_amount = max(0.0, sol_amount)

        price_sol = (sol_amount / token_amount) if token_amount > 0 and sol_amount > 0 else 0

        return {
            "token_mint": token_mint,
            "action": action,
            "amount": token_amount,
            "sol_amount": sol_amount,
            "price_sol": price_sol,
        }

    except Exception:
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

    # BUY = wallet received the token; SELL = wallet sent the token
    # sol_transfer.mint is always WRAPPED_SOL for SOL-based trades (both directions),
    # so we can't use that to determine action — use token flow direction instead.
    action = "BUY" if token_transfer.get("toUserAccount") == wallet else "SELL"

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

        # Try pump.fun / pump-amm parsing
        if "pumpfun" in dex_programs_found:
            ix = dex_programs_found["pumpfun"]
            parsed = parse_pumpfun_swap(wallet, signature, ix, accounts, meta, logs)
            if parsed:
                # Extract pool address: first account in the instruction's accounts list
                # For pump-amm, keys[0] = pool; for bonding-curve, keys[0] = bonding_curve
                ix_accounts = ix.get("accounts", [])
                pool_address = ix_accounts[0] if ix_accounts else ""

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
                    pool_address=pool_address,
                ))

    except Exception as e:
        pass

    return swaps


if __name__ == "__main__":
    print("Swap parser loaded")
    print(f"DEX programs: {list(DEX_PROGRAMS.keys())}")
