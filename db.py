from pathlib import Path
from datetime import datetime, timedelta, timezone
import duckdb
import pandas as pd

DB_PATH = Path(__file__).parent / "data" / "reef.db"
DATA_DIR = Path(__file__).parent / "data"

_conn = None

def get_db(read_only: bool = True) -> duckdb.DuckDBPyConnection:
    """Get or create the DuckDB connection (singleton).
    Dashboard callers should pass read_only=True (default).
    Scanner callers doing writes must pass read_only=False.
    """
    global _conn
    if _conn is None:
        _conn = duckdb.connect(str(DB_PATH), read_only=read_only)
    return _conn

def query_db(sql: str, params: list = None) -> list:
    """Execute a read-only query and return results as dicts. Opens/closes a connection each call — safe for concurrent use."""
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        if params:
            result = con.execute(sql, params).fetchall()
        else:
            result = con.execute(sql).fetchall()
        cols = [d[0] for d in con.description] if con.description else []
        con.close()
        return [dict(zip(cols, row)) for row in result]
    except Exception:
        con.close()
        raise

def get_writer_db() -> duckdb.DuckDBPyConnection:
    """Get a fresh write-capable connection (bypasses singleton). Use for scanner writes only."""
    return duckdb.connect(str(DB_PATH), read_only=False)

def init_db():
    """Create tables and indexes if they don't exist."""
    con = get_writer_db()
    con.execute("""
        CREATE TABLE IF NOT EXISTS swaps (
            signature   TEXT PRIMARY KEY,
            wallet      TEXT,
            dex         TEXT,
            token_mint  TEXT,
            action      TEXT,
            amount      DOUBLE,
            amount_sol   DOUBLE,
            price_sol   DOUBLE,
            slot        BIGINT,
            block_time  BIGINT,
            fee         BIGINT,
            solscan_sig TEXT DEFAULT ''
        )
    """)
    # Add missing columns to existing table
    try:
        con.execute("ALTER TABLE swaps ADD COLUMN solscan_sig TEXT DEFAULT ''")
    except Exception:
        pass  # column already exists
    con.execute("""
        CREATE TABLE IF NOT EXISTS wallets (
            address          TEXT PRIMARY KEY,
            score            DOUBLE,
            total_trades     INTEGER,
            win_rate         DOUBLE,
            profit_factor    DOUBLE,
            avg_roi          DOUBLE,
            best_roi         DOUBLE,
            worst_roi        DOUBLE,
            avg_hold_minutes INTEGER,
            last_active      TEXT,
            favorite_token  TEXT,
            solscan_link     TEXT
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_swaps_block_time ON swaps(block_time)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_wallets_score ON wallets(score)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_wallets_pf ON wallets(profit_factor)")

# ── Swaps ─────────────────────────────────────────────────────────────────────

def insert_swaps(swaps: list):
    """Batch-insert a list of ParsedSwap objects. Upserts by signature."""
    if not swaps:
        return
    con = get_writer_db()
    rows = [{
        "signature":  s.signature,
        "wallet":     s.wallet,
        "dex":        s.dex,
        "token_mint": s.token_mint,
        "action":     s.action,
        "amount":     float(s.amount) if s.amount else 0,
        "amount_sol": float(s.amount_sol) if s.amount_sol else 0,
        "price_sol":  float(s.price_sol) if s.price_sol else 0,
        "slot":       s.slot or 0,
        "block_time": s.block_time or 0,
        "fee":        s.fee or 0,
        "solscan_sig": f"https://solscan.io/tx/{s.signature}" if s.signature else "",
    } for s in swaps]
    df = pd.DataFrame(rows)
    con.execute("INSERT OR IGNORE INTO swaps BY NAME SELECT * FROM df")

def get_swaps_df() -> pd.DataFrame:
    return get_db().execute("SELECT * FROM swaps ORDER BY block_time DESC").df()

def get_all_swaps_list() -> list:
    rows = get_db().execute("SELECT * FROM swaps ORDER BY block_time DESC").fetchall()
    cols = [d[0] for d in get_db().description]
    return [dict(zip(cols, r)) for r in rows]

def get_recent_swaps(limit: int = 50) -> pd.DataFrame:
    """Get most recent swaps (capped for performance)."""
    cap = min(limit, 100)
    return get_db().execute(
        f"SELECT signature, wallet, dex, token_mint, action, amount, amount_sol, price_sol, slot, block_time, fee, solscan_sig "
        f"FROM swaps ORDER BY block_time DESC LIMIT {cap}"
    ).df()

def swap_count() -> int:
    """Total number of swaps in DB."""
    return get_db().execute("SELECT COUNT(*) FROM swaps").fetchone()[0]

# ── Wallets ───────────────────────────────────────────────────────────────────

def save_wallets(wallets: list):
    """Replace wallets table with fresh data from a list of WalletMetrics objects."""
    if not wallets:
        return
    rows = []
    for w in wallets:
        rows.append({
            "address":          w.address,
            "score":            round(w.score, 3),
            "total_trades":     w.total_trades,
            "win_rate":         round(w.win_rate, 3),
            "profit_factor":    round(w.profit_factor, 2),
            "avg_roi":          round(w.avg_roi, 3),
            "best_roi":         round(w.best_roi, 3),
            "worst_roi":        round(w.worst_roi, 3),
            "avg_hold_minutes": w.avg_hold_time_seconds // 60 if w.avg_hold_time_seconds else 0,
            "last_active":       w.last_active.isoformat() if w.last_active else "N/A",
            "favorite_token":   (w.favorite_token[:20] if w.favorite_token else "")[:20],
            "solscan_link":     f"https://solscan.io/account/{w.address}",
        })
    df = pd.DataFrame(rows)
    con = get_writer_db()
    con.execute("DELETE FROM wallets")
    con.execute("INSERT INTO wallets SELECT * FROM df")

def get_top_wallets(limit: int = 50) -> pd.DataFrame:
    """Top wallets by score (capped for performance)."""
    cap = min(limit, 20)
    return get_db().execute(
        f"SELECT address, score, total_trades, win_rate, profit_factor, avg_roi, best_roi, worst_roi, "
        f"avg_hold_minutes, last_active, favorite_token, solscan_link "
        f"FROM wallets ORDER BY score DESC LIMIT {cap}"
    ).df()

def get_qualified_wallets() -> pd.DataFrame:
    """Qualified wallets (score >= 0.5)."""
    return get_db().execute(
        "SELECT * FROM wallets WHERE score >= 0.5 ORDER BY score DESC"
    ).df()

def wallet_count() -> tuple[int, int]:
    """Returns (total_wallets, qualified_wallets)."""
    con = get_db()
    total = con.execute("SELECT COUNT(*) FROM wallets").fetchone()[0]
    qual = con.execute("SELECT COUNT(*) FROM wallets WHERE score >= 0.5").fetchone()[0]
    return total, qual

# ── Dashboard stats ────────────────────────────────────────────────────────────

def get_stats() -> dict:
    """Compute dashboard stats from DuckDB. Uses LIMIT caps + indexes for speed."""
    con = get_db()

    # Fast counts (COUNT with index hint is fast in DuckDB)
    swap_ct = con.execute("SELECT COUNT(*) FROM swaps").fetchone()[0]
    total_w, qual_w = wallet_count()
    buys  = con.execute("SELECT COUNT(*) FROM swaps WHERE action = 'BUY'").fetchone()[0]
    sells = con.execute("SELECT COUNT(*) FROM swaps WHERE action = 'SELL'").fetchone()[0]

    # DEX breakdown (fast with GROUP BY)
    dex_rows = con.execute(
        "SELECT dex, COUNT(*) as ct FROM swaps GROUP BY dex ORDER BY ct DESC LIMIT 5"
    ).fetchall()
    dex_counts = {dex: ct for dex, ct in dex_rows}

    # Last scan time
    last_st = con.execute("SELECT MAX(block_time) FROM swaps").fetchone()[0] or 0

    # Top 10 wallets (capped — don't need 50 for the UI table)
    top_wallets = get_top_wallets(10).to_dict("records")
    top_wallet = top_wallets[0] if top_wallets else None

    # Recent 25 swaps (capped — don't need 50)
    recent_swaps = get_recent_swaps(25).to_dict("records")

    return {
        "total_swaps":      swap_ct,
        "total_wallets":    total_w,
        "qualified_wallets": qual_w,
        "buys":             buys,
        "sells":            sells,
        "dex_counts":       dex_counts,
        "last_scan":        last_st,
        "top_wallet":       top_wallet,
        "top_wallets":      top_wallets,
        "recent_swaps":     recent_swaps,
    }

# ── Migration from CSV ─────────────────────────────────────────────────────────

def migrate_from_legacy():
    """One-time: import existing legacy CSV data into DuckDB."""
    swaps_csv   = DATA_DIR / "swaps.csv"
    wallets_csv = DATA_DIR / "wallets.csv"
    con = get_writer_db()

    if swaps_csv.exists():
        n = con.execute(f"SELECT COUNT(*) FROM swaps").fetchone()[0]
        if n == 0:
            df = pd.read_csv(swaps_csv, parse_dates=["block_time"])
            df["block_time"] = df["block_time"].astype("int64") // 1_000_000_000
            con.execute("INSERT INTO swaps SELECT * FROM df")
            print(f"  Migrated {len(df)} swaps from CSV")

    if wallets_csv.exists():
        n = con.execute("SELECT COUNT(*) FROM wallets").fetchone()[0]
        if n == 0:
            df = pd.read_csv(wallets_csv)
            con.execute("INSERT INTO wallets SELECT * FROM df")
            print(f"  Migrated {len(df)} wallets from CSV")

    con.execute("COMMIT")
