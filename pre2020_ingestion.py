import jugaad_data.nse as nse_module
from datetime import date, timedelta
import pandas as pd
import io
import sqlite3
import time
import os

DB_PATH = r"D:\iisc\project\options_data.db"
LOG_PATH = r"D:\iisc\project\pre2020_ingest_log.txt"

def already_done_dates():
    if not os.path.exists(LOG_PATH):
        return set()
    with open(LOG_PATH, "r") as f:
        return set(line.strip() for line in f if line.strip())

def mark_done(d, status):
    with open(LOG_PATH, "a") as f:
        f.write(f"{d.isoformat()},{status}\n")

def fetch_bhavcopy_fo_with_retry(d, max_retries=4, wait_seconds=3):
    for attempt in range(1, max_retries + 1):
        try:
            raw = nse_module.bhavcopy_fo_raw(d)
            return raw
        except Exception as e:
            if attempt < max_retries:
                time.sleep(wait_seconds)
            else:
                return None

def parse_and_map(raw, d):
    df = pd.read_csv(io.StringIO(raw))
    df.columns = [c.strip() for c in df.columns]

    mapped = pd.DataFrame({
        "date": d.isoformat(),
        "symbol": df["SYMBOL"],
        "expiry": pd.to_datetime(df["EXPIRY_DT"], format="%d-%b-%Y").dt.strftime("%Y-%m-%d"),
        "strike": df["STRIKE_PR"],
        "option_type": df["OPTION_TYP"],
        "open": df["OPEN"],
        "high": df["HIGH"],
        "low": df["LOW"],
        "close": df["CLOSE"],
        "last": df["SETTLE_PR"],
        "underlying_close": None,
        "volume": df["CONTRACTS"],
        "oi": df["OPEN_INT"],
        "oi_change": df["CHG_IN_OI"],
        "turnover": df["VAL_INLAKH"],
    })
    return mapped

def insert_batch(conn, df):
    df.to_sql("temp_insert_batch", conn, if_exists="replace", index=False)
    conn.execute("""
        INSERT OR IGNORE INTO nse_options
        (date, symbol, expiry, strike, option_type, open, high, low, close, last,
         underlying_close, volume, oi, oi_change, turnover)
        SELECT date, symbol, expiry, strike, option_type, open, high, low, close, last,
               underlying_close, volume, oi, oi_change, turnover
        FROM temp_insert_batch;
    """)
    conn.commit()

def main():
    start = date(2012, 1, 1)
    end = date(2019, 12, 31)
    done = already_done_dates()

    conn = sqlite3.connect(DB_PATH)

    d = start
    total_days = (end - start).days + 1
    processed = 0
    skipped_holidays = 0
    failed = 0

    while d <= end:
        processed += 1
        if d.weekday() >= 5:  # skip Sat/Sun
            d += timedelta(days=1)
            continue

        key = d.isoformat()
        if key in done:
            d += timedelta(days=1)
            continue

        raw = fetch_bhavcopy_fo_with_retry(d)
        if raw is None:
            mark_done(d, "FAILED")
            failed += 1
            print(f"[{processed}/{total_days}] {d}: FAILED (likely holiday or network issue)")
        else:
            try:
                mapped = parse_and_map(raw, d)
                insert_batch(conn, mapped)
                mark_done(d, f"OK-{len(mapped)}rows")
                print(f"[{processed}/{total_days}] {d}: OK - {len(mapped)} rows")
            except Exception as e:
                mark_done(d, f"PARSE_ERROR-{e}")
                print(f"[{processed}/{total_days}] {d}: PARSE ERROR - {e}")
                failed += 1

        time.sleep(1)  # be polite to NSE's servers
        d += timedelta(days=1)

    conn.close()
    print(f"\nDone. Failed/skipped: {failed}")

if __name__ == "__main__":
    main()