"""
Reef Retro Scanner — Historical DEX + Wallet Activity
Options B + C:
  B: Scan DEX program accounts (pumpfun first) backwards through time
  C: Scan known wallets backwards through time

Run: SCANNER_MODE=discover venv/bin/python retro_scan.py --days-back 2 --dex pumpfun --top-wallets 5
"""

import asyncio
import csv
import os
import sys
import time
import argparse
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    HELIUS_API_KEY,
    HELIUS_RPC_URL,
    DATA_DIR,
    WALLET_DB_FILE,
)
from swap_parser import (
    DEX_PROGRAMS,
    parse_transaction_for_swaps,
    ParsedSwap,
)
from models import WalletMetrics


# ── RPC Helpers ────────────────────────────────────────────────────────

from config import PUBLIC_RPC_ENDPOINTS

# Rotate through RPC endpoints
RPC_URLS = [HELIUS_RPC_URL] + PUBLIC_RPC_ENDPOINTS
_rpc_idx = 0

async def rpc_call(method: str, params: list) -> Optional[dict]:
    """Make a single RPC call, rotating through endpoints on failure"""
    global _rpc_idx
    for _attempt in range(len(RPC_URLS)):
        url = RPC_URLS[_rpc_idx % len(RPC_URLS)]
        _rpc_idx += 1
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if "error" in data:
                            continue  # Try next endpoint
                        return data.get("result")
                    continue
        except:
            continue
    return None


async def get_signatures_for_address(
    address: str,
    before: Optional[str] = None,
    limit: int = 1000,
) -> List[dict]:
    """
    Get transaction signatures for an account.
    Solana RPC expects positional params: [address, {options}]
    """
    params = [address, {"limit": limit}]
    if before:
        params[1]["before"] = before

    result = await rpc_call("getSignaturesForAddress", params)
    return result if result else []


async def get_transaction(sig: str) -> Optional[dict]:
    """Fetch a single transaction (fallback for individual calls)"""
    result = await rpc_call("getTransaction", [
        sig,
        {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
    ])
    return result


async def get_transactions_batch_helius(sigs: List[str]) -> List[Optional[dict]]:
    """
    Fetch transactions using Helius batch API — MUCH faster than individual calls.
    Returns Helius-enriched format with parsed tokenTransfers/nativeTransfers.
    Handles auth via x-api-key header.
    """
    if not sigs or not HELIUS_API_KEY:
        return []

    all_results = []
    for i in range(0, len(sigs), 100):
        batch = sigs[i:i+100]
        try:
            import aiohttp
            url = f"https://api.helius.xyz/v0/transactions?api-key={HELIUS_API_KEY}"
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json={"transactions": batch},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        all_results.extend(data if isinstance(data, list) else [])
                    else:
                        all_results.extend([None] * len(batch))
        except:
            all_results.extend([None] * len(batch))

        if i + 100 < len(sigs):
            await asyncio.sleep(0.05)

    return all_results


# ── Helius Swap Parser ─────────────────────────────────────────────────

def parse_helius_swap(tx: dict, dex_name: str) -> List[ParsedSwap]:
    """
    Parse a Helius-enriched SWAP transaction into ParsedSwap objects.
    Helius format has: signature, feePayer, tokenTransfers[], nativeTransfers[],
    timestamp, slot, type (should be 'SWAP').
    """
    if not tx or tx.get("type") != "SWAP":
        return []

    fee_payer = tx.get("feePayer", "")
    sig = tx.get("signature", "")
    slot = tx.get("slot", 0)
    block_time = tx.get("timestamp", 0)

    token_transfers = tx.get("tokenTransfers", [])
    native_transfers = tx.get("nativeTransfers", [])

    swaps = []

    # Find the user's SOL flow: nativeTransfers involving feePayer
    # Outgoing SOL (from feePayer) = BUY, Incoming SOL (to feePayer) = SELL
    user_native_out = 0
    user_native_in = 0
    for nt in native_transfers:
        if nt.get("fromUserAccount") == fee_payer:
            user_native_out += int(nt.get("amount", 0))
        if nt.get("toUserAccount") == fee_payer:
            user_native_in += int(nt.get("amount", 0))

    if user_native_out == 0 and user_native_in == 0:
        return []  # No SOL flow, not a real swap

    # Determine action
    if user_native_out > 0 and user_native_in == 0:
        action = "BUY"
        sol_amount = user_native_out
    elif user_native_in > 0 and user_native_out == 0:
        action = "SELL"
        sol_amount = user_native_in
    else:
        # Both directions — use net
        net = user_native_in - user_native_out
        action = "BUY" if net > 0 else "SELL"
        sol_amount = abs(net)

    # Find the non-SOL token mint from tokenTransfers
    # The user's account in tokenTransfers tells us direction
    user_token_out_mint = None
    user_token_in_mint = None
    user_token_out_amt = 0.0
    user_token_in_amt = 0.0

    for tt in token_transfers:
        mint = tt.get("mint", "")
        if mint == "So11111111111111111111111111111111111111112":
            continue  # Skip wrapped SOL
        from_acc = tt.get("fromUserAccount", "")
        to_acc = tt.get("toUserAccount", "")
        amount = float(tt.get("tokenAmount", 0))

        if from_acc == fee_payer:
            user_token_out_mint = mint
            user_token_out_amt = amount
        if to_acc == fee_payer:
            user_token_in_mint = mint
            user_token_in_amt = amount

    # Determine which mint is being traded and the amount
    if action == "BUY" and user_token_in_mint:
        token_mint = user_token_in_mint
        token_amount = user_token_in_amt
    elif action == "SELL" and user_token_out_mint:
        token_mint = user_token_out_mint
        token_amount = user_token_out_amt
    else:
        # Fallback: pick the non-zero mint
        if user_token_out_mint:
            token_mint = user_token_out_mint
            token_amount = user_token_out_amt
        elif user_token_in_mint:
            token_mint = user_token_in_mint
            token_amount = user_token_in_amt
        else:
            return []  # Can't determine token

    if token_amount == 0:
        return []

    # Price in SOL (lamports to SOL: divide by 1e9)
    sol_lamports = sol_amount
    sol_amount_sol = sol_lamports / 1_000_000_000
    price_sol = sol_amount_sol / token_amount if token_amount > 0 else 0

    swap = ParsedSwap(
        wallet=fee_payer,
        signature=sig,
        dex=dex_name,
        token_mint=token_mint,
        action=action,
        amount=token_amount,
        amount_sol=sol_amount_sol,
        price_sol=price_sol,
        slot=slot,
        block_time=block_time,
        fee=int(tx.get("fee", 0)),
    )
    swaps.append(swap)
    return swaps


# ── Option B: Scan DEX Program Account ─────────────────────────────────

async def scan_dex_program_history(
    dex_name: str,
    days_back: int = 2,
    max_sigs: int = 3000,
    concurrency: int = 3,
) -> Tuple[List[ParsedSwap], Dict[str, int]]:
    """
    Scan a DEX program's account history backwards.
    Gets signatures via getSignaturesForAddress, fetches txs concurrently.

    Args:
        dex_name: Name from DEX_PROGRAMS (e.g., 'pumpfun')
        days_back: How far back to scan
        max_sigs: Max signatures to process
        concurrency: How many signature pages to fetch in parallel
    """
    program_id = DEX_PROGRAMS.get(dex_name)
    if not program_id:
        print(f"  ❌ Unknown DEX: {dex_name}")
        return [], {}

    print(f"\n📡 Scanning {dex_name} ({program_id}) — last {days_back} days, max {max_sigs} sigs...")

    now_ts = datetime.now(timezone.utc).timestamp()
    start_ts = now_ts - (days_back * 86400)

    all_swaps: List[ParsedSwap] = []
    dex_counts: Dict[str, int] = defaultdict(int)
    seen_sigs: Set[str] = set()
    total_fetched = 0
    total_parsed = 0

    # ── Phase 1: Collect signatures via paginated getSignaturesForAddress ──
    print(f"  📋 Phase 1: Collecting signatures...")
    cursor = None
    page = 0
    sigs_collected = 0
    all_sig_infos = []

    while sigs_collected < max_sigs:
        page += 1
        sigs = await get_signatures_for_address(program_id, before=cursor, limit=1000)

        if not sigs:
            break

        for sig_info in sigs:
            ts = sig_info.get("blockTime", 0)
            if ts < start_ts:
                # Signatures are in desc order — once we go past the window, stop
                break
            if sig_info["signature"] not in seen_sigs:
                seen_sigs.add(sig_info["signature"])
                all_sig_infos.append(sig_info)
                sigs_collected += 1
                if sigs_collected >= max_sigs:
                    break

        # Move cursor to last signature (oldest in this batch)
        cursor = sigs[-1].get("signature")

        if page % 5 == 0:
            print(f"    ... page {page}, collected {sigs_collected} sigs")

        # Safety: if we got a full page but none were in our window, we've gone past
        if sigs_collected == 0 and sigs[0].get("blockTime", 0) < start_ts:
            break

        await asyncio.sleep(0.05)

    print(f"  ✅ Collected {len(all_sig_infos)} signatures in {page} pages")

    # ── Phase 2: Fetch transactions via Helius batch API ──
    print(f"  📥 Phase 2: Fetching {len(all_sig_infos)} transactions via Helius batch...")
    sigs_to_fetch = [s["signature"] for s in all_sig_infos]
    infos_by_sig = {s["signature"]: s for s in all_sig_infos}

    t0 = time.time()
    txs = await get_transactions_batch_helius(sigs_to_fetch)
    elapsed = time.time() - t0

    swap_type_count = 0
    for sig, tx in zip(sigs_to_fetch, txs):
        total_fetched += 1
        if not tx:
            continue

        # Add blockTime from sig_info
        sig_info = infos_by_sig.get(sig, {})
        if not tx.get("timestamp"):
            tx["timestamp"] = sig_info.get("blockTime", 0)
        if not tx.get("slot"):
            tx["slot"] = sig_info.get("slot", 0)

        if tx.get("type") == "SWAP":
            swap_type_count += 1
            try:
                parsed = parse_helius_swap(tx, dex_name)
                for swap in parsed:
                    all_swaps.append(swap)
                    dex_counts[swap.dex] += 1
                    total_parsed += 1
            except:
                pass

    elapsed = time.time() - t0
    rate = len(sigs_to_fetch) / elapsed if elapsed > 0 else 0
    print(f"  ✅ Fetched {len(sigs_to_fetch)} txs in {elapsed:.1f}s ({rate:.1f} tx/s)")
    print(f"  📊 SWAP-type txs: {swap_type_count}, parsed swaps: {total_parsed}")

    unique_wallets = len(set(s.wallet for s in all_swaps))
    print(f"  ✅ {dex_name}: fetched {total_fetched} txs, found {total_parsed} swaps across {unique_wallets} wallets")

    return all_swaps, dict(dex_counts)


# ── Option C: Retro Scan Known Wallets ─────────────────────────────────

async def scan_wallet_history(
    address: str,
    days_back: int = 3,
    max_sigs: int = 2000,
) -> List[ParsedSwap]:
    """
    Scan a wallet's transaction history going backwards.
    Used to build complete trade history for known winners.
    """
    print(f"\n  🔎 Retro scanning wallet {address[:10]}... ({days_back} days back)")

    now_ts = datetime.now(timezone.utc).timestamp()
    start_ts = now_ts - (days_back * 86400)

    all_swaps: List[ParsedSwap] = []
    seen_sigs: Set[str] = set()
    total_fetched = 0
    cursor = None
    page = 0

    while total_fetched < max_sigs:
        page += 1
        sigs = await get_signatures_for_address(address, before=cursor, limit=1000)

        if not sigs:
            break

        valid_infos = []
        for sig_info in sigs:
            ts = sig_info.get("blockTime", 0)
            if ts >= start_ts:
                if sig_info["signature"] not in seen_sigs:
                    seen_sigs.add(sig_info["signature"])
                    valid_infos.append(sig_info)
            else:
                break  # Past our window

        if not valid_infos:
            break

        cursor = sigs[-1].get("signature")

        # Respect max_sigs: only process up to (max_sigs - total_fetched) this page
        remaining = max_sigs - total_fetched
        if remaining <= 0:
            break
        if len(valid_infos) > remaining:
            valid_infos = valid_infos[:remaining]

        # Fetch txs via Helius batch
        sig_strings = [s["signature"] for s in valid_infos]
        sig_map = {s["signature"]: s for s in valid_infos}

        txs = await get_transactions_batch_helius(sig_strings)

        for sig, tx in zip(sig_strings, txs):
            total_fetched += 1
            if not tx:
                continue

            sig_info = sig_map.get(sig, {})
            if not tx.get("timestamp"):
                tx["timestamp"] = sig_info.get("blockTime", 0)

            # Helius batch: feePayer tells us who signed
            fee_payer = tx.get("feePayer", "")
            if fee_payer != address:
                continue

            if tx.get("type") == "SWAP":
                try:
                    parsed = parse_helius_swap(tx, "pumpfun")
                    for swap in parsed:
                        swap.wallet = address
                        all_swaps.append(swap)
                except:
                    continue

        if page % 3 == 0:
            print(f"    ... page {page}, fetched {total_fetched}, swaps: {len(all_swaps)}")

        await asyncio.sleep(0.1)

    print(f"  ✅ {address[:10]}...: {total_fetched} txs, {len(all_swaps)} swaps")
    return all_swaps


async def scan_top_wallets(
    days_back: int = 3,
    top_n: int = 5,
    max_sigs_per_wallet: int = 2000,
) -> List[ParsedSwap]:
    """
    Load top wallets from DB and retro scan each.
    
    Args:
        days_back: How far back to scan
        top_n: How many top wallets to scan
        max_sigs_per_wallet: Max signatures per wallet
    """
    if not os.path.exists(WALLET_DB_FILE):
        print("⚠️  No wallet DB found, skipping wallet scan")
        return []

    wallets = []
    with open(WALLET_DB_FILE, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                wallets.append({
                    "address": row["address"],
                    "score": float(row.get("score", 0)),
                    "total_trades": int(row.get("total_trades", 0)),
                })
            except:
                continue

    if not wallets:
        print("⚠️  No wallets in DB")
        return []

    wallets.sort(key=lambda w: w["score"], reverse=True)
    top = wallets[:top_n]

    print(f"\n🏆 Top {len(top)} wallets from DB:")
    for w in top:
        print(f"   {w['address'][:16]}... | score={w['score']:.3f} | trades={w['total_trades']}")

    all_swaps = []
    total_wallets_scanned = 0
    for i, w in enumerate(top):
        print(f"\n  🔎 [{i+1}/{len(top)}] Retro scanning {w['address'][:16]}... "
              f"({days_back} days, max {max_sigs_per_wallet} sigs)")
        swaps = await scan_wallet_history(
            w["address"],
            days_back=days_back,
            max_sigs=max_sigs_per_wallet,
        )
        all_swaps.extend(swaps)
        total_wallets_scanned += 1
        if i < len(top) - 1:
            await asyncio.sleep(0.5)  # Be nice to the RPC

    print(f"\n  ✅ Scanned {total_wallets_scanned} wallets, {len(all_swaps)} total swaps")
    return all_swaps


# ── Merge & Save ────────────────────────────────────────────────────────

def load_existing_swaps() -> List[ParsedSwap]:
    """Load swaps from swaps.csv"""
    swaps = []
    path = f"{DATA_DIR}/swaps.csv"
    if not os.path.exists(path):
        return swaps

    with open(path, newline="") as f:
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


def save_swaps(swaps: List[ParsedSwap], path: str):
    """Append new swaps to CSV"""
    if not swaps:
        return

    os.makedirs(os.path.dirname(path), exist_ok=True)
    file_exists = os.path.exists(path)

    with open(path, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow([
                "wallet", "signature", "dex", "token_mint", "action",
                "amount", "amount_sol", "price_sol", "slot", "block_time", "fee"
            ])
        for s in swaps:
            writer.writerow([
                s.wallet, s.signature, s.dex, s.token_mint, s.action,
                round(s.amount, 6), round(s.amount_sol, 6), round(s.price_sol, 9),
                s.slot, s.block_time, s.fee,
            ])
    print(f"\n💾 Saved {len(swaps)} swaps to {path}")


# ── Main ────────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="Reef Retro Scanner")
    parser.add_argument("--days-back", type=int, default=2, help="Days to look back (default: 2)")
    parser.add_argument("--dex", type=str, default="pumpfun", help="DEX to scan (default: pumpfun)")
    parser.add_argument("--top-wallets", type=int, default=5, help="Top N wallets to retro scan (default: 5)")
    parser.add_argument("--wallet-sigs", type=int, default=2000, help="Max signatures per wallet (default: 2000)")
    parser.add_argument("--no-dex", action="store_true", help="Skip DEX program scan")
    parser.add_argument("--no-wallets", action="store_true", help="Skip wallet history scan")
    parser.add_argument("--max-sigs", type=int, default=3000, help="Max signatures per DEX scan (default: 3000)")
    parser.add_argument(
        "--retro-fill",
        action="store_true",
        help="Fill history for top wallets: top 100 @ 500 sigs (7d) + top 20 @ 2000 sigs (14d)",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("🏄 Reef Retro Scanner — Historical DEX + Wallet Activity")
    print("=" * 60)

    all_new_swaps: List[ParsedSwap] = []
    total_dex_swaps = 0
    total_wallet_swaps = 0

    # ── Option B: DEX Program Scan ──
    if not args.no_dex:
        t0 = time.time()
        print(f"\n📊 DEX: {args.dex}, Days back: {args.days_back}, Max sigs: {args.max_sigs}")
        dex_swaps, dex_counts = await scan_dex_program_history(
            args.dex, days_back=args.days_back, max_sigs=args.max_sigs
        )
        all_new_swaps.extend(dex_swaps)
        total_dex_swaps = len(dex_swaps)
        elapsed = time.time() - t0
        print(f"\n  ⏱️  DEX scan took {elapsed:.1f}s")
        if dex_counts:
            print(f"  📊 DEX breakdown: {dict(dex_counts)}")
        await asyncio.sleep(1)

    # ── Option C: Top Wallets Scan ──
    if not args.no_wallets:
        if args.retro_fill:
            # Tier 1: Top 100 wallets, 7 days, 500 sigs each
            print(f"\n{'='*60}")
            print(f"📥 RETRO-FILL Mode — Tier 1 (breadth)")
            print(f"   Top 100 wallets, 7 days back, 500 sigs/wallet")
            print(f"{'='*60}")
            t0 = time.time()
            tier1_swaps = await scan_top_wallets(
                days_back=7,
                top_n=100,
                max_sigs_per_wallet=500,
            )
            all_new_swaps.extend(tier1_swaps)
            total_wallet_swaps += len(tier1_swaps)
            elapsed = time.time() - t0
            print(f"\n  ⏱️  Tier 1 took {elapsed:.1f}s, {len(tier1_swaps)} swaps")

            await asyncio.sleep(2)

            # Tier 2: Top 20 wallets (from DB), 14 days, 2000 sigs each
            print(f"\n{'='*60}")
            print(f"📥 RETRO-FILL Mode — Tier 2 (depth)")
            print(f"   Top 20 wallets, 14 days back, 2000 sigs/wallet")
            print(f"{'='*60}")
            t0 = time.time()
            tier2_swaps = await scan_top_wallets(
                days_back=14,
                top_n=20,
                max_sigs_per_wallet=2000,
            )
            all_new_swaps.extend(tier2_swaps)
            total_wallet_swaps += len(tier2_swaps)
            elapsed = time.time() - t0
            print(f"\n  ⏱️  Tier 2 took {elapsed:.1f}s, {len(tier2_swaps)} swaps")
        else:
            print(f"\n📊 Wallets: top {args.top_wallets}, {args.days_back} days back, "
                  f"{args.wallet_sigs} sigs/wallet")
            wallet_swaps = await scan_top_wallets(
                days_back=args.days_back,
                top_n=args.top_wallets,
                max_sigs_per_wallet=args.wallet_sigs,
            )
            all_new_swaps.extend(wallet_swaps)
            total_wallet_swaps = len(wallet_swaps)

    # ── Load existing + deduplicate ──
    existing = load_existing_swaps()
    existing_sigs = {s.signature for s in existing}
    truly_new = [s for s in all_new_swaps if s.signature not in existing_sigs]

    print(f"\n📊 Summary:")
    print(f"   New DEX swaps: {total_dex_swaps}")
    print(f"   New wallet swaps: {total_wallet_swaps}")
    print(f"   Duplicate (skipped): {len(all_new_swaps) - len(truly_new)}")
    print(f"   Truly new swaps: {len(truly_new)}")
    print(f"   Existing swaps: {len(existing)}")

    # ── Save new swaps ──
    if truly_new:
        save_swaps(truly_new, f"{DATA_DIR}/swaps.csv")

    print(f"\n✅ Retro scan complete!")
    print(f"   Run the regular scanner to recalculate wallet metrics with the new data.")


if __name__ == "__main__":
    asyncio.run(main())
