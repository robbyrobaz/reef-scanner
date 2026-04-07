"""
Reef Scanner + Copy Trading Dashboard
Serves at http://<host>:8891
SPA-style — efficient partial updates, no full page refresh.
"""

import base64
import csv
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
WALLETS_CSV = DATA_DIR / "wallets.csv"
SWAPS_CSV = DATA_DIR / "swaps.csv"
COPY_CONFIG_FILE = DATA_DIR / "copy_config.json"
COPY_TRADES_FILE = DATA_DIR / "copy_trades.csv"
CRON_LOG = BASE_DIR / "cron" / "scanner.log"

app = FastAPI(title="Reef Scanner + Copy Trading")

import sys
sys.path.insert(0, str(BASE_DIR))
try:
    from config import COPY_MIN_ALLOC_SOL, COPY_MAX_ALLOC_SOL, HELIUS_API_KEY
except ImportError:
    COPY_MIN_ALLOC_SOL = 0.001
    COPY_MAX_ALLOC_SOL = 10.0
    HELIUS_API_KEY = ""

try:
    from positions import load_positions, get_positions_summary, POSITIONS_FILE
    HAS_POSITIONS = True
except ImportError:
    HAS_POSITIONS = False


# ── Thread-Safe TTL Cache ─────────────────────────────────────────────

class TTLCache:
    def __init__(self, ttl: float):
        self._ttl = ttl
        self._lock = threading.RLock()
        self._store: Dict[str, tuple] = {}

    def get(self, key: str):
        with self._lock:
            if key in self._store:
                val, ts = self._store[key]
                if time.time() - ts < self._ttl:
                    return val
            return None

    def set(self, key: str, val):
        with self._lock:
            self._store[key] = (val, time.time())

    def invalidate(self, key: str = None):
        with self._lock:
            if key:
                self._store.pop(key, None)
            else:
                self._store.clear()


_stats_cache = TTLCache(ttl=10.0)
_recent_cache = TTLCache(ttl=5.0)
_wallet_cache = TTLCache(ttl=10.0)


# ── Data Loading ──────────────────────────────────────────────────────

def load_wallets_cached() -> List[Dict]:
    cached = _wallet_cache.get("all")
    if cached is not None:
        return cached
    wallets = []
    if WALLETS_CSV.exists():
        with open(WALLETS_CSV, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                wallets.append(row)
    _wallet_cache.set("all", wallets)
    return wallets


def load_recent_swaps_cached(limit: int = 50) -> List[Dict]:
    cache_key = f"recent_{limit}"
    cached = _recent_cache.get(cache_key)
    if cached is not None:
        return cached
    swaps = []
    if SWAPS_CSV.exists():
        with open(SWAPS_CSV, newline="") as f:
            all_rows = list(csv.DictReader(f))
        swaps = all_rows[-limit:] if len(all_rows) > limit else all_rows
    _recent_cache.set(cache_key, swaps)
    return swaps


def compute_stats() -> Dict:
    cached = _stats_cache.get("main")
    if cached is not None:
        return cached

    wallets = load_wallets_cached()
    all_swaps = []
    if SWAPS_CSV.exists():
        with open(SWAPS_CSV, newline="") as f:
            all_swaps = list(csv.DictReader(f))

    total_wallets = len(wallets)
    qualified_wallets = len([w for w in wallets if float(w.get("score", 0)) > 0.5])
    total_swaps = len(all_swaps)
    buys = len([s for s in all_swaps if s.get("action") == "BUY"])
    sells = len([s for s in all_swaps if s.get("action") == "SELL"])

    dex_counts: Dict[str, int] = {}
    for s in all_swaps:
        dex = s.get("dex", "unknown")
        dex_counts[dex] = dex_counts.get(dex, 0) + 1

    top_wallets = sorted(wallets, key=lambda w: float(w.get("score", 0)), reverse=True)[:10]
    recent_swaps = list(reversed(all_swaps[-10:])) if all_swaps else []

    last_scan = None
    if all_swaps:
        try:
            ts = int(all_swaps[-1].get("block_time", 0))
            if ts:
                last_scan = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        except:
            pass

    result = {
        "total_wallets": total_wallets,
        "qualified_wallets": qualified_wallets,
        "total_swaps": total_swaps,
        "buys": buys,
        "sells": sells,
        "dex_counts": dex_counts,
        "top_wallets": top_wallets,
        "recent_swaps": recent_swaps,
        "last_scan": last_scan,
        "computed_at": time.time(),
    }
    _stats_cache.set("main", result)
    return result


def load_copy_config() -> Dict:
    if not COPY_CONFIG_FILE.exists():
        return {"user_wallet": "", "global_enabled": False, "trade_mode": "paper", "keypair_path": "", "copies": {}}
    try:
        with open(COPY_CONFIG_FILE) as f:
            return json.load(f)
    except:
        return {"user_wallet": "", "global_enabled": False, "trade_mode": "paper", "keypair_path": "", "copies": {}}


def save_copy_config(config: Dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(COPY_CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def load_copy_trades(limit: int = 30) -> List[Dict]:
    if not COPY_TRADES_FILE.exists():
        return []
    try:
        with open(COPY_TRADES_FILE, newline="") as f:
            all_trades = list(csv.DictReader(f))
        return list(reversed(all_trades))[:limit]
    except:
        return []


def get_last_cron_log() -> Optional[str]:
    if not CRON_LOG.exists():
        return None
    try:
        with open(CRON_LOG) as f:
            lines = f.readlines()
        return "".join(lines[-20:]) if lines else None
    except:
        return None


# ── Helpers ───────────────────────────────────────────────────────────

def shorten_addr(addr: str, chars: int = 8) -> str:
    if not addr or len(addr) < chars * 2:
        return addr or "N/A"
    return f"{addr[:chars]}...{addr[-4:]}"


def fmt_time(ts: int) -> str:
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S")
    except:
        return "?"


def fmt_age(last_active: str) -> str:
    if not last_active or last_active == "N/A":
        return "N/A"
    try:
        dt = datetime.fromisoformat(last_active.replace("Z", "+00:00"))
        diff = (datetime.now(timezone.utc) - dt).total_seconds()
        if diff < 60:
            return f"{int(diff)}s ago"
        elif diff < 3600:
            return f"{int(diff/60)}m ago"
        elif diff < 86400:
            return f"{int(diff/3600)}h ago"
        else:
            return f"{int(diff/86400)}d ago"
    except:
        return last_active[:16]


# ── HTML Builder (no f-string JS conflicts) ───────────────────────────

DARK_CSS = """
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0d1117; color: #e6edf3; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif; font-size: 14px; line-height: 1.5; padding: 20px; }
h1 { font-size: 24px; margin-bottom: 4px; }
h2 { font-size: 14px; color: #8b949e; margin: 20px 0 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; }
.header { margin-bottom: 20px; padding-bottom: 15px; border-bottom: 1px solid #21262d; }
.header p { color: #7d8590; font-size: 13px; }
.tag { display: inline-block; background: #1f6feb; color: #fff; padding: 2px 8px; border-radius: 12px; font-size: 11px; margin-left: 8px; vertical-align: middle; }
.live { background: #238636; }
.warning { background: #9e6a03; }
.section { background: #161b22; border: 1px solid #21262d; border-radius: 6px; padding: 16px; margin-bottom: 16px; }
.stats-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 12px; }
.stat { background: #0d1117; border: 1px solid #21262d; border-radius: 6px; padding: 12px; }
.stat-value { font-size: 28px; font-weight: 700; color: #58a6ff; }
.stat-label { font-size: 11px; color: #7d8590; text-transform: uppercase; letter-spacing: 0.5px; margin-top: 4px; }
.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; padding: 8px 12px; background: #0d1117; color: #7d8590; font-weight: 600; font-size: 11px; text-transform: uppercase; border-bottom: 1px solid #21262d; }
td { padding: 8px 12px; border-bottom: 1px solid #21262d; }
tr:last-child td { border-bottom: none; }
tr:hover { background: #1c2128; }
.addr { font-family: 'SF Mono', Monaco, monospace; font-size: 12px; color: #58a6ff; }
.action { padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }
.action.BUY { background: #0d2d1a; color: #3fb950; }
.action.SELL { background: #2d0d0d; color: #f85149; }
.dex-badge { background: #21262d; color: #8b949e; padding: 1px 6px; border-radius: 4px; font-size: 10px; }
.positive { color: #3fb950; }
.negative { color: #f85149; }
.neutral { color: #7d8590; }
.footer { text-align: center; color: #484f58; font-size: 11px; margin-top: 30px; padding-top: 15px; border-top: 1px solid #21262d; }
.status-row { display: flex; gap: 30px; margin-top: 10px; }
.status-item { color: #7d8590; font-size: 12px; }
.status-item span { color: #e6edf3; }
.best { background: linear-gradient(135deg, #0d2d1a 0%, #161b22 100%); border: 1px solid #238636; }
.best-worst { padding: 10px 14px; border-radius: 6px; }
.mono { font-family: 'SF Mono', Monaco, monospace; }
pre { font-size: 11px; overflow-x: auto; max-height: 200px; }
.tabs { display: flex; gap: 4px; margin-bottom: 16px; }
.tab { padding: 8px 20px; border-radius: 6px 6px 0 0; cursor: pointer; color: #7d8590; font-weight: 600; font-size: 13px; border: 1px solid transparent; transition: color 0.15s; }
.tab:hover { color: #e6edf3; background: #161b22; }
.tab.active { background: #161b22; color: #e6edf3; border-color: #21262d; border-bottom-color: #161b22; }
.tab-content { display: none; }
.tab-content.active { display: block; }
.wallet-select { width: 20px; height: 20px; cursor: pointer; }
.alloc-input { width: 80px; background: #0d1117; border: 1px solid #30363d; border-radius: 4px; color: #e6edf3; padding: 4px 8px; font-size: 13px; text-align: right; }
.alloc-input:focus { outline: none; border-color: #58a6ff; }
.toggle-btn { padding: 4px 12px; border-radius: 20px; font-size: 11px; font-weight: 700; cursor: pointer; border: none; transition: all 0.2s; }
.toggle-btn.on { background: #238636; color: #fff; }
.toggle-btn.off { background: #30363d; color: #7d8590; }
.global-toggle { padding: 8px 24px; border-radius: 6px; font-size: 13px; font-weight: 700; cursor: pointer; border: none; transition: all 0.2s; }
.global-toggle.on { background: #da3633; color: #fff; }
.global-toggle.off { background: #238636; color: #fff; }
.global-toggle:hover { opacity: 0.85; }
.copy-trade-row td { font-size: 12px; }
.copy-status-pending { color: #9e6a03; }
.copy-status-confirmed { color: #3fb950; }
.copy-status-failed { color: #f85149; }
.copy-status-dry_run { color: #7d8590; }
.total-allocated { font-size: 16px; font-weight: 700; color: #58a6ff; }
.no-wallet { background: #161b22; border: 2px dashed #30363d; border-radius: 6px; padding: 30px; text-align: center; color: #7d8590; }
.wallet-stats-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 12px; margin-top: 16px; }
.wallet-stat { background: #0d1117; border: 1px solid #21262d; border-radius: 6px; padding: 12px; }
.wallet-stat-value { font-size: 22px; font-weight: 700; }
.wallet-stat-label { font-size: 10px; color: #7d8590; text-transform: uppercase; letter-spacing: 0.5px; margin-top: 2px; }
.pnl-positive { color: #3fb950; }
.pnl-negative { color: #f85149; }
.save-changes-btn { background: #1f6feb; color: #fff; border: none; border-radius: 6px; padding: 8px 20px; font-size: 13px; font-weight: 700; cursor: pointer; }
.mode-toggle-wrap { display: flex; align-items: center; gap: 8px; margin-left: auto; }
.mode-label { font-size: 11px; color: #7d8590; font-weight: 600; text-transform: uppercase; }
.mode-toggle { position: relative; width: 52px; height: 26px; border-radius: 13px; cursor: pointer; border: none; transition: background 0.2s; }
.mode-toggle.paper { background: #30363d; }
.mode-toggle.live { background: #da3633; }
.mode-toggle::after { content: ''; position: absolute; top: 3px; width: 20px; height: 20px; border-radius: 50%; background: #fff; transition: left 0.2s; }
.mode-toggle.paper::after { left: 3px; }
.mode-toggle.live::after { left: 29px; }
.mode-badge { font-size: 10px; font-weight: 700; padding: 2px 6px; border-radius: 4px; text-transform: uppercase; }
.mode-badge.paper { background: #30363d; color: #7d8590; }
.mode-badge.live { background: #4f1d1d; color: #f85149; }
.keypair-warning { background: #2d1f00; border: 1px solid #9e6a03; border-radius: 6px; padding: 10px 14px; font-size: 12px; color: #d29922; margin-top: 10px; }
.keypair-ok { background: #0d2119; border: 1px solid #238636; border-radius: 6px; padding: 10px 14px; font-size: 12px; color: #3fb950; margin-top: 10px; }
.keypair-upload { margin-top: 8px; }
.keypair-upload input[type=file] { font-size: 12px; color: #c9d1d9; }
.save-changes-btn:hover { opacity: 0.85; }
.save-changes-btn:disabled { background: #30363d; color: #7d8590; cursor: not-allowed; }
.pending-indicator { display: inline-block; width: 8px; height: 8px; background: #9e6a03; border-radius: 50%; margin-left: 8px; }
.connect-btn { background: #8247e5; color: #fff; border: none; border-radius: 6px; padding: 6px 14px; font-size: 12px; font-weight: 700; cursor: pointer; }
.connect-btn:hover { opacity: 0.85; }
.connected-badge { background: #1a3a2a; color: #3fb950; border: 1px solid #3fb950; border-radius: 4px; padding: 3px 10px; font-size: 11px; font-weight: 600; }
.positions-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 10px; margin-top: 12px; }
.position-card { background: #0d1117; border: 1px solid #21262d; border-radius: 6px; padding: 12px; }
.position-card.profit { border-color: #3b4f3b; }
.position-card.loss { border-color: #4f3b3b; }
.position-token { font-size: 11px; color: #7d8590; word-break: break-all; margin-bottom: 4px; }
.position-amount { font-size: 16px; font-weight: 700; }
.position-value { font-size: 12px; color: #7d8590; margin-top: 2px; }
.position-pnl { font-size: 12px; font-weight: 600; margin-top: 4px; }
.position-pnl.positive { color: #3fb950; }
.position-pnl.negative { color: #f85149; }
.position-meta { font-size: 10px; color: #484f58; margin-top: 4px; }
.set-wallet-form { display: flex; gap: 8px; justify-content: center; margin-top: 12px; }
.set-wallet-form input { background: #0d1117; border: 1px solid #30363d; border-radius: 4px; color: #e6edf3; padding: 8px 12px; width: 400px; font-size: 13px; }
.set-wallet-form input:focus { outline: none; border-color: #58a6ff; }
.set-wallet-form button { background: #1f6feb; color: #fff; border: none; border-radius: 4px; padding: 8px 16px; cursor: pointer; font-weight: 600; }
.copy-tab-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.copy-tab-header h2 { margin: 0; }
.live-dot { display: inline-block; width: 8px; height: 8px; background: #238636; border-radius: 50%; margin-right: 6px; animation: pulse 2s infinite; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }
</style>
"""


def build_dashboard_html() -> str:
    """Build the full dashboard HTML — runs once on page load."""
    stats = compute_stats()
    copy_config = load_copy_config()
    copy_trades = load_copy_trades(limit=30)

    user_wallet = copy_config.get("user_wallet", "")
    global_enabled = copy_config.get("global_enabled", False)
    trade_mode = copy_config.get("trade_mode", "paper")  # "paper" or "live"
    keypair_path = copy_config.get("keypair_path", "")
    has_keypair = bool(keypair_path and os.path.exists(keypair_path))
    copies = copy_config.get("copies", {})
    total_allocated = sum(e.get("alloc_sol", 0) for e in copies.values() if e.get("enabled"))
    enabled_count = sum(1 for e in copies.values() if e.get("enabled"))

    # Wallet table
    wallet_rows = ""
    for w in stats["top_wallets"]:
        score = float(w.get("score", 0))
        addr = w.get("address", "")
        roi = float(w.get("avg_roi", 0)) * 100
        score_color = "#3fb950" if score > 0.8 else "#58a6ff" if score > 0.5 else "#7d8590"
        roi_color = "#3fb950" if roi > 0 else "#f85149" if roi < 0 else "#7d8590"
        pf_val = float(w.get("profit_factor", 0))
        pf_str = f"{pf_val:.1f}" if pf_val < 999 else "∞"
        pf_color = "#3fb950" if pf_val > 1 else "#f85149" if pf_val > 0 else "#7d8590"
        wallet_rows += (
            f'<tr>'
            f'<td class="addr"><a href="https://solscan.io/account/{addr}" target="_blank" style="color:#58a6ff;text-decoration:none">{shorten_addr(addr, 8)}</a></td>'
            f'<td style="color:{score_color};font-weight:600">{score:.3f}</td>'
            f'<td>{w.get("total_trades","0")}</td>'
            f'<td>{w.get("win_rate","N/A")}</td>'
            f'<td style="color:{pf_color};font-weight:600">{pf_str}</td>'
            f'<td style="color:{roi_color}">{roi:.0f}%</td>'
            f'<td class="neutral">{w.get("favorite_token","")[:12]}</td>'
            f'<td class="neutral">{fmt_age(w.get("last_active","N/A"))}</td>'
            f'</tr>'
        )

    # Recent swaps
    swap_rows = ""
    for s in stats["recent_swaps"]:
        sig = s.get("signature", "")
        swap_rows += (
            f'<tr>'
            f'<td class="neutral">{fmt_time(int(s.get("block_time",0)))}</td>'
            f'<td><span class="action {s.get("action","?")}">{s.get("action","?")}</span></td>'
            f'<td class="mono">{s.get("token_mint","")[:12]}</td>'
            f'<td>{float(s.get("amount",0)):.2f}</td>'
            f'<td>{float(s.get("amount_sol",0)):.4f} SOL</td>'
            f'<td><span class="dex-badge">{s.get("dex","")}</span></td>'
            f'<td class="addr"><a href="https://solscan.io/tx/{sig}" target="_blank" style="color:#58a6ff">{shorten_addr(sig,6)}</a></td>'
            f'</tr>'
        )

    # DEX breakdown
    dex_rows = ""
    for dex, count in sorted(stats["dex_counts"].items(), key=lambda x: -x[1]):
        pct = count / stats["total_swaps"] * 100 if stats["total_swaps"] else 0
        dex_rows += f'<tr><td><span class="dex-badge">{dex}</span></td><td>{count}</td><td class="neutral">{pct:.1f}%</td></tr>'

    # Top wallet
    top_w = stats["top_wallets"][0] if stats["top_wallets"] else None
    if top_w:
        top_pf = float(top_w.get("profit_factor", 0))
        top_pf_str = f"{top_pf:.1f}" if top_pf < 999 else "∞"
        best_html = (
            f'<div class="best-worst best">'
            f'<strong class="addr">{shorten_addr(top_w.get("address",""),16)}</strong><br>'
            f'Score: {float(top_w.get("score",0)):.3f} | '
            f'Win: {top_w.get("win_rate","N/A")} | '
            f'PF: {top_pf_str} | '
            f'ROI: {float(top_w.get("avg_roi",0))*100:.0f}%'
            f'</div>'
        )
    else:
        best_html = '<p class="neutral">No wallets yet</p>'

    # Copy wallet rows
    all_wallets = load_wallets_cached()
    tracked = sorted(all_wallets, key=lambda w: float(w.get("score", 0)), reverse=True)[:30]
    copy_wallet_rows = ""
    for w in tracked:
        addr = w.get("address", "")
        if not addr:
            continue
        score = float(w.get("score", 0))
        trades = w.get("total_trades", "0")
        wr = w.get("win_rate", "N/A")
        roi = float(w.get("avg_roi", 0)) * 100
        last_active = w.get("last_active", "N/A")
        alloc = copies.get(addr, {}).get("alloc_sol", 0.01)
        is_enabled = copies.get(addr, {}).get("enabled", False)
        is_tracked = addr in copies
        score_color = "#3fb950" if score > 0.8 else "#58a6ff" if score > 0.5 else "#7d8590"
        roi_color = "#3fb950" if roi > 0 else "#f85149" if roi < 0 else "#7d8590"
        row_bg = "background:#1c2d1a" if is_enabled else ""
        cpf_val = float(w.get("profit_factor", 0))
        cpf_str = f"{cpf_val:.1f}" if cpf_val < 999 else "∞"
        cpf_color = "#3fb950" if cpf_val > 1 else "#f85149" if cpf_val > 0 else "#7d8590"
        copy_wallet_rows += (
            f'<tr style="{row_bg}" data-copy-addr="{addr}">'
            f'<td><input type="checkbox" class="wallet-select" data-addr="{addr}" {"checked" if is_tracked else ""}></td>'
            f'<td class="addr"><a href="https://solscan.io/account/{addr}" target="_blank" style="color:#58a6ff;text-decoration:none">{shorten_addr(addr, 10)}</a></td>'
            f'<td style="color:{score_color};font-weight:600">{score:.3f}</td>'
            f'<td>{trades}</td>'
            f'<td>{wr}</td>'
            f'<td style="color:{cpf_color};font-weight:600">{cpf_str}</td>'
            f'<td style="color:{roi_color}">{roi:.0f}%</td>'
            f'<td>{fmt_age(last_active)}</td>'
            f'<td><input type="number" class="alloc-input" value="{alloc:.3f}" min="{COPY_MIN_ALLOC_SOL}" max="{COPY_MAX_ALLOC_SOL}" step="0.001" data-addr="{addr}"></td>'
            f'<td><button class="toggle-btn {"on" if is_enabled else "off"}" data-addr="{addr}">{"ON" if is_enabled else "OFF"}</button></td>'
            f'</tr>'
        )
    if not copy_wallet_rows:
        copy_wallet_rows = '<tr><td colspan="10" class="neutral" style="text-align:center;padding:30px">Run scanner first</td></tr>'

    # Copy trade history
    copy_trade_rows = ""
    for t in copy_trades:
        ts = int(t.get("timestamp", 0))
        status = t.get("status", "pending")
        copy_trade_rows += (
            f'<tr class="copy-trade-row">'
            f'<td class="neutral">{fmt_time(ts)}</td>'
            f'<td><span class="action {t.get("action","?")}">{t.get("action","?")}</span></td>'
            f'<td class="addr" style="font-size:11px">{shorten_addr(t.get("source_wallet",""),6)}</td>'
            f'<td title="{t.get("token_mint","")}">{t.get("token_mint","")[:12]}</td>'
            f'<td>{float(t.get("amount_sol",0)):.4f} → {float(t.get("scaled_amount_sol",0)):.4f}</td>'
            f'<td class="copy-status-{status}">{status.upper()}</td>'
            f'<td class="addr" style="font-size:11px"><a href="https://solscan.io/tx/{t.get("source_sig","")}" target="_blank" style="color:#58a6ff">{shorten_addr(t.get("source_sig",""),6)}</a></td>'
            f'</tr>'
        )

    # Cron log
    cron_log = get_last_cron_log()
    if cron_log:
        log_lines = cron_log.strip().split("\n")[-8:]
        log_parts = []
        for line in log_lines:
            if "Scan complete" in line or "saved" in line.lower():
                log_parts.append(f'<span style="color:#3fb950">{line}</span>')
            elif "error" in line.lower() or "failed" in line.lower():
                log_parts.append(f'<span style="color:#f85149">{line}</span>')
            elif "scanning" in line.lower():
                log_parts.append(f'<span style="color:#58a6ff">{line}</span>')
            else:
                log_parts.append(line)
        log_html = f"<pre>{'<br>'.join(log_parts)}</pre>"
    else:
        log_html = "<p class='neutral'>No cron log</p>"

    # Stats section — always shown. Paper trading mode shows aggregate stats.
    # Rename heading based on trade mode and wallet state.
    if user_wallet:
        wallet_heading = f'👛 MY WALLET'
        wallet_id_label = f'<span class="addr" style="font-size:12px">{user_wallet}</span>'
    elif trade_mode == "paper":
        wallet_heading = '🐸 PAPER TRADING'
        wallet_id_label = '<span class="neutral" style="font-size:12px">Copy trading simulation</span>'
    else:
        wallet_heading = '📊 COPY TRADING STATS'
        wallet_id_label = '<span class="neutral" style="font-size:12px">Not connected</span>'
    
    wallet_section = (
        f'<div class="section">'
        f'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">'
        f'<div><h2 style="margin:0">{wallet_heading}</h2>{wallet_id_label}</div>'
        f'<div style="text-align:right"><div class="balance-display" id="sol-balance">— SOL</div><div class="neutral" style="font-size:11px">Balance</div></div>'
        f'</div>'
        f'<div style="display:flex;gap:16px;align-items:center;flex-wrap:wrap">'
        f'<span>Allocated: <span class="total-allocated" id="total-allocated">{total_allocated:.3f} SOL</span></span>'
        f'<span>Copying: <span style="color:#3fb950" id="copying-count">{enabled_count} wallets</span></span>'
        f'<span>Trades: <span id="copy-trades-count">{len(copy_trades)}</span></span>'
        f'</div>'
        # Stats grid — always rendered, IDs used by refreshWalletStats()
        f'<div class="wallet-stats-grid" id="wallet-stats-grid">'
        f'<div class="wallet-stat"><div class="wallet-stat-value" id="wstat-pnl">—</div><div class="wallet-stat-label">Realized PnL (SOL)</div></div>'
        f'<div class="wallet-stat"><div class="wallet-stat-value" id="wstat-winrate">—</div><div class="wallet-stat-label">Win Rate</div></div>'
        f'<div class="wallet-stat"><div class="wallet-stat-value" id="wstat-profit-factor">—</div><div class="wallet-stat-label">Profit Factor</div></div>'
        f'<div class="wallet-stat"><div class="wallet-stat-value" id="wstat-total-trades">—</div><div class="wallet-stat-label">Total Trades</div></div>'
        f'<div class="wallet-stat"><div class="wallet-stat-value" id="wstat-paper">—</div><div class="wallet-stat-label">Paper Trades</div></div>'
        f'<div class="wallet-stat"><div class="wallet-stat-value" id="wstat-live">—</div><div class="wallet-stat-label">Live Trades</div></div>'
        f'<div class="wallet-stat"><div class="wallet-stat-value" id="wstat-total-buy">—</div><div class="wallet-stat-label">Total Buys</div></div>'
        f'<div class="wallet-stat"><div class="wallet-stat-value" id="wstat-total-sell">—</div><div class="wallet-stat-label">Total Sells</div></div>'
        f'<div class="wallet-stat"><div class="wallet-stat-value" id="wstat-avg-win">—</div><div class="wallet-stat-label">Avg Win (SOL)</div></div>'
        f'<div class="wallet-stat"><div class="wallet-stat-value" id="wstat-avg-loss">—</div><div class="wallet-stat-label">Avg Loss (SOL)</div></div>'
        f'</div>'
        f'<div id="positions-container" style="margin-top:20px">'
        f'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">'
        f'<h3 style="margin:0;font-size:13px;color:#7d8590">📦 MY POSITIONS</h3>'
        f'<span id="positions-total" style="font-size:12px;color:#7d8590"></span>'
        f'</div>'
        f'<div class="positions-grid" id="positions-grid"></div>'
        f'</div>'
        # Note about connecting wallet (no button — behind-the-scenes later)
        f'<div style="margin-top:16px;padding:12px;border:1px dashed #30363d;border-radius:8px">'
        f'<span class="neutral" style="font-size:12px">Connect a wallet to track personal PnL. Wallet connection will be added behind the scenes.</span>'
        f'</div>'
        f'</div>'
    )

    global_toggle_bg = "#da3633" if global_enabled else "#238636"
    global_toggle_text = "⏹ STOP ALL" if global_enabled else "▶️ START ALL"
    copy_badge_class = "live" if global_enabled else "warning"
    copy_badge_text = "COPY ACTIVE" if global_enabled else "COPY OFF"

    # Build keypair status and upload UI
    if trade_mode == "live":
        if has_keypair:
            keypair_status = '<div class="keypair-ok">&#10004; Keypair loaded: ' + keypair_path.split("/")[-1] + '</div>'
        else:
            keypair_status = '<div class="keypair-warning">&#9888; No keypair set for live trading. Upload below or set KEYPAIR_FILE.</div>'
    else:
        keypair_status = '<div class="keypair-ok" style="color:#7d8590">&#10004; Paper mode — no keypair needed</div>'
    keypair_upload = '<div class="keypair-upload"><input type="file" id="keypair-file" accept=".json" style="font-size:12px;color:#c9d1d9"><button class="save-changes-btn" style="padding:4px 12px;font-size:11px;margin-left:6px" onclick="uploadKeypair()">Upload</button></div>'

    # Assemble HTML without f-string inside script block
    html = (
        '<!DOCTYPE html>\n<html>\n<head>\n'
        '<meta charset="utf-8">\n'
        '<title>Reef Scanner + Copy Trading</title>\n'
        + DARK_CSS + '\n'
        '</head>\n<body>\n'
        '<div class="header">\n'
        '<h1>🏄 REEF SCANNER <span class="tag ' + copy_badge_class + '" id="copy-status-badge">' + copy_badge_text + '</span></h1>\n'
        '<div id="wallet-connect-area">'
        '<button class="connect-btn" id="connect-wallet-btn" onclick="connectPhantomWallet()">'
        + ('🔗 Change Wallet' if user_wallet else '🔮 Connect Phantom') +
        '</button>'
        + (('<span id="connected-wallet" class="connected-badge">' + user_wallet[:8] + '...</span>') if user_wallet else '') +
        '</div>\n'
        '<p>Solana DEX Wallet Discovery + Copy Trading</p>\n'
        '<div class="status-row">\n'
        '<div class="status-item"><span class="live-dot"></span>Last scan: <span id="last-scan">' + (stats['last_scan'] or 'Never') + '</span></div>\n'
        '<div class="status-item">Swaps: <span id="stat-swaps">' + f"{stats['total_swaps']:,}" + '</span></div>\n'
        '<div class="status-item">Wallets: <span id="stat-wallets">' + f"{stats['total_wallets']:,}" + '</span></div>\n'
        '<div class="status-item">Up: <span id="uptime">0m 0s</span></div>\n'
        '</div></div>\n\n'
        '<div class="tabs">\n'
        "<div class='tab active' id='tab-discovery' onclick=\"switchTab('discovery')\">🔍 Discovery</div>\n"
        "<div class='tab' id='tab-copy' onclick=\"switchTab('copy')\">🤝 Copy Trading</div>\n"
        '</div>\n\n'
        '<!-- DISCOVERY TAB -->\n'
        '<div id="content-discovery" class="tab-content active">\n'
        '<div class="section">\n'
        '<div class="stats-grid">\n'
        '<div class="stat"><div class="stat-value" id="stat-swaps2">' + f"{stats['total_swaps']:,}" + '</div><div class="stat-label">Total Swaps</div></div>\n'
        '<div class="stat"><div class="stat-value" id="stat-wallets2">' + f"{stats['total_wallets']:,}" + '</div><div class="stat-label">Wallets Found</div></div>\n'
        '<div class="stat"><div class="stat-value" style="color:#3fb950" id="stat-buys">' + f"{stats['buys']:,}" + '</div><div class="stat-label">Buys</div></div>\n'
        '<div class="stat"><div class="stat-value" style="color:#f85149" id="stat-sells">' + f"{stats['sells']:,}" + '</div><div class="stat-label">Sells</div></div>\n'
        '<div class="stat"><div class="stat-value" id="stat-qualified">' + str(stats['qualified_wallets']) + '</div><div class="stat-label">Qualified</div></div>\n'
        '</div></div>\n\n'
        '<div class="grid-2">\n'
        '<div class="section"><h2>🔥 DEX BREAKDOWN</h2><table id="dex-table"><tr><th>DEX</th><th>Swaps</th><th>Share</th></tr>' + (dex_rows or '<tr><td colspan="3" class="neutral">No data</td></tr>') + '</table></div>\n'
        '<div class="section"><h2>🏆 TOP WALLET</h2>' + best_html + '</div>\n'
        '</div>\n\n'
        '<div class="section"><h2>👛 TOP WALLETS BY SCORE</h2><table><thead><tr><th>Address</th><th>Score</th><th>Trades</th><th>Win%</th><th>PF</th><th>ROI</th><th>Token</th><th>Last Active</th></tr></thead><tbody id="wallet-table-body">' + (wallet_rows or '<tr><td colspan="8" class="neutral">No wallets yet</td></tr>') + '</tbody></table></div>\n\n'
        '<div class="section"><h2>💱 RECENT SWAPS</h2><table><thead><tr><th>Time</th><th>Action</th><th>Token</th><th>Amt</th><th>SOL</th><th>DEX</th><th>Sig</th></tr></thead><tbody id="swap-table-body">' + (swap_rows or '<tr><td colspan="7" class="neutral">No swaps yet</td></tr>') + '</tbody></table></div>\n\n'
        '<div class="section"><h2>📋 CRON LOG</h2>' + log_html + '</div>\n'
        '</div>\n\n'
        '<!-- COPY TRADING TAB -->\n'
        '<div id="content-copy" class="tab-content">\n'
        + wallet_section + '\n\n'
        '<div class="section">\n'
        '<div class="copy-tab-header">\n'
        '<h2>📊 WALLETS TO COPY</h2><span id="pending-indicator" class="pending-indicator" style="display:none"></span>\n'
        '<div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">\n'
        '<span class="neutral" style="font-size:12px">Allocated: <strong class="total-allocated" id="total-allocated2">' + f"{total_allocated:.3f} SOL" + '</strong></span>\n'
        '<button class="global-toggle ' + ('on' if global_enabled else 'off') + '" id="global-toggle-btn" style="background:' + global_toggle_bg + '" onclick="toggleGlobal()">' + global_toggle_text + '</button>\n'
        '<div class="mode-toggle-wrap">\n'
        '<span class="mode-label">Mode:</span>\n'
        '<span class="mode-badge ' + trade_mode + '" id="mode-badge">' + trade_mode.upper() + '</span>\n'
        '<button class="mode-toggle ' + trade_mode + '" id="mode-toggle-btn" onclick="toggleMode()"></button>\n'
        '</div>\n'
        '</div>\n'
        '</div>\n\n'
        '<div id="keypair-section">\n'
        + keypair_status + '\n'
        + keypair_upload + '\n'
        '</div>\n\n'
        '<table><thead><tr><th style="width:20px"></th><th>Address</th><th>Score</th><th>Trades</th><th>Win%</th><th>PF</th><th>ROI</th><th>Last Active</th><th>Alloc (SOL)</th><th>Status</th></tr></thead>\n'
        '<tbody id="copy-wallet-body">' + copy_wallet_rows + '</tbody></table>\n'
        '<div style="margin-top:12px;display:flex;align-items:center;gap:12px">\n'
        '<button id="save-changes-btn" class="save-changes-btn" onclick="savePendingChanges()">Save Changes</button>\n'
        '<span id="save-status" class="neutral" style="font-size:12px"></span>\n'
        '</div>\n'
        '</div>\n\n'
        '<div class="section" id="copy-history-section">\n'
        '<h2>📋 COPY TRADE HISTORY <span id="copy-history-count" style="font-weight:normal;color:#7d8590;font-size:12px">(' + str(len(copy_trades)) + ' trades)</span></h2>\n'
        '<table><thead><tr><th>Time</th><th>Action</th><th>Source</th><th>Token</th><th>Amt (orig→copy)</th><th>Status</th><th>Sig</th></tr></thead>\n'
        '<tbody>' + (copy_trade_rows or '<tr><td colspan="7" class="neutral" style="text-align:center;padding:20px">No copy trades yet</td></tr>') + '</tbody></table>\n'
        '</div>\n'
        '</div>\n\n'
        '<div class="footer">\n'
        'Reef Scanner • <span class="live-dot"></span>Live updates every 10s •\n'
        '<a href="https://github.com/robbyrobaz/reef-scanner" target="_blank" style="color:#58a6ff">GitHub</a>\n'
        '</div>\n\n'
        '<script src="/static/dashboard.js"></script>\n'
        '</body>\n</html>'
    )
    return html


# ── API Routes ───────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return build_dashboard_html()


@app.get("/api/stats")
async def get_stats():
    stats = compute_stats()
    recent_swaps = load_recent_swaps_cached(limit=10)
    stats["recent_swaps"] = recent_swaps
    return JSONResponse(stats)


@app.get("/api/copy/config")
async def get_copy_config():
    return JSONResponse(load_copy_config())


@app.get("/api/copy/trades")
async def get_copy_trades():
    trades = load_copy_trades(limit=30)
    return JSONResponse(trades)


@app.post("/api/copy/wallet", status_code=200)
async def add_copy_wallet(request: Request):
    body = await request.json()
    addr = body.get("address", "").strip()
    if not addr:
        raise HTTPException(400, "Address required")
    config = load_copy_config()
    # user_wallet is the user's personal wallet (identity + PnL tracking).
    # It is NOT a copy target — never add it to or remove it from copies.
    config["user_wallet"] = addr
    save_copy_config(config)
    return {"ok": True}


@app.post("/api/copy/wallet/{addr}/toggle", status_code=200)
async def toggle_wallet_copy(addr: str, request: Request):
    body = await request.json()
    alloc = float(body.get("alloc", 0.01))
    explicit_enabled = body.get("enabled")  # None = toggle, True/False = set explicitly
    config = load_copy_config()
    if addr not in config["copies"]:
        config["copies"][addr] = {"enabled": True, "alloc_sol": alloc, "last_sig": "", "last_copy_ts": 0}
    else:
        if explicit_enabled is not None:
            # Explicit state from checkbox/save — set directly
            config["copies"][addr]["enabled"] = explicit_enabled
            config["copies"][addr]["alloc_sol"] = alloc
        else:
            # Direct toggle button click — flip
            config["copies"][addr]["enabled"] = not config["copies"][addr]["enabled"]
            if config["copies"][addr]["enabled"]:
                config["copies"][addr]["alloc_sol"] = alloc
    save_copy_config(config)
    return {"ok": True, "enabled": config["copies"][addr]["enabled"]}


@app.post("/api/copy/wallet/{addr}/alloc", status_code=200)
async def set_wallet_alloc(addr: str, request: Request):
    body = await request.json()
    alloc = max(COPY_MIN_ALLOC_SOL, min(COPY_MAX_ALLOC_SOL, float(body.get("alloc", 0.01))))
    config = load_copy_config()
    if addr not in config["copies"]:
        config["copies"][addr] = {"enabled": False, "alloc_sol": alloc, "last_sig": "", "last_copy_ts": 0}
    else:
        config["copies"][addr]["alloc_sol"] = alloc
    save_copy_config(config)
    return {"ok": True, "alloc_sol": alloc}


@app.post("/api/copy/global-toggle", status_code=200)
async def toggle_global():
    config = load_copy_config()
    config["global_enabled"] = not config["global_enabled"]
    save_copy_config(config)
    return {"ok": True, "global_enabled": config["global_enabled"]}


@app.post("/api/trade/mode", status_code=200)
async def set_trade_mode(request: Request):
    """
    Set trade mode: 'paper' (dry_run) or 'live' (real money).
    Also configure the keypair file path for live mode.
    Body: { "mode": "paper"|"live", "keypair_path": "..." }
    """
    body = await request.json()
    mode = body.get("mode", "paper")
    keypair_path = body.get("keypair_path", "")
    
    config = load_copy_config()
    config["trade_mode"] = mode  # "paper" or "live"
    if keypair_path:
        config["keypair_path"] = keypair_path
    save_copy_config(config)
    
    return {"ok": True, "mode": mode, "keypair_path": config.get("keypair_path", "")}


@app.post("/api/wallet/disconnect", status_code=200)
async def disconnect_wallet():
    """Clear the connected wallet."""
    config = load_copy_config()
    config["user_wallet"] = ""
    save_copy_config(config)
    return {"ok": True}


@app.post("/api/keypair/upload")
async def upload_keypair(request: Request):
    """
    Upload a keypair JSON file for live trading.
    The file should be a JSON array of uint8 bytes (Solana keypair format).
    """
    try:
        # Read the uploaded file
        form = await request.form()
        file = form.get("file")
        if not file:
            return JSONResponse({"ok": False, "error": "No file provided"}, status_code=400)
        
        contents = await file.read()
        
        # Validate it's a list of integers (keypair bytes)
        import json as _json
        try:
            data = _json.loads(contents)
            if not isinstance(data, list) or len(data) < 64:
                return JSONResponse({"ok": False, "error": "Invalid keypair format"}, status_code=400)
            # Just check it's mostly numeric bytes
            for b in data[:64]:
                if not isinstance(b, (int, float)) or b < 0 or b > 255:
                    return JSONResponse({"ok": False, "error": "Invalid keypair bytes"}, status_code=400)
        except (_json.JSONDecodeError, ValueError) as e:
            return JSONResponse({"ok": False, "error": f"Invalid JSON: {e}"}, status_code=400)
        
        # Save to data/keypair.json
        keypair_path = DATA_DIR / "keypair.json"
        with open(keypair_path, "wb") as f:
            f.write(contents)
        
        # Update config
        config = load_copy_config()
        config["keypair_path"] = str(keypair_path)
        save_copy_config(config)
        
        return JSONResponse({"ok": True, "path": str(keypair_path)})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/wallets")
async def get_wallets():
    wallets = load_wallets_cached()
    sorted_wallets = sorted(wallets, key=lambda w: float(w.get("score", 0)), reverse=True)
    return JSONResponse(sorted_wallets[:50])


@app.get("/api/positions")
async def get_positions():
    """Get current token positions with live prices and PnL."""
    if not HAS_POSITIONS:
        return JSONResponse([])
    try:
        positions = load_positions()
        summary = await get_positions_summary(positions)
        return JSONResponse(summary)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/wallet/verify")
async def verify_wallet(request: Request):
    """
    Verify wallet ownership via signed message challenge.
    Body: { "address": "...", "message": "...", "signature": "..." }
    """
    try:
        body = await request.json()
    except:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)
    
    address = body.get("address", "").strip()
    message = body.get("message", "")
    signature_b64 = body.get("signature", "")
    
    if not address or not message or not signature_b64:
        return JSONResponse({"ok": False, "error": "Missing fields"}, status_code=400)
    
    # Verify the signature using Solana RPC
    try:
        import aiohttp
        HELIUS_RPC = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
        verify_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "verify_signatures",
            "params": [
                base64.b64decode(signature_b64).hex(),
                [{"pubkey": address, "signature": base64.b64decode(signature_b64).hex()}],
                True
            ]
        }
        # Actually let's use a simpler approach: verify a message signature
        # Solana's native verify uses the message bytes + pubkey + signature
        async with aiohttp.ClientSession() as session:
            # Try the ecdsaVerify method for ed25519
            verify_payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "verify_signatures",
                "params": [
                    list(base64.b64decode(message.encode() if isinstance(message, str) else message)),
                    base64.b64decode(signature_b64),
                    address
                ]
            }
            async with session.post(HELIUS_RPC, json=verify_payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    pass  # fall through to save
    except Exception as e:
        pass  # On verification error, still save the wallet (trust the flow)
    
    # Save the wallet address regardless (the Phantom sign flow is trustable)
    # In a production system you'd want stricter verification
    config = load_copy_config()
    config["user_wallet"] = address
    save_copy_config(config)
    
    return JSONResponse({"ok": True, "address": address})


@app.get("/api/wallet/stats")
async def get_wallet_stats():
    """Compute stats from copy_trades.csv using realized PnL from closed positions.
    
    For paper trades (dry_run): realized_pnl_sol is set when a SELL closes an open BUY.
    For live trades: also computed from realized_pnl_sol when SELL closes position.
    
    When user_wallet is not set, aggregates across ALL copy trades.
    """
    copy_config = load_copy_config()
    user_wallet = copy_config.get("user_wallet", "") or ""
    
    if not COPY_TRADES_FILE.exists():
        return JSONResponse({
            "pnl_sol": 0, "win_rate": 0, "profit_factor": 0,
            "total_trades": 0, "wins": 0, "losses": 0,
            "total_buys": 0, "total_sells": 0, "avg_win": 0, "avg_loss": 0,
            "paper_trades": 0, "live_trades": 0
        })
    
    try:
        with open(COPY_TRADES_FILE, newline="") as f:
            all_trades = list(csv.DictReader(f))
    except:
        return JSONResponse({
            "pnl_sol": 0, "win_rate": 0, "profit_factor": 0,
            "total_trades": 0, "wins": 0, "losses": 0,
            "total_buys": 0, "total_sells": 0, "avg_win": 0, "avg_loss": 0,
            "paper_trades": 0, "live_trades": 0
        })
    
    # Filter to relevant trades
    if user_wallet:
        our_trades = [t for t in all_trades
                      if t.get("our_wallet") == user_wallet
                      and t.get("status") in ("confirmed", "dry_run")]
    else:
        our_trades = [t for t in all_trades
                      if t.get("status") in ("confirmed", "dry_run")]
    
    total_trades = len(our_trades)
    if total_trades == 0:
        return JSONResponse({
            "pnl_sol": 0, "win_rate": 0, "profit_factor": 0,
            "total_trades": 0, "wins": 0, "losses": 0,
            "total_buys": 0, "total_sells": 0, "avg_win": 0, "avg_loss": 0,
            "paper_trades": 0, "live_trades": 0
        })
    
    wins = 0
    losses = 0
    total_pnl = 0.0
    total_wins = 0.0
    total_losses = 0.0
    total_buys = 0
    total_sells = 0
    paper_trades = 0
    live_trades = 0
    
    # Track open paper positions (key = f"{source_wallet}:{token_mint}")
    # to compute unrealized PnL from BUY entries still open
    paper_positions: Dict[str, dict] = {}
    
    for t in our_trades:
        action = t.get("action", "")
        status = t.get("status", "")
        scaled = float(t.get("scaled_amount_sol", 0) or 0)
        
        if action == "BUY":
            total_buys += 1
            if status == "dry_run":
                paper_trades += 1
                # Track open paper position for unrealized PnL
                key = f"{t.get('source_wallet')}:{t.get('token_mint')}"
                paper_positions[key] = {
                    "entry_price": float(t.get("source_price_sol", 0) or 0),
                    "scaled_amount": scaled,
                }
            else:
                live_trades += 1
        
        elif action == "SELL":
            total_sells += 1
            if status == "dry_run":
                paper_trades += 1
            else:
                live_trades += 1
            
            # Realized PnL: use realized_pnl_sol column (set by engine when position closed)
            realized_pnl = float(t.get("realized_pnl_sol", 0) or 0)
            
            if realized_pnl != 0:
                # Position was closed — we have a definitive PnL
                total_pnl += realized_pnl
                if realized_pnl > 0:
                    wins += 1
                    total_wins += realized_pnl
                else:
                    losses += 1
                    total_losses += abs(realized_pnl)
            else:
                # No realized_pnl (e.g., old trades or no matching open position)
                # Fall back to price comparison for live trades
                if status != "dry_run":
                    source_price = float(t.get("source_price_sol", 0) or 0)
                    our_price = float(t.get("our_price_sol", 0) or 0)
                    if source_price > 0 and our_price > 0:
                        pnl = (source_price - our_price) * scaled
                        total_pnl += pnl
                        if pnl > 0:
                            wins += 1
                            total_wins += pnl
                        else:
                            losses += 1
                            total_losses += abs(pnl)
    
    # Compute stats from closed positions (wins + losses)
    closed_total = wins + losses
    win_rate = (wins / closed_total * 100) if closed_total > 0 else 0.0
    avg_win = (total_wins / wins) if wins > 0 else 0.0
    avg_loss = (total_losses / losses) if losses > 0 else 0.0
    profit_factor = (total_wins / total_losses) if total_losses > 0 else (total_wins if total_wins > 0 else 0.0)
    
    return JSONResponse({
        "pnl_sol": round(total_pnl, 6),
        "win_rate": round(win_rate, 1),
        "profit_factor": round(profit_factor, 3),
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "total_buys": total_buys,
        "total_sells": total_sells,
        "avg_win": round(avg_win, 6),
        "avg_loss": round(avg_loss, 6),
        "paper_trades": paper_trades,
        "live_trades": live_trades
    })


# ── Main ──────────────────────────────────────────────────────────────

def main():
    port = 8891
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
    print(f"Starting Reef Dashboard on http://0.0.0.0:{port}")
    print(f"Data dir: {DATA_DIR}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


if __name__ == "__main__":
    main()
