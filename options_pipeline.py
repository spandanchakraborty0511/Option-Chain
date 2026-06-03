"""
Options Chain Data Pipeline — Kite Connect → SQLite
=====================================================
Fetches historical options data for multiple instruments,
computes IV, Max Pain, PCR, and stores everything in a
local SQLite database for pattern study.

Database file: options_data.db (created in same folder)

Tables:
  - options_chain   : Daily OHLCV + OI per strike
  - iv_data         : Computed Implied Volatility per strike per day
  - max_pain        : Max Pain strike per instrument per day
  - pcr             : Put-Call Ratio per instrument per day

Setup:
    pip install kiteconnect pandas numpy scipy

Usage:
    python options_pipeline.py
    Run daily (or as needed) to keep the DB up to date.
"""

import time
import sqlite3
import numpy as np
import pandas as pd
from scipy.optimize import brentq
from scipy.stats import norm
from datetime import date, timedelta
from kiteconnect import KiteConnect

# ──────────────────────────────────────────────
# CONFIG — fill these in
# ──────────────────────────────────────────────
API_KEY      = "ujqgohskrn96s6n3"
API_SECRET   = "4chfbivdma7z6n59zyuxgzofu9tvq9zb"
ACCESS_TOKEN = "evfbR8JJ5syW15Mj4bFInS8x3Ta2LAfO"   # paste today's access token here to skip browser login
                    # leave empty "" to do the full browser login

INSTRUMENTS  = ["NIFTY", "BANKNIFTY"]   # add more e.g. "RELIANCE", "HDFCBANK"
STRIKE_COUNT = 10     # strikes each side of ATM

# Strike step per instrument (NIFTY=100, BANKNIFTY=100, stocks vary)
STRIKE_STEPS = {
    "NIFTY":     100,
    "BANKNIFTY": 100,
}
STRIKE_STEP  = 100    # default fallback

DAYS_BACK    = 60     # how many calendar days to fetch (max ~60 on Kite)
RISK_FREE    = 0.065  # RBI repo rate approx

DB_FILE      = "options_data.db"

INDEX_TOKENS = {
    "NIFTY":     256265,
    "BANKNIFTY": 260105,
}
FALLBACK_SPOTS = {
    "NIFTY":     24500,
    "BANKNIFTY": 52000,
}

# ──────────────────────────────────────────────
# AUTH
# ──────────────────────────────────────────────
def login(api_key, api_secret):
    kite = KiteConnect(api_key=api_key)

    # If today's access token is already saved, skip browser login
    if ACCESS_TOKEN:
        kite.set_access_token(ACCESS_TOKEN)
        print("✅  Using saved access token. Skipping browser login.")
        return kite

    # Otherwise do the full browser login
    print("\n🔗  Open this URL in your browser:")
    print(kite.login_url())
    token = input("\n📋  Paste request_token: ").strip()
    session = kite.generate_session(token, api_secret=api_secret)
    kite.set_access_token(session["access_token"])
    print(f"\n💡  Tip: Save this access token for reuse today:")
    print(f"    {session['access_token']}\n")
    print("✅  Login successful!\n")
    return kite


# ──────────────────────────────────────────────
# DATABASE SETUP
# ──────────────────────────────────────────────
def init_db(conn):
    """Create tables if they don't exist."""
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS options_chain (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            instrument  TEXT    NOT NULL,
            expiry      DATE    NOT NULL,
            date        DATE    NOT NULL,
            strike      REAL    NOT NULL,
            type        TEXT    NOT NULL,
            open        REAL,
            high        REAL,
            low         REAL,
            close       REAL,
            volume      INTEGER,
            oi          INTEGER,
            UNIQUE(instrument, expiry, date, strike, type)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS iv_data (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            instrument  TEXT    NOT NULL,
            expiry      DATE    NOT NULL,
            date        DATE    NOT NULL,
            strike      REAL    NOT NULL,
            type        TEXT    NOT NULL,
            iv          REAL,
            spot        REAL,
            UNIQUE(instrument, expiry, date, strike, type)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS max_pain (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            instrument      TEXT    NOT NULL,
            expiry          DATE    NOT NULL,
            date            DATE    NOT NULL,
            max_pain_strike REAL,
            UNIQUE(instrument, expiry, date)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS pcr (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            instrument      TEXT    NOT NULL,
            expiry          DATE    NOT NULL,
            date            DATE    NOT NULL,
            pcr_oi          REAL,
            pcr_volume      REAL,
            total_ce_oi     INTEGER,
            total_pe_oi     INTEGER,
            total_ce_volume INTEGER,
            total_pe_volume INTEGER,
            UNIQUE(instrument, expiry, date)
        )
    """)

    conn.commit()
    print("✅  Database initialized.\n")


# ──────────────────────────────────────────────
# FETCH HELPERS
# ──────────────────────────────────────────────
def get_options_chain(kite, name):
    """Return nearest upcoming expiry options chain for given instrument."""
    instruments = pd.DataFrame(kite.instruments("NFO"))
    opts = instruments[
        (instruments["name"] == name) &
        (instruments["instrument_type"].isin(["CE", "PE"]))
    ].copy()
    opts["expiry"] = pd.to_datetime(opts["expiry"])

    # Pick nearest expiry that is today or in the future
    nearest = opts[opts["expiry"] >= pd.Timestamp(date.today())]["expiry"].min()
    chain   = opts[opts["expiry"] == nearest].copy()
    print(f"  {name}: expiry {nearest.date()}, {len(chain)} contracts found")
    return chain, nearest


def fetch_spot_history(kite, token, from_date, to_date):
    """Fetch daily close prices for the underlying index."""
    data = kite.historical_data(token, from_date, to_date, "day")
    df   = pd.DataFrame(data)
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    return df.set_index("date")["close"]


def fetch_option_history(kite, chain, spot, from_date, to_date, strike_step=100):
    """Fetch daily OHLCV + OI for ATM ± STRIKE_COUNT strikes."""
    atm     = round(spot / strike_step) * strike_step
    strikes = [atm + i * strike_step for i in range(-STRIKE_COUNT, STRIKE_COUNT + 1)]
    subset  = chain[chain["strike"].isin(strikes)]
    records = []
    total   = len(subset)

    if total == 0:
        print("  ⚠  No matching strikes found. Check STRIKE_STEP config.")
        return pd.DataFrame()

    for idx, (_, row) in enumerate(subset.iterrows(), 1):
        print(f"    [{idx}/{total}] {row['tradingsymbol']}...        ", end="\r")
        try:
            hist = kite.historical_data(
                instrument_token=int(row["instrument_token"]),
                from_date=from_date,
                to_date=to_date,
                interval="day"
            )
            for h in hist:
                h["strike"] = row["strike"]
                h["type"]   = row["instrument_type"]
                records.append(h)
        except Exception as e:
            print(f"\n    ⚠  Skipped {row['tradingsymbol']}: {e}")
        time.sleep(0.35)   # stay within Kite rate limit

    print()
    df = pd.DataFrame(records)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    return df


# ──────────────────────────────────────────────
# COMPUTATIONS
# ──────────────────────────────────────────────
def bs_price(S, K, T, r, sigma, opt_type):
    """Black-Scholes option price."""
    if T <= 0:
        return max(0.0, S - K) if opt_type == "CE" else max(0.0, K - S)
    d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    if opt_type == "CE":
        return S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
    return K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def compute_iv(market_price, S, K, T, r, opt_type):
    """Compute implied volatility using Brent's method."""
    if T <= 0 or market_price <= 0 or S <= 0:
        return None
    try:
        return brentq(
            lambda s: bs_price(S, K, T, r, s, opt_type) - market_price,
            1e-6, 10.0, maxiter=300
        )
    except Exception:
        return None


def compute_max_pain(df_day):
    # handle both 'oi' and 'open_interest' column names
    oi_col = "oi" if "oi" in df_day.columns else "open_interest"
    if oi_col not in df_day.columns:
        return None

    strikes = sorted(df_day["strike"].unique())
    ce = df_day[df_day["type"] == "CE"].set_index("strike")[oi_col].to_dict()
    pe = df_day[df_day["type"] == "PE"].set_index("strike")[oi_col].to_dict()

    min_payout      = float("inf")
    max_pain_strike = None

    for expiry_price in strikes:
        total_payout = 0
        for k, oi in ce.items():
            if expiry_price > k:
                total_payout += (expiry_price - k) * (oi or 0)
        for k, oi in pe.items():
            if expiry_price < k:
                total_payout += (k - expiry_price) * (oi or 0)
        if total_payout < min_payout:
            min_payout      = total_payout
            max_pain_strike = expiry_price

    return max_pain_strike

def compute_pcr(df_day):
    oi_col  = "oi" if "oi" in df_day.columns else "open_interest"
    vol_col = "volume" if "volume" in df_day.columns else "vol"

    ce = df_day[df_day["type"] == "CE"]
    pe = df_day[df_day["type"] == "PE"]
    ce_oi  = int(ce[oi_col].sum())  if oi_col  in ce.columns else 0
    pe_oi  = int(pe[oi_col].sum())  if oi_col  in pe.columns else 0
    ce_vol = int(ce[vol_col].sum()) if vol_col in ce.columns else 0
    pe_vol = int(pe[vol_col].sum()) if vol_col in pe.columns else 0
    pcr_oi  = round(pe_oi  / ce_oi,  4) if ce_oi  > 0 else None
    pcr_vol = round(pe_vol / ce_vol, 4) if ce_vol > 0 else None
    return pcr_oi, pcr_vol, ce_oi, pe_oi, ce_vol, pe_vol


# ──────────────────────────────────────────────
# DATABASE WRITERS
# ──────────────────────────────────────────────
def save_options_chain(conn, df, instrument, expiry):
    rows = []
    for _, r in df.iterrows():
        rows.append((
            instrument, str(expiry.date()), str(r["date"].date()),
            r["strike"], r["type"],
            r.get("open"), r.get("high"), r.get("low"), r.get("close"),
            r.get("volume"), r.get("oi")
        ))
    conn.executemany("""
        INSERT OR IGNORE INTO options_chain
        (instrument, expiry, date, strike, type, open, high, low, close, volume, oi)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, rows)
    conn.commit()
    print(f"  💾  Saved {len(rows)} rows → options_chain")


def save_iv_data(conn, df, instrument, expiry, spot_series):
    expiry_naive = pd.Timestamp(expiry).tz_localize(None)
    rows = []
    for _, r in df.iterrows():
        row_date = pd.Timestamp(r["date"]).tz_localize(None)
        T  = (expiry_naive - row_date).days / 365.0
        S  = float(spot_series.get(row_date, 0))
        iv = compute_iv(r["close"], S, r["strike"], T, RISK_FREE, r["type"])
        rows.append((
            instrument, str(expiry_naive.date()), str(row_date.date()),
            r["strike"], r["type"], iv, S
        ))
    conn.executemany("""
        INSERT OR IGNORE INTO iv_data
        (instrument, expiry, date, strike, type, iv, spot)
        VALUES (?,?,?,?,?,?,?)
    """, rows)
    conn.commit()
    print(f"  💾  Saved {len(rows)} rows → iv_data")


def save_max_pain(conn, df, instrument, expiry):
    rows = []
    for day, grp in df.groupby("date"):
        mp = compute_max_pain(grp)
        rows.append((instrument, str(expiry.date()), str(day.date()), mp))
    conn.executemany("""
        INSERT OR IGNORE INTO max_pain
        (instrument, expiry, date, max_pain_strike)
        VALUES (?,?,?,?)
    """, rows)
    conn.commit()
    print(f"  💾  Saved {len(rows)} rows → max_pain")


def save_pcr(conn, df, instrument, expiry):
    rows = []
    for day, grp in df.groupby("date"):
        pcr_oi, pcr_vol, ce_oi, pe_oi, ce_vol, pe_vol = compute_pcr(grp)
        rows.append((
            instrument, str(expiry.date()), str(day.date()),
            pcr_oi, pcr_vol, ce_oi, pe_oi, ce_vol, pe_vol
        ))
    conn.executemany("""
        INSERT OR IGNORE INTO pcr
        (instrument, expiry, date, pcr_oi, pcr_volume,
         total_ce_oi, total_pe_oi, total_ce_volume, total_pe_volume)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, rows)
    conn.commit()
    print(f"  💾  Saved {len(rows)} rows → pcr")


# ──────────────────────────────────────────────
# QUERY HELPERS (pattern study preview)
# ──────────────────────────────────────────────
def query_examples(conn):
    print("\n" + "─"*55)
    print("  📊  SAMPLE PATTERN DATA FROM DATABASE")
    print("─"*55)

    print("\n[1] PCR trend for NIFTY (last 20 days):")
    df = pd.read_sql("""
        SELECT date, pcr_oi, pcr_volume, total_ce_oi, total_pe_oi
        FROM pcr WHERE instrument = 'NIFTY'
        ORDER BY date DESC LIMIT 20
    """, conn)
    print(df.to_string(index=False) if not df.empty else "  No data yet.")

    print("\n[2] Max Pain trend for NIFTY (last 20 days):")
    df = pd.read_sql("""
        SELECT date, max_pain_strike
        FROM max_pain WHERE instrument = 'NIFTY'
        ORDER BY date DESC LIMIT 20
    """, conn)
    print(df.to_string(index=False) if not df.empty else "  No data yet.")

    print("\n[3] OI buildup at ATM strike for NIFTY:")
    df = pd.read_sql("""
        SELECT date, strike, type, oi, volume, close
        FROM options_chain
        WHERE instrument = 'NIFTY'
          AND strike = (
              SELECT strike FROM options_chain
              WHERE instrument = 'NIFTY'
              GROUP BY strike ORDER BY SUM(oi) DESC LIMIT 1
          )
        ORDER BY date, type
    """, conn)
    print(df.to_string(index=False) if not df.empty else "  No data yet.")

    print("\n[4] IV smile on latest available date for NIFTY:")
    df = pd.read_sql("""
        SELECT date, strike, type, ROUND(iv*100, 2) as iv_pct
        FROM iv_data
        WHERE instrument = 'NIFTY'
          AND date = (SELECT MAX(date) FROM iv_data WHERE instrument='NIFTY')
        ORDER BY strike, type
    """, conn)
    print(df.to_string(index=False) if not df.empty else "  No data yet.")


# ──────────────────────────────────────────────
# MAIN PIPELINE
# ──────────────────────────────────────────────
def main():
    kite = login(API_KEY, API_SECRET)

    to_date   = date.today() - timedelta(days=1)
    from_date = to_date - timedelta(days=DAYS_BACK)
    print(f"📅  Fetching data from {from_date} to {to_date}\n")

    conn = sqlite3.connect(DB_FILE)
    init_db(conn)

    for name in INSTRUMENTS:
        print(f"{'─'*55}")
        print(f"  Processing: {name}")
        print(f"{'─'*55}")

        # 1. Get chain + expiry
        chain, expiry = get_options_chain(kite, name)

        # 2. Fetch spot history
        print(f"  Fetching spot history...")
        try:
            spot_series = fetch_spot_history(
                kite, INDEX_TOKENS[name], from_date, to_date
            )
            spot = float(spot_series.iloc[-1])
        except Exception as e:
            print(f"  ⚠  Spot fetch failed: {e}. Using fallback.")
            spot = FALLBACK_SPOTS.get(name, 24500)
            spot_series = pd.Series({pd.Timestamp(to_date): spot})

        print(f"  Latest spot: ₹{spot:,.2f}")

        # 3. Fetch options OHLCV
        step = STRIKE_STEPS.get(name, STRIKE_STEP)
        print(f"  Fetching options data (strike step={step})...")
        df = fetch_option_history(kite, chain, spot, from_date, to_date, strike_step=step)

        if df.empty:
            print(f"  ❌  No data returned for {name}. Skipping.")
            continue

        print(f"  ✅  Fetched {len(df)} rows of raw data")

        # 4. Save raw chain
        save_options_chain(conn, df, name, expiry)

        # 5. Compute + save IV
        print(f"  Computing Implied Volatility...")
        save_iv_data(conn, df, name, expiry, spot_series)

        # 6. Compute + save Max Pain
        print(f"  Computing Max Pain...")
        save_max_pain(conn, df, name, expiry)

        # 7. Compute + save PCR
        print(f"  Computing Put-Call Ratio...")
        save_pcr(conn, df, name, expiry)

        print(f"  🎯  {name} complete!\n")

    # Preview pattern data
    query_examples(conn)

    conn.close()
    print(f"\n{'─'*55}")
    print(f"🎉  All done!")
    print(f"    Database saved → {DB_FILE}")
    print(f"    Open in VSCode with the 'SQLite Viewer' extension")
    print(f"{'─'*55}\n")


if __name__ == "__main__":
    main()