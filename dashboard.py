"""
Reef Scanner Dashboard — Real-time view of wallet discovery
Serves at http://<host>:8899
"""

import os
import csv
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
import uvicorn

# ── Paths ──────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
WALLETS_CSV = DATA_DIR / "wallets.csv"
SWAPS_CSV = DATA_DIR / "swaps.csv"
CRON_LOG = BASE_DIR / "cron" / "scanner.log"

app = FastAPI(title="Reef Scanner Dashboard")


# ── Data Loading ──────────────────────────────────────────────────────

def load_wallets() -> List[Dict]:
    """Load wallet metrics from CSV"""
    if not WALLETS_CSV.exists():
        return []
    wallets = []
    with open(WALLETS_CSV, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            wallets.append(row)
    return wallets


def load_swaps(limit: int = 50) -> List[Dict]:
    """Load recent swaps from CSV"""
    if not SWAPS_CSV.exists():
        return []
    swaps = []
    with open(SWAPS_CSV, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            swaps.append(row)
    return swaps[-limit:]


def get_last_cron_log() -> Optional[str]:
    """Get last lines from cron log"""
    if not CRON_LOG.exists():
        return None
    with open(CRON_LOG) as f:
        lines = f.readlines()
    return "".join(lines[-20:]) if lines else None


def get_stats() -> Dict:
    """Calculate dashboard stats"""
    wallets = load_wallets()
    swaps = load_swaps(limit=1000)

    # Wallet stats
    total_wallets = len(wallets)
    qualified_wallets = len([w for w in wallets if float(w.get("score", 0)) > 0.5])

    # Swap stats
    total_swaps = len(swaps)
    buys = len([s for s in swaps if s.get("action") == "BUY"])
    sells = len([s for s in swaps if s.get("action") == "SELL"])

    # DEX breakdown
    dex_counts: Dict[str, int] = {}
    for s in swaps:
        dex = s.get("dex", "unknown")
        dex_counts[dex] = dex_counts.get(dex, 0) + 1

    # Top wallets by score
    top_wallets = sorted(wallets, key=lambda w: float(w.get("score", 0)), reverse=True)[:10]

    # Recent activity (last 10 swaps)
    recent_swaps = list(reversed(swaps[-10:]))

    # Time since last scan
    last_scan = None
    if swaps:
        try:
            last_swap = swaps[-1]
            ts = int(last_swap.get("block_time", 0))
            if ts:
                last_scan = datetime.fromtimestamp(ts, tz=timezone.utc)
        except:
            pass

    return {
        "total_wallets": total_wallets,
        "qualified_wallets": qualified_wallets,
        "total_swaps": total_swaps,
        "buys": buys,
        "sells": sells,
        "dex_counts": dex_counts,
        "top_wallets": top_wallets,
        "recent_swaps": recent_swaps,
        "last_scan": last_scan,
        "now": datetime.now(timezone.utc),
    }


# ── HTML Dashboard ───────────────────────────────────────────────────

DARK_CSS = """
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
    background: #0d1117;
    color: #e6edf3;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
    font-size: 14px;
    line-height: 1.5;
    padding: 20px;
}
h1 { font-size: 24px; margin-bottom: 4px; }
h2 { font-size: 16px; color: #8b949e; margin: 20px 0 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; }
h3 { font-size: 13px; color: #8b949e; margin: 15px 0 8px; font-weight: 600; }
.header { margin-bottom: 20px; padding-bottom: 15px; border-bottom: 1px solid #21262d; }
.header p { color: #7d8590; font-size: 13px; }
.tag { display: inline-block; background: #1f6feb; color: #fff; padding: 2px 8px; border-radius: 12px; font-size: 11px; margin-left: 8px; vertical-align: middle; }
.live { background: #238636; }
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
.action BUY { background: #0d2d1a; color: #3fb950; }
.action SELL { background: #2d0d0d; color: #f85149; }
.dex-badge { background: #21262d; color: #8b949e; padding: 1px 6px; border-radius: 4px; font-size: 10px; }
.positive { color: #3fb950; }
.negative { color: #f85149; }
.neutral { color: #7d8590; }
.footer { text-align: center; color: #484f58; font-size: 11px; margin-top: 30px; padding-top: 15px; border-top: 1px solid #21262d; }
.status-row { display: flex; gap: 30px; margin-top: 10px; }
.status-item { color: #7d8590; font-size: 12px; }
.status-item span { color: #e6edf3; }
.best { background: linear-gradient(135deg, #0d2d1a 0%, #161b22 100%); border: 1px solid #238636; }
.worst { background: linear-gradient(135deg, #2d0d0d 0%, #161b22 100%); border: 1px solid #da3633; }
.best-worst { padding: 10px 14px; border-radius: 6px; }
.mono { font-family: 'SF Mono', Monaco, monospace; }
</style>
"""


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    stats = get_stats()

    # Build wallet rows
    wallet_rows = ""
    for w in stats["top_wallets"]:
        score = float(w.get("score", 0))
        score_color = "#58a6ff" if score > 0.5 else "#7d8590"
        trades = w.get("total_trades", "0")
        win_rate = w.get("win_rate", "N/A")
        avg_roi = w.get("avg_roi", "0")
        roi_pct = float(avg_roi) * 100 if avg_roi else 0
        roi_str = f"{roi_pct:.0f}%"
        roi_color = "#3fb950" if roi_pct > 0 else "#f85149" if roi_pct < 0 else "#7d8590"
        addr = w.get("address", "")
        short_addr = f"{addr[:8]}...{addr[-4:]}" if addr else "N/A"
        link = w.get("solscan_link", "#")
        fav = w.get("favorite_token", "")[:12]
        last_active = w.get("last_active", "N/A")
        if last_active and last_active != "N/A":
            try:
                dt = datetime.fromisoformat(last_active.replace("Z", "+00:00"))
                last_active = dt.strftime("%m/%d %H:%M")
            except:
                pass

        wallet_rows += f"""
        <tr>
            <td class="addr"><a href="{link}" target="_blank" style="color:#58a6ff;text-decoration:none">{short_addr}</a></td>
            <td style="color:{score_color};font-weight:600">{score:.3f}</td>
            <td>{trades}</td>
            <td>{win_rate}</td>
            <td style="color:{roi_color}">{roi_str}</td>
            <td class="neutral">{fav}</td>
            <td class="neutral">{last_active}</td>
        </tr>"""

    # Build swap rows
    swap_rows = ""
    for s in stats["recent_swaps"]:
        action = s.get("action", "?")
        token = s.get("token_mint", "")[:12]
        amt = s.get("amount", "0")
        amt_sol = s.get("amount_sol", "0")
        dex = s.get("dex", "")
        sig = s.get("signature", "")
        short_sig = f"{sig[:8]}...{sig[-4:]}" if sig else "N/A"
        link = f"https://solscan.io/tx/{sig}" if sig else "#"

        try:
            ts = int(s.get("block_time", 0))
            if ts:
                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                time_str = dt.strftime("%H:%M:%S")
            else:
                time_str = "?"
        except:
            time_str = "?"

        swap_rows += f"""
        <tr>
            <td class="neutral">{time_str}</td>
            <td><span class="action {action}">{action}</span></td>
            <td class="mono" title="{s.get('token_mint', '')}">{token}</td>
            <td>{float(amt):.2f}</td>
            <td>{float(amt_sol):.4f} SOL</td>
            <td><span class="dex-badge">{dex}</span></td>
            <td class="addr"><a href="{link}" target="_blank" style="color:#58a6ff;text-decoration:none">{short_sig}</a></td>
        </tr>"""

    # Build DEX breakdown
    dex_rows = ""
    for dex, count in sorted(stats["dex_counts"].items(), key=lambda x: -x[1]):
        pct = count / stats["total_swaps"] * 100 if stats["total_swaps"] else 0
        dex_rows += f"""
        <tr>
            <td><span class="dex-badge">{dex}</span></td>
            <td>{count}</td>
            <td class="neutral">{pct:.1f}%</td>
        </tr>"""

    # Best/worst wallet
    best_wallet = stats["top_wallets"][0] if stats["top_wallets"] else None
    best_html = "No qualified wallets yet"
    if best_wallet and float(best_wallet.get("score", 0)) > 0:
        addr = best_wallet.get("address", "")
        best_html = f"""
        <div class="best-worst best">
            <strong class="addr">{addr[:16]}...{addr[-4:]}</strong><br>
            Score: {float(best_wallet.get('score', 0)):.3f} |
            Win Rate: {best_wallet.get('win_rate', 'N/A')} |
            ROI: {float(best_wallet.get('avg_roi', 0)) * 100:.0f}%
        </div>"""

    # Last scan time
    last_scan_str = "Never"
    if stats["last_scan"]:
        diff = (stats["now"] - stats["last_scan"]).total_seconds()
        if diff < 60:
            last_scan_str = f"{int(diff)}s ago"
        elif diff < 3600:
            last_scan_str = f"{int(diff/60)}m ago"
        else:
            last_scan_str = stats["last_scan"].strftime("%H:%M:%S")

    # Cron log
    cron_log = get_last_cron_log()
    log_html = ""
    if cron_log:
        log_lines = cron_log.strip().split("\n")[-8:]
        log_html = "<pre style='font-size:11px;overflow-x:auto;max-height:200px;'>"
        for line in log_lines:
            if "Scan complete" in line or "saved" in line.lower():
                log_html += f"<span style='color:#3fb950'>{line}</span>\n"
            elif "error" in line.lower() or "failed" in line.lower():
                log_html += f"<span style='color:#f85149'>{line}</span>\n"
            elif "scanning" in line.lower():
                log_html += f"<span style='color:#58a6ff'>{line}</span>\n"
            else:
                log_html += f"{line}\n"
        log_html += "</pre>"
    else:
        log_html = "<p class='neutral'>No cron log found</p>"

    html = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta http-equiv="refresh" content="30">
    <title>Reef Scanner Dashboard</title>
    {DARK_CSS}
</head>
<body>
    <div class="header">
        <h1>🏄 REEF SCANNER <span class="tag">LIVE</span></h1>
        <p>Solana DEX Wallet Discovery • Auto-scanning every 5 minutes</p>
        <div class="status-row">
            <div class="status-item">Last scan: <span>{last_scan_str}</span></div>
            <div class="status-item">Total swaps: <span>{stats['total_swaps']:,}</span></div>
            <div class="status-item">Wallets: <span>{stats['total_wallets']:,}</span></div>
            <div class="status-item">Up: <span id="uptime"></span></div>
        </div>
    </div>

    <div class="section">
        <h2>📊 SCAN STATS</h2>
        <div class="stats-grid">
            <div class="stat">
                <div class="stat-value">{stats['total_swaps']:,}</div>
                <div class="stat-label">Total Swaps</div>
            </div>
            <div class="stat">
                <div class="stat-value">{stats['total_wallets']:,}</div>
                <div class="stat-label">Wallets Found</div>
            </div>
            <div class="stat">
                <div class="stat-value" style="color:#3fb950">{stats['buys']:,}</div>
                <div class="stat-label">Buys</div>
            </div>
            <div class="stat">
                <div class="stat-value" style="color:#f85149">{stats['sells']:,}</div>
                <div class="stat-label">Sells</div>
            </div>
            <div class="stat">
                <div class="stat-value">{stats['qualified_wallets']}</div>
                <div class="stat-label">Qualified</div>
            </div>
        </div>
    </div>

    <div class="grid-2">
        <div class="section">
            <h2>🔥 DEX BREAKDOWN</h2>
            <table>
                <tr>
                    <th>DEX</th>
                    <th>Swaps</th>
                    <th>Share</th>
                </tr>
                {dex_rows or '<tr><td colspan=3 class="neutral">No data yet</td></tr>'}
            </table>
        </div>

        <div class="section">
            <h2>🏆 TOP WALLET</h2>
            {best_html}
        </div>
    </div>

    <div class="section">
        <h2>👛 TOP WALLETS BY SCORE</h2>
        <table>
            <tr>
                <th>Address</th>
                <th>Score</th>
                <th>Trades</th>
                <th>Win%</th>
                <th>ROI</th>
                <th>Favorite Token</th>
                <th>Last Active</th>
            </tr>
            {wallet_rows or '<tr><td colspan=7 class="neutral">No wallets yet</td></tr>'}
        </table>
    </div>

    <div class="section">
        <h2>💱 RECENT SWAPS</h2>
        <table>
            <tr>
                <th>Time</th>
                <th>Action</th>
                <th>Token</th>
                <th>Amount</th>
                <th>SOL Value</th>
                <th>DEX</th>
                <th>Signature</th>
            </tr>
            {swap_rows or '<tr><td colspan=7 class="neutral">No swaps yet</td></tr>'}
        </table>
    </div>

    <div class="section">
        <h2>📋 CRON LOG</h2>
        {log_html}
    </div>

    <div class="footer">
        Reef Scanner • Auto-refreshes every 30s • 
        <a href="https://github.com/robbyrobaz/reef-scanner" target="_blank" style="color:#58a6ff">GitHub</a> •
        Data: /home/rob/reef-workspace/data
    </div>

    <script>
    // Uptime counter
    let start = Date.now();
    function updateUptime() {{
        let s = Math.floor((Date.now() - start) / 1000);
        let m = Math.floor(s / 60);
        let h = Math.floor(m / 60);
        document.getElementById('uptime').textContent = 
            h > 0 ? h + 'h ' + (m % 60) + 'm' : m + 'm ' + (s % 60) + 's';
    }}
    setInterval(updateUptime, 1000);
    updateUptime();
    </script>
</body>
</html>"""
    return html


# ── Main ──────────────────────────────────────────────────────────────

def main():
    port = 8899
    print(f"Starting Reef Dashboard on http://0.0.0.0:{port}")
    print(f"Data dir: {DATA_DIR}")
    print(f"Wallets: {WALLETS_CSV}")
    print(f"Swaps: {SWAPS_CSV}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


if __name__ == "__main__":
    main()
