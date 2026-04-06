"""
Reef Position Tracker — tracks user's token positions.

Keeps track of:
- Which tokens we hold
- Average entry price
- Position size in SOL
- Unrealized PnL
- Entry timestamp

Run: python positions.py (to refresh positions)
Or import and call refresh_positions() in the copy engine loop.
"""

import asyncio
import csv
import json
import os
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import HELIUS_API_KEY, HELIUS_RPC_URL, DATA_DIR
from copy_config import load_copy_config
from swap_parser import parse_transaction_for_swaps, ParsedSwap

POSITIONS_FILE = f"{DATA_DIR}/positions.json"

# Cache for token decimals
TOKEN_DECIMALS: Dict[str, int] = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDj1v": 6,  # USDC
    "So11111111111111111111111111111111111111111112": 9,  # wSOL
}


def load_token_decimals(mint: str) -> int:
    """Get token decimals for a mint, with caching."""
    if mint in TOKEN_DECIMALS:
        return TOKEN_DECIMALS[mint]
    # Default to 9 (most SPL tokens)
    TOKEN_DECIMALS[mint] = 9
    return 9


@dataclass
class Position:
    token_mint: str
    amount: float          # raw amount of token
    avg_price_sol: float   # avg price in SOL we paid per token
    total_cost_sol: float  # total SOL spent
    entry_time: int        # unix timestamp
    source_wallets: List[str]  # which wallets we've been copying this from

    def current_value_sol(self, current_price: float) -> float:
        return self.amount * current_price

    def unrealized_pnl_sol(self, current_price: float) -> float:
        return self.current_value_sol(current_price) - self.total_cost_sol

    def unrealized_pnl_pct(self, current_price: float) -> float:
        if self.total_cost_sol == 0:
            return 0.0
        return (self.unrealized_pnl_sol(current_price) / self.total_cost_sol) * 100


def load_positions() -> Dict[str, Position]:
    """Load positions from JSON file."""
    if not os.path.exists(POSITIONS_FILE):
        return {}
    try:
        with open(POSITIONS_FILE) as f:
            data = json.load(f)
        return {mint: Position(**p) for mint, p in data.items()}
    except:
        return {}


def save_positions(positions: Dict[str, Position]) -> None:
    """Save positions to JSON file."""
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(POSITIONS_FILE, "w") as f:
        json.dump({mint: asdict(p) for mint, p in positions.items()}, f, indent=2)


async def get_spl_token_balances(wallet: str) -> Dict[str, float]:
    """Get all SPL token balances for a wallet via Helius."""
    balances = {}
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            # Use Helius DAS API to get token accounts
            async with session.post(
                f"https://api.helius.xyz/v0/addresses/{wallet}/balances?api-key={HELIUS_API_KEY}",
                json={},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    tokens = data.get("tokens", [])
                    for token in tokens:
                        mint = token.get("mint", "")
                        amount = token.get("amount", 0)
                        decimals = token.get("decimals", 9)
                        if amount > 0 and mint not in (
                            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDj1v",  # skip USDC
                            "So11111111111111111111111111111111111111111112",  # skip wSOL
                        ):
                            balances[mint] = amount / (10 ** decimals)
    except Exception as e:
        print(f"    ⚠️  Failed to get balances for {wallet[:16]}...: {e}")
    return balances


async def get_token_price_sol(mint: str) -> float:
    """Get current price of a token in SOL via Jupiter."""
    if mint == "So11111111111111111111111111111111111111111112":
        return 1.0
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://quote-api.jup.ag/v6/price",
                params={"ids": mint},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return float(data.get(mint, {}).get("price", 0))
    except:
        pass
    return 0.0


async def refresh_positions(positions: Dict[str, Position], wallet: str) -> Dict[str, Position]:
    """
    Refresh position data by fetching current balances.
    Adds new positions from our copy trades. Updates existing ones.
    """
    if not wallet:
        return positions
    
    # Get current balances
    balances = await get_spl_token_balances(wallet)
    
    # Remove positions we no longer hold
    for mint in list(positions.keys()):
        if mint not in balances:
            del positions[mint]
    
    # Update/add positions from copy trades
    # (The copy engine calls add_position when a BUY is copied)
    
    return positions


def add_position_from_trade(
    positions: Dict[str, Position],
    token_mint: str,
    amount_tokens: float,
    price_sol: float,
    source_wallet: str,
) -> Dict[str, Position]:
    """Add or update a position from a copy trade BUY."""
    total_cost = amount_tokens * price_sol
    
    if token_mint in positions:
        p = positions[token_mint]
        new_total_cost = p.total_cost_sol + total_cost
        new_amount = p.amount + amount_tokens
        p.avg_price_sol = new_total_cost / new_amount if new_amount > 0 else 0
        p.total_cost_sol = new_total_cost
        p.amount = new_amount
        if source_wallet not in p.source_wallets:
            p.source_wallets.append(source_wallet)
    else:
        positions[token_mint] = Position(
            token_mint=token_mint,
            amount=amount_tokens,
            avg_price_sol=price_sol,
            total_cost_sol=total_cost,
            entry_time=int(time.time()),
            source_wallets=[source_wallet] if source_wallet else [],
        )
    
    return positions


def reduce_position(
    positions: Dict[str, Position],
    token_mint: str,
    amount_tokens: float,
) -> Dict[str, Position]:
    """Reduce a position when we sell. Returns updated positions."""
    if token_mint not in positions:
        return positions
    
    p = positions[token_mint]
    p.amount -= amount_tokens
    p.total_cost_sol = max(0, p.total_cost_sol - (amount_tokens * p.avg_price_sol))
    
    if p.amount <= 0.000001:  # dust
        del positions[token_mint]
    
    return positions


async def get_positions_summary(positions: Dict[str, Position]) -> List[dict]:
    """
    Get a summary of all positions with current prices and PnL.
    """
    summary = []
    for mint, pos in positions.items():
        current_price = await get_token_price_sol(mint)
        pnl_sol = pos.unrealized_pnl_sol(current_price)
        pnl_pct = pos.unrealized_pnl_pct(current_price)
        summary.append({
            "token_mint": mint,
            "amount": round(pos.amount, 6),
            "avg_price_sol": round(pos.avg_price_sol, 9),
            "current_price_sol": round(current_price, 9),
            "total_cost_sol": round(pos.total_cost_sol, 6),
            "current_value_sol": round(pos.current_value_sol(current_price), 6),
            "pnl_sol": round(pnl_sol, 6),
            "pnl_pct": round(pnl_pct, 2),
            "entry_time": datetime.fromtimestamp(pos.entry_time).isoformat(),
            "source_wallets": pos.source_wallets,
        })
    
    # Sort by value descending
    summary.sort(key=lambda x: x["current_value_sol"], reverse=True)
    return summary


async def check_should_sell(
    positions: Dict[str, Position],
    token_mint: str,
    source_wallet: str,
) -> bool:
    """
    Check if we should sell a token based on source wallet activity.
    For now: if the source wallet we copied from sells, we sell too.
    Returns True if we should execute a sell.
    """
    # Simple strategy: if we hold the token and the source wallet sells it, we sell too
    return token_mint in positions


if __name__ == "__main__":
    async def main():
        config = load_copy_config()
        wallet = config.user_wallet
        
        if not wallet:
            print("No user wallet configured")
            return
        
        print(f"Refreshing positions for {wallet[:16]}...")
        positions = load_positions()
        positions = await refresh_positions(positions, wallet)
        
        summary = await get_positions_summary(positions)
        print(f"\nPositions ({len(summary)}):")
        total_value = 0
        total_pnl = 0
        for s in summary:
            print(f"  {s['token_mint'][:20]}... | Amt: {s['amount']:.4f} | Value: {s['current_value_sol']:.4f} SOL | PnL: {s['pnl_sol']:+.4f} SOL ({s['pnl_pct']:+.1f}%)")
            total_value += s['current_value_sol']
            total_pnl += s['pnl_sol']
        
        print(f"\nTotal: {total_value:.4f} SOL | PnL: {total_pnl:+.4f} SOL")
        save_positions(positions)

    asyncio.run(main())
