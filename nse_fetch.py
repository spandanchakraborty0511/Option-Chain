"""
NSE F&O Bhavcopy Downloader (New Format) - Fixed session handling
"""

import requests, zipfile, io, sqlite3, pandas as pd, time
from datetime import date, timedelta

DB_PATH   = r"D:\iisc\project\options_data.db"
FROM_DATE = date(2024, 1, 1)
TO_DATE = date(2024, 12, 31)
SYMBOLS   = {"NIFTY", "BANKNIFTY"}

COL_MAP = {
    "TradDt":          "date",
    "TckrSymb":        "symbol",
    "XpryDt":          "expiry",
    "StrkPric":        "strike",
    "OptnTp":          "option_type",
    "OpnPric":         "open",
    "HghPric":         "high",
    "LwPric":          "low",
    "ClsPric":         "close",
    "LastPric":        "last",
    "UndrlygPric":     "underlying_close",
    "TtlTradgVol":     "volume",
    "OpnIntrst":       "oi",
    "ChngInOpnIntrst": "oi_change",
    "TtlTrfVal":       "turnover",
}

HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection":      "keep-alive",
}


def warm_session(session):
    """Hit NSE homepage to get cookies, then the F&O page for extra cookies."""
    warmup_urls = [
        "https://www.nseindia.com/",
        "https://www.nseindia.com/market-data/live-equity-market",
        "https://www.nseindia.com/option-chain",
    ]
    for url in warmup_urls:
        try:
            r = session.get(url, headers=HEADERS, timeout=20)
            print(f"  Warmup {url} → {r.status_code}, cookies: {list(session.cookies.keys())}")
        except Exception as e:
            print(f"  Warmup failed for {url}: {e}")
        time.sleep(1)


def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS nse_options(
            date             TEXT,
            symbol           TEXT,
            expiry           TEXT,
            strike           REAL,
            option_type      TEXT,
            open             REAL,
            high             REAL,
            low              REAL,
            close            REAL,
            last             REAL,
            underlying_close REAL,
            volume           INTEGER,
            oi               INTEGER,
            oi_change        INTEGER,
            turnover         REAL,
            PRIMARY KEY (date, symbol, expiry, strike, option_type)
        )
    """)
    conn.commit()


def trading_days(start, end):
    days, d = [], start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def already_fetched(conn, dt):
    return conn.execute(
        "SELECT 1 FROM nse_options WHERE date=? LIMIT 1", (dt.isoformat(),)
    ).fetchone() is not None


def fetch_day(session, dt):
    url = (
        "https://nsearchives.nseindia.com/content/fo/"
        f"BhavCopy_NSE_FO_0_0_0_{dt.strftime('%Y%m%d')}_F_0000.csv.zip"
    )
    dl_headers = {**HEADERS, "Referer": "https://www.nseindia.com/"}

    for attempt in range(3):
        try:
            r = session.get(url, headers=dl_headers, timeout=30)
            if r.status_code == 404:
                return None          # holiday
            if r.status_code == 403:
                print(f"    403 on attempt {attempt+1}, re-warming...", end=" ")
                warm_session(session)
                time.sleep(3)
                continue
            if r.status_code != 200:
                print(f"    HTTP {r.status_code}")
                return None

            z  = zipfile.ZipFile(io.BytesIO(r.content))
            df = pd.read_csv(z.open(z.namelist()[0]))

            # Filter to our symbols
            df = df[df["TckrSymb"].isin(SYMBOLS)]
            return df

        except Exception as e:
            print(f"    Error attempt {attempt+1}: {e}")
            time.sleep(2)

    return None


def save_day(conn, df):
    existing = {c: COL_MAP[c] for c in COL_MAP if c in df.columns}
    df2 = df[list(existing.keys())].rename(columns=existing).copy()

    for col in ("date", "expiry"):
        if col in df2.columns:
            df2[col] = pd.to_datetime(df2[col], errors="coerce").dt.strftime("%Y-%m-%d")

    df2["strike"]    = pd.to_numeric(df2.get("strike"),    errors="coerce")
    df2["volume"]    = pd.to_numeric(df2.get("volume"),    errors="coerce").fillna(0).astype(int)
    df2["oi"]        = pd.to_numeric(df2.get("oi"),        errors="coerce").fillna(0).astype(int)
    df2["oi_change"] = pd.to_numeric(df2.get("oi_change"), errors="coerce").fillna(0).astype(int)

    rows = [tuple(r) for r in df2.itertuples(index=False, name=None)]
    sql  = (f"INSERT OR IGNORE INTO nse_options ({','.join(df2.columns)}) "
            f"VALUES ({','.join(['?']*len(df2.columns))})")
    conn.executemany(sql, rows)
    conn.commit()
    return len(rows)


def main():
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    days = trading_days(FROM_DATE, TO_DATE)
    print(f"Instruments: {', '.join(sorted(SYMBOLS))}")
    print("=" * 60)
    print(f"Total trading days to process: {len(days)}")
    print()

    session = requests.Session()
    print("Warming up NSE session...")
    warm_session(session)
    print()

    saved_total = 0
    for i, dt in enumerate(days, 1):
        label = f"[{i}/{len(days)}] {dt}"

        if already_fetched(conn, dt):
            print(f"{label} — already in DB")
            continue

        print(f"{label} — downloading...", end=" ", flush=True)
        df = fetch_day(session, dt)

        if df is None or df.empty:
            print("holiday / no data")
            time.sleep(0.5)
            continue

        n = save_day(conn, df)
        saved_total += n
        print(f"saved {n} rows  (total: {saved_total})")
        time.sleep(1.0)

    conn.close()
    print(f"\nDone. {saved_total} new rows saved.")

if __name__ == "__main__":
    main()