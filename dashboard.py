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
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import jinja2

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
SCANNER_DIR = BASE_DIR.parent  # /home/rob/reef-scanner

# ── DuckDB ─────────────────────────────────────────────────────────────────────
from db import init_db
init_db()


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
    """Load last N copy trades from CSV using tail-read (skip full scan)."""
    import subprocess
    path = DATA_DIR / "copy_trades.csv"
    if not path.exists():
        return []
    # Read header + last N lines efficiently via tail
    try:
        header = path.open().readline().strip()
        result = subprocess.run(
            ["tail", "-n", str(limit), str(path)],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return []
        fields = header.split(",")
        rows = []
        for line in result.stdout.strip().split("\n"):
            if not line or line.startswith(fields[0]):
                continue
            vals = line.split(",")
            if len(vals) >= len(fields):
                rows.append(dict(zip(fields, vals)))
        return rows
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
    trades = load_copy_trades(limit=500)

    paper = [t for t in trades if t.get("mode") == "paper"]
    live = [t for t in trades if t.get("mode") == "live"]

    wins = sum(1 for t in trades if _is_profitable(t))
    total = len(trades)
    win_rate = (wins / total * 100) if total > 0 else 0
    profit = sum(_trade_pnl(t) for t in trades)
    buys = sum(1 for t in trades if t.get("action", "").upper() == "BUY")
    sells = sum(1 for t in trades if t.get("action", "").upper() == "SELL")

    gains = [t for t in trades if _is_profitable(t)]
    losses = [t for t in trades if not _is_profitable(t)]
    avg_win = (sum(_trade_pnl(t) for t in gains) / len(gains)) if gains else 0
    avg_loss = (sum(_trade_pnl(t) for t in losses) / len(losses)) if losses else 0

    return {
        "pnl_sol": profit,
        "win_rate": win_rate,
        "profit_factor": abs(sum(_trade_pnl(t) for t in gains) / sum(_trade_pnl(t) for t in losses)) if losses and sum(_trade_pnl(t) for t in losses) != 0 else 0,
        "total_trades": total,
        "paper_trades": len(paper),
        "live_trades": len(live),
        "total_buys": buys,
        "total_sells": sells,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
    }

def _is_profitable(t):
    return float(t.get("copy_pnl_sol", 0) or 0) > 0

def _trade_pnl(t):
    return float(t.get("copy_pnl_sol", 0) or 0)

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

# ── Helpers ───────────────────────────────────────────────────────────────────
def _save_config(cfg):
    import json
    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / "copy_config.json").write_text(json.dumps(cfg, indent=2))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8891)
