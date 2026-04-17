"""One-shot pipeline test: buy 0.005 SOL of BONK via Jupiter, confirm on-chain.
Isolates the Jupiter + public-RPC path from pump-amm creator-vault quirks.
"""
import asyncio
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from swap_executor import execute_swap_legacy, load_solana_keypair, SOL_MINT
import swap_executor

# Force live mode (not dry run)
swap_executor.DRY_RUN = False

BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"  # well-known, deeply liquid

async def main():
    kp = await load_solana_keypair("data/keypair.json")
    if not kp:
        print("❌ no keypair")
        return
    print(f"🔑 wallet: {kp.pubkey()}")
    print(f"🎯 buying 0.005 SOL of BONK via Jupiter, 300bps slippage...")

    result = await execute_swap_legacy(
        kp, SOL_MINT, BONK,
        amount_sol=0.005, slippage_bps=300,
    )

    print(f"\nResult: success={result.success}")
    print(f"  sig: {result.signature}")
    print(f"  error: {result.error}")
    if result.signature and result.signature not in ("confirmed", "DRY_RUN", "DRY_RUN_SIG"):
        print(f"\n🔗 https://solscan.io/tx/{result.signature}")
        # Poll on-chain
        import aiohttp
        print(f"\nPolling for confirmation up to 60s...")
        for i in range(20):
            await asyncio.sleep(3)
            async with aiohttp.ClientSession() as s:
                async with s.post("https://solana.publicnode.com", json={
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getSignatureStatuses",
                    "params": [[result.signature], {"searchTransactionHistory": True}],
                }) as resp:
                    d = await resp.json()
                    v = (d.get("result", {}).get("value") or [None])[0]
                    if v:
                        cs = v.get("confirmationStatus")
                        err = v.get("err")
                        print(f"  [{(i+1)*3}s] status={cs} err={err}")
                        if cs in ("confirmed", "finalized"):
                            print(f"\n{'✅ LANDED' if not err else '❌ LANDED WITH ERR: ' + str(err)}")
                            return
                    else:
                        print(f"  [{(i+1)*3}s] not yet in chain")
        print("\n⏳ never confirmed in 60s")

if __name__ == "__main__":
    asyncio.run(main())
