"""
One-shot liquidator for stale SPL token holdings in the hot wallet.

Scans wallet SPL accounts, for each non-dust balance > 0, fires a Jupiter
SELL to recover SOL. Closes the ATA after sell to reclaim rent. Prints
net SOL recovered.

Usage:  venv/bin/python liquidate_stale.py [--dry-run]
"""
import asyncio, os, sys, json
from pathlib import Path

for line in open('.env'):
    if '=' in line and not line.strip().startswith('#'):
        k, v = line.strip().split('=', 1)
        os.environ.setdefault(k, v.strip('"\''))

import swap_executor
from swap_executor import execute_swap_legacy, load_solana_keypair, SOL_MINT
from copy_engine import _close_empty_ata
import aiohttp

# Force live mode — the module default is DRY_RUN=True
swap_executor.DRY_RUN = "--dry-run" in sys.argv
RPC_URL = "https://solana.publicnode.com"  # reliable for getBalance + tokenAccounts

DRY_RUN = "--dry-run" in sys.argv

async def list_spl_holdings(wallet: str):
    """Return list of (mint, balance_raw) for non-zero SPL balances."""
    body = {
        "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
        "params": [wallet, {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
                   {"encoding": "jsonParsed"}]
    }
    # Try also Token-2022 program
    body2 = {
        "jsonrpc": "2.0", "id": 2, "method": "getTokenAccountsByOwner",
        "params": [wallet, {"programId": "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"},
                   {"encoding": "jsonParsed"}]
    }
    async with aiohttp.ClientSession() as s:
        holdings = []
        for b in [body, body2]:
            try:
                async with s.post(RPC_URL, json=b, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    d = await r.json()
                for acc in d.get("result", {}).get("value", []):
                    info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
                    mint = info.get("mint")
                    amt = info.get("tokenAmount", {})
                    raw = int(amt.get("amount", 0))
                    ui = float(amt.get("uiAmount") or 0)
                    if raw > 0 and ui > 0:
                        holdings.append((mint, raw, ui, bool(b is body2)))  # (mint, raw, ui, is_token2022)
            except Exception as e:
                print(f"  RPC err for program {b['params'][1]['programId'][:10]}: {e}")
        return holdings


async def main():
    keypair = await load_solana_keypair("data/keypair.json")
    if not keypair:
        print("❌ no keypair"); return
    wallet = str(keypair.pubkey())
    print(f"wallet: {wallet}")

    # starting balance
    async with aiohttp.ClientSession() as s:
        async with s.post(RPC_URL, json={"jsonrpc":"2.0","id":1,"method":"getBalance","params":[wallet]}) as r:
            d = await r.json()
            start_balance = d.get("result", {}).get("value", 0) / 1e9
    print(f"starting SOL: {start_balance:.6f}")

    holdings = await list_spl_holdings(wallet)
    print(f"\nfound {len(holdings)} SPL holdings:")
    for m, raw, ui, t22 in holdings:
        tag = " [Token-2022]" if t22 else ""
        print(f"  mint={m}  ui_balance={ui:.4f}  raw={raw}{tag}")

    if DRY_RUN:
        print("\n(dry run — not firing any SELLs)"); return

    # Sell each
    recovered = 0.0
    fails = []
    for i, (mint, raw, ui, t22) in enumerate(holdings, 1):
        tag = " [T22]" if t22 else ""
        print(f"\n[{i}/{len(holdings)}] Selling {ui:.4f} of {mint[:16]}...{tag}")
        res = await execute_swap_legacy(
            keypair=keypair, input_mint=mint, output_mint=SOL_MINT,
            amount_sol=0, slippage_bps=500,  # 5% slippage — we want out
        )
        if res.success:
            out = (res.output_amount or 0) / 1e9
            recovered += out
            print(f"  ✅ sold → recovered {out:.6f} SOL | sig {(res.signature or '')[:20]}")
            # close ATA to reclaim rent
            try:
                await _close_empty_ata(keypair, mint)
            except Exception as e:
                print(f"  ⚠ ATA close err: {e}")
        else:
            print(f"  ❌ failed: {res.error}")
            fails.append((mint, res.error))
        await asyncio.sleep(2)

    # ending balance
    async with aiohttp.ClientSession() as s:
        async with s.post(RPC_URL, json={"jsonrpc":"2.0","id":1,"method":"getBalance","params":[wallet]}) as r:
            d = await r.json()
            end_balance = d.get("result", {}).get("value", 0) / 1e9

    print(f"\n=== SUMMARY ===")
    print(f"starting balance: {start_balance:.6f} SOL")
    print(f"ending balance:   {end_balance:.6f} SOL")
    print(f"net change:       {end_balance - start_balance:+.6f} SOL")
    print(f"successful sells: {len(holdings) - len(fails)} / {len(holdings)}")
    if fails:
        print(f"failed ({len(fails)}):")
        for m, e in fails: print(f"  {m[:16]}... → {e[:60]}")


if __name__ == "__main__":
    asyncio.run(main())
