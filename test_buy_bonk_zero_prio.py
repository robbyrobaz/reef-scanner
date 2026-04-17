"""Test BONK buy at ZERO priority fee. Time-to-land test."""
import asyncio, sys, os, time, base64, aiohttp
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from solders.transaction import VersionedTransaction
from swap_executor import load_solana_keypair, get_jupiter_quote, SOL_MINT

BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
RPC  = "https://solana.publicnode.com"

async def main():
    kp = await load_solana_keypair("data/keypair.json")
    print(f"🔑 {kp.pubkey()}")
    print(f"🎯 Buy 0.003 SOL BONK | priority_fee=0 lamports | slippage=300bps\n")

    t0 = time.time()
    quote = await get_jupiter_quote(SOL_MINT, BONK, 3_000_000, 300)
    if not quote:
        print("no quote"); return
    print(f"[{time.time()-t0:.2f}s] got quote, impact={quote.get('priceImpactPct','?')}%")

    async with aiohttp.ClientSession() as s:
        async with s.post("https://api.jup.ag/swap/v1/swap", json={
            "quoteResponse": quote,
            "userPublicKey": str(kp.pubkey()),
            "wrapAndUnwrapSol": True,
            "prioritizationFeeLamports": 0,  # <-- ZERO priority
        }, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                print(f"swap api {resp.status}: {(await resp.text())[:200]}"); return
            data = await resp.json()
        print(f"[{time.time()-t0:.2f}s] got swap tx from Jupiter")

        tx_b64 = data.get("swapTransaction") or data.get("transaction","")
        tx = VersionedTransaction(VersionedTransaction.from_bytes(base64.b64decode(tx_b64)).message, [kp])

        async with s.post(RPC, json={
            "jsonrpc":"2.0","id":1,"method":"sendTransaction",
            "params":[base64.b64encode(bytes(tx)).decode(), {"encoding":"base64","skipPreFlight":True,"maxRetries":3}],
        }, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            d = await resp.json()
            sig = d.get("result")
            if not sig:
                print(f"submit failed: {d}"); return
        t_submit = time.time() - t0
        print(f"[{t_submit:.2f}s] submitted: {sig}")
        print(f"🔗 https://solscan.io/tx/{sig}")

        # Poll every 2s up to 90s
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
                    print(f"[{t_conf:.2f}s] {'✅ LANDED' if not err else '❌ err=' + str(err)}")
                    print(f"\nTime-to-land from tx build start: {t_conf:.2f}s")
                    print(f"Time-to-land from submission:      {t_conf - t_submit:.2f}s")
                    # Check on-chain fee paid
                    import requests
                    r = requests.post("https://api.mainnet-beta.solana.com", json={
                        "jsonrpc":"2.0","id":1,"method":"getTransaction",
                        "params":[sig,{"encoding":"json","maxSupportedTransactionVersion":0,"commitment":"confirmed"}]
                    }, timeout=15).json()
                    if r.get("result"):
                        fee = r["result"]["meta"]["fee"]
                        print(f"Total fee paid:                    {fee} lamports = {fee/1e9:.6f} SOL")
                    return
        print(f"\n⏳ not confirmed in 90s — tx probably dropped from mempool at 0 priority")

if __name__ == "__main__":
    asyncio.run(main())
