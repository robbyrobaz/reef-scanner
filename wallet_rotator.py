#!/usr/bin/env python3
"""
Wallet Rotator — Daily auto-rotation of watched copy-trade wallets.

Strategy:
  1. Pull top CANDIDATE_POOL wallets from reef.db (recent, profitable)
  2. Simulate copying each wallet at COPY_ALLOC_SOL per trade (FIFO)
  3. Score profit-heavy: 50% net PnL, 25% profit factor, 15% win rate, 10% recency
  4. Build new top-MAX_WATCHED list from simulation results
  5. Rotate copy_config.json: drop underperformers, add better wallets
  6. Protections: never drop wallets with recent copy activity, max MAX_ROTATE_PER_RUN changes

Run:   python wallet_rotator.py [--dry-run]
Cron:  17 3 * * * cd /home/rob/reef-workspace && venv/bin/python wallet_rotator.py >> cron/rotator.log 2>&1
"""

import sys
import json
import time
import argparse
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple

import duckdb

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))
from copy_config import load_copy_config, save_copy_config, CopyEntry, config_lock

# ── Tuning ───────────────────────────────────────────────────────────────────
COPY_ALLOC_SOL       = 0.01   # Simulated position size per buy
MAX_WATCHED          = 15     # Max wallets to watch simultaneously (10 core + 5 exploration)
MAX_ROTATE_PER_RUN   = 5      # Max wallets to swap out per daily run (limits churn)
MIN_SIM_TRADES       = 15     # Min completed round-trips from simulation to qualify
MIN_ACTIVE_DAYS      = 7      # Wallet must have traded within this many days
LOOKBACK_DAYS        = 30     # How far back to pull swaps for simulation
CANDIDATE_POOL       = 300    # DB candidates to simulate before ranking
PROTECT_COPY_HOURS   = 24     # Don't drop a wallet that copied a trade within this many hours
DROP_IF_SIM_PNL_BELOW = -0.05 # Always drop if simulated PnL is this negative (chronic loser)

# Live performance override: wallets with enough real copy data and bad live PnL
# are NEVER protected from rotation, regardless of recency.
LIVE_OVERRIDE_MIN_TRADES = 8    # Need at least this many completed live round-trips
LIVE_OVERRIDE_MAX_PNL    = -0.01 # Override protection if live net PnL is below this (SOL)
MAX_PRICE_SOL        = 5.0    # Skip swaps with price > this (likely parse errors)
MAX_TRADE_RETURN_X   = 30.0   # Cap any single trade return at 30x — prevents price
                               # parse artifacts from dominating sim (e.g. 1e-10 buy price)
MIN_SIM_LOSSES       = 2      # Require at least 2 losing trades — pure win streaks
                               # are almost always incomplete data, not skill

DB_PATH              = BASE_DIR / "data" / "reef.db"
PAPER_POSITIONS_FILE = BASE_DIR / "data" / "paper_positions.json"
LOG_FILE             = BASE_DIR / "cron" / "rotator.log"

# ── Score weights (must sum to 1.0) ──────────────────────────────────────────
W_NET_PNL        = 0.50
W_PROFIT_FACTOR  = 0.25
W_WIN_RATE       = 0.15
W_RECENCY        = 0.10


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts}  {msg}", flush=True)


# ── Simulation ────────────────────────────────────────────────────────────────

def simulate(swaps: List[dict], alloc_sol: float = COPY_ALLOC_SOL) -> dict:
    """
    FIFO copy-trade simulation.
    BUY  → open a position of `alloc_sol` at entry price
    SELL → close oldest open position at exit price, compute PnL
    Returns simulation stats dict.
    """
    positions: Dict[str, list] = defaultdict(list)
    gross_profit = gross_loss = 0.0
    wins = losses = 0

    for swap in sorted(swaps, key=lambda x: x["block_time"]):
        price = float(swap.get("price_sol") or 0)
        if price <= 0 or price > MAX_PRICE_SOL:
            continue

        token  = swap["token_mint"]
        action = swap.get("action", "").upper()

        if action == "BUY":
            positions[token].append({"entry": price, "sol_in": alloc_sol})

        elif action == "SELL" and positions[token]:
            pos      = positions[token].pop(0)          # FIFO
            ratio    = price / pos["entry"]
            # Cap return at MAX_TRADE_RETURN_X to suppress price-parse artifacts
            ratio    = min(ratio, MAX_TRADE_RETURN_X)
            sol_out  = pos["sol_in"] * ratio
            pnl      = sol_out - pos["sol_in"]
            if pnl > 0:
                gross_profit += pnl
                wins += 1
            else:
                gross_loss += abs(pnl)
                losses += 1

    completed = wins + losses
    net_pnl   = gross_profit - gross_loss
    pf        = (gross_profit / gross_loss) if gross_loss > 0 else (
                 999.0 if gross_profit > 0 else 0.0)
    wr        = wins / completed if completed > 0 else 0.0

    return {
        "completed": completed,
        "wins":      wins,
        "losses":    losses,
        "win_rate":  wr,
        "net_pnl":   net_pnl,
        "gross_profit": gross_profit,
        "gross_loss":   gross_loss,
        "profit_factor": pf,
    }


def score(sim: dict, last_active_ts: int, now_ts: int) -> float:
    """
    Profit-heavy composite score from simulation.
    Returns -999 if wallet doesn't meet minimum trade threshold.
    """
    if sim["completed"] < MIN_SIM_TRADES:
        return -999.0
    # Require minimum losses — wallets with no losing trades have incomplete
    # data (buys without matched sells) and are unreliable signals.
    if sim["losses"] < MIN_SIM_LOSSES:
        return -999.0

    # PnL score: 0 baseline at 0 SOL, max 1.0 at +2 SOL, min 0.0 at -2 SOL
    pnl_score = max(0.0, min(1.0, (sim["net_pnl"] + 2.0) / 4.0))

    # Profit factor score: PF=1 → 0.0, PF=5 → 0.5, PF=9+ → 1.0
    pf = min(sim["profit_factor"], 10.0)
    pf_score = max(0.0, min(1.0, (pf - 1.0) / 9.0))

    # Recency: 1.0 if active today, 0.0 if >= MIN_ACTIVE_DAYS old
    days_ago  = (now_ts - last_active_ts) / 86400
    recency   = max(0.0, 1.0 - (days_ago / MIN_ACTIVE_DAYS))

    return (
        W_NET_PNL       * pnl_score    +
        W_PROFIT_FACTOR * pf_score     +
        W_WIN_RATE      * sim["win_rate"] +
        W_RECENCY       * recency
    )


# ── DB Helpers ────────────────────────────────────────────────────────────────

def get_candidates(con, lookback_cutoff: int) -> List[dict]:
    """
    Pull candidate wallets from DB: decent PF, active recently, enough trades.
    Excludes wallets with suspiciously perfect records (PF >= 500) from top pool
    but still includes them as lower-priority candidates so simulation can judge.
    """
    rows = con.execute("""
        SELECT
            w.address,
            w.profit_factor,
            w.win_rate,
            w.total_trades,
            CAST(EPOCH(w.last_active) AS BIGINT)  AS last_active_ts
        FROM wallets w
        WHERE w.total_trades  >= ?
          AND w.profit_factor  > 1.0
          AND w.win_rate       >= 0.45
          AND EPOCH(w.last_active) >= ?
        ORDER BY
            CASE WHEN w.profit_factor >= 500 THEN 0 ELSE 1 END DESC,
            (w.profit_factor * GREATEST(w.avg_roi, 0.0)) DESC
        LIMIT ?
    """, [MIN_SIM_TRADES, lookback_cutoff, CANDIDATE_POOL]).fetchall()

    cols = [d[0] for d in con.description]
    return [dict(zip(cols, row)) for row in rows]


def get_swaps_batch(con, addresses: List[str], since: int) -> Dict[str, List[dict]]:
    """Fetch swaps for a list of wallets in one query. Returns {address: [swaps]}."""
    if not addresses:
        return {}
    ph = ",".join("?" for _ in addresses)
    rows = con.execute(
        f"SELECT wallet, token_mint, action, price_sol, block_time "
        f"FROM swaps WHERE wallet IN ({ph}) AND block_time >= ? ORDER BY block_time",
        addresses + [since],
    ).fetchall()

    by_wallet: Dict[str, List[dict]] = defaultdict(list)
    for wallet, token_mint, action, price_sol, block_time in rows:
        by_wallet[wallet].append({
            "token_mint": token_mint,
            "action":     action,
            "price_sol":  price_sol,
            "block_time": block_time,
        })
    return dict(by_wallet)


# ── Rotation Logic ────────────────────────────────────────────────────────────

def live_pnl_by_wallet() -> Dict[str, dict]:
    """
    Read copy_trades.csv and compute live realized PnL + completed trade count
    per source wallet.  Counts all SELL rows (including 0-PnL expired ones).
    Note: auto-expired positions are logged under source_wallet="auto_expire",
    so real wallet stats are not polluted by expiry noise.
    Returns {address: {"net_pnl": float, "completed": int}}
    """
    import csv as _csv
    trades_file = BASE_DIR / "data" / "copy_trades.csv"
    result: Dict[str, dict] = {}
    if not trades_file.exists():
        return result
    try:
        with open(trades_file) as f:
            for row in _csv.DictReader(f):
                if row.get("action") != "SELL":
                    continue
                try:
                    pnl = float(row["realized_pnl_sol"])
                except (ValueError, KeyError):
                    continue
                addr = row.get("source_wallet", "")
                if not addr:
                    continue
                if addr not in result:
                    result[addr] = {"net_pnl": 0.0, "completed": 0}
                result[addr]["net_pnl"] += pnl
                result[addr]["completed"] += 1
    except Exception:
        pass
    return result


def source_wallets_with_open_positions() -> set:
    """
    Return the set of source wallet addresses that have an open paper position
    we copied from them (i.e. we copied their BUY but haven't yet seen their SELL).
    Removing these wallets would orphan the open position permanently.
    """
    import csv as _csv
    paper_file = PAPER_POSITIONS_FILE
    trades_file = BASE_DIR / "data" / "copy_trades.csv"

    if not paper_file.exists() or not trades_file.exists():
        return set()
    try:
        open_mints = set(json.loads(paper_file.read_text()).keys())
        if not open_mints:
            return set()

        protected_wallets = set()
        with open(trades_file) as f:
            reader = _csv.DictReader(f)
            for row in reader:
                if row.get("action") == "BUY" and row.get("token_mint") in open_mints:
                    if row.get("source_wallet"):
                        protected_wallets.add(row["source_wallet"])
        return protected_wallets
    except Exception:
        return set()


def run_rotation(dry_run: bool = False):
    now_ts      = int(time.time())
    since_ts    = now_ts - LOOKBACK_DAYS * 86400
    active_cutoff = now_ts - MIN_ACTIVE_DAYS * 86400

    log(f"{'='*60}")
    log(f"Wallet Rotator  {'[DRY RUN] ' if dry_run else ''}— {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log(f"Lookback: {LOOKBACK_DAYS}d  |  Min trades: {MIN_SIM_TRADES}  |  Max rotate: {MAX_ROTATE_PER_RUN}")
    log(f"{'='*60}")

    # ── Load current config ───────────────────────────────────────────
    config   = load_copy_config()
    watched  = {addr: entry for addr, entry in config.copies.items() if entry.enabled}
    log(f"Currently watching {len(watched)} wallet(s)")

    # Wallets protected from removal:
    #  (a) source wallet has an open position we copied — removing it orphans the trade
    #  (b) copied a trade within PROTECT_COPY_HOURS — mid-trade, don't interrupt
    open_position_wallets = source_wallets_with_open_positions()
    protect_cutoff        = now_ts - PROTECT_COPY_HOURS * 3600
    recent_copy_wallets   = {
        addr for addr, entry in watched.items()
        if entry.last_copy_ts and entry.last_copy_ts >= protect_cutoff
    }

    # Live performance override: strip protection from wallets with enough real
    # data that show chronic losses.  Open-position protection is never overridden
    # (removing the wallet would orphan the trade permanently).
    live_stats = live_pnl_by_wallet()
    live_override = {
        addr for addr in recent_copy_wallets
        if addr not in open_position_wallets
        and live_stats.get(addr, {}).get("completed", 0) >= LIVE_OVERRIDE_MIN_TRADES
        and live_stats.get(addr, {}).get("net_pnl", 0.0) < LIVE_OVERRIDE_MAX_PNL
    }

    protected = open_position_wallets | (recent_copy_wallets - live_override)

    if open_position_wallets:
        log(f"  {len(open_position_wallets)} wallet(s) protected (open positions): "
            + ", ".join(a[:12] + "..." for a in open_position_wallets))
    if recent_copy_wallets:
        log(f"  {len(recent_copy_wallets)} wallet(s) protected (copied within {PROTECT_COPY_HOURS}h): "
            + ", ".join(a[:12] + "..." for a in recent_copy_wallets))
    if live_override:
        log(f"  {len(live_override)} wallet(s) protection OVERRIDDEN (bad live PnL ≥{LIVE_OVERRIDE_MIN_TRADES} trades): "
            + ", ".join(f"{a[:12]}... live={live_stats[a]['net_pnl']:+.4f} SOL ({live_stats[a]['completed']} trades)"
                        for a in live_override))

    # ── Query DB ─────────────────────────────────────────────────────
    log(f"\nQuerying reef.db for up to {CANDIDATE_POOL} candidates...")
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        candidates = get_candidates(con, active_cutoff)
        log(f"  {len(candidates)} candidates from DB")

        # Merge: make sure currently-watched wallets are always included for scoring
        candidate_addrs = {c["address"] for c in candidates}
        for addr, entry in watched.items():
            if addr not in candidate_addrs:
                candidates.append({
                    "address":        addr,
                    "profit_factor":  0.0,
                    "win_rate":       0.0,
                    "total_trades":   0,
                    "last_active_ts": 0,
                })

        all_addrs    = [c["address"] for c in candidates]
        log(f"  Fetching swaps for {len(all_addrs)} wallets since {LOOKBACK_DAYS}d ago...")
        swaps_by_wallet = get_swaps_batch(con, all_addrs, since_ts)
        log(f"  Got swap data for {len(swaps_by_wallet)} wallets")
    finally:
        con.close()

    # ── Simulate & Score ─────────────────────────────────────────────
    log(f"\nSimulating copy trades at {COPY_ALLOC_SOL} SOL/trade...")
    scored: List[Tuple[float, dict]] = []

    for c in candidates:
        addr      = c["address"]
        swaps     = swaps_by_wallet.get(addr, [])
        sim       = simulate(swaps)
        s         = score(sim, c.get("last_active_ts", 0), now_ts)
        scored.append((s, {**c, "sim": sim, "score": s}))

    # Sort descending by score
    scored.sort(key=lambda x: x[0], reverse=True)

    # Top MAX_WATCHED from simulation
    qualified = [(s, w) for s, w in scored if s > -999.0]
    new_top   = [w for _, w in qualified[:MAX_WATCHED]]
    new_top_addrs = {w["address"] for w in new_top}

    log(f"\n{'─'*60}")
    log(f"Top {min(25, len(qualified))} wallets by simulated PnL:")
    log(f"  {'Address':>20}  {'Score':>6}  {'Net PnL':>8}  {'PF':>6}  {'WR':>6}  {'Trades':>6}  {'Status'}")
    for s, w in qualified[:25]:
        sim    = w["sim"]
        status = ""
        if w["address"] in watched:
            status = "KEEP" if w["address"] in new_top_addrs else "DROP"
        else:
            status = "ADD " if w["address"] in new_top_addrs else "    "
        log(f"  {w['address'][:20]}  {s:6.3f}  {sim['net_pnl']:+8.4f}  "
            f"{sim['profit_factor']:6.2f}  {sim['win_rate']:5.1%}  "
            f"{sim['completed']:6d}  {status}")

    # ── Determine changes ─────────────────────────────────────────────
    to_add    = [w for w in new_top if w["address"] not in watched]
    to_remove = [
        addr for addr in watched
        if addr not in new_top_addrs
        and addr not in protected
    ]

    # Chronic losers: always drop regardless of MAX_ROTATE_PER_RUN
    chronic_losers = [
        addr for addr, entry in watched.items()
        if addr not in protected
        and any(
            w["address"] == addr and w["sim"]["net_pnl"] < DROP_IF_SIM_PNL_BELOW
            for _, w in scored
        )
    ]
    forced_drops = [a for a in chronic_losers if a not in to_remove]
    to_remove    = list(dict.fromkeys(to_remove + forced_drops))  # dedupe, preserve order

    # Cap rotation (but always honour forced drops of chronic losers)
    forced_count = len(forced_drops)
    normal_slots = max(0, MAX_ROTATE_PER_RUN - forced_count)
    to_remove_capped = forced_drops + [a for a in to_remove if a not in forced_drops][:normal_slots]

    # Also fill any empty slots up to MAX_WATCHED (e.g. after a manual removal).
    # These don't count against MAX_ROTATE_PER_RUN — filling a gap isn't churn.
    free_slots    = max(0, MAX_WATCHED - len(watched))
    to_add_capped = to_add[:len(to_remove_capped) + free_slots]

    log(f"\n{'─'*60}")
    log(f"Rotation plan:")
    if not to_remove_capped and not to_add_capped:
        log("  No changes needed — current wallet set is already optimal.")
    else:
        for addr in to_remove_capped:
            sim_entry = next((w for _, w in scored if w["address"] == addr), None)
            pnl_str   = f"  simPnL={sim_entry['sim']['net_pnl']:+.4f} SOL" if sim_entry else ""
            log(f"  REMOVE  {addr[:20]}...{pnl_str}")
        for w in to_add_capped:
            log(f"  ADD     {w['address'][:20]}...  simPnL={w['sim']['net_pnl']:+.4f} SOL  "
                f"PF={w['sim']['profit_factor']:.2f}  WR={w['sim']['win_rate']:.1%}  "
                f"trades={w['sim']['completed']}")

    if not dry_run and (to_remove_capped or to_add_capped):
        log(f"\nApplying changes...")
        for addr in to_remove_capped:
            entry = config.copies.pop(addr, None)
            log(f"  Removed {addr[:20]}...")

        for w in to_add_capped:
            sim   = w["sim"]
            label = f"rotator-{w['score']:.3f}"
            config.copies[w["address"]] = CopyEntry(
                enabled       = True,
                alloc_sol     = COPY_ALLOC_SOL,
                label         = label,
            )
            log(f"  Added   {w['address'][:20]}...  label={label}")

        with config_lock():
            save_copy_config(config)
        log(f"  Saved copy_config.json  ({len([e for e in config.copies.values() if e.enabled])} active wallets)")
    elif dry_run:
        log(f"\n[DRY RUN] — no changes written. Re-run without --dry-run to apply.")

    # ── Summary ───────────────────────────────────────────────────────
    log(f"\n{'─'*60}")
    log(f"Summary:")
    log(f"  Candidates evaluated:  {len(qualified)}")
    log(f"  Currently watching:    {len(watched)}")
    log(f"  Rotated out:           {len(to_remove_capped)}")
    log(f"  Rotated in:            {len(to_add_capped)}")
    protected_skipped = [a for a in to_remove if a in protected]
    if protected_skipped:
        log(f"  Skipped (protected):   {len(protected_skipped)} — {', '.join(a[:12]+'...' for a in protected_skipped)}")
    log(f"{'='*60}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reef wallet rotator")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without modifying copy_config.json")
    args = parser.parse_args()
    run_rotation(dry_run=args.dry_run)
