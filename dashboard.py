"""
Reef Scanner + Copy Trading Dashboard
Serves at http://<host>:8891
SPA-style — efficient partial updates, no full page refresh.
"""

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
    from config import COPY_MIN_ALLOC_SOL, COPY_MAX_ALLOC_SOL
except ImportError:
    COPY_MIN_ALLOC_SOL = 0.001
    COPY_MAX_ALLOC_SOL = 10.0


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
        return {"user_wallet": "", "global_enabled": False, "copies": {}}
    try:
        with open(COPY_CONFIG_FILE) as f:
            return json.load(f)
    except:
        return {"user_wallet": "", "global_enabled": False, "copies": {}}


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
        wallet_rows += (
            f'<tr>'
            f'<td class="addr"><a href="https://solscan.io/account/{addr}" target="_blank" style="color:#58a6ff;text-decoration:none">{shorten_addr(addr, 8)}</a></td>'
            f'<td style="color:{score_color};font-weight:600">{score:.3f}</td>'
            f'<td>{w.get("total_trades","0")}</td>'
            f'<td>{w.get("win_rate","N/A")}</td>'
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
        best_html = (
            f'<div class="best-worst best">'
            f'<strong class="addr">{shorten_addr(top_w.get("address",""),16)}</strong><br>'
            f'Score: {float(top_w.get("score",0)):.3f} | '
            f'Win: {top_w.get("win_rate","N/A")} | '
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
        copy_wallet_rows += (
            f'<tr style="{row_bg}" data-copy-addr="{addr}">'
            f'<td><input type="checkbox" class="wallet-select" data-addr="{addr}" {"checked" if is_tracked else ""}></td>'
            f'<td class="addr"><a href="https://solscan.io/account/{addr}" target="_blank" style="color:#58a6ff;text-decoration:none">{shorten_addr(addr, 10)}</a></td>'
            f'<td style="color:{score_color};font-weight:600">{score:.3f}</td>'
            f'<td>{trades}</td>'
            f'<td>{wr}</td>'
            f'<td style="color:{roi_color}">{roi:.0f}%</td>'
            f'<td>{fmt_age(last_active)}</td>'
            f'<td><input type="number" class="alloc-input" value="{alloc:.3f}" min="{COPY_MIN_ALLOC_SOL}" max="{COPY_MAX_ALLOC_SOL}" step="0.001" data-addr="{addr}"></td>'
            f'<td><button class="toggle-btn {"on" if is_enabled else "off"}" data-addr="{addr}">{"ON" if is_enabled else "OFF"}</button></td>'
            f'</tr>'
        )
    if not copy_wallet_rows:
        copy_wallet_rows = '<tr><td colspan="9" class="neutral" style="text-align:center;padding:30px">Run scanner first</td></tr>'

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

    # Wallet section
    if user_wallet:
        wallet_section = (
            f'<div class="section">'
            f'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">'
            f'<div><h2 style="margin:0">👛 MY WALLET</h2><span class="addr" style="font-size:12px">{user_wallet}</span></div>'
            f'<div style="text-align:right"><div class="balance-display" id="sol-balance">— SOL</div><div class="neutral" style="font-size:11px">Balance</div></div>'
            f'</div>'
            f'<div style="display:flex;gap:16px;align-items:center">'
            f'<span>Allocated: <span class="total-allocated" id="total-allocated">{total_allocated:.3f} SOL</span></span>'
            f'<span>Copying: <span style="color:#3fb950" id="copying-count">{enabled_count} wallets</span></span>'
            f'<span>Trades: <span id="copy-trades-count">{len(copy_trades)}</span></span>'
            f'</div></div>'
        )
    else:
        wallet_section = (
            '<div class="no-wallet">'
            '<h2 style="margin:0 0 8px">👛 Set Your Wallet</h2>'
            '<p>Enter your Solana wallet to enable copy trading</p>'
            '<form class="set-wallet-form" onsubmit="return false">'
            '<input type="text" id="new-wallet-input" placeholder="Solana wallet address...">'
            '<button onclick="setWallet()">Set Wallet</button>'
            '</form></div>'
        )

    global_toggle_bg = "#da3633" if global_enabled else "#238636"
    global_toggle_text = "⏹ STOP ALL" if global_enabled else "▶️ START ALL"
    copy_badge_class = "live" if global_enabled else "warning"
    copy_badge_text = "COPY ACTIVE" if global_enabled else "COPY OFF"

    # Assemble HTML without f-string inside script block
    html = (
        '<!DOCTYPE html>\n<html>\n<head>\n'
        '<meta charset="utf-8">\n'
        '<title>Reef Scanner + Copy Trading</title>\n'
        + DARK_CSS + '\n'
        '</head>\n<body>\n'
        '<div class="header">\n'
        '<h1>🏄 REEF SCANNER <span class="tag ' + copy_badge_class + '" id="copy-status-badge">' + copy_badge_text + '</span></h1>\n'
        '<p>Solana DEX Wallet Discovery + Copy Trading</p>\n'
        '<div class="status-row">\n'
        '<div class="status-item"><span class="live-dot"></span>Last scan: <span id="last-scan">' + (stats['last_scan'] or 'Never') + '</span></div>\n'
        '<div class="status-item">Swaps: <span id="stat-swaps">' + f"{stats['total_swaps']:,}" + '</span></div>\n'
        '<div class="status-item">Wallets: <span id="stat-wallets">' + f"{stats['total_wallets']:,}" + '</span></div>\n'
        '<div class="status-item">Up: <span id="uptime">0m 0s</span></div>\n'
        '</div></div>\n\n'
        '<div class="tabs">\n'
        '<div class="tab active" id="tab-discovery" onclick="switchTab(&apos;discovery&apos;)">🔍 Discovery</div>\n'
        '<div class="tab" id="tab-copy" onclick="switchTab(&apos;copy&apos;)">🤝 Copy Trading</div>\n'
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
        '<div class="section"><h2>👛 TOP WALLETS BY SCORE</h2><table><thead><tr><th>Address</th><th>Score</th><th>Trades</th><th>Win%</th><th>ROI</th><th>Token</th><th>Last Active</th></tr></thead><tbody id="wallet-table-body">' + (wallet_rows or '<tr><td colspan="7" class="neutral">No wallets yet</td></tr>') + '</tbody></table></div>\n\n'
        '<div class="section"><h2>💱 RECENT SWAPS</h2><table><thead><tr><th>Time</th><th>Action</th><th>Token</th><th>Amt</th><th>SOL</th><th>DEX</th><th>Sig</th></tr></thead><tbody id="swap-table-body">' + (swap_rows or '<tr><td colspan="7" class="neutral">No swaps yet</td></tr>') + '</tbody></table></div>\n\n'
        '<div class="section"><h2>📋 CRON LOG</h2>' + log_html + '</div>\n'
        '</div>\n\n'
        '<!-- COPY TRADING TAB -->\n'
        '<div id="content-copy" class="tab-content">\n'
        + wallet_section + '\n\n'
        '<div class="section">\n'
        '<div class="copy-tab-header">\n'
        '<h2>📊 WALLETS TO COPY</h2>\n'
        '<div style="display:flex;gap:12px;align-items:center">\n'
        '<span class="neutral" style="font-size:12px">Allocated: <strong class="total-allocated" id="total-allocated2">' + f"{total_allocated:.3f} SOL" + '</strong></span>\n'
        '<button class="global-toggle ' + ('on' if global_enabled else 'off') + '" id="global-toggle-btn" style="background:' + global_toggle_bg + '" onclick="toggleGlobal()">' + global_toggle_text + '</button>\n'
        '</div></div>\n'
        '<table><thead><tr><th style="width:20px"></th><th>Address</th><th>Score</th><th>Trades</th><th>Win%</th><th>ROI</th><th>Last Active</th><th>Alloc (SOL)</th><th>Status</th></tr></thead>\n'
        '<tbody id="copy-wallet-body">' + copy_wallet_rows + '</tbody></table>\n'
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
        '<script>\n'
        '// ── State ─────────────────────────────────────────────────────\n'
        'var activeTab = "discovery";\n'
        'var uptimeStart = Date.now();\n'
        'var lastStatsTs = 0;\n\n'
        '// ── Tab Switching ──────────────────────────────────────────────\n'
        'function switchTab(name) {\n'
        '  activeTab = name;\n'
        '  document.querySelectorAll(".tab").forEach(function(t){ t.classList.remove("active"); });\n'
        '  document.querySelectorAll(".tab-content").forEach(function(c){ c.classList.remove("active"); });\n'
        '  document.getElementById("tab-" + name).classList.add("active");\n'
        '  document.getElementById("content-" + name).classList.add("active");\n'
        '}\n\n'
        '// ── REST ───────────────────────────────────────────────────────\n'
        'function api(url, opts) {\n'
        '  return fetch(url, opts).then(function(r){ return r.ok ? r.json() : null; }).catch(function(){ return null; });\n'
        '}\n\n'
        '// ── Partial DOM updates ─────────────────────────────────────────\n'
        'function rebuildSwaps(swaps) {\n'
        '  var tbody = document.getElementById("swap-table-body");\n'
        '  if (!swaps || !swaps.length) { tbody.innerHTML = \'<tr><td colspan="7" class="neutral">No swaps yet</td></tr>\'; return; }\n'
        '  var html = swaps.slice().reverse().map(function(s){\n'
        '    var sig = s.signature || "";\n'
        '    return "<tr><td class=\"neutral\">" + fmtTs(s.block_time) + "</td>" +\n'
        '      "<td><span class=\"action " + s.action + "\">" + s.action + "</span></td>" +\n'
        '      "<td class=\"mono\">" + (s.token_mint||"").slice(0,12) + "</td>" +\n'
        '      "<td>" + (Number(s.amount)||0).toFixed(2) + "</td>" +\n'
        '      "<td>" + (Number(s.amount_sol)||0).toFixed(4) + " SOL</td>" +\n'
        '      "<td><span class=\"dex-badge\">" + (s.dex||"") + "</span></td>" +\n'
        '      "<td class=\"addr\"><a href=\"https://solscan.io/tx/" + sig + "\" target=\"_blank\" style=\"color:#58a6ff\">" + shorten(sig,6) + "</a></td></tr>";\n'
        '  }).join("");\n'
        '  tbody.innerHTML = html;\n'
        '}\n\n'
        'function rebuildWallets(wallets) {\n'
        '  var tbody = document.getElementById("wallet-table-body");\n'
        '  if (!wallets || !wallets.length) { tbody.innerHTML = \'<tr><td colspan="7" class="neutral">No wallets yet</td></tr>\'; return; }\n'
        '  var html = wallets.map(function(w){\n'
        '    var score = Number(w.score||0), roi = Number(w.avg_roi||0)*100;\n'
        '    var addr = w.address||"";\n'
        '    var scoreColor = score > 0.8 ? "#3fb950" : score > 0.5 ? "#58a6ff" : "#7d8590";\n'
        '    var roiColor = roi > 0 ? "#3fb950" : roi < 0 ? "#f85149" : "#7d8590";\n'
        '    return "<tr><td class=\"addr\"><a href=\"https://solscan.io/account/" + addr + "\" target=\"_blank\" style=\"color:#58a6ff;text-decoration:none\">" + shorten(addr,8) + "</a></td>" +\n'
        '      "<td style=\"color:" + scoreColor + ";font-weight:600\">" + score.toFixed(3) + "</td>" +\n'
        '      "<td>" + (w.total_trades||"0") + "</td>" +\n'
        '      "<td>" + (w.win_rate||"N/A") + "</td>" +\n'
        '      "<td style=\"color:" + roiColor + "\">" + roi.toFixed(0) + "%</td>" +\n'
        '      "<td class=\"neutral\">" + ((w.favorite_token||"")||"").slice(0,12) + "</td>" +\n'
        '      "<td class=\"neutral\">" + fmtAge(w.last_active||"N/A") + "</td></tr>";\n'
        '  }).join("");\n'
        '  tbody.innerHTML = html;\n'
        '}\n\n'
        'function rebuildDex(dexCounts, totalSwaps) {\n'
        '  var table = document.getElementById("dex-table");\n'
        '  var rows = Object.entries(dexCounts||{}).sort(function(a,b){ return b[1]-a[1]; })\n'
        '    .map(function(e){ var pct = totalSwaps ? (e[1]/totalSwaps*100).toFixed(1) : "0.0"; return "<tr><td><span class=\"dex-badge\">" + e[0] + "</span></td><td>" + e[1] + "</td><td class=\"neutral\">" + pct + "%</td></tr>"; })\n'
        '    .join("");\n'
        '  table.innerHTML = "<tr><th>DEX</th><th>Swaps</th><th>Share</th></tr>" + rows;\n'
        '}\n\n'
        '// ── Refresh functions ──────────────────────────────────────────\n'
        'async function refreshStats() {\n'
        '  var data = await api("/api/stats");\n'
        '  if (!data) return;\n'
        '  if (data.computed_at === lastStatsTs) return;\n'
        '  lastStatsTs = data.computed_at;\n'
        '  document.getElementById("stat-swaps").textContent = Number(data.total_swaps).toLocaleString();\n'
        '  document.getElementById("stat-swaps2").textContent = Number(data.total_swaps).toLocaleString();\n'
        '  document.getElementById("stat-wallets").textContent = Number(data.total_wallets).toLocaleString();\n'
        '  document.getElementById("stat-wallets2").textContent = Number(data.total_wallets).toLocaleString();\n'
        '  document.getElementById("stat-buys").textContent = Number(data.buys).toLocaleString();\n'
        '  document.getElementById("stat-sells").textContent = Number(data.sells).toLocaleString();\n'
        '  document.getElementById("stat-qualified").textContent = data.qualified_wallets;\n'
        '  if (data.last_scan) {\n'
        '    var diff = (Date.now()/1000 - new Date(data.last_scan).getTime()/1000);\n'
        '    var ago = diff < 60 ? Math.floor(diff) + "s ago" : diff < 3600 ? Math.floor(diff/60) + "m ago" : data.last_scan.split("T")[1].slice(0,5);\n'
        '    document.getElementById("last-scan").textContent = ago;\n'
        '  }\n'
        '  if (activeTab === "discovery") {\n'
        '    rebuildSwaps(data.recent_swaps);\n'
        '    rebuildWallets(data.top_wallets);\n'
        '  }\n'
        '  rebuildDex(data.dex_counts, data.total_swaps);\n'
        '  updateCopyStatus();\n'
        '}\n\n'
        'async function refreshCopy() {\n'
        '  if (activeTab !== "copy") return;\n'
        '  var config = await api("/api/copy/config");\n'
        '  if (!config) return;\n'
        '  var totalAlloc = Object.values(config.copies||{}).filter(function(e){ return e.enabled; }).reduce(function(s,e){ return s+(e.alloc_sol||0); }, 0);\n'
        '  var enabledCount = Object.values(config.copies||{}).filter(function(e){ return e.enabled; }).length;\n'
        '  document.getElementById("total-allocated").textContent = totalAlloc.toFixed(3) + " SOL";\n'
        '  document.getElementById("total-allocated2").textContent = totalAlloc.toFixed(3) + " SOL";\n'
        '  document.getElementById("copying-count").textContent = enabledCount + " wallets";\n'
        '  var btn = document.getElementById("global-toggle-btn");\n'
        '  if (btn) { btn.className = "global-toggle " + (config.global_enabled ? "on" : "off"); btn.style.background = config.global_enabled ? "#da3633" : "#238636"; btn.textContent = config.global_enabled ? "⏹ STOP ALL" : "▶ START ALL"; }\n'
        '  var badge = document.getElementById("copy-status-badge");\n'
        '  if (badge) { badge.textContent = config.global_enabled ? "COPY ACTIVE" : "COPY OFF"; badge.className = "tag " + (config.global_enabled ? "live" : "warning"); }\n'
        '  Object.entries(config.copies||{}).forEach(function(e){\n'
        '    var addr = e[0], info = e[1];\n'
        '    var row = document.querySelector("tr[data-copy-addr=\\"" + addr + "\\"]");\n'
        '    if (!row) return;\n'
        '    var b = row.querySelector(".toggle-btn");\n'
        '    if (b) { b.className = "toggle-btn " + (info.enabled ? "on" : "off"); b.textContent = info.enabled ? "ON" : "OFF"; }\n'
        '    var inp = row.querySelector(".alloc-input");\n'
        '    if (inp) inp.value = (info.alloc_sol||0.01).toFixed(3);\n'
        '    row.style.background = info.enabled ? "#1c2d1a" : "";\n'
        '  });\n'
        '  var trades = await api("/api/copy/trades");\n'
        '  if (trades && trades.length > 0) {\n'
        '    var tbody = document.querySelector("#copy-history-section table tbody");\n'
        '    if (tbody) {\n'
        '      tbody.innerHTML = trades.map(function(t){\n'
        '        return "<tr class="copy-trade-row"><td class="neutral">" + fmtTs(t.timestamp) + "</td>" +\n'
        '          "<td><span class="action " + t.action + "">" + t.action + "</span></td>" +\n'
        '          "<td class="addr" style="font-size:11px">" + shorten(t.source_wallet||"",6) + "</td>" +\n'
        '          "<td>" + (t.token_mint||"").slice(0,12) + "</td>" +\n'
        '          "<td>" + Number(t.amount_sol||0).toFixed(4) + " → " + Number(t.scaled_amount_sol||0).toFixed(4) + "</td>" +\n'
        '          "<td class="copy-status-" + (t.status||"pending") + "">" + (t.status||"pending").toUpperCase() + "</td>" +\n'
        '          "<td class="addr" style="font-size:11px"><a href="https://solscan.io/tx/" + (t.source_sig||"") + " style="color:#58a6ff">" + shorten(t.source_sig||"",6) + "</a></td></tr>";\n'
        '      }).join("");\n'
        '    }\n'
        '    document.getElementById("copy-history-count").textContent = "(" + trades.length + " trades)";\n'
        '  }\n'
        '}\n\n'
        'function updateCopyStatus() {\n'
        '  api("/api/copy/config").then(function(config){\n'
        '    if (!config) return;\n'
        '    var badge = document.getElementById("copy-status-badge");\n'
        '    if (badge) { badge.textContent = config.global_enabled ? "COPY ACTIVE" : "COPY OFF"; badge.className = "tag " + (config.global_enabled ? "live" : "warning"); }\n'
        '  });\n'
        '}\n\n'
        '// ── Actions ────────────────────────────────────────────────────\n'
        'async function setWallet() {\n'
        '  var addr = document.getElementById("new-wallet-input").value.trim();\n'
        '  if (!addr) return;\n'
        '  var res = await api("/api/copy/wallet", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({address:addr})});\n'
        '  if (res && res.ok) location.reload();\n'
        '}\n\n'
        'async function toggleGlobal() {\n'
        '  await api("/api/copy/global-toggle", {method:"POST"});\n'
        '  updateCopyStatus();\n'
        '  refreshCopy();\n'
        '}\n\n'
        '// Event delegation for toggle buttons\n'
        'document.addEventListener("click", function(e){\n'
        '  var btn = e.target.closest(".toggle-btn");\n'
        '  if (!btn) return;\n'
        '  e.stopPropagation();\n'
        '  var addr = btn.dataset.addr;\n'
        '  api("/api/copy/wallet/" + addr + "/toggle", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({alloc:0.01})})\n'
        '    .then(function(res){\n'
        '      if (!res) return;\n'
        '      btn.className = "toggle-btn " + (res.enabled ? "on" : "off");\n'
        '      btn.textContent = res.enabled ? "ON" : "OFF";\n'
        '      var row = btn.closest("tr");\n'
        '      if (row) row.style.background = res.enabled ? "#1c2d1a" : "";\n'
        '      refreshCopy();\n'
        '    });\n'
        '});\n\n'
        '// Alloc input — save on blur\n'
        'document.addEventListener("blur", function(e){\n'
        '  if (!e.target.classList.contains("alloc-input")) return;\n'
        '  var addr = e.target.dataset.addr;\n'
        '  var alloc = parseFloat(e.target.value);\n'
        '  if (isNaN(alloc) || alloc <= 0) return;\n'
        '  api("/api/copy/wallet/" + addr + "/alloc", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({alloc:alloc})});\n'
        '}, true);\n\n'
        '// Wallet checkbox\n'
        'document.addEventListener("change", function(e){\n'
        '  if (!e.target.classList.contains("wallet-select")) return;\n'
        '  var addr = e.target.dataset.addr;\n'
        '  api("/api/copy/wallet/" + addr + "/toggle", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({alloc:0.01})})\n'
        '    .then(function(){ refreshCopy(); });\n'
        '});\n\n'
        '// ── Utils ─────────────────────────────────────────────────────\n'
        'function shorten(s, n) { if (!s || s.length < n*2) return s||""; return s.slice(0,n) + "..." + s.slice(-4); }\n'
        'function fmtTs(ts) { if (!ts) return "?"; var d = new Date(ts * 1000); return d.toISOString().slice(11,19); }\n'
        'function fmtAge(s) {\n'
        '  if (!s || s === "N/A") return "N/A";\n'
        '  try { var diff = (Date.now() - new Date(s.replace("Z","+00:00")).getTime()) / 1000;\n'
        '    if (diff < 60) return Math.floor(diff) + "s ago";\n'
        '    if (diff < 3600) return Math.floor(diff/60) + "m ago";\n'
        '    if (diff < 86400) return Math.floor(diff/3600) + "h ago";\n'
        '    return Math.floor(diff/86400) + "d ago";\n'
        '  } catch(e) { return s.slice(0,16); }\n'
        '}\n\n'
        '// ── Uptime ────────────────────────────────────────────────────\n'
        'function updateUptime() {\n'
        '  var s = Math.floor((Date.now() - uptimeStart) / 1000);\n'
        '  var m = Math.floor(s / 60), h = Math.floor(m / 60);\n'
        '  document.getElementById("uptime").textContent = h > 0 ? h + "h " + (m%60) + "m" : m + "m " + (s%60) + "s";\n'
        '}\n'
        'setInterval(updateUptime, 1000);\n\n'
        '// ── Background polling — no full page reload ───────────────────\n'
        'function backgroundRefresh() {\n'
        '  if (activeTab === "discovery") refreshStats();\n'
        '  else if (activeTab === "copy") refreshCopy();\n'
        '}\n'
        'setInterval(backgroundRefresh, 10000);\n'
        'setTimeout(backgroundRefresh, 3000);\n'
        '</script>\n'
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
    if addr in config["copies"]:
        del config["copies"][addr]
        config["global_enabled"] = False
    config["user_wallet"] = addr
    save_copy_config(config)
    return {"ok": True}


@app.post("/api/copy/wallet/{addr}/toggle", status_code=200)
async def toggle_wallet_copy(addr: str, request: Request):
    body = await request.json()
    alloc = float(body.get("alloc", 0.01))
    config = load_copy_config()
    if addr not in config["copies"]:
        config["copies"][addr] = {"enabled": True, "alloc_sol": alloc, "last_sig": "", "last_copy_ts": 0}
    else:
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


@app.get("/api/wallets")
async def get_wallets():
    wallets = load_wallets_cached()
    sorted_wallets = sorted(wallets, key=lambda w: float(w.get("score", 0)), reverse=True)
    return JSONResponse(sorted_wallets[:50])


# ── Main ──────────────────────────────────────────────────────────────

def main():
    port = 8891
    print(f"Starting Reef Dashboard on http://0.0.0.0:{port}")
    print(f"Data dir: {DATA_DIR}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


if __name__ == "__main__":
    main()
