from pathlib import Path
from datetime import datetime, timedelta, timezone
import time
import duckdb
import pandas as pd

DB_PATH = Path(__file__).parent / "data" / "reef.db"
DATA_DIR = Path(__file__).parent / "data"

def get_db(read_only: bool = True, retries: int = 8, base_delay: float = 0.5) -> duckdb.DuckDBPyConnection:
    """Open a DuckDB connection with retry on lock contention.
    No singleton — open/close per call to avoid holding locks.
    Dashboard uses this for reads; scanner uses get_writer_db() for writes.
    Retries up to `retries` times with exponential backoff (0.5s, 1s, 2s…).
    Max wait ≈ 63s — covers the full scanner write phase (insert_swaps + save_wallets).
    """
    last_err = None
    for attempt in range(retries):
        try:
            return duckdb.connect(str(DB_PATH), read_only=read_only)
        except Exception as e:
            if 'lock' in str(e).lower() and attempt < retries - 1:
                delay = base_delay * (2 ** attempt)
                time.sleep(delay)
                last_err = e
                continue
            raise
    raise last_err  # unreachable but satisfies type checkers

def query_db(sql: str, params: list = None) -> list:
    """Execute a read-only query and return results as dicts. Opens/closes a connection each call — safe for concurrent use."""
    con = get_db()  # inherits retry logic
    try:
        if params:
            result = con.execute(sql, params).fetchall()
        else:
            result = con.execute(sql).fetchall()
        cols = [d[0] for d in con.description] if con.description else []
        return [dict(zip(cols, row)) for row in result]
    finally:
        con.close()

def get_writer_db(retries: int = 6, base_delay: float = 1.0) -> duckdb.DuckDBPyConnection:
    """Get a fresh write-capable connection with retry on lock contention.
    Retries up to `retries` times with exponential backoff (1s, 2s, 4s…).
    Dashboard read-only connections hold a shared lock briefly; retrying
    avoids the scanner crashing on a transient collision.
    """
    last_err = None
    for attempt in range(retries):
        try:
            return duckdb.connect(str(DB_PATH), read_only=False)
        except Exception as e:
            if 'lock' in str(e).lower() and attempt < retries - 1:
                delay = base_delay * (2 ** attempt)
                time.sleep(delay)
                last_err = e
                continue
            raise
    raise last_err  # unreachable but satisfies type checkers

def init_db():
    """Create tables and indexes if they don't exist. Opens+closes writer connection.
    Fast path: uses read-only connection to check for existing tables; skips write
    lock acquisition entirely on all runs after first setup — eliminates 99% of
    lock contention between scanner and dashboard.
    """
    # Fast read-only check — tables exist on every run after first setup.
    try:
        con = get_db(read_only=True)
        tables = {row[0] for row in con.execute("SHOW TABLES").fetchall()}
        con.close()
        if 'swaps' in tables and 'wallets' in tables:
            return  # Already initialized — no write lock needed
    except Exception:
        pass  # DB may not exist yet; fall through to create

    con = get_writer_db()
    try:
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
    finally:
        con.close()  # Release the write lock so scanner + dashboard don't conflict

# ── Swaps ─────────────────────────────────────────────────────────────────────

def insert_swaps(swaps: list):
    """Batch-insert a list of ParsedSwap objects. Upserts by signature."""
    if not swaps:
        return
    con = get_writer_db()
    try:
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
    finally:
        con.close()

def get_swaps_df(limit: int = 1000) -> pd.DataFrame:
    """Get recent swaps as DataFrame. Capped to avoid full table scan."""
    cap = min(limit, 5000)
    con = get_db()
    try:
        return con.execute(f"SELECT * FROM swaps ORDER BY block_time DESC LIMIT {cap}").df()
    finally:
        con.close()

def get_all_swaps_list(limit: int = 1000000) -> list:
    """Get swaps as list of dicts. Default: all swaps (for scanner recompute)."""
    cap = limit
    con = get_db()
    rows = con.execute(f"SELECT * FROM swaps ORDER BY block_time DESC LIMIT {cap}").fetchall()
    cols = [d[0] for d in con.description]
    con.close()
    return [dict(zip(cols, r)) for r in rows]

def get_recent_swaps(limit: int = 50) -> pd.DataFrame:
    """Get most recent swaps (capped for performance)."""
    cap = min(limit, 100)
    con = get_db()
    try:
        return con.execute(
            f"SELECT signature, wallet, dex, token_mint, action, amount, amount_sol, price_sol, slot, block_time, fee, solscan_sig "
            f"FROM swaps ORDER BY block_time DESC LIMIT {cap}"
        ).df()
    finally:
        con.close()

def swap_count() -> int:
    """Total number of swaps in DB."""
    con = get_db()
    try:
        return con.execute("SELECT COUNT(*) FROM swaps").fetchone()[0]
    finally:
        con.close()

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
    try:
        con.execute("DELETE FROM wallets")
        con.execute("INSERT INTO wallets SELECT * FROM df")
    finally:
        con.close()

def get_top_wallets(limit: int = 50) -> pd.DataFrame:
    """Top wallets by score (capped for performance)."""
    cap = min(limit, 20)
    con = get_db()
    try:
        return con.execute(
            f"SELECT address, score, total_trades, win_rate, profit_factor, avg_roi, best_roi, worst_roi, "
            f"avg_hold_minutes, last_active, favorite_token, solscan_link "
            f"FROM wallets ORDER BY score DESC LIMIT {cap}"
        ).df()
    finally:
        con.close()

def get_qualified_wallets(limit: int = 200) -> pd.DataFrame:
    """Qualified wallets (score >= 0.5). Capped for performance."""
    cap = min(limit, 500)
    con = get_db()
    try:
        return con.execute(
            f"SELECT * FROM wallets WHERE score >= 0.5 ORDER BY score DESC LIMIT {cap}"
        ).df()
    finally:
        con.close()

def wallet_count() -> tuple[int, int]:
    """Returns (total_wallets, qualified_wallets)."""
    con = get_db()
    try:
        total = con.execute("SELECT COUNT(*) FROM wallets").fetchone()[0]
        qual = con.execute("SELECT COUNT(*) FROM wallets WHERE score >= 0.5").fetchone()[0]
        return total, qual
    finally:
        con.close()

# ── Dashboard stats ────────────────────────────────────────────────────────────

def get_stats() -> dict:
    """Compute dashboard stats from DuckDB. Uses LIMIT caps + indexes for speed."""
    con = get_db()
    try:
        # Fast counts (COUNT with index hint is fast in DuckDB)
        swap_ct = con.execute("SELECT COUNT(*) FROM swaps").fetchone()[0]
        buys  = con.execute("SELECT COUNT(*) FROM swaps WHERE action = 'BUY'").fetchone()[0]
        sells = con.execute("SELECT COUNT(*) FROM swaps WHERE action = 'SELL'").fetchone()[0]

        # DEX breakdown (fast with GROUP BY)
        dex_rows = con.execute(
            "SELECT dex, COUNT(*) as ct FROM swaps GROUP BY dex ORDER BY ct DESC LIMIT 5"
        ).fetchall()
        dex_counts = {dex: ct for dex, ct in dex_rows}

        # Last scan time
        last_st = con.execute("SELECT MAX(block_time) FROM swaps").fetchone()[0] or 0

        # Wallet counts (inline — avoid opening a second connection)
        total_w = con.execute("SELECT COUNT(*) FROM wallets").fetchone()[0]
        qual_w  = con.execute("SELECT COUNT(*) FROM wallets WHERE score >= 0.5").fetchone()[0]

        # Top 10 wallets
        top_rows = con.execute(
            "SELECT address, score, total_trades, win_rate, profit_factor, avg_roi, best_roi, worst_roi, "
            "avg_hold_minutes, last_active, favorite_token, solscan_link "
            "FROM wallets ORDER BY score DESC LIMIT 10"
        ).fetchall()
        cols = [d[0] for d in con.description]
        top_wallets = [dict(zip(cols, r)) for r in top_rows]
        top_wallet = top_wallets[0] if top_wallets else None

        # Recent 25 swaps
        swap_rows = con.execute(
            "SELECT signature, wallet, dex, token_mint, action, amount, amount_sol, price_sol, slot, block_time, fee, solscan_sig "
            "FROM swaps ORDER BY block_time DESC LIMIT 25"
        ).fetchall()
        swap_cols = [d[0] for d in con.description]
        recent_swaps = [dict(zip(swap_cols, r)) for r in swap_rows]
    finally:
        con.close()

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
    try:
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
    finally:
        con.close()
