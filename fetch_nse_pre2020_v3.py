"""
fetch_nse_pre2020_v3.py — same as v2, but reads from the cleaned catalog
"""

import sqlite3
import time
import calendar
from datetime import date, datetime
import pandas as pd
from nse import NSE

DB_PATH = r"D:\iisc\project\options_data.db"
DOWNLOAD_FOLDER = r"D:\iisc\project\nse_cache"
CLEAN_CATALOG_CSV = r"D:\nifty_intraday\pre2020_symbol_catalog_clean.csv"

START_YEAR = 2012
END_YEAR = 2019
REQUEST_DELAY = 0.4

INDEX_SYMBOLS = {"NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK"}
EXCLUDE_FROM_STOCKS = {"NIFTY", "BANKNIFTY", "INDIAVIX", "CNX-IT"}


def parse_date_flex(date_str: str) -> int:
    day, mon, year = date_str.split("-")
    dt = datetime.strptime(f"{day}-{mon.capitalize()}-{year}", "%d-%b-%Y")
    return int(dt.strftime("%Y%m%d"))


def build_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pre2020_price_history (
            date INTEGER, time TEXT, symbol TEXT, instrument_type TEXT,
            open REAL, high REAL, low REAL, close REAL,
            volume REAL, open_interest REAL, source_file TEXT,
            UNIQUE(date, time, symbol, instrument_type)
        )
    """)
    conn.commit()


def insert_rows(conn, rows):
    if not rows:
        return 0
    cur = conn.executemany(
        """INSERT OR IGNORE INTO pre2020_price_history
           (date,time,symbol,instrument_type,open,high,low,close,volume,open_interest,source_file)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        rows
    )
    conn.commit()
    return cur.rowcount if cur.rowcount != -1 else len(rows)


def fetch_equity_year(nse, symbol, year):
    from_d = date(year, 1, 1)
    to_d = date(year, 12, 31)
    try:
        rows = nse.fetch_equity_historical_data(symbol, from_date=from_d, to_date=to_d)
    except Exception as e:
        print(f"    ERROR fetching {symbol} {year}: {e}", flush=True)
        return []
    time.sleep(REQUEST_DELAY)
    out = []
    for r in rows:
        try:
            d = parse_date_flex(r["mtimestamp"])
            out.append((d, "15:30", symbol, "SPOT",
                        float(r["chOpeningPrice"]), float(r["chTradeHighPrice"]),
                        float(r["chTradeLowPrice"]), float(r["chClosingPrice"]),
                        float(r["chTotTradedQty"]) if r.get("chTotTradedQty") is not None else None,
                        None, "NSE_API_equity"))
        except (KeyError, ValueError, TypeError):
            continue
    return out


def main():
    conn = sqlite3.connect(DB_PATH)
    build_db(conn)

    catalog = pd.read_csv(CLEAN_CATALOG_CSV)
    stock_symbols = sorted(
        s for s in catalog["symbol"].unique() if s not in EXCLUDE_FROM_STOCKS
    )
    print(f"Found {len(stock_symbols)} clean stock symbols to fetch", flush=True)

    total_inserted = 0
    with NSE(download_folder=DOWNLOAD_FOLDER) as nse:
        for i, symbol in enumerate(stock_symbols):
            symbol_total = 0
            for year in range(START_YEAR, END_YEAR + 1):
                rows = fetch_equity_year(nse, symbol, year)
                inserted = insert_rows(conn, rows)
                symbol_total += inserted
                total_inserted += inserted
            print(f"  [{i+1}/{len(stock_symbols)}] {symbol}: {symbol_total} rows inserted", flush=True)

    print(f"\nDone. Total rows inserted: {total_inserted}", flush=True)
    conn.close()


if __name__ == "__main__":
    main()