"""Retest pump-amm buy via Jupiter with stepped priority fees to find landing threshold."""
import asyncio, sys, os, aiohttp, base64
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from solders.transaction import VersionedTransaction
from swap_executor import load_solana_keypair, get_jupiter_quote, SOL_MINT
from config import HELIUS_RPC_URL  # unused but kept for import chain

TARGET = "GsS8NKf3Fd1kqMeiinQKRppYgfDpPNMEKf8eRpgme5km"
RPC = "https://solana.publicnode.com"

async def try_buy(priority_lamports: int):
    kp = await load_solana_keypair("data/keypair.json")
    print(f"\n🎯 Buy 0.003 SOL of {TARGET[:20]}... | Jupiter priority={priority_lamports} ({priority_lamports/1e9:.5f} SOL)")
    quote = await get_jupiter_quote(SOL_MINT, TARGET, 3_000_000, 1000)
    if not quote:
        print("  no quote"); return
    async with aiohttp.ClientSession() as session:
        async with session.post("https://api.jup.ag/swap/v1/swap", json={
            "quoteResponse": quote,
            "userPublicKey": str(kp.pubkey()),
            "wrapAndUnwrapSol": True,
            "prioritizationFeeLamports": priority_lamports,
        }, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                print(f"  swap api {resp.status}: {(await resp.text())[:200]}"); return
            data = await resp.json()
        tx_b64 = data.get("swapTransaction") or data.get("transaction","")
        tx = VersionedTransaction(VersionedTransaction.from_bytes(base64.b64decode(tx_b64)).message, [kp])
        async with session.post(RPC, json={
            "jsonrpc":"2.0","id":1,"method":"sendTransaction",
            "params":[base64.b64encode(bytes(tx)).decode(), {"encoding":"base64","skipPreFlight":True,"maxRetries":3}],
        }, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            d = await resp.json()
            sig = d.get("result")
            if not sig:
                print(f"  submit failed: {d}"); return
            print(f"  sig: {sig}")

        # Poll 30s
        for i in range(10):
            await asyncio.sleep(3)
            async with session.post(RPC, json={
                "jsonrpc":"2.0","id":1,"method":"getSignatureStatuses",
                "params":[[sig],{"searchTransactionHistory":True}],
            }) as resp:
                d = await resp.json()
                v = (d.get("result",{}).get("value") or [None])[0]
                if v and v.get("confirmationStatus") in ("confirmed","finalized"):
                    if v.get("err"):
                        print(f"  [{(i+1)*3}s] LANDED WITH ERR: {v.get('err')}")
                        return "err"
                    print(f"  [{(i+1)*3}s] ✅ LANDED")
                    return "ok"
        print(f"  ⏳ never confirmed in 30s")
        return "dropped"

async def main():
    # Try just 1_000_000 (0.001 SOL) — if it lands, we're done
    result = await try_buy(1_000_000)
    print(f"\nResult: {result}")

if __name__ == "__main__":
    asyncio.run(main())
