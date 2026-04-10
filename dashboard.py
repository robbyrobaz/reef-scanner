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
        header = path.open().readline().strip()
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

def load_swaps_csv(limit=50):
    """Load recent swaps from DuckDB sorted by block_time desc."""
    from db import get_recent_swaps
    df = get_recent_swaps(limit)
    return df.to_dict("records")

def load_positions():
    path = DATA_DIR / "positions.json"
    if path.exists():
        return __import__("json").loads(path.read_text())
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

    # ── PAPER stats (simulated, no real money) ──
    paper_wins = sum(1 for t in paper if _is_profitable(t))
    paper_pnl = sum(_trade_pnl(t) for t in paper)
    paper_gains = [t for t in paper if _is_profitable(t)]
    paper_losses = [t for t in paper if not _is_profitable(t) and _trade_pnl(t) != 0]
    paper_gain_sum = sum(_trade_pnl(t) for t in paper_gains)
    paper_loss_sum = sum(_trade_pnl(t) for t in paper_losses)

    # ── LIVE stats (real money) ──
    live_wins = sum(1 for t in live if _is_profitable(t))
    live_pnl = sum(_trade_pnl(t) for t in live)
    live_buys = sum(1 for t in live if t.get("action", "").upper() == "BUY")
    live_sells = sum(1 for t in live if t.get("action", "").upper() == "SELL")
    live_gains = [t for t in live if _is_profitable(t)]
    live_losses = [t for t in live if not _is_profitable(t) and _trade_pnl(t) != 0]
    live_gain_sum = sum(_trade_pnl(t) for t in live_gains)
    live_loss_sum = sum(_trade_pnl(t) for t in live_losses)

    # ── Real balance P&L (actual SOL change) ──
    starting_balance = 0.18  # SOL sent to hot wallet
    try:
        import aiohttp
        rpc_url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
        # We can't await here easily, so use the cached balance
        current_balance = None
    except:
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
        # Win rate denominator = closed trades only (SELLs with non-zero PnL)
        # BUYs always have pnl=0 and would dilute the win rate if included
        closed = wins + len(losses)
        return {
            "pnl": pnl,
            "trades": n,
            "wins": wins,
            "losses": len(losses),
            "wr": (wins / closed * 100) if closed else 0,
            "pf": abs(gain_sum) / abs(loss_sum) if loss_sum != 0 else 0,
            "buys": buys,
            "sells": sells,
            "avg_win": (gain_sum / len(gains)) if gains else 0,
            "avg_loss": (loss_sum / len(losses)) if losses else 0,
            "best": max((_trade_pnl(t) for t in trades_list), default=0),
            "worst": min((_trade_pnl(t) for t in trades_list), default=0),
        }

    return {
        "paper": _stats(paper),
        "live": _stats(live),
        "failed_trades": len(failed),
        "starting_sol": starting_balance,
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
    try:
        import aiohttp
        rpc_url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
        async with aiohttp.ClientSession() as session:
            async with session.post(
                rpc_url,
                json={
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getTokenAccountsByOwner",
                    "params": [
                        addr,
                        {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
                        {"encoding": "jsonParsed"},
                    ],
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    accounts = data.get("result", {}).get("value", [])
                    positions = []
                    for acc in accounts:
                        info = acc["account"]["data"]["parsed"]["info"]
                        amount = int(info["tokenAmount"]["amount"])
                        if amount > 0:
                            decimals = info["tokenAmount"]["decimals"]
                            positions.append({
                                "mint": info["mint"],
                                "amount": amount / (10 ** decimals),
                                "raw": amount,
                                "decimals": decimals,
                            })
                    return {"positions": positions, "count": len(positions)}
    except Exception:
        pass
    return {"positions": [], "count": 0}

# ── API: Wallet Balance ──────────────────────────────────────────────────────
@app.get("/api/wallet/balance")
async def get_wallet_balance():
    cfg = load_copy_config()
    addr = cfg.get("user_wallet", "")
    if not addr:
        return {"balance_sol": 0, "address": ""}
    try:
        import aiohttp
        rpc_url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
        async with aiohttp.ClientSession() as session:
            async with session.post(
                rpc_url,
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
    import json
    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / "copy_config.json").write_text(json.dumps(cfg, indent=2))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8891)
