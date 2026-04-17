"""Sell 100% of held BONK back to SOL via Jupiter. Proves full round-trip."""
import asyncio, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from swap_executor import execute_swap_legacy, load_solana_keypair, SOL_MINT
import swap_executor

swap_executor.DRY_RUN = False

BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"

async def main():
    kp = await load_solana_keypair("data/keypair.json")
    print(f"🔑 wallet: {kp.pubkey()}")
    print(f"🎯 selling 100% of BONK back to SOL (slippage 300bps)")

    # execute_swap_legacy detects input != SOL_MINT and queries full balance internally
    result = await execute_swap_legacy(
        kp, BONK, SOL_MINT,
        amount_sol=0, slippage_bps=300,
    )

    print(f"\nResult: success={result.success}  sig={result.signature}  err={result.error}")
    if result.signature and result.signature not in ("confirmed", "DRY_RUN", "DRY_RUN_SIG"):
        print(f"🔗 https://solscan.io/tx/{result.signature}")
        import aiohttp
        for i in range(20):
            await asyncio.sleep(3)
            async with aiohttp.ClientSession() as s:
                async with s.post("https://solana.publicnode.com", json={
                    "jsonrpc": "2.0", "id": 1, "method": "getSignatureStatuses",
                    "params": [[result.signature], {"searchTransactionHistory": True}],
                }) as resp:
                    d = await resp.json()
                    v = (d.get("result", {}).get("value") or [None])[0]
                    if v and v.get("confirmationStatus") in ("confirmed", "finalized"):
                        print(f"[{(i+1)*3}s] {'✅ SELL LANDED' if not v.get('err') else '❌ err=' + str(v.get('err'))}")
                        return
        print("⏳ never confirmed")

if __name__ == "__main__":
    asyncio.run(main())
