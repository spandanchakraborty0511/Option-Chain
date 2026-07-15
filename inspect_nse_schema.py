"""
fetch_nse_pre2020.py
──────────────────────────────────────────────────────────────────────────────
Pulls clean NIFTY/BANKNIFTY SPOT (index) and near-month FUTURES history
directly from NSE (via the `nse` package) for 2012-2019, and loads it into
pre2020_price_history in options_data.db — replacing the messy local-file
ingestion entirely.

Install first:
    pip install -U nse

Run:
    python fetch_nse_pre2020.py
"""

import sqlite3
import time
from datetime import date, datetime

from nse import NSE

DB_PATH = r"D:\iisc\project\options_data.db"
DOWNLOAD_FOLDER = r"D:\iisc\project\nse_cache"

SYMBOLS = {
    "NIFTY": "NIFTY 50",       # (our symbol name, NSE's index name)
    "BANKNIFTY": "NIFTY BANK",
}

START_YEAR = 2012
END_YEAR = 2019

REQUEST_DELAY = 0.4  # seconds between requests; NSE allows 3/sec, this keeps us under that


def parse_date_flex(date_str: str) -> int:
    """Parses NSE's date strings (case varies: '01-JAN-2018' or '01-Jan-2018')
    into an int YYYYMMDD, matching the schema the dashboard already expects."""
    day, mon, year = date_str.split("-")
    mon = mon.capitalize()
    dt = datetime.strptime(f"{day}-{mon}-{year}", "%d-%b-%Y")
    return int(dt.strftime("%Y%m%d"))


def build_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pre2020_price_history (
            date INTEGER,
            time TEXT,
            symbol TEXT,
            instrument_type TEXT,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL,
            open_interest REAL,
            source_file TEXT,
            UNIQUE(date, time, symbol, instrument_type)
        )
    """)
    conn.commit()


def fetch_spot_year(nse, our_symbol, nse_index_name, year):
    """Fetches one calendar year of index (spot) data for one symbol."""
    from_d = date(year, 1, 1)
    to_d = date(year, 12, 31)
    rows = nse.fetch_historical_index_data(nse_index_name, from_date=from_d, to_date=to_d)
    time.sleep(REQUEST_DELAY)

    out = []
    for r in rows:
        try:
            d = parse_date_flex(r["EOD_TIMESTAMP"])
            out.append((
                d, "15:30", our_symbol, "SPOT",
                float(r["EOD_OPEN_INDEX_VAL"]), float(r["EOD_HIGH_INDEX_VAL"]),
                float(r["EOD_LOW_INDEX_VAL"]), float(r["EOD_CLOSE_INDEX_VAL"]),
                float(r["HIT_TRADED_QTY"]) if r.get("HIT_TRADED_QTY") is not None else None,
                None,  # no open interest for spot index
                "NSE_API_index"
            ))
        except (KeyError, ValueError, TypeError) as e:
            print(f"    Skipped a malformed spot row for {our_symbol} {year}: {e}")
    return out


def fetch_futures_year(nse, our_symbol, year):
    """Fetches one calendar year of futures data, keeping only the
    near-month (earliest expiry) contract per trading day."""
    from_d = date(year, 1, 1)
    to_d = date(year, 12, 31)
    rows = nse.fetch_historical_fno_data(
        our_symbol, instrument="FUTIDX", from_date=from_d, to_date=to_d
    )
    time.sleep(REQUEST_DELAY)

    # Group by trade date, keep the row with the earliest expiry (near-month)
    by_date = {}
    for r in rows:
        try:
            d = parse_date_flex(r["FH_TIMESTAMP"])
            exp_day, exp_mon, exp_year = r["FH_EXPIRY_DT"].split("-")
            exp_key = (int(exp_year), exp_mon.capitalize(), int(exp_day))  # rough sort key
            exp_sortable = datetime.strptime(r["FH_EXPIRY_DT"], "%d-%b-%Y")
        except (KeyError, ValueError, TypeError) as e:
            print(f"    Skipped a malformed futures row for {our_symbol} {year}: {e}")
            continue

        if d not in by_date or exp_sortable < by_date[d][0]:
            by_date[d] = (exp_sortable, r)

    out = []
    for d, (_, r) in by_date.items():
        try:
            out.append((
                d, "15:30", our_symbol, "FUTURES_1",
                float(r["FH_OPENING_PRICE"]), float(r["FH_TRADE_HIGH_PRICE"]),
                float(r["FH_TRADE_LOW_PRICE"]), float(r["FH_CLOSING_PRICE"]),
                float(r["FH_TOT_TRADED_QTY"]) if r.get("FH_TOT_TRADED_QTY") is not None else None,
                float(r["FH_OPEN_INT"]) if r.get("FH_OPEN_INT") is not None else None,
                "NSE_API_futures"
            ))
        except (KeyError, ValueError, TypeError) as e:
            print(f"    Skipped a malformed futures row for {our_symbol} {year}: {e}")
    return out


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DROP TABLE IF EXISTS pre2020_price_history")
    build_db(conn)

    total_inserted = 0

    with NSE(download_folder=DOWNLOAD_FOLDER) as nse:
        for our_symbol, nse_index_name in SYMBOLS.items():
            for year in range(START_YEAR, END_YEAR + 1):
                print(f"Fetching {our_symbol} SPOT {year}...", flush=True)
                spot_rows = fetch_spot_year(nse, our_symbol, nse_index_name, year)
                print(f"  {len(spot_rows)} spot rows")

                print(f"Fetching {our_symbol} FUTURES {year}...", flush=True)
                fut_rows = fetch_futures_year(nse, our_symbol, year)
                print(f"  {len(fut_rows)} futures rows (near-month only)")

                all_rows = spot_rows + fut_rows
                if all_rows:
                    cur = conn.executemany(
                        """INSERT OR IGNORE INTO pre2020_price_history
                           (date,time,symbol,instrument_type,open,high,low,close,volume,open_interest,source_file)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        all_rows
                    )
                    inserted = cur.rowcount if cur.rowcount != -1 else 0
                    total_inserted += inserted
                    conn.commit()
                    print(f"  Inserted {inserted} rows for {our_symbol} {year}")

    print(f"\nDone. Total rows inserted: {total_inserted}")
    conn.close()


if __name__ == "__main__":
    main()