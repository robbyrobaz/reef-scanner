#!/usr/bin/env python3
"""
Reef Scanner Dashboard — FastAPI + Jinja2 templates
Mounts at /reef prefix for Tailscale HTTPS reverse proxy.
"""
import os
import asyncio
import csv
import math
import time
from pathlib import Path
from functools import wraps

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import jinja2

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
SCANNER_DIR = BASE_DIR.parent  # /home/rob/reef-scanner

# ── DuckDB ─────────────────────────────────────────────────────────────────────
# Dashboard is READ-ONLY — do NOT call init_db() here, it grabs a write lock.
# Tables are created by the scanner on first run.


# ── Config ──────────────────────────────────────────────────────────────────────
def load_env():
    path = BASE_DIR / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line:
                k, v = line.strip().split("=", 1)
                os.environ.setdefault(k, v)

load_env()
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "")
# Helius key exhausted Apr 17 — use publicnode for balance/token queries
RPC_URL = "https://solana.publicnode.com"

# ── FastAPI app ────────────────────────────────────────────────────────────────
app = FastAPI(title="Reef Scanner + Copy Trading", version="2.0")

# Templates (inline CSS/JS now — no static file deps for the main page)
jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(BASE_DIR / "templates")),
    autoescape=jinja2.select_autoescape(['html', 'xml']),
)
# Keep static mount for any future assets
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# ── Data helpers ────────────────────────────────────────────────────────────────
def load_copy_config() -> dict:
    path = DATA_DIR / "copy_config.json"
    if path.exists():
        return __import__("json").loads(path.read_text())
    return {"user_wallet": "", "global_enabled": False, "trade_mode": "paper",
            "keypair_path": "", "copies": {}, "default_alloc": 0.01}

def load_copy_trades(limit=50):
    """Load last N copy trades from CSV. Uses csv.DictReader on tail output."""
    import subprocess, io
    path = DATA_DIR / "copy_trades.csv"
    if not path.exists():
        return []
    try:
        with path.open() as fh:
            header = fh.readline().strip()
        result = subprocess.run(
            ["tail", "-n", str(limit), str(path)],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []
        # Use csv.DictReader to handle quoted fields properly
        text = header + "\n" + result.stdout.strip()
        reader = csv.DictReader(io.StringIO(text))
        return list(reader)
    except Exception:
        # Fallback: read all
        rows = []
        with path.open() as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
        return rows[-limit:]

def load_wallets_csv(limit=50):
    """Load top wallets from DuckDB sorted by score desc."""
    from db import get_top_wallets
    df = get_top_wallets(limit)
    return df.to_dict("records")

def load_watched_wallet_stats() -> list:
    """Return scanner stats for every wallet in the copy watch list."""
    cfg = load_copy_config()
    copies = cfg.get("copies", {})
    # Load wallets.csv into a lookup dict
    wallets_path = DATA_DIR / "wallets.csv"
    db: dict = {}
    if wallets_path.exists():
        with wallets_path.open() as f:
            for row in csv.DictReader(f):
                db[row["address"]] = row
    result = []
    for addr, entry in copies.items():
        w = db.get(addr, {})
        result.append({
            "address": addr,
            "enabled": entry.get("enabled", True),
            "alloc_sol": entry.get("alloc_sol", 0.01),
            "score": float(w.get("score", 0) or 0),
            "win_rate": float(w.get("win_rate", 0) or 0),
            "total_trades": int(w.get("total_trades", 0) or 0),
            "avg_roi": float(w.get("avg_roi", 0) or 0),
            "profit_factor": float(w.get("profit_factor", 0) or 0),
            "avg_hold_minutes": float(w.get("avg_hold_minutes", 0) or 0),
            "last_active": w.get("last_active", ""),
            "solscan_link": w.get("solscan_link", f"https://solscan.io/account/{addr}"),
        })
    result.sort(key=lambda x: x["score"], reverse=True)
    return result

def load_swaps_csv(limit=50):
    """Load recent swaps from DuckDB sorted by block_time desc."""
    from db import get_recent_swaps
    df = get_recent_swaps(limit)
    return df.to_dict("records")

def load_positions():
    # Use paper_positions.json (updated by copy_engine); positions.json is for on-chain balances only
    import json as _json
    path = DATA_DIR / "paper_positions.json"
    if path.exists():
        try:
            data = _json.loads(path.read_text())
            # Keys are "{source_wallet}::{token_mint}" composite. Prefer embedded token_mint
            # field; fall back to parsing the key for any entry that predates the migration.
            if isinstance(data, dict):
                out = []
                for k, v in data.items():
                    mint = v.get("token_mint") or (k.split("::", 1)[1] if "::" in k else k)
                    out.append({"mint": mint, **v})
                return out
            return data
        except Exception:
            pass
    return []

# ── Compute stats ──────────────────────────────────────────────────────────────
def compute_stats():
    """Use fast SQL-based db.get_stats() for all aggregations."""
    from db import get_stats
    return get_stats()

def _count_wallets():
    from db import wallet_count
    total, _ = wallet_count()
    return total

def _count_qualified():
    from db import wallet_count
    _, qual = wallet_count()
    return qual

def _count_swaps():
    path = DATA_DIR / "swaps.csv"
    if not path.exists():
        return 0
    with path.open() as f:
        return sum(1 for _ in f) - 1

# ── Dashboard route ────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Serve the dashboard HTML with base path pre-set via Jinja2."""
    stats = compute_stats()
    config = load_copy_config()
    copy_trades = load_copy_trades(limit=30)

    # Extract base from X-Base-Path header (set by tailscale proxy), or from URL path
    base = request.headers.get("X-Base-Path", "")
    if not base:
        path_parts = request.url.path.rstrip("/").split("/")
        base = "/" + path_parts[1] if len(path_parts) > 1 and path_parts[1] else ""

    tmpl = jinja_env.get_template("dashboard.html")
    html = tmpl.render(base=base)
    return HTMLResponse(content=html)

# ── API: Stats ───────────────────────────────────────────────────────────────────
@app.get("/api/stats")
async def get_stats():
    return compute_stats()

# ── API: Wallets (top 50, sorted by score) ───────────────────────────────────
@app.get("/api/wallets")
async def get_wallets(limit: int = 50):
    return load_wallets_csv(limit=limit)

# ── API: Recent Swaps ─────────────────────────────────────────────────────────
@app.get("/api/swaps")
async def get_swaps(limit: int = 50):
    return load_swaps_csv(limit=limit)

# ── API: Positions ─────────────────────────────────────────────────────────────
@app.get("/api/positions")
async def get_positions():
    return load_positions()

# ── API: Wallet stats ─────────────────────────────────────────────────────────
@app.get("/api/wallet/stats")
async def get_wallet_stats():
    cfg = load_copy_config()
    positions = load_positions()
    trades = load_copy_trades(limit=100000)  # load all — CSV has ~10k rows, fast

    # Status breakdown. Split paper into:
    #   - historical paper (status=dry_run, error != watch_mode)
    #   - watch-mode (ongoing evaluation, status=dry_run, error == watch_mode)
    paper_historical = [t for t in trades if t.get("status") == "dry_run" and t.get("error") not in ("watch_mode", "watch_large")]
    paper_watch      = [t for t in trades if t.get("status") == "dry_run" and t.get("error") == "watch_mode"]
    paper_large      = [t for t in trades if t.get("status") == "dry_run" and t.get("error") == "watch_large"]
    paper = paper_historical  # backward-compat: old dashboard uses 'paper' key for historical
    live = [t for t in trades if t.get("status") == "confirmed"]
    failed = [t for t in trades if t.get("status") == "failed"]

    # ── Real balance P&L (actual SOL change) ──
    # 0.0087 was wallet balance before Apr 17 live flip; 0.2 SOL added → 0.2087 starting live.
    # Paper-phase activity before Apr 17 didn't move SOL, so this is the live-phase baseline.
    starting_balance = 0.2087
    current_balance = None

    def _stats(trades_list):
        """Compute stats for a list of trades."""
        wins = sum(1 for t in trades_list if _is_profitable(t))
        pnl = sum(_trade_pnl(t) for t in trades_list)
        buys = sum(1 for t in trades_list if t.get("action", "").upper() == "BUY")
        sells = sum(1 for t in trades_list if t.get("action", "").upper() == "SELL")
        gains = [t for t in trades_list if _is_profitable(t)]
        losses = [t for t in trades_list if not _is_profitable(t) and _trade_pnl(t) != 0]
        gain_sum = sum(_trade_pnl(t) for t in gains)
        loss_sum = sum(_trade_pnl(t) for t in losses)
        n = len(trades_list)
        closed = wins + len(losses)
        # Open positions = BUYs with no matching same-(source,mint) SELL
        # Composite key prevents cross-wallet cancelation (matches engine semantics)
        open_keys = {}
        for t in trades_list:
            k = f"{t.get('source_wallet','')}::{t.get('token_mint','')}"
            if t.get("action","").upper() == "BUY":
                open_keys[k] = True
            elif t.get("action","").upper() == "SELL":
                open_keys.pop(k, None)
        # Tail-outcome tracking — ROI per closed position, to surface moonshots.
        # A "rip" is a round-trip ROI >= 10× capital deployed (0.01 SOL → >= 0.10 SOL profit).
        # We measure per-SELL ROI: pnl / basis (basis = scaled_amount_sol of the BUY leg,
        # which is always 0.01 SOL in this engine).
        tail_rois = []
        rips_10x = rips_50x = rips_100x = 0
        for t in trades_list:
            if t.get("action","").upper() != "SELL": continue
            basis = float(t.get("scaled_amount_sol") or 0.01) or 0.01
            trade_pnl = _trade_pnl(t)
            if trade_pnl == 0: continue
            roi = trade_pnl / basis  # 1.0 = doubled, 10.0 = 10×
            tail_rois.append(roi)
            if roi >= 9: rips_10x += 1  # ≥10× means pnl ≥ 9× basis (entry basis is the 10th x)
            if roi >= 49: rips_50x += 1
            if roi >= 99: rips_100x += 1
        tail_rois.sort()
        p95 = tail_rois[int(len(tail_rois)*0.95)] if tail_rois else 0
        max_roi = tail_rois[-1] if tail_rois else 0
        # Per-trade average PnL — the key strategy metric. Grind-positive
        # strategies (this one) should show a small positive number here net of
        # fees. If live diverges below watch, execution is eating the edge.
        # Units: milliSOL per closed SELL (sell = round-trip close).
        pnl_per_sell_msol = (pnl / sells * 1000.0) if sells > 0 else 0.0

        return {
            "pnl": pnl,
            "trades": n,
            "wins": wins,
            "losses": len(losses),
            "wr": (wins / closed * 100) if closed else 0,
            "pf": abs(gain_sum) / abs(loss_sum) if loss_sum != 0 else 0,
            "buys": buys,
            "sells": sells,
            "open": len(open_keys),
            "avg_win": (gain_sum / len(gains)) if gains else 0,
            "avg_loss": (loss_sum / len(losses)) if losses else 0,
            "best": max((_trade_pnl(t) for t in gains + losses), default=0),
            "worst": min((_trade_pnl(t) for t in gains + losses), default=0),
            "pnl_per_sell_msol": pnl_per_sell_msol,
            # Tail metrics — the moonshot-counting stats
            "p95_roi": p95,       # 95th percentile ROI (0.1 = +10%, 1.0 = +100%)
            "max_roi": max_roi,   # best single-trade ROI
            "rips_10x": rips_10x,
            "rips_50x": rips_50x,
            "rips_100x": rips_100x,
        }

    # Compute last_updated from most recent trade timestamp
    last_ts = None
    for t in trades:
        ts = t.get("timestamp")
        if ts:
            try:
                ts_int = int(ts)
                if last_ts is None or ts_int > last_ts:
                    last_ts = ts_int
            except (ValueError, TypeError):
                pass

    return {
        "paper": _stats(paper),
        "live":  _stats(live),
        "watch": _stats(paper_watch),   # NEW: watch-mode paper stats (ongoing eval of 80 candidates)
        "watch_large": _stats(paper_large),  # Large-order-follow bucket (different wallet universe, no size pre-filter)
        "failed_trades": len(failed),
        "starting_sol": starting_balance,
        "last_updated": last_ts,       # Unix timestamp of most recent trade
        "last_updated_age_s": (int(__import__("time").time()) - last_ts) if last_ts else None,
    }

def _is_profitable(t):
    """CSV field is 'realized_pnl_sol' (not 'copy_pnl_sol')."""
    return float(t.get("realized_pnl_sol", 0) or 0) > 0

def _trade_pnl(t):
    """CSV field is 'realized_pnl_sol' (not 'copy_pnl_sol')."""
    return float(t.get("realized_pnl_sol", 0) or 0)

# ── API: Open Positions (token balances) ─────────────────────────────────────
_POSITIONS_CACHE: dict = {"ts": 0, "data": None}
_POSITIONS_TTL = 20  # seconds — positions rarely change faster than this


@app.get("/api/wallet/positions")
async def get_wallet_positions():
    """Return held token balances. Cached 20s. Fans out RPC calls in parallel
    with tight per-call timeouts so a single slow RPC can't block the page."""
    cfg = load_copy_config()
    addr = cfg.get("user_wallet", "")
    if not addr:
        return {"positions": [], "count": 0}

    now = time.time()
    if _POSITIONS_CACHE["data"] is not None and now - _POSITIONS_CACHE["ts"] < _POSITIONS_TTL:
        return _POSITIONS_CACHE["data"]

    import aiohttp
    rpcs = [RPC_URL, "https://api.mainnet-beta.solana.com"]
    programs = [
        ("spl", "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"),
        ("t22", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"),
    ]

    async def fetch_one(rpc: str, prog_name: str, prog_id: str):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(rpc, json={
                    "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
                    "params": [addr, {"programId": prog_id}, {"encoding": "jsonParsed"}],
                }, timeout=aiohttp.ClientTimeout(total=4)) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
                    return (prog_name, data.get("result", {}).get("value", []))
        except Exception:
            return None

    # Race 4 in parallel (2 RPCs × 2 programs). Take first success per program.
    tasks = [fetch_one(rpc, pn, pid) for rpc in rpcs for pn, pid in programs]
    results = await asyncio.gather(*tasks)

    seen_programs = set()
    positions = []
    for r in results:
        if r is None: continue
        prog_name, accounts = r
        if prog_name in seen_programs: continue
        seen_programs.add(prog_name)
        for acc in accounts:
            info = acc["account"]["data"]["parsed"]["info"]
            amount = int(info["tokenAmount"]["amount"])
            if amount > 0:
                positions.append({
                    "mint": info["mint"],
                    "amount": amount / (10 ** info["tokenAmount"]["decimals"]),
                    "raw": amount,
                    "decimals": info["tokenAmount"]["decimals"],
                    "program": prog_name,
                })

    result = {"positions": positions, "count": len(positions)}
    _POSITIONS_CACHE["ts"] = now
    _POSITIONS_CACHE["data"] = result
    return result

# ── API: Wallet Balance ──────────────────────────────────────────────────────
@app.get("/api/wallet/balance")
async def get_wallet_balance():
    cfg = load_copy_config()
    addr = cfg.get("user_wallet", "")
    if not addr:
        return {"balance_sol": 0, "address": ""}
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.post(
                RPC_URL,
                json={
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getBalance",
                    "params": [addr],
                },
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    lamports = data.get("result", {}).get("value", 0)
                    return {"balance_sol": lamports / 1e9, "address": addr}
    except Exception as e:
        pass
    return {"balance_sol": None, "address": addr}

# ── API: Copy Config ───────────────────────────────────────────────────────────
@app.get("/api/copy/config")
async def get_copy_config():
    return load_copy_config()

# ── API: Copy trades log ──────────────────────────────────────────────────────
@app.get("/api/copy/trades")
async def get_copy_trades(limit: int = 50):
    return load_copy_trades(limit=limit)

# ── API: ROI by 6-hour UTC bucket (all SELL trades) ──────────────────────────
def _roi_buckets_for(status_filter: set):
    """Shared logic: avg ROI % per 6-hour UTC window for SELLs matching status_filter."""
    from datetime import datetime, timezone
    path = DATA_DIR / "copy_trades.csv"
    if not path.exists():
        return []
    buckets: dict = {}
    with path.open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("action", "").upper() != "SELL":
                continue
            if row.get("status") not in status_filter:
                continue
            pnl_raw = row.get("realized_pnl_sol", "")
            ts_raw  = row.get("timestamp", "")
            if not pnl_raw or not ts_raw:
                continue
            try:
                pnl = float(pnl_raw)
                ts  = int(ts_raw)
            except (ValueError, TypeError):
                continue
            if pnl == 0:
                continue
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            bucket_h = dt.hour - (dt.hour % 6)
            key = datetime(dt.year, dt.month, dt.day, bucket_h, tzinfo=timezone.utc).strftime("%Y-%m-%d %H:%M")
            alloc  = float(row.get("scaled_amount_sol", 0.01) or 0.01)
            cost   = alloc if alloc > 0 else 0.01
            roi_pct = (pnl / cost) * 100
            buckets.setdefault(key, []).append(roi_pct)
    return [
        {"label": k, "avg_roi": round(sum(v) / len(v), 2), "count": len(v)}
        for k, v in sorted(buckets.items())
    ]


@app.get("/api/copy/roi-buckets")
async def get_roi_buckets():
    """Avg ROI % per 6-hour UTC window, paper trades (dry_run SELLs)."""
    return _roi_buckets_for({"dry_run"})


@app.get("/api/copy/roi-buckets-live")
async def get_roi_buckets_live():
    """Avg ROI % per 6-hour UTC window, live trades (confirmed SELLs only)."""
    return _roi_buckets_for({"confirmed"})


_TX_FEE_CACHE: dict = {}  # sig -> {"fee": int, "priority": int, "slot": int, "err": any}


async def _fetch_tx_fee(sig: str) -> dict:
    """Lookup tx fee from chain, cached. Returns {"fee", "priority", "slot", "err"}."""
    if not sig or sig in ("confirmed", "DRY_RUN", "DRY_RUN_SIG"):
        return {"fee": 0, "priority": 0, "slot": None, "err": None}
    if sig in _TX_FEE_CACHE:
        return _TX_FEE_CACHE[sig]
    import aiohttp
    for rpc in ["https://api.mainnet-beta.solana.com", "https://solana.publicnode.com"]:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(rpc, json={
                    "jsonrpc": "2.0", "id": 1, "method": "getTransaction",
                    "params": [sig, {"encoding": "json", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}],
                }, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        continue
                    d = await resp.json()
                    tx = d.get("result")
                    if not tx:
                        continue
                    meta = tx.get("meta", {})
                    fee = meta.get("fee", 0)
                    num_sigs = len(tx.get("transaction", {}).get("signatures", []))
                    base = 5000 * num_sigs
                    result = {"fee": fee, "priority": max(0, fee - base),
                              "slot": tx.get("slot"), "err": meta.get("err")}
                    _TX_FEE_CACHE[sig] = result
                    return result
        except Exception:
            continue
    return {"fee": None, "priority": None, "slot": None, "err": None}


@app.get("/api/live/round-trips")
async def get_live_round_trips():
    """
    Rich live-trade view: match BUY→SELL pairs by (source_wallet, token_mint),
    compute hold time, entry/exit prices, pnl SOL + %, running R-multiple.
    Returns most-recent first. Includes still-open BUYs.
    """
    path = DATA_DIR / "copy_trades.csv"
    if not path.exists():
        return {"round_trips": [], "open_positions": [], "running": {}}

    # Read live rows: confirmed = real tx, expired = auto-expired or ghost-reconciled
    # (both represent valid close events; expired rows have 0 PnL since exact exit unknown).
    rows = []
    with path.open() as f:
        for r in csv.DictReader(f):
            if r.get("status") in ("confirmed", "expired"):
                rows.append(r)
    rows.sort(key=lambda r: int(r.get("timestamp", 0) or 0))

    # Walk chronologically: BUY opens a position, SELL closes oldest-open for key.
    # Using a list per key (not dict) so we don't silently drop orphan positions.
    from collections import defaultdict, deque
    opens = defaultdict(deque)
    closed = []
    running_loss_sum = 0.0
    running_loss_count = 0
    for r in rows:
        key = f"{r['source_wallet']}::{r['token_mint']}"
        action = r.get("action", "").upper()
        try:
            ts = int(r.get("timestamp", 0) or 0)
            alloc = float(r.get("scaled_amount_sol", 0) or 0)
            price = float(r.get("source_price_sol", 0) or 0)
            pnl = float(r.get("realized_pnl_sol", 0) or 0)
        except (ValueError, TypeError):
            continue
        if action == "BUY":
            opens[key].append({
                "buy_ts": ts, "buy_sig": r.get("our_sig", ""),
                "entry_price": price, "alloc": alloc,
                "source_wallet": r["source_wallet"],
                "token_mint": r["token_mint"],
            })
        elif action == "SELL" and opens[key]:
            pos = opens[key].popleft()
            # Update running avg loss for R-multiple calc (use absolute of negative pnls)
            if pnl < 0:
                running_loss_sum += abs(pnl)
                running_loss_count += 1
            avg_loss = (running_loss_sum / running_loss_count) if running_loss_count else 0
            r_mult = (pnl / avg_loss) if avg_loss > 0 else None
            pct = (pnl / pos["alloc"] * 100) if pos["alloc"] > 0 else 0
            closed.append({
                "status": "closed",
                "buy_ts": pos["buy_ts"],
                "sell_ts": ts,
                "hold_s": ts - pos["buy_ts"],
                "source_wallet": pos["source_wallet"],
                "token_mint": pos["token_mint"],
                "entry_price": pos["entry_price"],
                "exit_price": price,
                "alloc_sol": pos["alloc"],
                "pnl_sol": pnl,
                "pnl_pct": pct,
                "r_mult": r_mult,
                "buy_sig": pos["buy_sig"],
                "sell_sig": r.get("our_sig", ""),
            })

    # Still-open positions (no matching SELL yet)
    import time
    now = int(time.time())
    open_list = []
    for key, queue in opens.items():
        for pos in queue:
            open_list.append({
                "status": "open",
                "buy_ts": pos["buy_ts"],
                "hold_s": now - pos["buy_ts"],
                "source_wallet": pos["source_wallet"],
                "token_mint": pos["token_mint"],
                "entry_price": pos["entry_price"],
                "alloc_sol": pos["alloc"],
                "buy_sig": pos["buy_sig"],
            })
    open_list.sort(key=lambda x: x["buy_ts"], reverse=True)
    closed.sort(key=lambda x: x["sell_ts"], reverse=True)

    # Fetch on-chain fees for all sigs in parallel (cached after first lookup)
    all_sigs = set()
    for c in closed:
        if c.get("buy_sig"): all_sigs.add(c["buy_sig"])
        if c.get("sell_sig"): all_sigs.add(c["sell_sig"])
    for p in open_list:
        if p.get("buy_sig"): all_sigs.add(p["buy_sig"])
    fee_results = await asyncio.gather(*[_fetch_tx_fee(s) for s in all_sigs])
    fees = dict(zip(all_sigs, fee_results))

    # Attach fee data to each row and compute net PnL
    for c in closed:
        b = fees.get(c["buy_sig"]) or {}
        s = fees.get(c["sell_sig"]) or {}
        buy_fee = b.get("fee") or 0
        sell_fee = s.get("fee") or 0
        c["buy_fee_lam"] = buy_fee
        c["sell_fee_lam"] = sell_fee
        c["buy_priority_lam"] = b.get("priority") or 0
        c["sell_priority_lam"] = s.get("priority") or 0
        c["total_fee_sol"] = (buy_fee + sell_fee) / 1e9
        c["net_pnl_sol"] = c["pnl_sol"] - c["total_fee_sol"]
    for p in open_list:
        b = fees.get(p["buy_sig"]) or {}
        p["buy_fee_lam"] = b.get("fee") or 0
        p["buy_priority_lam"] = b.get("priority") or 0

    # Running aggregates — compute BOTH gross and net views
    # Gross = trade PnL before fees (matches paper/backtest numbers)
    # Net   = trade PnL after fees (real money you keep)
    gross_wins   = [c for c in closed if c["pnl_sol"] > 0]
    gross_losses = [c for c in closed if c["pnl_sol"] < 0]
    gross_win_sum  = sum(c["pnl_sol"] for c in gross_wins)
    gross_loss_sum = sum(abs(c["pnl_sol"]) for c in gross_losses)

    net_wins   = [c for c in closed if c["net_pnl_sol"] > 0]
    net_losses = [c for c in closed if c["net_pnl_sol"] < 0]
    net_win_sum  = sum(c["net_pnl_sol"] for c in net_wins)
    net_loss_sum = sum(abs(c["net_pnl_sol"]) for c in net_losses)

    total_fees = sum(c.get("total_fee_sol", 0) for c in closed) + sum(p.get("buy_fee_lam", 0)/1e9 for p in open_list)
    net_pnl_total = sum(c.get("net_pnl_sol", c["pnl_sol"]) for c in closed)
    running = {
        "closed_count": len(closed),
        "open_count": len(open_list),
        "wins": len(gross_wins),        # retain gross win/loss counts for backward compat
        "losses": len(gross_losses),
        "wins_net": len(net_wins),
        "losses_net": len(net_losses),
        "win_rate": (len(gross_wins) / len(closed) * 100) if closed else 0,
        "win_rate_net": (len(net_wins) / len(closed) * 100) if closed else 0,
        "gross_pnl_sol": sum(c["pnl_sol"] for c in closed),
        "total_fees_sol": total_fees,
        "net_pnl_sol": net_pnl_total,
        "profit_factor": (gross_win_sum / gross_loss_sum) if gross_loss_sum > 0 else None,
        "profit_factor_net": (net_win_sum / net_loss_sum) if net_loss_sum > 0 else None,
        "avg_win_sol": (gross_win_sum / len(gross_wins)) if gross_wins else 0,
        "avg_loss_sol": -(gross_loss_sum / len(gross_losses)) if gross_losses else 0,
        "avg_hold_s": (sum(c["hold_s"] for c in closed) / len(closed)) if closed else 0,
        "avg_fee_per_trip_lam": int(total_fees * 1e9 / max(len(closed), 1)),
    }

    return {"round_trips": closed, "open_positions": open_list, "running": running}



# ── API: Watched wallet scanner stats ─────────────────────────────────────────
@app.get("/api/copy/wallet-stats")
async def get_copy_wallet_stats():
    return load_watched_wallet_stats()

# ── API: Toggle wallet copy ────────────────────────────────────────────────────
@app.post("/api/copy/wallet/{addr}/toggle")
async def toggle_copy_wallet(addr: str):
    cfg = load_copy_config()
    copies = cfg.get("copies", {})
    if addr in copies:
        copies[addr]["enabled"] = not copies[addr].get("enabled", True)
    else:
        copies[addr] = {"enabled": True, "alloc_sol": cfg.get("default_alloc", 0.01),
                        "last_sig": "", "last_copy_ts": 0}
    cfg["copies"] = copies
    _save_config(cfg)
    return {"ok": True, "copies": copies}

# ── API: Set alloc ────────────────────────────────────────────────────────────
@app.post("/api/copy/wallet/{addr}/alloc")
async def set_copy_alloc(addr: str, request: Request):
    body = await request.json()
    alloc = float(body.get("alloc", 0.01))
    cfg = load_copy_config()
    copies = cfg.get("copies", {})
    if addr in copies:
        copies[addr]["alloc_sol"] = alloc
    else:
        copies[addr] = {"enabled": True, "alloc_sol": alloc, "last_sig": "", "last_copy_ts": 0}
    cfg["copies"] = copies
    _save_config(cfg)
    return {"ok": True}

# ── API: Remove wallet from copy list ─────────────────────────────────────────
@app.delete("/api/copy/wallet/{addr}")
async def remove_copy_wallet(addr: str):
    cfg = load_copy_config()
    copies = cfg.get("copies", {})
    if addr in copies:
        del copies[addr]
        cfg["copies"] = copies
        _save_config(cfg)
    return {"ok": True}

# ── API: Add wallet to copy list ───────────────────────────────────────────────
@app.post("/api/copy/wallet")
async def add_copy_wallet(request: Request):
    body = await request.json()
    addr = (body.get("address") or "").strip()
    alloc = float(body.get("alloc_sol") or 0.01)
    if not addr:
        raise HTTPException(status_code=400, detail="No address provided")
    # Basic Solana address validation (base58, 32-44 chars)
    if len(addr) < 32 or len(addr) > 44:
        raise HTTPException(status_code=400, detail="Invalid Solana address")
    cfg = load_copy_config()
    copies = cfg.get("copies", {})
    copies[addr] = {"enabled": True, "alloc_sol": alloc, "last_sig": "", "last_copy_ts": 0}
    cfg["copies"] = copies
    _save_config(cfg)
    return {"ok": True, "copies": copies}

# ── API: Global toggle ────────────────────────────────────────────────────────
@app.post("/api/copy/global-toggle")
async def global_toggle():
    cfg = load_copy_config()
    cfg["global_enabled"] = not cfg.get("global_enabled", False)
    _save_config(cfg)
    return {"ok": True, "global_enabled": cfg["global_enabled"]}

# ── API: Trade mode ────────────────────────────────────────────────────────────
@app.post("/api/trade/mode")
async def trade_mode(request: Request):
    body = await request.json()
    mode = body.get("mode", "paper")
    cfg = load_copy_config()
    cfg["trade_mode"] = mode
    _save_config(cfg)
    return {"ok": True, "trade_mode": mode}

# ── API: Wallet verify (from seed phrase) ─────────────────────────────────────
@app.post("/api/wallet/verify")
async def verify_wallet(request: Request):
    body = await request.json()
    phrase = body.get("phrase", "").strip()
    if not phrase:
        raise HTTPException(status_code=400, detail="No phrase provided")

    words = phrase.split()
    if len(words) not in (24, 25):
        raise HTTPException(status_code=400, detail="Must be 24 or 25 words")

    # Derive wallet from seed phrase (simplified — use solders/solana-py properly in prod)
    try:
        from solders.keypair import Keypair
        from mnemonic import Mnemonic
        import hashlib

        mn = Mnemonic("english")
        seed = mn.to_seed(phrase)
        keypair = Keypair.from_seed(seed[:32])
        address = str(keypair.pubkey())
        return {"address": address}
    except ImportError:
        # Fallback: just hash the phrase to show we received it
        import hashlib
        addr = hashlib.sha256(phrase.encode()).hexdigest()[:44]
        return {"address": addr, "warning": "Full key derivation not available — install solders"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# ── API: Wallet disconnect ─────────────────────────────────────────────────────
@app.post("/api/wallet/disconnect")
async def wallet_disconnect():
    cfg = load_copy_config()
    cfg["user_wallet"] = ""
    cfg["keypair_path"] = ""
    _save_config(cfg)
    return {"ok": True}

# ── API: Engine Log Tail ──────────────────────────────────────────────────────
LOG_PATH = BASE_DIR / "cron" / "copy_engine.log"

@app.get("/api/log/tail")
async def log_tail(lines: int = 200):
    """Return last N lines of copy_engine.log as JSON array."""
    if not LOG_PATH.exists():
        return {"lines": [], "exists": False}
    try:
        import subprocess
        result = subprocess.run(
            ["tail", "-n", str(lines), str(LOG_PATH)],
            capture_output=True, text=True, timeout=5
        )
        return {"lines": result.stdout.splitlines(), "exists": True}
    except Exception as e:
        return {"lines": [], "exists": True, "error": str(e)}

@app.get("/api/log/stream")
async def log_stream():
    """SSE stream of copy_engine.log — sends last 100 lines then follows new output."""
    async def event_gen():
        # Send last 100 lines on connect
        if LOG_PATH.exists():
            try:
                import subprocess
                result = subprocess.run(
                    ["tail", "-n", "100", str(LOG_PATH)],
                    capture_output=True, text=True, timeout=5
                )
                for line in result.stdout.splitlines():
                    safe = line.replace("\n", " ").replace("\r", "")
                    yield f"data: {safe}\n\n"
            except Exception:
                pass

        # Follow new lines
        if LOG_PATH.exists():
            with LOG_PATH.open() as f:
                f.seek(0, 2)  # seek to end
                while True:
                    line = f.readline()
                    if line:
                        safe = line.rstrip().replace("\n", " ").replace("\r", "")
                        yield f"data: {safe}\n\n"
                    else:
                        await asyncio.sleep(0.5)
        else:
            yield "data: [log file not found — engine may not be running]\n\n"
            return

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

# ── Helpers ───────────────────────────────────────────────────────────────────
def _save_config(cfg):
    """Atomic write via tmp+rename under the shared config_lock so we don't
    race with copy_engine's polling-loop writes or wallet_rotator's nightly run."""
    import json
    from copy_config import config_lock
    DATA_DIR.mkdir(exist_ok=True)
    tmp = DATA_DIR / "copy_config.json.tmp"
    with config_lock():
        tmp.write_text(json.dumps(cfg, indent=2))
        tmp.replace(DATA_DIR / "copy_config.json")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8891)
