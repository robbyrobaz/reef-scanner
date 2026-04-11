"""
Reef Copy Trading Engine
========================
Monitors target wallets and copies their trades in real-time.

Detection stack (fastest → slowest):
  1. Helius logsSubscribe WS — per-wallet, ~200-500ms latency
  2. PumpPortal WS            — bonding-curve only, milliseconds
  3. Polling loop             — fallback, ~30s latency

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
    HELIUS_RPC_URL,
    COPY_ENGINE_INTERVAL_S,
    COPY_MIN_ALLOC_SOL,
    COPY_MAX_ALLOC_SOL,
    COPY_TRADES_FILE,
    DATA_DIR,
)
from copy_config import load_copy_config, save_copy_config, CopyConfig, CopyEntry
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
        return json.loads(PAPER_POSITIONS_FILE.read_text())
    except Exception:
        return {}

def save_paper_positions(positions: Dict[str, dict]) -> None:
    os.makedirs(os.path.dirname(PAPER_POSITIONS_FILE), exist_ok=True)
    tmp = PAPER_POSITIONS_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(positions))
    tmp.rename(PAPER_POSITIONS_FILE)

def record_paper_trade_pnl(trade: "CopyTrade", positions: Dict[str, dict]) -> Optional[float]:
    """
    Returns realized PnL (SOL) on SELL, 0.0 on BUY (position opened), or None if
    this SELL has no matching BUY position (caller should skip recording the trade).
    """
    key = trade.token_mint  # keyed by mint only — any wallet's SELL closes our position
    if trade.action == "BUY":
        if trade.source_price_sol <= 0:
            return None  # can't open position with unknown price, skip recording
        if key in positions:
            return None  # position already open — don't overwrite cost basis, skip recording
        positions[key] = {
            "entry_price": trade.source_price_sol,
            "scaled_amount": trade.scaled_amount_sol,
            "timestamp": trade.timestamp,
        }
        return 0.0
    elif trade.action == "SELL" and key in positions:
        if trade.source_price_sol <= 0:
            return None  # can't compute PnL with unknown exit price, skip recording
        pos = positions.pop(key)
        # PnL = (exit_price - entry_price) * token_count
        # token_count = sol_in / entry_price
        token_count = pos["scaled_amount"] / pos["entry_price"]
        return (trade.source_price_sol - pos["entry_price"]) * token_count
    # SELL with no matching BUY — don't record (would inflate trade count with zero-PnL rows)
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
    file_exists = os.path.exists(COPY_TRADES_FILE)
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
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.post(
                HELIUS_RPC_URL,
                json={"jsonrpc": "2.0", "id": 1,
                      "method": "getSignaturesForAddress",
                      "params": [address, {"commitment": "confirmed", "limit": limit}]},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                return (await resp.json()).get("result", [])
    except Exception as e:
        print(f"    ⚠️  getSigs error: {e}")
        return []


async def get_transaction(sig: str) -> Optional[dict]:
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.post(
                HELIUS_RPC_URL,
                json={"jsonrpc": "2.0", "id": 1,
                      "method": "getTransaction",
                      "params": [sig, {"encoding": "jsonParsed",
                                       "maxSupportedTransactionVersion": 0}]},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                return (await resp.json()).get("result")
    except Exception as e:
        print(f"    ⚠️  getTx error: {e}")
        return None


# ── Swap Execution ────────────────────────────────────────────────────────────
SOL_MINT = "So11111111111111111111111111111111111111112"

async def execute_copy_trade(trade: CopyTrade) -> bool:
    global KEYPAIR_LOADED
    if KEYPAIR_LOADED is None:
        KEYPAIR_LOADED = await load_solana_keypair()
    if KEYPAIR_LOADED is None:
        trade.error = "No keypair"
        return False

    try:
        # Step 1: PumpPortal (bonding curve)
        result = await execute_pumpfun_swap(
            KEYPAIR_LOADED, trade.action.lower(), trade.token_mint,
            trade.scaled_amount_sol, slippage=15, priority_fee=0.005, pool="auto",
        )

        # Step 2: PumpSwap AMM (graduated tokens)
        if not result.success:
            ps = await execute_pumpswap(
                KEYPAIR_LOADED, trade.action.lower(), trade.token_mint,
                trade.scaled_amount_sol, slippage=15, pool_address=trade.pool_address,
            )
            if ps.success:
                result = SwapResult(success=True, signature=ps.signature, dex="pumpswap")
            else:
                # Step 3: Jupiter fallback
                in_mint  = SOL_MINT if trade.action == "BUY" else trade.token_mint
                out_mint = trade.token_mint if trade.action == "BUY" else SOL_MINT
                result = await execute_swap_legacy(
                    KEYPAIR_LOADED, in_mint, out_mint,
                    trade.scaled_amount_sol, slippage_bps=500,
                )

        if result.success:
            trade.our_sig = result.signature
            trade.our_price_sol = result.price_sol if result.price_sol > 0 else trade.source_price_sol
            return True
        trade.error = result.error
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
    if not _live:
        trade.our_price_sol = price_sol  # paper: assume we'd get the same price as source
        pnl = record_paper_trade_pnl(trade, paper_positions)
        if pnl is None:
            # No valid position to open/close — skip recording this trade entirely
            print(f"  ⚪ {tag}PAPER {action} skipped (no price or no matching BUY) → {token_mint[:16]}...")
            return
        trade.realized_pnl_sol = pnl
        trade.status = "dry_run"
        pnl_str = f" pnl={trade.realized_pnl_sol:+.6f}" if trade.realized_pnl_sol else ""
        print(f"  🐸 {tag}PAPER {action} {scaled:.4f} SOL → {token_mint[:16]}...{pnl_str}")
        save_copy_trade(trade)
        save_paper_positions(paper_positions)
    else:
        print(f"  🔴 {tag}LIVE {action} {scaled:.4f} SOL → {token_mint[:16]}...")
        success = await execute_copy_trade(trade)
        trade.status = "confirmed" if success else "failed"
        if success:
            print(f"  ✅ {tag}BUY landed: {scaled:.4f} SOL | {token_mint[:16]}... | {trade.our_sig[:20]}...")
        save_copy_trade(trade)


# ── Consensus processor ───────────────────────────────────────────────────────
async def consensus_processor(paper_positions_ref: Dict) -> None:
    """
    Every 2s: check if any token has MIN_WALLETS_CONSENSUS independent buy signals
    within CONSENSUS_WINDOW_S. If so, fire the trade.

    When MIN_WALLETS_CONSENSUS=1, every signal fires immediately on next tick (≤2s delay).
    When MIN_WALLETS_CONSENSUS=2, we require 2 different watched wallets to buy the
    same token within CONSENSUS_WINDOW_S — dramatically improves signal quality.
    """
    while True:
        await asyncio.sleep(2)
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


# ── Helius logsSubscribe Listener ─────────────────────────────────────────────
PUMP_AMM_PROG = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
JUPITER_PROG  = "JUP6LkbZbjS3jtsKSqf5joF4BSrFEh7WEZg3Xs5ycD1c"
RAYDIUM_PROG  = "675kPX9MHTjS2zt1qfr1NYHuzeSxPGBY4eNTtRMqDxGD"
DEX_PROGS     = {PUMP_AMM_PROG, JUPITER_PROG, RAYDIUM_PROG}

async def helius_logs_listener() -> None:
    """
    Subscribe to Helius logsSubscribe for each watched wallet.
    Fires within ~200-500ms of a transaction landing — far faster than polling.

    For each notification:
      1. Check logs mention a known DEX program
      2. Fetch full transaction (async, non-blocking)
      3. Parse for swaps
      4. Add to consensus buffer
    """
    WS_URL = f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
    delay = 2

    while True:
        try:
            config = load_copy_config()
            wallets = [w for w, e in config.copies.items()
                       if e.enabled and w != config.user_wallet]
            if not wallets:
                await asyncio.sleep(30)
                continue

            print(f"  🔔 Helius logsSubscribe: connecting ({len(wallets)} wallets)...")
            async with websockets.connect(
                WS_URL,
                ping_interval=30,
                ping_timeout=30,
                close_timeout=10,
                max_size=10 * 1024 * 1024,
            ) as ws:
                # Map: request_id → wallet, subscription_id → wallet
                req_map: Dict[int, str] = {}
                sub_map: Dict[int, str] = {}

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

                print(f"  ✅ Helius logsSubscribe: {len(wallets)} subscriptions sent")
                delay = 2  # reset backoff

                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=300.0)
                    except asyncio.TimeoutError:
                        print(f"  ⚠️  Helius WS: no message in 5m — reconnecting")
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
            print(f"  ⚠️  Helius WS closed: {e} — reconnect in {delay}s")
        except Exception as e:
            print(f"  ❌ Helius WS error: {e} — reconnect in {delay}s")

        await asyncio.sleep(delay)
        delay = min(delay * 2, 60)


async def _process_helius_sig(sig: str, wallet: str) -> None:
    """Fetch a transaction from Helius and add signals to the consensus buffer."""
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

    print(f"  🔔 Helius: {wallet[:16]}... → "
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
async def cleanup_stale_positions(paper_positions: Dict) -> None:
    """Auto-expire paper positions open longer than STALE_POSITION_HOURS.
    Records a SELL at entry_price (0% PnL) since we can't know the real exit price —
    the source wallet likely sold while we were offline or the token quietly died."""
    now_unix = int(time.time())
    stale_mints = [
        mint for mint, pos in list(paper_positions.items())
        if now_unix - pos.get("timestamp", now_unix) > STALE_POSITION_HOURS * 3600
    ]
    if not stale_mints:
        return

    print(f"  🧹 Auto-expiring {len(stale_mints)} position(s) open > {STALE_POSITION_HOURS:.0f}h...")
    config = load_copy_config()
    for mint in stale_mints:
        pos = paper_positions.pop(mint, None)
        if not pos:
            continue
        age_h = (now_unix - pos.get("timestamp", now_unix)) / 3600
        entry_price = pos["entry_price"]
        trade = CopyTrade(
            timestamp=now_unix,
            source_wallet="auto_expire",
            source_sig="",
            our_wallet=config.user_wallet,
            action="SELL",
            token_mint=mint,
            amount_sol=pos["scaled_amount"],
            scaled_amount_sol=pos["scaled_amount"],
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
            # Expire positions that have been open too long without a sell signal
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
                    entry.last_copy_ts = int(time.time())
                    config_changed = True

            if config_changed:
                save_copy_config(config)
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

    POSITIONS = load_positions()
    print(f"   Positions loaded: {len(POSITIONS)}")

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

    # Run all listeners + consensus processor concurrently
    await asyncio.gather(
        helius_logs_listener(),          # fastest: ~200-500ms via logsSubscribe
        pumpportal_ws_listener(),        # fast: ms latency for bonding-curve
        polling_loop(paper_positions),   # slow: fallback for non-pump / WS gaps
        consensus_processor(paper_positions),  # fires buffered signals
    )


if __name__ == "__main__":
    asyncio.run(run_engine())
