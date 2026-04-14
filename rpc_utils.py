"""
Shared RPC helper with Helius-primary, public-fallback logic.
Handles HTTP 429 from Helius free tier automatically.

Key findings (tested 2026-04-13):
- solana.publicnode.com returns getTransaction data ~300ms after a
  processed WS notification, faster than api.mainnet-beta.solana.com
- api.mainnet-beta.solana.com sometimes returns null result for
  getTransaction immediately after a logsSubscribe notification even
  with commitment=confirmed
- So publicnode is listed first in PUBLIC_RPC_ENDPOINTS
"""


async def rpc_post(payload: dict, timeout: float = 15.0,
                   fallthrough_on_null_result: bool = False) -> dict:
    """
    POST a JSON-RPC payload to Helius RPC first; fall back to public RPCs
    on HTTP 429, connection errors, or (optionally) null result.

    Args:
        fallthrough_on_null_result: if True, try the next URL when the
            response has "result": null.  Use this for getTransaction so
            we don't stop at a node that hasn't propagated the tx yet.
    """
    import aiohttp
    from config import HELIUS_RPC_URL, PUBLIC_RPC_ENDPOINTS

    urls = [HELIUS_RPC_URL] + PUBLIC_RPC_ENDPOINTS
    for url in urls:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    if resp.status == 429 or resp.status != 200:
                        continue
                    data = await resp.json()
                    if fallthrough_on_null_result and data.get("result") is None:
                        continue
                    return data
        except Exception:
            continue
    return {}
