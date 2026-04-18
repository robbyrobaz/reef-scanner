"""
Reef Copy Trading Engine
========================
Monitors target wallets and copies their trades in real-time.

Detection stack (fastest → slowest):
  1. Solana logsSubscribe WS (public RPC) — per-wallet, ~300-500ms latency
  2. PumpPortal WS                        — bonding-curve only, milliseconds
  3. Polling loop (public RPC)            — fallback, every 5s

Signal quality filters:
  • Token cooldown (TOKEN_COOLDOWN_S): don't buy same token twice in 5 min
  • Consensus (MIN_WALLETS_CONSENSUS): require N watched wallets buying same
    token within CONSENSUS_WINDOW_S before executing (1 = disabled)
  • Age filter (MAX_TRADE_AGE_S): skip stale polling trades

Run: python copy_engine.py          # paper mode (from config)
     python copy_engine.py --live   # force live
"""

import asyncio
import csv
import json
import os
import sys
import time
from collections import deque
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    HELIUS_API_KEY,
    COPY_ENGINE_INTERVAL_S,
    COPY_MIN_ALLOC_SOL,
    COPY_MAX_ALLOC_SOL,
    COPY_TRADES_FILE,
    DATA_DIR,
)
from copy_config import load_copy_config, save_copy_config, CopyConfig, CopyEntry, config_lock
from swap_parser import parse_transaction_for_swaps, ParsedSwap
import websockets

from swap_executor import execute_swap_legacy, load_solana_keypair, SwapResult
from pumpfun_executor import execute_pumpfun_swap
from pumpswap_executor import execute_pumpswap
from positions import load_positions, save_positions, refresh_positions

# ── Engine state ─────────────────────────────────────────────────────────────
DRY_RUN = True
KEYPAIR_LOADED = None
POSITIONS: Dict = {}

# ── Signal quality config ────────────────────────────────────────────────────
# Don't re-buy the same token within this window (prevents 4x buys of dead tokens)
TOKEN_COOLDOWN_S = int(os.getenv("TOKEN_COOLDOWN_S", "300"))  # 5 min default

# Require N distinct watched wallets to buy the same token within the window
# before we execute. Set to 1 to disable (copy every signal). 2+ = consensus mode.
MIN_WALLETS_CONSENSUS = int(os.getenv("MIN_WALLETS_CONSENSUS", "1"))
CONSENSUS_WINDOW_S    = int(os.getenv("CONSENSUS_WINDOW_S", "15"))  # seconds

# Skip polling trades older than this (pump tokens die fast — polling is slow fallback)
MAX_TRADE_AGE_S = int(os.getenv("COPY_MAX_TRADE_AGE_S", "60"))

# Auto-close paper positions that have been open longer than this without a sell signal
STALE_POSITION_HOURS = float(os.getenv("STALE_POSITION_HOURS", "24"))

# Force-close disabled by default — the earlier 5-min default was based on paper
# buckets computed with corrupt source_price data. True hold distribution in the
# 30d real-swap mine: p25=15min, p50=32min, p75=210min. Only 7% of profitable
# wallets hold <5min. A 5-min cutoff was aggressively cutting winners.
# Set LIVE_FORCE_EXIT_MIN env var if you want force-exit back (in minutes).
LIVE_FORCE_EXIT_MIN = float(os.getenv("LIVE_FORCE_EXIT_MIN", "0"))  # 0 = disabled

# Watch-mode slip simulation: after a source signal, wait this many seconds
# then fetch a Jupiter quote to simulate what we'd have actually filled at.
# 4s matches our observed live execution lag (2-6s range). Set to 0 to disable
# and fall back to source-price-as-fill (old behavior).
WATCH_SIM_LAG_S = float(os.getenv("WATCH_SIM_LAG_S", "4.0"))
# Slip gate for live BUYs: skip if Jupiter quote at T+2s is more than this %
# above source price. 5% is the default — stops us from chasing already-pumped
# entries while allowing normal drift. Set env to tune.
LIVE_SLIP_GATE_PCT = float(os.getenv("LIVE_SLIP_GATE_PCT", "5.0"))

# ── Shared state ─────────────────────────────────────────────────────────────
# Dedup: sigs seen by any listener (prevents double-execution across WS + polling)
# Uses a parallel deque to maintain insertion order for proper FIFO eviction.
# Evicting random set elements (the naive approach) risks discarding recent sigs,
# which would cause double-execution in live mode.
_SEEN_SIGS: set = set()
_SEEN_SIGS_QUEUE: deque = deque()
_SEEN_SIGS_MAX = 20_000

def _seen_add(sig: str) -> None:
    if sig in _SEEN_SIGS:
        return
    if len(_SEEN_SIGS) >= _SEEN_SIGS_MAX:
        old = _SEEN_SIGS_QUEUE.popleft()  # evict oldest, not random
        _SEEN_SIGS.discard(old)
    _SEEN_SIGS.add(sig)
    _SEEN_SIGS_QUEUE.append(sig)

# Per-token cooldown: mint → timestamp of last BUY we executed
_token_cooldown: Dict[str, float] = {}

# Consensus buffer: mint → list of (wallet, timestamp, action, sol_amt, pool_addr, price)
_signal_buffer: Dict[str, List[Tuple]] = {}
_signal_lock = asyncio.Lock()  # initialized in run_engine


# ── Paper Position Tracking ───────────────────────────────────────────────────
PAPER_POSITIONS_FILE = Path(DATA_DIR) / "paper_positions.json"

def load_paper_positions() -> Dict[str, dict]:
    if not PAPER_POSITIONS_FILE.exists():
        return {}
    try:
        data = json.loads(PAPER_POSITIONS_FILE.read_text())
    except Exception:
        return {}
    # Migrate legacy mint-only keys → composite "legacy::mint" keys with embedded fields.
    # Legacy entries have keys that are bare mint addresses (no "::"). We don't know which
    # wallet opened them, so they'll never match an incoming SELL and will auto-expire at 0%.
    migrated = {}
    for k, v in data.items():
        if "::" in k:
            migrated[k] = v
        else:
            v = dict(v)
            v.setdefault("source_wallet", "legacy")
            v.setdefault("token_mint", k)
            migrated[f"legacy::{k}"] = v
    return migrated

def save_paper_positions(positions: Dict[str, dict]) -> None:
    os.makedirs(os.path.dirname(PAPER_POSITIONS_FILE), exist_ok=True)
    tmp = PAPER_POSITIONS_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(positions))
    tmp.rename(PAPER_POSITIONS_FILE)

def record_paper_trade_pnl(trade: "CopyTrade", positions: Dict[str, dict]) -> Optional[float]:
    """
    Returns realized PnL (SOL) on SELL, 0.0 on BUY (position opened), or None if
    this SELL has no matching BUY position (caller should skip recording the trade).

    Uses our ACTUAL fill price (trade.our_price_sol, set from on-chain receipt
    in live mode) when available — otherwise source price as fallback for paper.
    """
    key = f"{trade.source_wallet}::{trade.token_mint}"
    # Prefer our real fill price; fall back to source's price only if unknown
    price = trade.our_price_sol if trade.our_price_sol > 0 else trade.source_price_sol
    if trade.action == "BUY":
        if price <= 0:
            return None
        if key in positions:
            return None
        positions[key] = {
            "source_wallet": trade.source_wallet,
            "token_mint": trade.token_mint,
            "entry_price": price,  # our actual BUY fill (live) or source price (paper)
            "scaled_amount": trade.scaled_amount_sol,
            "timestamp": trade.timestamp,
        }
        return 0.0
    elif trade.action == "SELL" and key in positions:
        if price <= 0:
            return None
        pos = positions.pop(key)
        token_count = pos["scaled_amount"] / pos["entry_price"]
        # PnL uses our actual SELL fill (live) or source price (paper).
        pnl = (price - pos["entry_price"]) * token_count
        # Sanity: cap absurd PnL (e.g. tiny entry_price + garbage sell price → blow-up).
        # Scaled amount is the cost basis; realistic max gain is ~100x = 100× basis.
        cap = max(pos["scaled_amount"] * 200.0, 1.0)
        if abs(pnl) > cap:
            print(f"    ⚠ clamped absurd PnL {pnl:.2e} → 0 for {trade.token_mint[:16]}... (entry={pos['entry_price']:.3e} exit={price:.3e})")
            return 0.0
        return pnl
    return None


# ── Copy Trade Record ─────────────────────────────────────────────────────────
@dataclass
class CopyTrade:
    timestamp: int
    source_wallet: str
    source_sig: str
    our_wallet: str
    our_sig: str = ""
    action: str = ""
    token_mint: str = ""
    amount_sol: float = 0.0
    scaled_amount_sol: float = 0.0
    source_price_sol: float = 0.0
    our_price_sol: float = 0.0
    status: str = "pending"
    error: str = ""
    realized_pnl_sol: float = 0.0
    pool_address: str = ""


def save_copy_trade(trade: CopyTrade) -> None:
    fields = [
        "timestamp", "source_wallet", "source_sig", "our_wallet",
        "our_sig", "action", "token_mint", "amount_sol",
        "scaled_amount_sol", "source_price_sol", "our_price_sol",
        "status", "error", "realized_pnl_sol",
    ]
    os.makedirs(os.path.dirname(COPY_TRADES_FILE), exist_ok=True)
    file_exists = os.path.exists(COPY_TRADES_FILE)
    try:
        with open(COPY_TRADES_FILE, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            if not file_exists:
                writer.writeheader()
            writer.writerow({
                "timestamp": trade.timestamp,
                "source_wallet": trade.source_wallet,
                "source_sig": trade.source_sig,
                "our_wallet": trade.our_wallet,
                "our_sig": trade.our_sig,
                "action": trade.action,
                "token_mint": trade.token_mint,
                "amount_sol": round(trade.amount_sol, 6),
                "scaled_amount_sol": round(trade.scaled_amount_sol, 6),
                "source_price_sol": round(trade.source_price_sol, 9),
                "our_price_sol": round(trade.our_price_sol, 9),
                "status": trade.status,
                "error": str(trade.error)[:200],
                "realized_pnl_sol": round(trade.realized_pnl_sol, 9),
            })
    except OSError as e:
        print(f"  ⚠️  save_copy_trade failed: {e}")


# ── Token cooldown helpers ────────────────────────────────────────────────────
def _is_token_on_cooldown(mint: str) -> bool:
    last = _token_cooldown.get(mint, 0)
    if time.time() - last < TOKEN_COOLDOWN_S:
        ago = int(time.time() - last)
        print(f"    ⏩ {mint[:16]}... on cooldown ({ago}s / {TOKEN_COOLDOWN_S}s) — skip")
        return True
    return False

def _mark_token_bought(mint: str) -> None:
    _token_cooldown[mint] = time.time()


# ── RPC Helpers ───────────────────────────────────────────────────────────────
async def get_signatures_for_address(address: str, limit: int = 10) -> List[dict]:
    from rpc_utils import rpc_post
    data = await rpc_post({
        "jsonrpc": "2.0", "id": 1,
        "method": "getSignaturesForAddress",
        "params": [address, {"commitment": "confirmed", "limit": limit}],
    })
    return data.get("result", [])


async def get_transaction(sig: str) -> Optional[dict]:
    from rpc_utils import rpc_post
    # fallthrough_on_null_result=True: if one RPC node hasn't propagated
    # the tx yet (returns null), try the next one rather than stopping.
    # commitment=confirmed so it's available ~400ms after processed WS notification.
    data = await rpc_post({
        "jsonrpc": "2.0", "id": 1,
        "method": "getTransaction",
        "params": [sig, {"encoding": "jsonParsed",
                         "maxSupportedTransactionVersion": 0,
                         "commitment": "confirmed"}],
    }, fallthrough_on_null_result=True)
    return data.get("result")


# ── Swap Execution ────────────────────────────────────────────────────────────
SOL_MINT = "So11111111111111111111111111111111111111112"

# Public RPC for chain-confirmation polling. PumpPortal and Jupiter return
# success on RPC-accept (tx submitted, signature issued) which is NOT the same
# as on-chain confirmation — txs die in mempool all the time. Only PumpSwap SDK
# confirms internally. Poll ourselves for the other two paths so CSV "confirmed"
# status matches reality.
# Token decimals cache — mint → decimals. Fetched once per mint via getTokenSupply.
_DECIMALS_CACHE: Dict[str, int] = {}


async def _get_token_decimals(mint: str) -> int:
    """Return token decimals (cached). Default 6 for pump-amm tokens."""
    if mint in _DECIMALS_CACHE:
        return _DECIMALS_CACHE[mint]
    import aiohttp
    for rpc in ["https://solana.publicnode.com", "https://api.mainnet-beta.solana.com"]:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(rpc, json={
                    "jsonrpc": "2.0", "id": 1, "method": "getTokenSupply",
                    "params": [mint],
                }, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status != 200: continue
                    d = await resp.json()
                    dec = (d.get("result", {}).get("value") or {}).get("decimals")
                    if dec is not None:
                        _DECIMALS_CACHE[mint] = int(dec)
                        return int(dec)
        except Exception:
            continue
    _DECIMALS_CACHE[mint] = 6  # pump.fun default
    return 6


async def _simulate_live_quote_price(action: str, token_mint: str, amount_sol: float) -> Optional[float]:
    """Simulate Jupiter fill at this moment (no actual swap).
    Returns SOL per UI-token (same unit as swap_parser's price_sol).

    For BUY: query SOL→token quote; price = SOL spent / tokens received.
    For SELL: always query SOL→token direction (symmetric on AMMs) and
      use it as the current market reference price, since we don't have
      a token balance at this point. Accuracy is ~1% vs true sell price
      on normal pools — plenty for slip estimation.
    """
    if amount_sol <= 0 or not token_mint:
        return None
    import aiohttp
    token_dec = await _get_token_decimals(token_mint)
    # Always query the SOL→TOKEN direction for consistent market reference
    amount_lamports = int(amount_sol * 1e9)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("https://api.jup.ag/swap/v1/quote", params={
                "inputMint": SOL_MINT, "outputMint": token_mint,
                "amount": amount_lamports, "slippageBps": 500,
            }, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                if resp.status != 200:
                    return None
                q = await resp.json()
                in_a = int(q.get("inAmount", 0) or 0)  # SOL lamports
                out_a = int(q.get("outAmount", 0) or 0)  # token base units
                if in_a <= 0 or out_a <= 0:
                    return None
                # SOL per UI-token: (in_a / 1e9) / (out_a / 10^dec)
                return (in_a * (10 ** token_dec)) / (out_a * 1e9)
    except Exception:
        return None


async def _fetch_actual_fill(sig: str, action: str, token_mint: str, wallet_pubkey: str) -> Optional[float]:
    """
    Read a confirmed tx from chain and compute the ACTUAL price we filled at.
    For BUY:  price = SOL_spent / tokens_received
    For SELL: price = SOL_received / tokens_sold
    Uses real pre/post balances, not Jupiter's quote (which is pre-slippage).
    Returns None on any lookup failure (caller falls back to source_price_sol).
    """
    if not sig or sig in ("confirmed", "DRY_RUN", "DRY_RUN_SIG"):
        return None
    import aiohttp
    for rpc in ["https://api.mainnet-beta.solana.com", "https://solana.publicnode.com"]:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(rpc, json={
                    "jsonrpc": "2.0", "id": 1, "method": "getTransaction",
                    "params": [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}],
                }, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200: continue
                    d = await resp.json()
                    tx = d.get("result")
                    if not tx: continue
                    meta = tx.get("meta", {})
                    if meta.get("err"): return None

                    # Find wallet index in accountKeys
                    account_keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
                    wallet_idx = None
                    for i, k in enumerate(account_keys):
                        pubkey = k.get("pubkey") if isinstance(k, dict) else k
                        if pubkey == wallet_pubkey:
                            wallet_idx = i; break
                    if wallet_idx is None: return None

                    # Native SOL delta (post - pre), plus fee (we paid it)
                    pre_sol  = meta.get("preBalances",  [])
                    post_sol = meta.get("postBalances", [])
                    if len(pre_sol) <= wallet_idx or len(post_sol) <= wallet_idx: return None
                    fee = meta.get("fee", 0)
                    # On BUY: wallet SOL decreased by (SOL_spent + fee). Reverse: SOL_spent = pre - post - fee
                    # On SELL: wallet SOL increased by (SOL_received - fee). Reverse: SOL_received = post - pre + fee
                    if action == "BUY":
                        sol_amt = (pre_sol[wallet_idx] - post_sol[wallet_idx] - fee) / 1e9
                    else:
                        sol_amt = (post_sol[wallet_idx] - pre_sol[wallet_idx] + fee) / 1e9
                    if sol_amt <= 0: return None

                    # Token delta: find the wallet's token account for target mint
                    pre_tokens  = {b["accountIndex"]: b for b in meta.get("preTokenBalances",  [])}
                    post_tokens = {b["accountIndex"]: b for b in meta.get("postTokenBalances", [])}
                    indices = set(pre_tokens.keys()) | set(post_tokens.keys())
                    token_delta_raw = 0
                    decimals = 6
                    for idx in indices:
                        pre  = pre_tokens.get(idx, {})
                        post = post_tokens.get(idx, {})
                        mint = post.get("mint") or pre.get("mint")
                        owner = post.get("owner") or pre.get("owner")
                        if mint != token_mint or owner != wallet_pubkey: continue
                        pre_amt  = int((pre.get("uiTokenAmount") or {}).get("amount",  0) or 0)
                        post_amt = int((post.get("uiTokenAmount") or {}).get("amount", 0) or 0)
                        decimals = int((post.get("uiTokenAmount") or pre.get("uiTokenAmount") or {}).get("decimals", 6))
                        token_delta_raw = abs(post_amt - pre_amt)
                    if token_delta_raw <= 0: return None
                    token_amount = token_delta_raw / (10 ** decimals)
                    if token_amount <= 0: return None
                    return sol_amt / token_amount
        except Exception:
            continue
    return None


async def _close_empty_ata(keypair, token_mint: str) -> bool:
    """Close an empty token account to reclaim ~0.002 SOL rent. Silent no-op on
    any failure — best-effort rent recovery, not critical path."""
    try:
        import aiohttp, base64
        from solders.pubkey import Pubkey
        from solders.instruction import Instruction, AccountMeta
        from solders.transaction import VersionedTransaction
        from solders.message import MessageV0

        owner = keypair.pubkey()
        mint_pk = Pubkey.from_string(token_mint)
        TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
        ATA_PROGRAM   = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
        # Derive associated token account
        ata, _ = Pubkey.find_program_address(
            [bytes(owner), bytes(TOKEN_PROGRAM), bytes(mint_pk)], ATA_PROGRAM
        )
        # closeAccount instruction: discriminator=9, no data; accounts [ata, dest=owner, authority=owner]
        ix = Instruction(
            program_id=TOKEN_PROGRAM,
            accounts=[
                AccountMeta(pubkey=ata,   is_signer=False, is_writable=True),
                AccountMeta(pubkey=owner, is_signer=False, is_writable=True),
                AccountMeta(pubkey=owner, is_signer=True,  is_writable=False),
            ],
            data=bytes([9]),
        )
        # Fresh blockhash
        async with aiohttp.ClientSession() as s:
            async with s.post("https://api.mainnet-beta.solana.com", json={
                "jsonrpc":"2.0","id":1,"method":"getLatestBlockhash","params":[{"commitment":"confirmed"}],
            }, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status != 200: return False
                blockhash_str = (await resp.json())["result"]["value"]["blockhash"]
            from solders.hash import Hash
            msg = MessageV0.try_compile(owner, [ix], [], Hash.from_string(blockhash_str))
            tx = VersionedTransaction(msg, [keypair])
            async with s.post("https://api.mainnet-beta.solana.com", json={
                "jsonrpc":"2.0","id":1,"method":"sendTransaction",
                "params":[base64.b64encode(bytes(tx)).decode(),
                          {"encoding":"base64","skipPreFlight":True,"maxRetries":3}],
            }, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                d = await resp.json()
                if "result" in d:
                    print(f"    🧹 closed empty ATA for {token_mint[:16]}... sig={d['result'][:20]}...")
                    return True
    except Exception as e:
        pass
    return False


async def _wait_for_confirmation(sig: str, timeout_s: float = 45.0) -> bool:
    if not sig or sig in ("confirmed", "DRY_RUN", "DRY_RUN_SIG"):
        return True  # PumpSwap confirms internally; DRY_RUN sentinels pass through
    import aiohttp
    rpcs = ["https://solana.publicnode.com", "https://api.mainnet-beta.solana.com"]
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for rpc in rpcs:
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.post(rpc, json={
                        "jsonrpc": "2.0", "id": 1,
                        "method": "getSignatureStatuses",
                        "params": [[sig], {"searchTransactionHistory": False}],
                    }, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.json()
                        val = (data.get("result", {}).get("value") or [None])[0]
                        if val and val.get("confirmationStatus") in ("confirmed", "finalized"):
                            if val.get("err"):
                                return False  # tx ran on-chain but errored
                            return True
            except Exception:
                continue
        await asyncio.sleep(2.0)
    return False


async def execute_copy_trade(trade: CopyTrade) -> bool:
    global KEYPAIR_LOADED
    if KEYPAIR_LOADED is None:
        KEYPAIR_LOADED = await load_solana_keypair()
    if KEYPAIR_LOADED is None:
        trade.error = "No keypair"
        return False

    try:
        # Jupiter is the single execution path. Verified Apr 17:
        #   - BONK round-trip landed in 3s each direction at 0.00025 priority
        #   - pump-amm targets quote cleanly via Jupiter's "Pump.fun Amm" route
        # The vendored pump_swap SDK path silently aborts on missing creator vault
        # for many pools; PumpPortal returns 400 for graduated tokens. Both removed.
        # Jupiter handles bonding-curve, pump-amm, Raydium, Orca — one path, one
        # priority fee dial, one confirmation check. 1000bps slippage handles the
        # volatile pump-amm routes (price impact is typically <0.1%; slippage budget
        # absorbs the 2-6s execution lag vs source).
        in_mint  = SOL_MINT if trade.action == "BUY" else trade.token_mint
        out_mint = trade.token_mint if trade.action == "BUY" else SOL_MINT
        # Slippage 500 bps (5%) — 1000 bps was inviting sandwich attacks while
        # still failing on MEV-hot pools. Tighter slip sacrifices some fills but
        # protects the ones that do land from being eaten.
        result = await execute_swap_legacy(
            KEYPAIR_LOADED, in_mint, out_mint,
            trade.scaled_amount_sol, slippage_bps=500,
        )

        if result.success:
            trade.our_sig = result.signature
            trade.our_price_sol = result.price_sol if result.price_sol > 0 else trade.source_price_sol
            # Chain-confirm for PumpPortal and Jupiter paths — both return success
            # on RPC accept, not on-chain landing. Without this, "confirmed" status
            # in the CSV/dashboard is a lie for txs that die in mempool.
            confirmed = await _wait_for_confirmation(result.signature)
            if not confirmed:
                trade.error = "submitted but not confirmed on-chain within 45s"
                print(f"    ⏳ {trade.action} {trade.token_mint[:16]}... submitted but not confirmed — marking failed")
                return False
            # ANTI-GHOST: getSignatureStatuses can briefly report "confirmed" for a tx
            # that later forks out or expires with blockhash. We need to VERIFY the tx
            # actually moved tokens to us, not just that it reached some confirmation
            # state momentarily. Retry the fill lookup — if we still can't confirm
            # real token delta, mark the trade failed rather than carry a ghost position.
            real_price = None
            for attempt in range(3):
                real_price = await _fetch_actual_fill(
                    result.signature, trade.action, trade.token_mint, str(KEYPAIR_LOADED.pubkey())
                )
                if real_price and real_price > 0:
                    break
                await asyncio.sleep(3)
            if not real_price or real_price <= 0:
                trade.error = "confirmed but no token delta — likely ghost tx (orphaned or failed inner swap)"
                print(f"    👻 {trade.action} {trade.token_mint[:16]}... no token delta after confirm — marking failed")
                return False
            # Real fill succeeded — log slip if significant
            if trade.action == "BUY":
                slip_pct = (real_price - trade.source_price_sol) / trade.source_price_sol * 100 if trade.source_price_sol > 0 else 0
            else:
                slip_pct = (trade.source_price_sol - real_price) / trade.source_price_sol * 100 if trade.source_price_sol > 0 else 0
            if abs(slip_pct) > 0.5:
                print(f"    🎯 real fill: {real_price:.3e} (source {trade.source_price_sol:.3e}, slip {slip_pct:+.2f}%)")
            trade.our_price_sol = real_price
            return True
        trade.error = result.error
        print(f"    ❌ exec failed ({trade.action} {trade.token_mint[:16]}...): {result.error[:180]}")
        return False
    except Exception as e:
        trade.error = str(e)
        return False


# ── Signal execution (shared by all listeners) ────────────────────────────────
async def _execute_signal(
    action: str,
    token_mint: str,
    sol_amt: float,
    price_sol: float,
    source_wallet: str,
    source_sig: str,
    pool_address: str,
    paper_positions: Dict,
    config: CopyConfig,
    label: str = "",
) -> None:
    """Execute or paper-record a single copy trade signal."""
    entry = config.copies.get(source_wallet)
    if not entry or not entry.enabled:
        return

    scale  = min(1.0, entry.alloc_sol / max(sol_amt, 0.0001))
    scaled = round(sol_amt * scale, 9)
    scaled = max(COPY_MIN_ALLOC_SOL, min(COPY_MAX_ALLOC_SOL, scaled))

    trade = CopyTrade(
        timestamp=int(time.time()),
        source_wallet=source_wallet,
        source_sig=source_sig,
        our_wallet=config.user_wallet,
        action=action,
        token_mint=token_mint,
        amount_sol=sol_amt,
        scaled_amount_sol=scaled,
        source_price_sol=price_sol,
        pool_address=pool_address,
    )

    tag = f"[{label}] " if label else ""
    # Re-read trade_mode from config at execution time (belt-and-suspenders safety)
    _live = not DRY_RUN and config.trade_mode == "live"

    # Per-wallet override: if this wallet is set to copy_mode="watch", force paper
    # simulation even when engine is live. Lets us evaluate candidate wallets without
    # committing real SOL to them.
    is_watch = False
    if _live and entry.copy_mode == "watch":
        _live = False
        tag = f"[watch:{label}] " if label else "[watch] "
        is_watch = True

    # Strategy = "large_order": only act when source amount_sol >= min_source_sol.
    # Below threshold, skip entirely (don't record — keeps bucket pure).
    is_large_order = getattr(entry, "strategy", "default") == "large_order"
    if is_large_order:
        threshold = getattr(entry, "min_source_sol", 0.0) or 0.0
        if sol_amt < threshold:
            return  # source's own trade too small; ignore
        tag = f"[watch_large:{label}] " if label else "[watch_large] "
        is_watch = True  # treat as watch regardless of copy_mode
        _live = False

    # Skip LIVE SELL if we never opened this (source_wallet, mint) position.
    # Saves wasted RPC calls + CSV pollution on "No balance to sell" failures.
    # Many source SELLs signal mints we never bought (offline, cooldowns, etc.).
    if _live and action == "SELL":
        pos_key = f"{source_wallet}::{token_mint}"
        if pos_key not in paper_positions:
            return  # never opened — nothing to close

    # Skip live BUY if we're already holding this (source_wallet, mint) position.
    # The 5-min token cooldown prevents back-to-back BUYs but expires while we
    # might still hold the first position, causing silent double-buys (saw this
    # Apr 17 on C7cYcU7: BUY→SELL→BUY→BUY→SELL left 0.01 SOL stuck in tokens).
    if _live and action == "BUY":
        pos_key = f"{source_wallet}::{token_mint}"
        if pos_key in paper_positions:
            print(f"  ⏭  {tag}LIVE BUY SKIP — already holding {token_mint[:16]}... from this wallet")
            return
    if not _live:
        # For WATCH mode: simulate the real execution lag by fetching a Jupiter
        # quote ~4s after the source signal. This captures actual slip the way
        # live would experience it, without spending SOL. Set env WATCH_SIM_LAG_S=0
        # to disable (and fall back to source-price-as-fill paper behavior).
        trade.our_price_sol = price_sol  # start with source price as fallback
        if is_watch and WATCH_SIM_LAG_S > 0:
            await asyncio.sleep(WATCH_SIM_LAG_S)
            simulated_price = await _simulate_live_quote_price(action, token_mint, scaled)
            if simulated_price and simulated_price > 0:
                # Sanity check: Jupiter occasionally returns garbage for illiquid
                # mints or routing edge cases. If simulated price is >3x or <1/3x
                # the source price, discard and fall back to source-price paper.
                # Real slip in live data was ~0-30% for normal wallets, ~50% worst
                # case for the most sniped wallets. Anything past 200% is noise.
                ratio = simulated_price / price_sol if price_sol > 0 else 0
                if 0.33 <= ratio <= 3.0:
                    trade.our_price_sol = simulated_price
                    slip_pct = (ratio - 1) * 100
                    if abs(slip_pct) >= 1.0:
                        print(f"    📊 watch-sim slip {action} {token_mint[:16]}...: {slip_pct:+.1f}% (src {price_sol:.3e} vs sim {simulated_price:.3e})")
                else:
                    # Garbage quote — log once but don't use it
                    print(f"    ⚠ watch-sim discarded bad quote {action} {token_mint[:16]}...: ratio {ratio:.2e} (src {price_sol:.3e} vs sim {simulated_price:.3e})")
        pnl = record_paper_trade_pnl(trade, paper_positions)
        if pnl is None:
            # No valid position to open/close — skip recording this trade entirely
            print(f"  ⚪ {tag}PAPER {action} skipped (no price or no matching BUY) → {token_mint[:16]}...")
            return
        trade.realized_pnl_sol = pnl
        trade.status = "dry_run"
        # Tag watch-mode rows in error field so dashboard can separate ongoing
        # evaluation (watch) from historical backtest (pre-live paper).
        if is_watch:
            trade.error = "watch_large" if is_large_order else "watch_mode"
        pnl_str = f" pnl={trade.realized_pnl_sol:+.6f}" if trade.realized_pnl_sol else ""
        print(f"  🐸 {tag}PAPER {action} {scaled:.4f} SOL → {token_mint[:16]}...{pnl_str}")
        save_copy_trade(trade)
        save_paper_positions(paper_positions)
    else:
        # ── Slip gate for live BUYs ───────────────────────────────────────────
        # Before committing real SOL, fetch a Jupiter quote at roughly the same
        # lag we'll land at (~4s) and compare vs source price. Skip if adverse
        # beyond LIVE_SLIP_GATE_PCT. Prevents the "+20% entry slip" bleed that
        # killed the Apr 17 live session on sniped wallets.
        if action == "BUY" and price_sol > 0:
            try:
                # brief wait already close to our landing time
                await asyncio.sleep(2.0)
                sim_price = await _simulate_live_quote_price("BUY", token_mint, scaled)
                if sim_price and sim_price > 0:
                    ratio = sim_price / price_sol
                    adverse_pct = (ratio - 1) * 100  # positive = we'd pay more than source
                    if adverse_pct > LIVE_SLIP_GATE_PCT:
                        print(f"  🛑 {tag}LIVE BUY SKIP — slip gate: Jupiter quote {adverse_pct:+.1f}% > {LIVE_SLIP_GATE_PCT}% threshold (src {price_sol:.3e} vs sim {sim_price:.3e})")
                        trade.status = "skipped_slip"
                        trade.error = f"slip_gate_{adverse_pct:.1f}pct"
                        save_copy_trade(trade)
                        # release the cooldown that consensus_processor set
                        _token_cooldown.pop(token_mint, None)
                        return
            except Exception as e:
                print(f"  ⚠ slip gate check errored: {e} — proceeding without gate")
        print(f"  🔴 {tag}LIVE {action} {scaled:.4f} SOL → {token_mint[:16]}...")
        success = await execute_copy_trade(trade)
        trade.status = "confirmed" if success else "failed"
        if success:
            print(f"  📤 {tag}{action} submitted: {scaled:.4f} SOL | {token_mint[:16]}... | {trade.our_sig[:20]}...")
            # PnL from our actual fill prices (fetched from tx receipt in execute_copy_trade)
            pnl = record_paper_trade_pnl(trade, paper_positions)
            if pnl is not None:
                trade.realized_pnl_sol = pnl
                if pnl:
                    pnl_str = f" pnl={pnl:+.6f}"
                    print(f"    💰 {action} closed{pnl_str} SOL")
                save_paper_positions(paper_positions)
            # After a successful SELL, close the empty ATA to reclaim ~0.002 SOL rent.
            # Fire-and-forget background task so we don't block the consensus_processor.
            if action == "SELL":
                asyncio.create_task(_close_empty_ata(KEYPAIR_LOADED, token_mint))
        elif action == "BUY":
            # Release the cooldown that consensus_processor set pre-execute.
            # Otherwise a failed BUY locks us out of this mint for 5 min,
            # causing us to miss valid subsequent BUY signals from other wallets.
            _token_cooldown.pop(token_mint, None)
            print(f"    🔓 cooldown released on failed BUY → {token_mint[:16]}...")
        save_copy_trade(trade)


# ── Consensus processor ───────────────────────────────────────────────────────
async def consensus_processor(paper_positions_ref: Dict) -> None:
    """
    Checks signal buffer and fires trades. Tick rate scales with consensus setting:

    MIN_WALLETS_CONSENSUS=1 → 0.1s tick, near-immediate fire (no benefit to waiting
      when any single signal qualifies; the 2s tick was pure added latency on top
      of our already-2-6s lag behind source)
    MIN_WALLETS_CONSENSUS>=2 → 2s tick, gives wallets time to accumulate before
      the window expires

    CONSENSUS_WINDOW_S is the rolling window for de-duping same-mint signals and
    counting distinct wallets for the consensus vote — independent of tick rate.
    """
    tick = 0.1 if MIN_WALLETS_CONSENSUS == 1 else 2.0
    while True:
        await asyncio.sleep(tick)
        now = time.time()
        config = load_copy_config()

        if not config.global_enabled:
            continue

        # Collect signals to fire BEFORE releasing lock — then execute outside the lock
        # so live RPC calls don't block listeners for 5-30s
        to_fire = []
        async with _signal_lock:
            for mint in list(_signal_buffer.keys()):
                signals = _signal_buffer[mint]
                # Drop expired signals
                fresh = [s for s in signals if now - s[1] < CONSENSUS_WINDOW_S]
                _signal_buffer[mint] = fresh

                if not fresh:
                    del _signal_buffer[mint]
                    continue

                # Pick action with the most wallet support; break ties toward BUY.
                # Previously took the most-recent signal's action, which meant 3 BUY
                # votes could be ignored just because 1 newer SELL failed consensus.
                buy_wallets  = len(set(s[0] for s in fresh if s[2] == "BUY"))
                sell_wallets = len(set(s[0] for s in fresh if s[2] == "SELL"))
                if buy_wallets >= sell_wallets and buy_wallets >= MIN_WALLETS_CONSENSUS:
                    action = "BUY"
                elif sell_wallets >= MIN_WALLETS_CONSENSUS:
                    action = "SELL"
                else:
                    continue
                unique_wallets = buy_wallets if action == "BUY" else sell_wallets

                # Pick the most recent signal of the winning action
                best = sorted((s for s in fresh if s[2] == action), key=lambda s: s[1])[-1]
                wallet, ts, _, sol_amt, pool_addr, price = best

                # Check cooldown before firing
                if action == "BUY" and _is_token_on_cooldown(mint):
                    del _signal_buffer[mint]
                    continue

                if action == "BUY":
                    _mark_token_bought(mint)

                del _signal_buffer[mint]

                n = unique_wallets
                label = f"consensus/{n}w" if n > 1 else "signal"
                print(f"  🎯 {label}: {n} wallet(s) → {action} {mint[:16]}...")
                to_fire.append((action, mint, sol_amt, price, wallet, pool_addr, label))

        # Execute outside the lock so RPC calls don't stall listeners
        for action, mint, sol_amt, price, wallet, pool_addr, label in to_fire:
            await _execute_signal(
                action, mint, sol_amt, price,
                wallet, "", pool_addr,
                paper_positions_ref, config, label,
            )


def _add_signal(wallet: str, action: str, mint: str, sol_amt: float,
                pool_addr: str, price: float) -> None:
    """Add a raw trade signal to the consensus buffer."""
    if mint not in _signal_buffer:
        _signal_buffer[mint] = []
    # Dedup by (wallet, action) — same wallet can have both a BUY and a SELL
    # buffered for the same mint (quick flip). Deduping by wallet alone silently
    # drops the SELL when a BUY from the same wallet is already pending.
    existing = {(s[0], s[2]) for s in _signal_buffer[mint]}
    if (wallet, action) in existing:
        return
    _signal_buffer[mint].append((wallet, time.time(), action, sol_amt, pool_addr, price))


# ── Solana logsSubscribe Listener ─────────────────────────────────────────────
PUMP_AMM_PROG = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
JUPITER_PROG  = "JUP6LkbZbjS3jtsKSqf5joF4BSrFEh7WEZg3Xs5ycD1c"
RAYDIUM_PROG  = "675kPX9MHTjS2zt1qfr1NYHuzeSxPGBY4eNTtRMqDxGD"
DEX_PROGS     = {PUMP_AMM_PROG, JUPITER_PROG, RAYDIUM_PROG}

# WS endpoint priority: Helius first (best SLA, 1M free credits/month),
# fall back to public Solana RPC on HTTP 429 (credits exhausted).
# Format/protocol is identical — both speak standard Solana logsSubscribe.
def _ws_urls() -> list:
    urls = []
    if HELIUS_API_KEY:
        urls.append(f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}")
    urls.append("wss://api.mainnet-beta.solana.com")
    return urls


async def helius_logs_listener(shard_wallets: Optional[List[str]] = None, shard_label: str = "") -> None:
    """
    Subscribe to logsSubscribe for each watched wallet.
    Tries Helius WS first (better SLA); falls back to public Solana RPC
    automatically when Helius returns HTTP 429 (monthly credits exhausted).
    Fires within ~300-500ms of a transaction landing.

    For each notification:
      1. Check logs mention a known DEX program
      2. Fetch full transaction via public RPC (async, non-blocking)
      3. Parse for swaps
      4. Add to consensus buffer
    """
    delay = 2
    ws_url_index = 0  # start with Helius

    # shard_wallets/shard_label are passed in by main() so we can spawn multiple
    # listener tasks, each owning up to ~80 subs, to stay under public-RPC's
    # per-connection cap (seen as 1013 close with 170 subs on one connection).
    while True:
        try:
            config = load_copy_config()
            if shard_wallets is not None:
                wallets = [w for w in shard_wallets
                           if config.copies.get(w) and config.copies[w].enabled
                           and w != config.user_wallet]
            else:
                wallets = [w for w, e in config.copies.items()
                           if e.enabled and w != config.user_wallet]
            if not wallets:
                await asyncio.sleep(30)
                continue

            urls = _ws_urls()
            ws_url = urls[ws_url_index % len(urls)]
            label = ("Helius" if "helius" in ws_url else "Solana public") + (f"-{shard_label}" if shard_label else "")
            print(f"  🔔 Solana WS ({label}): connecting ({len(wallets)} wallets)...")
            async with websockets.connect(
                ws_url,
                ping_interval=None,
                close_timeout=10,
                max_size=10 * 1024 * 1024,
            ) as ws:
                # Map: request_id → wallet, subscription_id → wallet
                req_map: Dict[int, str] = {}
                sub_map: Dict[int, str] = {}

                # Throttle subscription rate. Public RPC closes the socket with
                # 1013 "too many subscriptions attempted" when we flood >~100 subs
                # instantly. 50ms per request = 8.5s to subscribe 170 wallets;
                # well under any rate limit and reliably stays connected.
                for i, wallet in enumerate(wallets):
                    req_id = 200 + i
                    req_map[req_id] = wallet
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "method": "logsSubscribe",
                        "params": [
                            {"mentions": [wallet]},
                            {"commitment": "processed"},
                        ],
                    }))
                    if label != "Helius":
                        await asyncio.sleep(0.05)  # 50ms between subs on public RPC

                print(f"  ✅ Solana WS ({label}): {len(wallets)} subscriptions active")
                delay = 2        # reset backoff on successful connect
                # Don't reset ws_url_index — we were thrashing between Helius (429)
                # and public on every reconnect. Stay on whichever URL succeeded.
                # Engine restart will try Helius again; in-session we stick with
                # the working endpoint.
                _ws_connected_at = time.time()

                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=300.0)
                    except asyncio.TimeoutError:
                        print(f"  ⚠️  Solana WS: no message in 5m — reconnecting")
                        break
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue

                    # Subscription confirmations
                    if isinstance(msg.get("result"), int) and msg.get("id") in req_map:
                        sub_map[msg["result"]] = req_map[msg["id"]]
                        continue

                    if msg.get("method") != "logsNotification":
                        continue

                    params = msg.get("params", {})
                    sub_id = params.get("subscription")
                    wallet = sub_map.get(sub_id)
                    if not wallet:
                        continue

                    value = params.get("result", {}).get("value", {})
                    sig   = value.get("signature", "")
                    err   = value.get("err")
                    logs  = value.get("logs", [])

                    if err or not sig or sig in _SEEN_SIGS:
                        continue

                    # Quick pre-filter: only care about DEX transactions
                    log_str = " ".join(logs)
                    if not any(p in log_str for p in DEX_PROGS):
                        continue

                    _seen_add(sig)

                    # Fetch + parse asynchronously (don't block the WS read loop)
                    asyncio.create_task(_process_helius_sig(sig, wallet))

        except (websockets.ConnectionClosed, OSError, ConnectionError) as e:
            err_str = str(e)
            if "429" in err_str and ws_url_index == 0:
                # Helius credits exhausted — switch to public endpoint immediately
                ws_url_index = 1
                print(f"  ⚠️  Helius WS 429 (credits exhausted) — switching to public endpoint")
                delay = 2  # no backoff needed, it's a planned fallback
            else:
                print(f"  ⚠️  Solana WS closed: {e} — reconnect in {delay}s")
        except Exception as e:
            err_str = str(e)
            if "429" in err_str and ws_url_index == 0:
                ws_url_index = 1
                print(f"  ⚠️  Helius WS 429 (credits exhausted) — switching to public endpoint")
                delay = 2
            else:
                print(f"  ❌ Solana WS error: {e} — reconnect in {delay}s")

        await asyncio.sleep(delay)
        delay = min(delay * 2, 60)


async def _process_helius_sig(sig: str, wallet: str) -> None:
    """Fetch a transaction via public RPC and add signals to the consensus buffer."""
    tx = await get_transaction(sig)
    if not tx:
        return
    swaps = parse_transaction_for_swaps(tx)
    if not swaps:
        return

    config = load_copy_config()
    if not config.copies.get(wallet):
        return

    async with _signal_lock:
        for swap in swaps:
            if swap.price_sol <= 0:
                continue  # no price = unusable signal regardless of action
            if swap.action == "SELL":
                _add_signal(wallet, "SELL", swap.token_mint,
                            swap.amount_sol, swap.pool_address, swap.price_sol)
            else:
                if not _is_token_on_cooldown(swap.token_mint):
                    _add_signal(wallet, "BUY", swap.token_mint,
                                swap.amount_sol, swap.pool_address, swap.price_sol)

    print(f"  🔔 WS: {wallet[:16]}... → "
          f"{', '.join(f'{s.action} {s.token_mint[:12]}...' for s in swaps)}")


# ── PumpPortal WS Listener ────────────────────────────────────────────────────
async def pumpportal_ws_listener() -> None:
    """
    PumpPortal WS: real-time bonding-curve trades only.
    Fires milliseconds after a pump.fun bonding-curve trade.
    Adds signals to consensus buffer (same path as Helius listener).
    """
    WS_URL = "wss://pumpportal.fun/api/data"
    delay = 2

    while True:
        try:
            config = load_copy_config()
            wallets = [w for w, e in config.copies.items()
                       if e.enabled and w != config.user_wallet]
            if not wallets:
                await asyncio.sleep(30)
                continue

            print(f"  🌐 PumpPortal WS: connecting ({len(wallets)} wallets)...")
            async with websockets.connect(
                WS_URL, ping_interval=20, ping_timeout=20, close_timeout=10,
            ) as ws:
                await ws.send(json.dumps({"method": "subscribeAccountTrade", "keys": wallets}))
                print(f"  ✅ PumpPortal WS: subscribed")
                delay = 2

                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=600.0)
                    except asyncio.TimeoutError:
                        print(f"  ⚠️  PumpPortal WS: no message in 10m — reconnecting")
                        break
                    try:
                        data = json.loads(raw)
                    except Exception:
                        continue

                    sig     = data.get("signature", "")
                    trader  = data.get("traderPublicKey", "")
                    mint    = data.get("mint", "")
                    tx_type = data.get("txType", "")
                    sol_amt = float(data.get("sol") or data.get("solAmount") or data.get("sol_amount") or 0)

                    if not sig or not trader or not mint or not tx_type:
                        continue
                    if sig in _SEEN_SIGS:
                        continue
                    _seen_add(sig)

                    config = load_copy_config()
                    if not config.copies.get(trader) or trader == config.user_wallet:
                        continue

                    action = "BUY" if tx_type == "buy" else "SELL"

                    # Skip zero-SOL signals — can't compute price, position would be useless
                    if sol_amt == 0:
                        print(f"  ⚠️  PumpPortal: skipping {action} with sol=0 for {mint[:16]}...")
                        _SEEN_SIGS.discard(sig)  # don't dedup — Helius may still pick it up with real price
                        continue

                    tok_amt = float(data.get("tokenAmount", 1) or 1)
                    price   = sol_amt / tok_amt if tok_amt > 0 else 0

                    async with _signal_lock:
                        if action == "BUY" and _is_token_on_cooldown(mint):
                            continue
                        _add_signal(trader, action, mint, sol_amt, "", price)

                    print(f"  🌊 PumpPortal: {trader[:16]}... {action} {mint[:16]}... {sol_amt:.4f} SOL")

        except (websockets.ConnectionClosed, OSError, ConnectionError) as e:
            print(f"  ⚠️  PumpPortal WS closed: {e} — reconnect in {delay}s")
        except Exception as e:
            print(f"  ❌ PumpPortal WS error: {e} — reconnect in {delay}s")

        await asyncio.sleep(delay)
        delay = min(delay * 2, 120)


# ── Polling Loop ──────────────────────────────────────────────────────────────
async def force_exit_live_stale(paper_positions: Dict) -> None:
    """Live-mode only: force-close positions held longer than LIVE_FORCE_EXIT_MIN.
    DISABLED by default (LIVE_FORCE_EXIT_MIN=0). Earlier 5-min default was wrong —
    based on paper buckets that used corrupt source_price_sol data. True 30d
    real-swap hold distribution: p25=15min, p50=32min, p75=210min.
    Kept here in case we want to re-enable later with a properly-derived cutoff."""
    if LIVE_FORCE_EXIT_MIN <= 0:
        return
    config = load_copy_config()
    if config.trade_mode != "live" or not KEYPAIR_LOADED:
        return
    now_unix = int(time.time())
    expire_s = LIVE_FORCE_EXIT_MIN * 60
    stale_keys = [
        key for key, pos in list(paper_positions.items())
        if now_unix - pos.get("timestamp", now_unix) > expire_s
    ]
    if not stale_keys:
        return

    print(f"  ⏰ Force-exiting {len(stale_keys)} live position(s) past {LIVE_FORCE_EXIT_MIN}m hold")
    for key in list(stale_keys):
        pos = paper_positions.get(key)
        if not pos: continue
        age_s = now_unix - pos.get("timestamp", now_unix)
        mint = pos.get("token_mint") or (key.split("::", 1)[1] if "::" in key else key)
        src = pos.get("source_wallet") or (key.split("::", 1)[0] if "::" in key else "")
        entry = pos.get("entry_price", 0.0)
        scaled = pos.get("scaled_amount", 0.01)
        print(f"    ⏰ force-SELL {mint[:16]}... age={age_s}s (source didn't sell within window)")
        # Fire a synthetic SELL through the regular execution path so it goes
        # through Jupiter + confirmation polling + real-fill price like any other trade.
        trade = CopyTrade(
            timestamp=now_unix, source_wallet=src, source_sig="",
            our_wallet=config.user_wallet, action="SELL", token_mint=mint,
            amount_sol=scaled, scaled_amount_sol=scaled,
            source_price_sol=entry,  # placeholder; _fetch_actual_fill will overwrite
        )
        success = await execute_copy_trade(trade)
        trade.status = "confirmed" if success else "failed"
        if success:
            pnl = record_paper_trade_pnl(trade, paper_positions)
            if pnl is not None:
                trade.realized_pnl_sol = pnl
                print(f"    💰 force-exit closed pnl={pnl:+.6f}")
            trade.error = "force_exit_5min"  # tag so analysis can separate these
            save_paper_positions(paper_positions)
            asyncio.create_task(_close_empty_ata(KEYPAIR_LOADED, mint))
        else:
            # If force-SELL fails to land, leave position tracked and try again next cycle
            trade.error = f"force_exit_failed: {trade.error}"
        save_copy_trade(trade)


async def ghost_sweep_loop(paper_positions: Dict) -> None:
    """Dedicated fast-ticking task for the ghost sweep. Polling loop with
    100 watched wallets takes ~100s per iteration; sweep inside it runs too
    slowly to catch fresh ghosts. Separate task ticks every 20s."""
    while True:
        try:
            await sweep_ghost_positions(paper_positions)
        except Exception as e:
            print(f"  ⚠️ ghost_sweep_loop error: {e}")
        await asyncio.sleep(20)


async def sweep_ghost_positions(paper_positions: Dict) -> None:
    """Safety-net: find live-mode open positions where the claimed BUY tx
    doesn't have our wallet holding any of the token, after enough time
    has passed for a legit fill to settle. Inline anti-ghost check sometimes
    misses forked-out txs that briefly appeared confirmed — this sweep
    catches them ~60s later and closes them.

    Only operates on positions that were opened in LIVE mode (has a real
    our_sig, not a DRY_RUN/"confirmed" sentinel)."""
    config = load_copy_config()
    if config.trade_mode != "live" or not KEYPAIR_LOADED:
        return
    now = int(time.time())
    GRACE_S = 60  # wait 60s before declaring a position a ghost
    import aiohttp
    addr = str(KEYPAIR_LOADED.pubkey())
    to_remove = []
    # Parallel on-chain balance check for all eligible positions.
    # publicnode-first with mainnet-beta fallback (matches executors).
    RPCS = ["https://solana.publicnode.com", "https://api.mainnet-beta.solana.com"]

    async def check_balance(mint: str) -> int | None:
        """Return held amount (0 = ghost) or None if couldn't verify."""
        for rpc in RPCS:
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.post(rpc, json={
                        "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
                        "params": [addr, {"mint": mint}, {"encoding": "jsonParsed"}],
                    }, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                        if resp.status != 200: continue
                        d = await resp.json()
                        total = 0
                        for a in d.get("result", {}).get("value", []):
                            info = a.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
                            total += int(info.get("tokenAmount", {}).get("amount", 0) or 0)
                        return total
            except Exception:
                continue
        return None

    eligible = []
    for key, pos in list(paper_positions.items()):
        ts = pos.get("timestamp", now)
        if now - ts < GRACE_S: continue
        # Skip watch-mode positions — they're SIMULATIONS, not real buys,
        # so they'll always show 0 on-chain. Only sweep live-copy positions.
        src = pos.get("source_wallet") or (key.split("::", 1)[0] if "::" in key else "")
        src_entry = config.copies.get(src)
        if src_entry and src_entry.copy_mode == "watch":
            continue
        mint = pos.get("token_mint") or (key.split("::", 1)[1] if "::" in key else key)
        eligible.append((key, pos, mint))
    if not eligible:
        return
    # Parallel: all eligible checked in one gather
    results = await asyncio.gather(*[check_balance(mint) for _, _, mint in eligible],
                                     return_exceptions=True)
    for (key, pos, mint), amt in zip(eligible, results):
        if isinstance(amt, Exception) or amt is None:
            continue
        if amt == 0:
            to_remove.append((key, pos, mint))

    if not to_remove:
        return
    print(f"  👻 Ghost sweep: {len(to_remove)} live positions have no on-chain tokens — closing")
    for key, pos, mint in to_remove:
        age = now - pos.get("timestamp", now)
        src = pos.get("source_wallet") or (key.split("::", 1)[0] if "::" in key else "")
        entry = pos.get("entry_price", 0.0)
        scaled = pos.get("scaled_amount", 0.01)
        print(f"    👻 ghost close {mint[:16]}... age={age}s (tx didn't deliver tokens)")
        # Pop from paper_positions
        paper_positions.pop(key, None)
        # Write a reconcile-SELL row so dashboard round-trip matcher closes it
        trade = CopyTrade(
            timestamp=now, source_wallet=src, source_sig="",
            our_wallet=config.user_wallet, action="SELL", token_mint=mint,
            amount_sol=scaled, scaled_amount_sol=scaled,
            source_price_sol=entry, our_price_sol=entry,
            status="expired",
            error="ghost_sweep: no on-chain tokens after 60s grace",
            realized_pnl_sol=0.0,
        )
        save_copy_trade(trade)
    save_paper_positions(paper_positions)


async def cleanup_stale_positions(paper_positions: Dict) -> None:
    """Auto-expire paper positions open longer than STALE_POSITION_HOURS.
    Records a SELL at entry_price (0% PnL) since we can't know the real exit price —
    the source wallet likely sold while we were offline or the token quietly died."""
    now_unix = int(time.time())
    stale_keys = [
        key for key, pos in list(paper_positions.items())
        if now_unix - pos.get("timestamp", now_unix) > STALE_POSITION_HOURS * 3600
    ]
    if not stale_keys:
        return

    print(f"  🧹 Auto-expiring {len(stale_keys)} position(s) open > {STALE_POSITION_HOURS:.0f}h...")
    config = load_copy_config()
    for key in stale_keys:
        pos = paper_positions.pop(key, None)
        if not pos:
            continue
        age_h = (now_unix - pos.get("timestamp", now_unix)) / 3600
        entry_price = pos.get("entry_price", 0.0)
        scaled = pos.get("scaled_amount", 0.0)
        mint = pos.get("token_mint") or (key.split("::", 1)[1] if "::" in key else key)
        src_wallet = pos.get("source_wallet") or (key.split("::", 1)[0] if "::" in key else "auto_expire")
        trade = CopyTrade(
            timestamp=now_unix,
            source_wallet=src_wallet,
            source_sig="",
            our_wallet=config.user_wallet,
            action="SELL",
            token_mint=mint,
            amount_sol=scaled,
            scaled_amount_sol=scaled,
            source_price_sol=entry_price,  # assume flat — no sell signal received
            our_price_sol=entry_price,
            status="expired",
            realized_pnl_sol=0.0,
        )
        save_copy_trade(trade)
        print(f"  🕒 Expired: {mint[:16]}... age={age_h:.0f}h  (0% PnL — no sell signal)")

    save_paper_positions(paper_positions)


async def polling_loop(paper_positions: Dict) -> None:
    """
    Fallback polling: checks each watched wallet's recent txs every COPY_ENGINE_INTERVAL_S.
    Slower than WS listeners but catches non-pump DEX trades and covers WS gaps.
    Adds signals to consensus buffer (same path as WS listeners).
    """
    while True:
        try:
            # force-exit + 24h cleanup still ride the polling cadence
            # (ghost-sweep moved to its own fast-ticking task — see ghost_sweep_loop)
            await force_exit_live_stale(paper_positions)
            await cleanup_stale_positions(paper_positions)

            config = load_copy_config()
            if not config.global_enabled:
                await asyncio.sleep(COPY_ENGINE_INTERVAL_S)
                continue

            enabled = {w: e for w, e in config.copies.items()
                       if e.enabled and w != config.user_wallet}
            total = 0
            config_changed = False

            for wallet_addr, entry in enabled.items():
                sigs = await get_signatures_for_address(wallet_addr, limit=10)
                if not sigs:
                    await asyncio.sleep(0.3)
                    continue

                new_sigs = []
                for si in sigs:
                    if si["signature"] == entry.last_sig:
                        break
                    new_sigs.append(si)

                new_sigs = list(reversed(new_sigs))

                wallet_signals_before = total  # track swap signals found for THIS wallet

                for si in new_sigs:
                    sig = si["signature"]
                    if sig in _SEEN_SIGS:
                        continue
                    _seen_add(sig)

                    # Skip stale trades
                    block_time = si.get("blockTime") or 0
                    age = int(time.time()) - block_time
                    if block_time and age > MAX_TRADE_AGE_S:
                        print(f"    ⏩ Polling: {sig[:20]}... {age}s old — skip")
                        continue

                    tx = await get_transaction(sig)
                    if not tx:
                        continue
                    swaps = parse_transaction_for_swaps(tx)
                    if not swaps:
                        continue

                    async with _signal_lock:
                        for swap in swaps:
                            if swap.price_sol <= 0:
                                continue  # no price = unusable signal
                            if swap.action == "BUY" and _is_token_on_cooldown(swap.token_mint):
                                continue
                            _add_signal(wallet_addr, swap.action, swap.token_mint,
                                        swap.amount_sol, swap.pool_address, swap.price_sol)
                            total += 1

                await asyncio.sleep(0.3)

                # Advance last_sig after processing all sigs for this wallet —
                # even if none had swaps, so we don't re-fetch them on restart.
                # Must be INSIDE the wallet loop — at outer scope only the last
                # wallet's entry/new_sigs are in scope, breaking all prior wallets.
                if new_sigs:
                    entry.last_sig = new_sigs[-1]["signature"]
                    config_changed = True
                    # Only update last_copy_ts when we actually found DEX swap signals —
                    # not just any Solana tx (SOL transfers, NFT mints, etc.).
                    # last_copy_ts is used by the wallet_rotator to protect "active" wallets
                    # from rotation; setting it on non-swap activity caused ALL wallets to
                    # appear permanently active, blocking the rotator from ever firing.
                    if total > wallet_signals_before:
                        entry.last_copy_ts = int(time.time())

            if config_changed:
                with config_lock():
                    # Re-load so we don't overwrite a concurrent rotator write.
                    # Only the last_sig / last_copy_ts fields are ours to update.
                    fresh = load_copy_config()
                    for addr, entry in enabled.items():
                        if addr in fresh.copies:
                            fresh.copies[addr].last_sig = entry.last_sig
                            fresh.copies[addr].last_copy_ts = entry.last_copy_ts
                    save_copy_config(fresh)
            if total > 0:
                print(f"  ✅ {total} signal(s) buffered (polling)")

        except Exception as e:
            print(f"  ❌ Polling error: {e}")

        await asyncio.sleep(COPY_ENGINE_INTERVAL_S)


# ── Engine entry point ────────────────────────────────────────────────────────
async def run_engine() -> None:
    global DRY_RUN, KEYPAIR_LOADED, _signal_lock

    _signal_lock = asyncio.Lock()

    print("=" * 60)
    print("🏄 Reef Copy Trading Engine")
    print("=" * 60)

    config = load_copy_config()
    trade_mode = config.trade_mode

    cli_live = "--live" in sys.argv
    DRY_RUN = not cli_live and trade_mode != "live"

    import swap_executor, pumpfun_executor, pumpswap_executor
    swap_executor.DRY_RUN = DRY_RUN
    pumpfun_executor.DRY_RUN = DRY_RUN
    pumpswap_executor.DRY_RUN = DRY_RUN

    mode_str = "🐸 PAPER (dry run)" if DRY_RUN else "🔴 LIVE — REAL MONEY"
    print(f"   Mode:            {mode_str}")
    print(f"   Poll interval:   {COPY_ENGINE_INTERVAL_S}s")
    print(f"   Token cooldown:  {TOKEN_COOLDOWN_S}s  (env: TOKEN_COOLDOWN_S)")
    print(f"   Consensus:       {MIN_WALLETS_CONSENSUS} wallet(s) / {CONSENSUS_WINDOW_S}s window")
    print(f"   Max trade age:   {MAX_TRADE_AGE_S}s  (env: COPY_MAX_TRADE_AGE_S)")
    print()

    print(f"   Positions: (tracked separately via paper_positions)")

    keypair_path = config.keypair_path or str(Path(DATA_DIR) / "keypair.json")
    KEYPAIR_LOADED = await load_solana_keypair(keypair_path)
    if KEYPAIR_LOADED:
        print(f"   Keypair: {KEYPAIR_LOADED.pubkey()}")
    else:
        print(f"   ⚠️  No keypair — paper mode only")

    print(f"   Wallet:          {config.user_wallet[:20] if config.user_wallet else 'NOT SET'}...")
    enabled_count = sum(1 for e in config.copies.values() if e.enabled)
    print(f"   Watching:        {enabled_count} wallets")
    print(f"   Global enabled:  {config.global_enabled}")
    print()

    # Shared paper positions dict (passed by reference to all coroutines)
    paper_positions = load_paper_positions()

    # Shard logsSubscribe across multiple WS connections to stay under the
    # public-RPC per-connection cap (~100 subs triggers 1013 close).
    # 80 per shard is a safe margin; 170 wallets → 3 shards.
    all_wallets = [w for w, e in config.copies.items()
                   if e.enabled and w != config.user_wallet]
    SHARD_SIZE = 80
    shards = [all_wallets[i:i+SHARD_SIZE] for i in range(0, len(all_wallets), SHARD_SIZE)] or [[]]
    print(f"   WS shards: {len(shards)} × up to {SHARD_SIZE} wallets each")

    listener_coros = [
        helius_logs_listener(shard_wallets=s, shard_label=f"{i+1}/{len(shards)}")
        for i, s in enumerate(shards)
    ]

    await asyncio.gather(
        *listener_coros,
        pumpportal_ws_listener(),
        polling_loop(paper_positions),
        consensus_processor(paper_positions),
        ghost_sweep_loop(paper_positions),
    )


if __name__ == "__main__":
    asyncio.run(run_engine())
