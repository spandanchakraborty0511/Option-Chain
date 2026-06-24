"""
auto_fetch.py
─────────────────────────────────────────────────────────────
Kite Connect data fetcher with semi-automated login.
• Opens Kite's login page in a VISIBLE Chrome browser
• Pre-fills your User ID only
• You type your password and TOTP yourself, directly into the page
• Once logged in, the script captures the access token automatically
• Fetches fresh options chain data and stores it in options_data.db
  (safe to re-run — no duplicates)
• Runs immediately every time you launch it — no scheduling, no flags

SETUP (one time):
  pip install selenium webdriver-manager kiteconnect pandas numpy scipy

Set these environment variables (no password needed):
  setx KITE_API_KEY "your_api_key"
  setx KITE_API_SECRET "your_api_secret"
  setx ZERODHA_USER_ID "your_user_id"

Then just run:
  python auto_fetch.py
"""

import os
import re
import sys
import io
import time
import json
import sqlite3
import logging
import datetime
import numpy as np
import pandas as pd

from scipy.optimize import brentq
from scipy.stats import norm
from kiteconnect import KiteConnect

# Force UTF-8 stdout/stderr so Unicode characters (─, ✅, →, etc.) don't
# crash when this script runs as a subprocess on Windows (which defaults
# stdout to cp1252 in that case instead of the console's UTF-8 codepage).
if sys.stdout.encoding is None or sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ─────────────────────────────────────────────────────────────
#  CONFIG — loaded from config.py (kept on your machine only)
# ─────────────────────────────────────────────────────────────
# Create a file named config.py in the same folder as this script,
# containing:
#
#   API_KEY          = "your_api_key"
#   API_SECRET        = "your_api_secret"
#   ZERODHA_USER_ID  = "your_user_id"
#
# Do NOT paste config.py contents into any chat or commit it to git.
# Add "config.py" to your .gitignore if this folder is a git repo.

try:
    from config import API_KEY, API_SECRET, ZERODHA_USER_ID
except ImportError:
    raise RuntimeError(
        "config.py not found. Create config.py in this folder with "
        "API_KEY, API_SECRET, and ZERODHA_USER_ID defined."
    )

INSTRUMENTS  = ["NIFTY", "BANKNIFTY"]
STRIKE_RANGE = 10        # ATM ± 10 strikes
DAYS_BACK    = 60
RISK_FREE    = 0.065
DB_PATH      = "options_data.db"

# Strike steps per instrument
STRIKE_STEPS = {
    "NIFTY":     50,
    "BANKNIFTY": 100,
}

# NSE index tokens for spot price
INDEX_TOKENS = {
    "NIFTY":     256265,
    "BANKNIFTY": 260105,
}

# ─────────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("auto_fetch.log", encoding="utf-8"),
    ]
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
#  LOGIN via Selenium (User ID pre-filled; you type password + TOTP)
# ─────────────────────────────────────────────────────────────

def auto_login():
    """
    Opens the Kite login page in a visible Chrome browser and
    pre-fills your User ID. You then type your password and TOTP
    code directly into the real Kite page yourself. Once Kite
    redirects after successful login, the script captures the
    request_token from the URL and returns a logged-in
    KiteConnect instance.
    """
    log.info("Opening Kite login page...")

    if not API_KEY or not API_SECRET or not ZERODHA_USER_ID:
        raise RuntimeError(
            "Missing credentials. Set KITE_API_KEY, KITE_API_SECRET, "
            "ZERODHA_USER_ID as environment variables."
        )

    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from webdriver_manager.chrome import ChromeDriverManager
    from selenium.webdriver.chrome.service import Service

    options = Options()
    # NOT headless — you need to see the page to type your password + TOTP
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1280,900")
    options.add_argument("--log-level=3")

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=options
    )

    try:
        login_url = "https://kite.zerodha.com/connect/login?api_key=" + API_KEY + "&v=3"
        driver.get(login_url)
        wait = WebDriverWait(driver, 300)  # generous timeout since you're typing manually

        # ── Step 1: pre-fill User ID only ──
        wait.until(EC.presence_of_element_located((By.ID, "userid")))
        driver.find_element(By.ID, "userid").send_keys(ZERODHA_USER_ID)
        log.info("  Pre-filled User ID: " + ZERODHA_USER_ID)
        log.info("  >>> Please enter your PASSWORD and TOTP in the browser window now. <<<")

        # ── Step 2: wait for you to finish login + TOTP yourself ──
        wait.until(lambda d: "request_token" in d.current_url)
        redirect_url = driver.current_url
        log.info("  Login detected. Redirect URL: " + redirect_url)

    finally:
        driver.quit()

    match = re.search(r"request_token=([^&]+)", redirect_url)
    if not match:
        raise RuntimeError("request_token not found in redirect URL: " + redirect_url)

    request_token = match.group(1)
    log.info("  Got request_token: " + request_token[:8] + "...")

    kite = KiteConnect(api_key=API_KEY)
    session = kite.generate_session(request_token, api_secret=API_SECRET)
    kite.set_access_token(session["access_token"])

    token_data = {
        "access_token": session["access_token"],
        "date":         str(datetime.date.today()),
    }
    with open("kite_token.json", "w") as f:
        json.dump(token_data, f)
    log.info("  Access token saved to kite_token.json")

    log.info("Auto-login successful!")
    return kite


def load_kite():
    """
    Returns a logged-in KiteConnect instance.
    Reuses today's saved token if available, otherwise does auto-login
    (which will prompt for TOTP via the local webpage).
    """
    token_file = "kite_token.json"
    if os.path.exists(token_file):
        with open(token_file) as f:
            data = json.load(f)
        if data.get("date") == str(datetime.date.today()):
            log.info("Reusing today's saved access token.")
            kite = KiteConnect(api_key=API_KEY)
            kite.set_access_token(data["access_token"])
            return kite

    return auto_login()


# ─────────────────────────────────────────────────────────────
#  DATABASE
# ─────────────────────────────────────────────────────────────

def init_db(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS options_chain (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        instrument TEXT, expiry TEXT, date TEXT,
        strike REAL, type TEXT,
        open REAL, high REAL, low REAL, close REAL,
        volume INTEGER, oi INTEGER,
        UNIQUE(instrument, expiry, date, strike, type)
    );
    CREATE TABLE IF NOT EXISTS iv_data (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        instrument TEXT, expiry TEXT, date TEXT,
        strike REAL, type TEXT, iv REAL, spot REAL,
        UNIQUE(instrument, expiry, date, strike, type)
    );
    CREATE TABLE IF NOT EXISTS max_pain (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        instrument TEXT, expiry TEXT, date TEXT,
        max_pain_strike REAL,
        UNIQUE(instrument, expiry, date)
    );
    CREATE TABLE IF NOT EXISTS pcr (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        instrument TEXT, expiry TEXT, date TEXT,
        pcr_oi REAL, pcr_volume REAL,
        total_ce_oi INTEGER, total_pe_oi INTEGER,
        total_ce_volume INTEGER, total_pe_volume INTEGER,
        UNIQUE(instrument, expiry, date)
    );
    """)
    conn.commit()
    log.info("Database ready: " + DB_PATH)


# ─────────────────────────────────────────────────────────────
#  BLACK-SCHOLES IV
# ─────────────────────────────────────────────────────────────

def bs_price(S, K, T, r, sigma, opt_type):
    if T <= 0:
        return max(0, S - K) if opt_type == "CE" else max(0, K - S)
    d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    if opt_type == "CE":
        return S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
    return K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def compute_iv(price, S, K, T, r, opt_type):
    if T <= 0 or price <= 0 or S <= 0:
        return None
    try:
        return brentq(lambda s: bs_price(S, K, T, r, s, opt_type) - price,
                      1e-6, 10.0, maxiter=200)
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
#  FETCH + SAVE
# ─────────────────────────────────────────────────────────────

def fetch_and_store(kite, conn):
    to_date   = datetime.date.today() - datetime.timedelta(days=1)
    from_date = to_date - datetime.timedelta(days=DAYS_BACK)
    log.info("Fetching data: " + str(from_date) + " → " + str(to_date))

    instruments_df = pd.DataFrame(kite.instruments("NFO"))

    for inst in INSTRUMENTS:
        log.info("─── " + inst + " ───")

        opts = instruments_df[
            (instruments_df["name"] == inst) &
            (instruments_df["instrument_type"].isin(["CE", "PE"]))
        ].copy()
        opts["expiry"] = pd.to_datetime(opts["expiry"])
        expiry = opts["expiry"].min()
        opts   = opts[opts["expiry"] == expiry]
        log.info("  Expiry: " + str(expiry.date()) + "  |  Contracts: " + str(len(opts)))

        spot_token = INDEX_TOKENS.get(inst)
        spot_hist  = kite.historical_data(spot_token, from_date, to_date, "day")
        spot_df    = pd.DataFrame(spot_hist)
        spot_df["date"] = pd.to_datetime(spot_df["date"]).dt.tz_localize(None)
        spot_series = spot_df.set_index("date")["close"].to_dict()
        latest_spot = spot_df["close"].iloc[-1] if len(spot_df) else 0
        log.info("  Latest spot: " + str(latest_spot))

        step = STRIKE_STEPS.get(inst, 100)
        atm  = round(latest_spot / step) * step
        strike_list = [atm + step * i for i in range(-STRIKE_RANGE, STRIKE_RANGE + 1)]
        subset = opts[opts["strike"].isin(strike_list)]

        records = []
        total   = len(subset)
        for idx, (_, row) in enumerate(subset.iterrows(), 1):
            sym = row["tradingsymbol"]
            try:
                hist = kite.historical_data(
                    int(row["instrument_token"]),
                    from_date, to_date, "day", oi=True
                )
                for h in hist:
                    h["strike"] = row["strike"]
                    h["type"]   = row["instrument_type"]
                records.extend(hist)
                log.info("  [" + str(idx) + "/" + str(total) + "] " + sym)
            except Exception as e:
                log.warning("  Skipped " + sym + ": " + str(e))
            time.sleep(0.35)

        if not records:
            log.warning("  No records fetched for " + inst)
            continue

        df = pd.DataFrame(records)
        df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)

        rows = []
        for _, r in df.iterrows():
            rows.append((
                inst, str(expiry.date()), str(r["date"].date()),
                r["strike"], r["type"],
                r.get("open"), r.get("high"), r.get("low"), r.get("close"),
                int(r.get("volume", 0) or 0),
                int(r.get("oi", 0) or 0),
            ))
        conn.executemany("""
            INSERT OR IGNORE INTO options_chain
            (instrument,expiry,date,strike,type,open,high,low,close,volume,oi)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, rows)
        conn.commit()
        log.info("  Saved " + str(len(rows)) + " rows → options_chain")

        expiry_naive = pd.Timestamp(expiry).tz_localize(None)
        iv_rows = []
        for _, r in df.iterrows():
            T = (expiry_naive - r["date"]).days / 365.0
            S = float(spot_series.get(r["date"], 0))
            iv = compute_iv(r["close"], S, r["strike"], T, RISK_FREE, r["type"])
            iv_rows.append((
                inst, str(expiry.date()), str(r["date"].date()),
                r["strike"], r["type"], iv, S
            ))
        conn.executemany("""
            INSERT OR IGNORE INTO iv_data
            (instrument,expiry,date,strike,type,iv,spot)
            VALUES (?,?,?,?,?,?,?)
        """, iv_rows)
        conn.commit()
        log.info("  Saved " + str(len(iv_rows)) + " rows → iv_data")

        mp_rows = []
        for date_val, grp in df.groupby("date"):
            strikes = sorted(grp["strike"].unique())
            ce_oi = grp[grp["type"] == "CE"].set_index("strike")["oi"].to_dict()
            pe_oi = grp[grp["type"] == "PE"].set_index("strike")["oi"].to_dict()
            best_strike, best_pain = None, float("inf")
            for ep in strikes:
                pain = sum((ep - k) * (v or 0) for k, v in ce_oi.items() if ep > k)
                pain += sum((k - ep) * (v or 0) for k, v in pe_oi.items() if ep < k)
                if pain < best_pain:
                    best_pain, best_strike = pain, ep
            mp_rows.append((inst, str(expiry.date()), str(date_val.date()), best_strike))
        conn.executemany("""
            INSERT OR IGNORE INTO max_pain (instrument,expiry,date,max_pain_strike)
            VALUES (?,?,?,?)
        """, mp_rows)
        conn.commit()
        log.info("  Saved " + str(len(mp_rows)) + " rows → max_pain")

        pcr_rows = []
        for date_val, grp in df.groupby("date"):
            ce = grp[grp["type"] == "CE"]
            pe = grp[grp["type"] == "PE"]
            ce_oi_t  = int(ce["oi"].sum())
            pe_oi_t  = int(pe["oi"].sum())
            ce_vol_t = int(ce["volume"].sum())
            pe_vol_t = int(pe["volume"].sum())
            pcr_oi  = round(pe_oi_t  / ce_oi_t,  4) if ce_oi_t  > 0 else None
            pcr_vol = round(pe_vol_t / ce_vol_t, 4) if ce_vol_t > 0 else None
            pcr_rows.append((
                inst, str(expiry.date()), str(date_val.date()),
                pcr_oi, pcr_vol, ce_oi_t, pe_oi_t, ce_vol_t, pe_vol_t
            ))
        conn.executemany("""
            INSERT OR IGNORE INTO pcr
            (instrument,expiry,date,pcr_oi,pcr_volume,
             total_ce_oi,total_pe_oi,total_ce_volume,total_pe_volume)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, pcr_rows)
        conn.commit()
        log.info("  Saved " + str(len(pcr_rows)) + " rows → pcr")

        log.info("  ✅  " + inst + " done!")


# ─────────────────────────────────────────────────────────────
#  SCHEDULED JOB
# ─────────────────────────────────────────────────────────────

def daily_job():
    now = datetime.datetime.now()
    if now.weekday() >= 5:
        log.info("Weekend — skipping fetch.")
        return

    log.info("=" * 55)
    log.info("Fetch starting at " + str(now.strftime("%Y-%m-%d %H:%M:%S")))
    log.info("=" * 55)

    try:
        kite = load_kite()
        conn = sqlite3.connect(DB_PATH)
        init_db(conn)
        fetch_and_store(kite, conn)
        conn.close()
        log.info("Fetch complete.")
    except Exception as e:
        log.error("Fetch FAILED: " + str(e), exc_info=True)


# ─────────────────────────────────────────────────────────────
#  MAIN — always runs the fetch immediately, then exits
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    conn.close()

    daily_job()