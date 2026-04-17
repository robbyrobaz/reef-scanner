"""BONK buy with Jupiter onlyDirectRoutes=true — tests 'as direct as possible'.
Compare time-to-land vs the aggregated Jupiter path."""
import asyncio, sys, os, time, base64, aiohttp
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from solders.transaction import VersionedTransaction
from swap_executor import load_solana_keypair, SOL_MINT

BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
RPC  = "https://solana.publicnode.com"

async def main():
    kp = await load_solana_keypair("data/keypair.json")
    print(f"🔑 {kp.pubkey()}\n🎯 BONK buy | onlyDirectRoutes=true | priority_fee=0\n")
    t0 = time.time()
    async with aiohttp.ClientSession() as s:
        async with s.get("https://api.jup.ag/swap/v1/quote", params={
            "inputMint": SOL_MINT, "outputMint": BONK, "amount": 3_000_000,
            "slippageBps": 300, "onlyDirectRoutes": "true",
        }, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            quote = await resp.json() if resp.status == 200 else None
        if not quote:
            print("no quote"); return
        route = " → ".join(p.get("swapInfo",{}).get("label","?") for p in quote.get("routePlan",[]))
        print(f"[{time.time()-t0:.2f}s] quote via: {route}")
        async with s.post("https://api.jup.ag/swap/v1/swap", json={
            "quoteResponse": quote, "userPublicKey": str(kp.pubkey()),
            "wrapAndUnwrapSol": True, "prioritizationFeeLamports": 0,
        }, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
        tx_b64 = data.get("swapTransaction","")
        tx = VersionedTransaction(VersionedTransaction.from_bytes(base64.b64decode(tx_b64)).message, [kp])
        async with s.post(RPC, json={
            "jsonrpc":"2.0","id":1,"method":"sendTransaction",
            "params":[base64.b64encode(bytes(tx)).decode(), {"encoding":"base64","skipPreFlight":True,"maxRetries":3}],
        }, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            d = await resp.json()
            sig = d.get("result")
            if not sig: print(f"fail: {d}"); return
        t_submit = time.time() - t0
        print(f"[{t_submit:.2f}s] submitted: {sig}")
        print(f"🔗 https://solscan.io/tx/{sig}")
        for i in range(45):
            await asyncio.sleep(2)
            async with s.post(RPC, json={
                "jsonrpc":"2.0","id":1,"method":"getSignatureStatuses",
                "params":[[sig],{"searchTransactionHistory":True}],
            }) as resp:
                d = await resp.json()
                v = (d.get("result",{}).get("value") or [None])[0]
                if v and v.get("confirmationStatus") in ("confirmed","finalized"):
                    t_conf = time.time() - t0
                    err = v.get("err")
                    print(f"[{t_conf:.2f}s] {'✅ LANDED' if not err else '❌ err='+str(err)}")
                    print(f"build+submit: {t_submit:.2f}s | on-chain: {t_conf - t_submit:.2f}s")
                    import requests
                    r = requests.post("https://api.mainnet-beta.solana.com", json={
                        "jsonrpc":"2.0","id":1,"method":"getTransaction",
                        "params":[sig,{"encoding":"json","maxSupportedTransactionVersion":0,"commitment":"confirmed"}]
                    }, timeout=15).json()
                    if r.get("result"):
                        print(f"fee paid: {r['result']['meta']['fee']} lamports")
                    return
        print("⏳ never confirmed in 90s")

if __name__ == "__main__":
    asyncio.run(main())
