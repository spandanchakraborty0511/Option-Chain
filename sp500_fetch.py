"""
sp500_fetch.py
──────────────────────────────────────────────────────────────────────────────
Loads SPY EOD options Parquet files (one per year) into SQLite.

Source files : D:\iisc\project\spy_data\spy_eod_YYYY.parquet
Output table : sp500_options  in  sp500_data.db
Output schema: long format — one row per strike per option_type (CE/PE)

Usage:
    python sp500_fetch.py
"""

import sqlite3
import os
import pandas as pd

# ── Config ────────────────────────────────────────────────────────────────────
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
DATA_DIR   = os.path.join(BASE_DIR, "spy_data")
DB_PATH    = os.path.join(BASE_DIR, "sp500_data.db")
TABLE      = "sp500_options"
YEARS      = range(2010, 2024)  # 2010 -> 2023

# ── DB setup ──────────────────────────────────────────────────────────────────
def init_db(conn):
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            date          TEXT,
            expiry        TEXT,
            strike        REAL,
            option_type   TEXT,
            underlying    REAL,
            price         REAL,
            bid           REAL,
            ask           REAL,
            volume        REAL,
            iv            REAL,
            dte           INTEGER,
            PRIMARY KEY (date, expiry, strike, option_type)
        )
    """)
    conn.commit()


def already_loaded(conn, year):
    """Return True if any row for this year exists in DB."""
    r = conn.execute(
        f"SELECT 1 FROM {TABLE} WHERE date LIKE '{year}%' LIMIT 1"
    ).fetchone()
    return r is not None


# ── Transform one parquet file ────────────────────────────────────────────────
def process_file(df: pd.DataFrame) -> pd.DataFrame:
    """Melt wide CE+PE columns into long format."""

    # Normalise dates to YYYY-MM-DD string
    df["[QUOTE_DATE]"]  = pd.to_datetime(df["[QUOTE_DATE]"],  errors="coerce").dt.strftime("%Y-%m-%d")
    df["[EXPIRE_DATE]"] = pd.to_datetime(df["[EXPIRE_DATE]"], errors="coerce").dt.strftime("%Y-%m-%d")

    # Drop rows where dates couldn't be parsed
    df = df.dropna(subset=["[QUOTE_DATE]", "[EXPIRE_DATE]"])

    # Build CE rows
    ce = df[["[QUOTE_DATE]", "[EXPIRE_DATE]", "[STRIKE]",
             "[UNDERLYING_LAST]", "[C_LAST]", "[C_BID]", "[C_ASK]",
             "[C_VOLUME]", "[C_IV]", "[DTE]"]].copy()
    ce.columns = ["date", "expiry", "strike",
                  "underlying", "price", "bid", "ask",
                  "volume", "iv", "dte"]
    ce["option_type"] = "CE"

    # Build PE rows
    pe = df[["[QUOTE_DATE]", "[EXPIRE_DATE]", "[STRIKE]",
             "[UNDERLYING_LAST]", "[P_LAST]", "[P_BID]", "[P_ASK]",
             "[P_VOLUME]", "[P_IV]", "[DTE]"]].copy()
    pe.columns = ["date", "expiry", "strike",
                  "underlying", "price", "bid", "ask",
                  "volume", "iv", "dte"]
    pe["option_type"] = "PE"

    combined = pd.concat([ce, pe], ignore_index=True)

    # Drop rows with no price AND no volume
    combined = combined[
        combined["price"].notna() | combined["volume"].notna()
    ]

    return combined


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    if not os.path.exists(DATA_DIR):
        print(f"ERROR: Data folder not found at {DATA_DIR}")
        return

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    print(f"Loading Parquet files from {DATA_DIR}")
    print("=" * 60)

    total_saved = 0

    for year in YEARS:
        fname = f"spy_eod_{year}.parquet"
        fpath = os.path.join(DATA_DIR, fname)

        if not os.path.exists(fpath):
            print(f"  {year}: file not found, skipping.")
            continue

        if already_loaded(conn, year):
            print(f"  {year}: already in DB, skipping.")
            continue

        print(f"  {year}: reading {fname}...", end=" ", flush=True)
        df = pd.read_parquet(fpath)
        print(f"{len(df):,} rows read", end=" | ", flush=True)

        processed = process_file(df)

        if processed.empty:
            print("0 rows after processing, skipping.")
            continue

        processed.to_sql(TABLE, conn, if_exists="append",
                         index=False, method="multi",
                         chunksize=100)
        conn.commit()

        saved = len(processed)
        total_saved += saved
        print(f"saved {saved:,} rows | {processed['date'].min()} -> {processed['date'].max()}")

    # Final stats
    print("=" * 60)
    total      = conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
    date_range = conn.execute(f"SELECT MIN(date), MAX(date) FROM {TABLE}").fetchone()
    print(f"Done. Total rows in DB : {total:,}")
    print(f"Date range             : {date_range[0]} -> {date_range[1]}")
    conn.close()


if __name__ == "__main__":
    main()