"""
Reef DEX Scanner — Find PROFITABLE WALLETS automatically
Single-pass block scanner: finds DEX activity + extracts swaps + identifies wallets

Run: SCANNER_MODE=discover venv/bin/python scanner.py
"""

import asyncio
import csv
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Set

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    HELIUS_API_KEY,
    HELIUS_RPC_URL,
    MIN_TRADES,
    MIN_TRADES_30D,
    MIN_WIN_RATE,
    MIN_AVG_ROI,
    MIN_SPAN_HOURS,
    BOT_GAP_THRESHOLD_S,
    ACTIVITY_WINDOW_DAYS,
    WALLET_DB_FILE,
    DATA_DIR,
)
from models import WalletMetrics
from swap_parser import (
    DEX_PROGRAMS,
    WRAPPED_SOL,
    parse_transaction_for_swaps,
    ParsedSwap,
)


# ── RPC Helpers ────────────────────────────────────────────────────────

async def get_current_slot() -> Optional[int]:
    """Get the current finalized slot"""
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.post(
                HELIUS_RPC_URL,
                json={"jsonrpc": "2.0", "id": 1, "method": "getSlot", "params": [{"commitment": "finalized"}]},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                data = await resp.json()
                return data.get("result", 0)
    except:
        return None


async def get_block_transactions(slot: int) -> List[dict]:
    """Get all transactions in a block, with blockTime propagated"""
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.post(
                HELIUS_RPC_URL,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getBlock",
                    "params": [
                        slot,
                        {
                            "encoding": "jsonParsed",
                            "maxSupportedTransactionVersion": 0,
                            "transactionDetails": "full",
                            "rewards": False,
                        }
                    ]
                },
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    result = data.get("result", {})
                    block_time = result.get("blockTime", 0)
                    txs = result.get("transactions", [])
                    # Propagate blockTime to each transaction
                    for tx in txs:
                        tx["blockTime"] = block_time
                    return txs
                return []
    except:
        return []


# ── Single-Pass Block Scanner ─────────────────────────────────────────

async def scan_blocks_and_find_wallets(num_blocks: int = 30) -> List[ParsedSwap]:
    """
    Scan blocks ONCE, extract all swaps and their wallets in a single pass.
    This is MUCH faster than two-pass (scan blocks → then fetch wallet histories).
    """
    print(f"\n🔍 Scanning {num_blocks} blocks for DEX activity...")

    slot = await get_current_slot()
    if slot is None:
        print("  ❌ Could not get current slot")
        return []

    start_slot = slot - 5  # Slight delay to ensure finality

    all_swaps: List[ParsedSwap] = []
    wallet_swap_counts: Dict[str, int] = defaultdict(int)
    dex_counts: Dict[str, int] = defaultdict(int)

    for i in range(num_blocks):
        current_slot = start_slot - i

        txs = await get_block_transactions(current_slot)

        for tx in txs:
            try:
                meta = tx.get("meta", {})
                if meta.get("err"):
                    continue

                # Get fee payer (wallet)
                accounts = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
                if not accounts:
                    continue

                fee_payer = accounts[0].get("pubkey", "") if isinstance(accounts[0], dict) else accounts[0]
                if not fee_payer or len(fee_payer) < 32:
                    continue

                # Check if this is a DEX transaction
                instructions = tx.get("transaction", {}).get("message", {}).get("instructions", [])
                involved_dex = None
                for ix in instructions:
                    prog = ix.get("programId", "")
                    if prog in DEX_PROGRAMS.values():
                        involved_dex = prog
                        break

                if not involved_dex:
                    continue

                dex_name = [k for k, v in DEX_PROGRAMS.items() if v == involved_dex]
                dex_name = dex_name[0] if dex_name else "unknown"
                dex_counts[dex_name] += 1

                # Parse swaps from this transaction
                swaps = parse_transaction_for_swaps(tx)

                for swap in swaps:
                    swap.wallet = fee_payer  # Tag with wallet
                    all_swaps.append(swap)
                    wallet_swap_counts[fee_payer] += 1

            except Exception as e:
                continue

        if (i + 1) % 5 == 0:
            print(f"  ... scanned {i + 1}/{num_blocks} blocks, found {len(all_swaps)} swaps, {len(wallet_swap_counts)} wallets")

        await asyncio.sleep(0.05)  # Rate limit

    print(f"\n✅ Scan complete:")
    print(f"   Blocks scanned: {num_blocks}")
    print(f"   Total swaps: {len(all_swaps)}")
    print(f"   Wallets found: {len(wallet_swap_counts)}")
    print(f"   DEX breakdown:")
    for dex, count in sorted(dex_counts.items(), key=lambda x: -x[1])[:5]:
        print(f"     - {dex}: {count}")

    return all_swaps


# ── Analyze Wallets from Collected Swaps ─────────────────────────────

def aggregate_wallet_metrics(all_swaps: List[ParsedSwap]) -> List[WalletMetrics]:
    """
    Aggregate swaps by wallet and calculate metrics.
    No additional API calls needed - we already have all the swap data!
    """
    print(f"\n📊 Analyzing {len(all_swaps)} swaps from {len(set(s.wallet for s in all_swaps))} wallets...")

    # Group swaps by wallet
    wallet_swaps: Dict[str, List[ParsedSwap]] = defaultdict(list)
    for swap in all_swaps:
        wallet_swaps[swap.wallet].append(swap)

    # Calculate metrics for each wallet
    all_metrics: List[WalletMetrics] = []

    for wallet, swaps in wallet_swaps.items():
        if len(swaps) < 2:  # Lowered from 5 to catch winners faster during accumulation
            continue

        metrics = calculate_metrics(wallet, swaps)
        if metrics:
            all_metrics.append(metrics)

    print(f"   Calculated metrics for {len(all_metrics)} wallets")
    return all_metrics


def calculate_metrics(wallet: str, swaps: List[ParsedSwap]) -> Optional[WalletMetrics]:
    """Calculate trading metrics for a wallet"""
    metrics = WalletMetrics(address=wallet)

    # Sort by time (oldest first for ROI calc)
    swaps = sorted(swaps, key=lambda s: s.block_time)

    # ── Span & Gap metrics ──
    if len(swaps) >= 2:
        times = [s.block_time for s in swaps]
        metrics.span_seconds = max(times) - min(times)
        gaps = [times[i+1] - times[i] for i in range(len(times)-1)]
        metrics.avg_gap_seconds = sum(gaps) / len(gaps)

    # Group by token
    token_swaps: Dict[str, List[ParsedSwap]] = defaultdict(list)
    for swap in swaps:
        token_swaps[swap.token_mint].append(swap)

    all_rois = []
    hold_times = []

    for token_mint, token_list in token_swaps.items():
        buys = [s for s in token_list if s.action == "BUY"]
        sells = [s for s in token_list if s.action == "SELL"]

        metrics.buy_count += len(buys)
        metrics.sell_count += len(sells)

        # Pair each buy with the next sell
        for buy in buys:
            next_sells = [s for s in sells if s.block_time > buy.block_time]
            if next_sells:
                sell = next_sells[0]
                buy_price = buy.price_sol
                sell_price = sell.price_sol

                if buy_price > 0:
                    roi = (sell_price - buy_price) / buy_price
                    all_rois.append(roi)

                    hold_sec = sell.block_time - buy.block_time
                    if hold_sec > 0:
                        hold_times.append(hold_sec)

                    profit_sol = sell.amount_sol - buy.amount_sol
                    if roi > 0:
                        metrics.win_count += 1
                        metrics.gross_profit += profit_sol
                    else:
                        metrics.loss_count += 1
                        metrics.gross_loss += abs(profit_sol)

    metrics.total_trades = metrics.win_count + metrics.loss_count

    if all_rois:
        metrics.avg_roi = sum(all_rois) / len(all_rois)
        metrics.best_roi = max(all_rois)
        metrics.worst_roi = min(all_rois)

    if hold_times:
        metrics.avg_hold_time_seconds = sum(hold_times) // len(hold_times)

    if swaps:
        # Last active = most recent swap
        last_swap = max(swaps, key=lambda s: s.block_time)
        metrics.last_active = datetime.fromtimestamp(last_swap.block_time, tz=timezone.utc)

    if token_swaps:
        metrics.favorite_token = max(token_swaps.keys(), key=lambda k: len(token_swaps[k]))

    return metrics


# ── Filtering & Output ────────────────────────────────────────────────

def filter_and_rank(wallets: List[WalletMetrics]) -> List[WalletMetrics]:
    """Filter by thresholds and rank by score"""
    from config import MIN_TRADES, MIN_WIN_RATE, MIN_SPAN_HOURS, MIN_AVG_ROI

    filtered = [
        w for w in wallets
        if w.total_trades >= MIN_TRADES
        and w.win_rate >= MIN_WIN_RATE
        and w.avg_roi >= MIN_AVG_ROI
        and (w.span_seconds / 3600) >= MIN_SPAN_HOURS
    ]
    return sorted(filtered, key=lambda w: w.score, reverse=True)


def purge_old_entries(filepath: str, max_age_days: int = 30):
    """
    Auto-purge: Remove wallet entries older than max_age_days.
    Simple timestamp-based cleanup.
    """
    if not os.path.exists(filepath):
        return

    with open(filepath, "r", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    if not rows:
        return

    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    purged = 0
    kept = []

    for row in rows:
        last_active_str = row.get("last_active", "")
        if not last_active_str or last_active_str == "N/A":
            # No timestamp - keep it
            kept.append(row)
            continue

        try:
            last_active = datetime.fromisoformat(last_active_str.replace("Z", "+00:00"))
            if last_active >= cutoff:
                kept.append(row)
            else:
                purged += 1
        except:
            kept.append(row)

    if purged > 0:
        with open(filepath, "w", newline="") as f:
            writer = csv.DictReader
            fieldnames = ["address", "score", "total_trades", "win_rate", "profit_factor", "avg_roi",
                          "best_roi", "worst_roi", "avg_hold_minutes", "last_active",
                          "favorite_token", "solscan_link"]
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(kept)
        print(f"\n🧹 Auto-purged {purged} stale wallets (> {max_age_days} days old)")

    return purged


def load_historical_swaps(filepath: str) -> List[ParsedSwap]:
    """Load all swaps from historical CSV"""
    if not os.path.exists(filepath):
        return []

    swaps = []
    with open(filepath, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                swap = ParsedSwap(
                    wallet=row["wallet"],
                    signature=row["signature"],
                    dex=row["dex"],
                    token_mint=row["token_mint"],
                    action=row["action"],
                    amount=float(row["amount"]),
                    amount_sol=float(row["amount_sol"]),
                    price_sol=float(row["price_sol"]),
                    slot=int(row["slot"]),
                    block_time=int(row["block_time"]),
                    fee=int(row.get("fee", 0)),
                )
                swaps.append(swap)
            except:
                continue
    return swaps


def save_csv(wallets: List[WalletMetrics], filepath: str):
    """Save to CSV (overwrite with latest state)"""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)

    with open(filepath, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "address", "score", "total_trades", "win_rate", "profit_factor", "avg_roi",
            "best_roi", "worst_roi", "avg_hold_minutes", "last_active",
            "favorite_token", "solscan_link"
        ])

        for w in wallets:
            avg_hold = w.avg_hold_time_seconds // 60 if w.avg_hold_time_seconds else 0
            writer.writerow([
                w.address,
                round(w.score, 3),
                w.total_trades,
                round(w.win_rate, 3),
                round(w.profit_factor, 2),
                round(w.avg_roi, 3),
                round(w.best_roi, 3),
                round(w.worst_roi, 3),
                avg_hold,
                w.last_active.isoformat() if w.last_active else "N/A",
                w.favorite_token[:20] if w.favorite_token else "",
                f"https://solscan.io/account/{w.address}",
            ])

    print(f"\n💾 Saved {len(wallets)} wallets to {filepath}")


def save_swaps_csv(swaps: List[ParsedSwap], filepath: str):
    """Append raw swaps to CSV for historical tracking"""
    if not swaps:
        return

    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    file_exists = os.path.exists(filepath)

    with open(filepath, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow([
                "wallet", "signature", "dex", "token_mint", "action",
                "amount", "amount_sol", "price_sol", "slot", "block_time", "fee"
            ])

        for s in swaps:
            writer.writerow([
                s.wallet,
                s.signature,
                s.dex,
                s.token_mint,
                s.action,
                round(s.amount, 6),
                round(s.amount_sol, 6),
                round(s.price_sol, 9),
                s.slot,
                s.block_time,
                s.fee,
            ])

    print(f"\n💾 Appended {len(swaps)} swaps to {filepath} (total accumulated)")


# ── Main ───────────────────────────────────────────────────────────────

async def main():
    print("=" * 60)
    print("🏄 Reef DEX Scanner — Find Profitable Wallets")
    print("=" * 60)

    if not HELIUS_API_KEY:
        print("❌ No Helius API key in .env")
        sys.exit(1)

    print(f"\n📊 Config:")
    print(f"   Min trades: {MIN_TRADES}")
    print(f"   Min win rate: {MIN_WIN_RATE:.0%}")
    print(f"   Min span: {MIN_SPAN_HOURS}h")
    print(f"   Min avg ROI: {MIN_AVG_ROI:.0%}")
    print(f"   Bot gap threshold: {BOT_GAP_THRESHOLD_S}s")
    print(f"   Blocks to scan: 30")

    # Single pass: scan blocks and collect all swaps
    new_swaps = await scan_blocks_and_find_wallets(num_blocks=30)

    if not new_swaps:
        print("\n😕 No DEX swaps found in scanned blocks")
        print("   Try scanning more blocks or check API connection")
        sys.exit(1)

    # Load historical swaps and merge with new ones
    hist_path = f"{DATA_DIR}/swaps.csv"
    historical_swaps = load_historical_swaps(hist_path)

    # Deduplicate by signature (don't double-count same tx)
    seen_sigs = {s.signature for s in historical_swaps}
    truly_new_swaps = [s for s in new_swaps if s.signature not in seen_sigs]

    all_swaps = historical_swaps + truly_new_swaps

    print(f"\n📜 Historical swaps loaded: {len(historical_swaps)}")
    print(f"🆕 New swaps this run: {len(truly_new_swaps)}")
    print(f"📊 Total swaps for analysis: {len(all_swaps)}")

    # Aggregate swaps by wallet and calculate metrics from ALL history
    wallets = aggregate_wallet_metrics(all_swaps)

    # Filter and rank
    qualified = filter_and_rank(wallets)
    print(f"\n🎯 {len(qualified)} wallets passed filters")

    # Show ALL wallets found (even without full buy/sell pairs)
    # Prioritize qualified (filtered) wallets in display, fill rest from all wallets
    if qualified:
        display_wallets = qualified[:15]
    else:
        display_wallets = wallets[:15] if len(wallets) >= 15 else wallets

    print(f"\n{'='*75}")
    print(f"{'#':<4} {'Address':<16} {'Score':<6} {'Trades':<6} {'Win%':<6} {'PF':<6} {'Span':<7} {'Type':<7} {'ROI%':<10}")
    print(f"{'='*75}")

    for i, w in enumerate(display_wallets, 1):
        roi_pct = f"{w.avg_roi * 100:.0f}%" if w.avg_roi else "0%"
        win_str = f"{w.win_rate:.0%}" if w.total_trades > 0 else "N/A"
        pf_str = f"{w.profit_factor:.1f}" if w.profit_factor < 999 else "∞"
        span_h = f"{w.span_seconds/3600:.1f}h" if w.span_seconds else "?"
        ttype = w.trader_type
        flag = "⚠️ " if ttype == "BOT" else ""
        print(f"{i:<4} {w.address[:14]}..{w.address[-4:]} "
              f"{w.score:.3f}  {w.total_trades:<6} {win_str:<6} {pf_str:<6} {span_h:<7} {flag}{ttype:<7} {roi_pct}")

    # Save NEW swaps (append mode) + wallet DB (recalculated from full history)
    save_swaps_csv(truly_new_swaps, f"{DATA_DIR}/swaps.csv")
    
    if wallets:
        purge_old_entries(WALLET_DB_FILE)
        save_csv(wallets, WALLET_DB_FILE)

    print(f"\n✅ Scan complete!")
    print(f"   Swaps saved: {len(all_swaps)}")
    print(f"   Wallets found: {len(wallets)}")
    if qualified:
        top = qualified[0]
        print(f"   🏆 Top wallet: {top.address[:20]}...")
        print(f"   Score: {top.score:.3f}")
        print(f"   Win rate: {top.win_rate:.0%}")
        pf_display = f"{top.profit_factor:.2f}" if top.profit_factor < 999 else "∞"
        print(f"   Profit factor: {pf_display}")
        print(f"   Avg ROI: {top.avg_roi * 100:.0f}%")
        print(f"   https://solscan.io/account/{top.address}")

    return qualified


if __name__ == "__main__":
    ranked = asyncio.run(main())
