"""Test: can Jupiter find a route for a pump-amm graduated token our engine targets?
If yes, we can bypass the broken pump_swap SDK entirely via Jupiter with wider slippage."""
import asyncio, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from swap_executor import execute_swap_legacy, load_solana_keypair, SOL_MINT, get_jupiter_quote
import swap_executor
swap_executor.DRY_RUN = False

# A recent target from our engine that PumpSwap SDK failed on (missing creator vault)
TARGET = "GsS8NKf3Fd1kqMeiinQKRppYgfDpPNMEKf8eRpgme5km"

async def main():
    # 1. Just a quote first — cheap probe, no tx
    print(f"🔍 Jupiter quote for 0.003 SOL → {TARGET[:20]}... ...")
    for slippage_bps in [300, 1000, 3000]:
        q = await get_jupiter_quote(SOL_MINT, TARGET, 3_000_000, slippage_bps)
        if q:
            in_amt = int(q.get("inAmount", 0))
            out_amt = int(q.get("outAmount", 0))
            price_impact = q.get("priceImpactPct", "?")
            route = " → ".join([p.get("swapInfo", {}).get("label", "?") for p in q.get("routePlan", [])])
            print(f"  slippage={slippage_bps}bps ✓ in={in_amt} out={out_amt} impact={price_impact}% route={route[:80]}")
        else:
            print(f"  slippage={slippage_bps}bps ✗ no route found")

    # 2. If any slippage worked, try actual swap at the cheapest that did
    print(f"\n🎯 Attempting 0.003 SOL buy at 1000bps (10%)...")
    kp = await load_solana_keypair("data/keypair.json")
    result = await execute_swap_legacy(kp, SOL_MINT, TARGET, amount_sol=0.003, slippage_bps=1000)
    print(f"\nResult: success={result.success}  sig={result.signature}  err={result.error[:200]}")
    if result.signature and result.signature not in ("confirmed", "DRY_RUN", "DRY_RUN_SIG"):
        print(f"🔗 https://solscan.io/tx/{result.signature}")
        import aiohttp
        for i in range(15):
            await asyncio.sleep(3)
            async with aiohttp.ClientSession() as s:
                async with s.post("https://solana.publicnode.com", json={
                    "jsonrpc":"2.0","id":1,"method":"getSignatureStatuses",
                    "params":[[result.signature],{"searchTransactionHistory":True}],
                }) as resp:
                    d = await resp.json()
                    v = (d.get("result",{}).get("value") or [None])[0]
                    if v and v.get("confirmationStatus") in ("confirmed","finalized"):
                        print(f"[{(i+1)*3}s] {'✅ LANDED' if not v.get('err') else '❌ err=' + str(v.get('err'))[:200]}")
                        return

if __name__ == "__main__":
    asyncio.run(main())
