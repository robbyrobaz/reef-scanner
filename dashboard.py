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
    trades = load_copy_trades(limit=5000)  # All trades for accurate stats

    # Status breakdown
    paper = [t for t in trades if t.get("status") == "dry_run"]
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
        "live": _stats(live),
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
@app.get("/api/wallet/positions")
async def get_wallet_positions():
    cfg = load_copy_config()
    addr = cfg.get("user_wallet", "")
    if not addr:
        return {"positions": [], "count": 0}
    # Try both SPL Token and Token-2022 programs. Multi-RPC fallback because
    # publicnode intermittently times out on getTokenAccountsByOwner.
    rpcs = [RPC_URL, "https://api.mainnet-beta.solana.com"]
    programs = [
        "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",  # classic SPL
        "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",  # Token-2022
    ]
    import aiohttp
    positions = []
    for prog in programs:
        for rpc in rpcs:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(rpc, json={
                        "jsonrpc": "2.0", "id": 1,
                        "method": "getTokenAccountsByOwner",
                        "params": [addr, {"programId": prog}, {"encoding": "jsonParsed"}],
                    }, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.json()
                        for acc in data.get("result", {}).get("value", []):
                            info = acc["account"]["data"]["parsed"]["info"]
                            amount = int(info["tokenAmount"]["amount"])
                            if amount > 0:
                                positions.append({
                                    "mint": info["mint"],
                                    "amount": amount / (10 ** info["tokenAmount"]["decimals"]),
                                    "raw": amount,
                                    "decimals": info["tokenAmount"]["decimals"],
                                    "program": "spl" if prog.startswith("Token") else "t22",
                                })
                        break  # success for this program — don't try next RPC
            except Exception:
                continue
    return {"positions": positions, "count": len(positions)}

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

    # Read all live-confirmed rows, chronological
    rows = []
    with path.open() as f:
        for r in csv.DictReader(f):
            if r.get("status") == "confirmed":
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

    # Running aggregate
    wins = [c for c in closed if c["pnl_sol"] > 0]
    losses = [c for c in closed if c["pnl_sol"] < 0]
    gross_win = sum(c["pnl_sol"] for c in wins)
    gross_loss = sum(abs(c["pnl_sol"]) for c in losses)
    running = {
        "closed_count": len(closed),
        "open_count": len(open_list),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(closed) * 100) if closed else 0,
        "total_pnl_sol": sum(c["pnl_sol"] for c in closed),
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
        "avg_win_sol": (gross_win / len(wins)) if wins else 0,
        "avg_loss_sol": -(gross_loss / len(losses)) if losses else 0,
        "avg_hold_s": (sum(c["hold_s"] for c in closed) / len(closed)) if closed else 0,
    }

    return {"round_trips": closed, "open_positions": open_list, "running": running}


@app.get("/live", response_class=HTMLResponse)
async def live_page(request: Request):
    """Detailed live-trading view."""
    tmpl = jinja_env.get_template("live.html")
    return HTMLResponse(content=tmpl.render())

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
