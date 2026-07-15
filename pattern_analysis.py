"""
pattern_analysis.py
──────────────────────────────────────────────────────────────────────────────
Unified Options Chain Pattern Analysis Dashboard.
Builds ONE index.html with tabs for NIFTY, BANKNIFTY, SPY, pre-2020 index
options tabs, an aggregate All-Stocks (Pre-2020) view, and the pre-2020
historical price tabs (NIFTY / BANKNIFTY, 2012-2019).

NIFTY/BANKNIFTY  <- nse_options + options_chain (Kite)  in options_data.db
SPY              <- sp500_options                        in sp500_data.db
Pre-2020 history <- pre2020_price_history                in options_data.db
"""

import sys, io, sqlite3, json, webbrowser, os, math
import pandas as pd
import numpy as np
from scipy.stats import norm

if sys.stdout.encoding is None or sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ── Config ────────────────────────────────────────────────────────────────────
BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
INDIA_DB     = os.path.join(BASE_DIR, "options_data.db")
SPY_DATA_DIR = os.path.join(BASE_DIR, "spy_data")   # folder with spy_eod_YYYY.parquet files
OUT_FILE     = os.path.join(BASE_DIR, "index.html")
LOOKBACK     = 120
RISK_FREE    = 0.07   # for India IV calc
RISK_FREE_US = 0.045  # for SPY (unused, SPY IV pre-computed)
PRE2020_LOOKBACK = 500  # pre-2020 history is daily bars, so allow a longer window

# Symbols in nse_options that are NOT individual stocks (indices, global
# products, sector baskets) — excluded from stock-level aggregation.
EXCLUDE_NON_STOCKS = [
    'NIFTY', 'BANKNIFTY', 'MINIFTY', 'NIFTYINFRA', 'NIFTYIT', 'NIFTYMID50',
    'NIFTYPSE', 'NIFTYCPSE', 'NFTYMCAP50', 'CNXINFRA', 'CNXIT', 'CNXPSE',
    'FTSE100', 'S&P500'
]


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 1 — INDIA (NIFTY / BANKNIFTY) DATA LOADING
# ═══════════════════════════════════════════════════════════════════════════

def _load_table(conn, table, instrument, date_start=None, date_end=None):
    """Generic loader for an India options table. Returns standard-schema df."""
    try:
        cols_raw = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except Exception:
        return pd.DataFrame()
    cols = [c[1] for c in cols_raw]
    if not cols:
        return pd.DataFrame()

    def pick(*candidates):
        for c in candidates:
            if c in cols:
                return c
        return None

    sym_col    = pick("instrument", "symbol")
    date_col   = pick("date")
    expiry_col = pick("expiry")
    strike_col = pick("strike")
    otype_col  = pick("type", "option_type")
    price_col  = pick("close", "last")
    ul_col     = pick("underlying_close", "underlying_price")
    oi_col     = pick("oi", "open_interest")
    vol_col    = pick("volume")

    missing = [n for n, c in [
        ("symbol", sym_col), ("date", date_col), ("expiry", expiry_col),
        ("strike", strike_col), ("option_type", otype_col),
        ("price", price_col), ("OI", oi_col)
    ] if c is None]
    if missing:
        return pd.DataFrame()

    where_clauses = [f"{sym_col} = ?", f"{price_col} > 0", f"{otype_col} IN ('CE','PE')"]
    params = [instrument]
    if date_start:
        where_clauses.append(f"{date_col} >= ?")
        params.append(date_start)
    if date_end:
        where_clauses.append(f"{date_col} < ?")
        params.append(date_end)

    query = f"""
        SELECT
            {sym_col}    AS symbol,
            {date_col}   AS date,
            {expiry_col} AS expiry,
            CAST({strike_col} AS REAL) AS strike,
            {otype_col}  AS option_type,
            CAST({price_col} AS REAL) AS price,
            {'CAST(' + ul_col + ' AS REAL)' if ul_col else 'NULL'} AS underlying_price,
            CAST({oi_col} AS REAL)    AS open_interest
            {', CAST(' + vol_col + ' AS REAL) AS volume' if vol_col else ''}
        FROM {table}
        WHERE {' AND '.join(where_clauses)}
    """
    df = pd.read_sql(query, conn, params=params)
    return df


def load_india_data(instrument: str, date_start=None, date_end=None) -> pd.DataFrame:
    if not os.path.exists(INDIA_DB):
        return pd.DataFrame()
    conn = sqlite3.connect(INDIA_DB)

    nse = _load_table(conn, "nse_options", instrument, date_start=date_start, date_end=date_end)
    kite = _load_table(conn, "options_chain", instrument, date_start=date_start, date_end=date_end)

    if not nse.empty and not kite.empty:
        existing_dates = set(nse["date"].unique())
        kite = kite[~kite["date"].isin(existing_dates)]

    conn.close()

    df = pd.concat([nse, kite], ignore_index=True)
    if df.empty:
        return df

    df = df.sort_values(["date", "expiry", "strike"]).reset_index(drop=True)
    df["date"]   = pd.to_datetime(df["date"])
    df["expiry"] = pd.to_datetime(df["expiry"])
    return df


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 1B — PRE-2020 HISTORICAL PRICE DATA LOADING (NIFTY / BANKNIFTY)
# ═══════════════════════════════════════════════════════════════════════════

def load_pre2020_data(symbol: str) -> pd.DataFrame:
    """Loads raw intraday SPOT + FUTURES_1 rows for `symbol` from
    pre2020_price_history and aggregates them into daily EOD bars.

    Returns a df with columns: date, spot_close, futures_close
    (one row per calendar date; either column may be NaN if that
    instrument type has no data for that date).
    """
    if not os.path.exists(INDIA_DB):
        return pd.DataFrame()

    conn = sqlite3.connect(INDIA_DB)
    try:
        cols = [c[1] for c in conn.execute(
            "PRAGMA table_info(pre2020_price_history)").fetchall()]
    except Exception:
        conn.close()
        return pd.DataFrame()

    if not cols:
        conn.close()
        return pd.DataFrame()

    query = """
        SELECT date, time, instrument_type, open, high, low, close, volume
        FROM pre2020_price_history
        WHERE symbol = ? AND instrument_type IN ('SPOT', 'FUTURES_1')
    """
    raw = pd.read_sql(query, conn, params=(symbol,))
    conn.close()

    if raw.empty:
        return raw

    # date column is stored as e.g. 20150101 (int/str); time as "HH:MM".
    # Sort so groupby "first"/"last" pick the correct open/close.
    raw = raw.sort_values(["instrument_type", "date", "time"])

    daily = (
        raw.groupby(["instrument_type", "date"])
           .agg(open=("open", "first"),
                high=("high", "max"),
                low=("low", "min"),
                close=("close", "last"),
                volume=("volume", "sum"))
           .reset_index()
    )
    daily["date"] = pd.to_datetime(daily["date"], format="%Y%m%d", errors="coerce")
    daily = daily.dropna(subset=["date"])

    spot = (daily[daily["instrument_type"] == "SPOT"][["date", "close"]]
            .rename(columns={"close": "spot_close"}))
    fut = (daily[daily["instrument_type"] == "FUTURES_1"][["date", "close"]]
           .rename(columns={"close": "futures_close"}))

    merged = pd.merge(spot, fut, on="date", how="outer").sort_values("date").reset_index(drop=True)
    return merged


def compute_pre2020_metrics(df: pd.DataFrame) -> pd.DataFrame:
    """Adds basis (futures - spot) and 20-day annualized realized vol (%)."""
    df = df.sort_values("date").reset_index(drop=True)
    df["basis"] = df["futures_close"] - df["spot_close"]

    spot = df["spot_close"]
    log_ret = np.log(spot / spot.shift(1))
    df["realized_vol_20d"] = log_ret.rolling(20).std() * np.sqrt(252) * 100
    return df


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 1C — AGGREGATE ALL-STOCKS PRE-2020 VIEW (SQL-side, fast at scale)
# ═══════════════════════════════════════════════════════════════════════════

def load_all_stocks_pcr(date_start, date_end, exclude=EXCLUDE_NON_STOCKS):
    """Computes a single combined OI-weighted PCR curve across every
    individual stock (excludes indices/global products), aggregated in
    SQL rather than Python loops so it stays fast even at tens of millions
    of rows."""
    conn = sqlite3.connect(INDIA_DB)
    placeholders = ','.join('?' for _ in exclude)
    query = f"""
        SELECT date,
               SUM(CASE WHEN option_type='CE' THEN oi ELSE 0 END) as total_ce_oi,
               SUM(CASE WHEN option_type='PE' THEN oi ELSE 0 END) as total_pe_oi
        FROM nse_options
        WHERE date >= ? AND date < ? AND option_type IN ('CE','PE')
              AND symbol NOT IN ({placeholders})
        GROUP BY date
        ORDER BY date;
    """
    params = [date_start, date_end] + exclude
    df = pd.read_sql(query, conn, params=params)
    conn.close()
    if df.empty:
        return df
    df['pcr'] = df.apply(
        lambda r: round(r['total_pe_oi'] / r['total_ce_oi'], 4) if r['total_ce_oi'] > 0 else None,
        axis=1
    )
    return df


def load_stock_coverage(date_end, exclude=EXCLUDE_NON_STOCKS):
    """Per-stock row count and date range, for the coverage table."""
    conn = sqlite3.connect(INDIA_DB)
    placeholders = ','.join('?' for _ in exclude)
    query = f"""
        SELECT symbol, COUNT(*) as row_count, MIN(date) as min_date, MAX(date) as max_date
        FROM nse_options
        WHERE date < ? AND option_type IN ('CE','PE')
              AND symbol NOT IN ({placeholders})
        GROUP BY symbol
        ORDER BY row_count DESC;
    """
    df = pd.read_sql(query, conn, params=[date_end] + exclude)
    conn.close()
    return df


def build_all_stocks_payload(pcr_df: pd.DataFrame, coverage_df: pd.DataFrame) -> dict:
    if pcr_df.empty:
        return {"symbol": "All Stocks (Pre-2020)", "market": "all_stocks", "has_data": False,
                "pcr": [], "coverage": [], "kpi": {}, "date_range": "N/A", "row_count": 0}

    pcr_data = [{"x": str(r["date"])[:10], "y": r["pcr"]}
                for _, r in pcr_df.iterrows() if r["pcr"] is not None]

    coverage_data = [{"symbol": r["symbol"], "rows": int(r["row_count"]),
                       "range": f"{str(r['min_date'])[:10]} to {str(r['max_date'])[:10]}"}
                      for _, r in coverage_df.iterrows()]

    latest_pcr = pcr_data[-1]["y"] if pcr_data else None

    return {
        "symbol": "All Stocks (Pre-2020)", "market": "all_stocks", "has_data": True,
        "pcr": pcr_data, "coverage": coverage_data,
        "kpi": {
            "total_stocks": len(coverage_data),
            "latest_pcr": safe_round(latest_pcr) if latest_pcr is not None else "N/A"
        },
        "date_range": f"{str(pcr_df['date'].min())[:10]} \u2192 {str(pcr_df['date'].max())[:10]}",
        "row_count": int(coverage_df["row_count"].sum()) if not coverage_df.empty else 0
    }


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 2 — SPY DATA LOADING
# ═══════════════════════════════════════════════════════════════════════════

def load_spy_data() -> pd.DataFrame:
    """Reads SPY data directly from yearly Parquet files (no SQLite needed).
    Only loads the most recent 2 years' files since the dashboard only shows
    the last LOOKBACK trading days anyway — keeps memory/time low."""
    if not os.path.isdir(SPY_DATA_DIR):
        return pd.DataFrame()

    files = sorted([f for f in os.listdir(SPY_DATA_DIR)
                    if f.startswith("spy_eod_") and f.endswith(".parquet")])
    if not files:
        return pd.DataFrame()

    # Only need the last 2 years of files to cover LOOKBACK trading days
    files = files[-1:]

    frames = []
    for fname in files:
        fpath = os.path.join(SPY_DATA_DIR, fname)
        raw = pd.read_parquet(fpath)

        raw["[QUOTE_DATE]"]  = pd.to_datetime(raw["[QUOTE_DATE]"], errors="coerce")
        raw["[EXPIRE_DATE]"] = pd.to_datetime(raw["[EXPIRE_DATE]"], errors="coerce")
        raw = raw.dropna(subset=["[QUOTE_DATE]", "[EXPIRE_DATE]"])

        ce = raw[["[QUOTE_DATE]", "[EXPIRE_DATE]", "[STRIKE]",
                  "[UNDERLYING_LAST]", "[C_LAST]", "[C_VOLUME]", "[C_IV]"]].copy()
        ce.columns = ["date", "expiry", "strike", "underlying_price", "price", "volume", "iv"]
        ce["option_type"] = "CE"

        pe = raw[["[QUOTE_DATE]", "[EXPIRE_DATE]", "[STRIKE]",
                  "[UNDERLYING_LAST]", "[P_LAST]", "[P_VOLUME]", "[P_IV]"]].copy()
        pe.columns = ["date", "expiry", "strike", "underlying_price", "price", "volume", "iv"]
        pe["option_type"] = "PE"

        combined = pd.concat([ce, pe], ignore_index=True)
        combined = combined[combined["price"].notna() | combined["volume"].notna()]
        frames.append(combined)

    df = pd.concat(frames, ignore_index=True)
    if df.empty:
        return df

    df["strike"] = df["strike"].astype(float)
    df["symbol"] = "SPY"
    df["open_interest"] = df["volume"]   # SPY has no OI; use volume as proxy where needed
    return df


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 3 — SHARED COMPUTATIONS
# ═══════════════════════════════════════════════════════════════════════════

def compute_pcr(df: pd.DataFrame, by="oi") -> pd.DataFrame:
    """by='oi' for India (OI-based PCR), by='volume' for SPY."""
    col = "open_interest" if by == "oi" else "volume"
    rows = []
    for date, grp in df.groupby("date"):
        ce_val = grp[grp["option_type"] == "CE"][col].sum()
        pe_val = grp[grp["option_type"] == "PE"][col].sum()
        pcr = round(pe_val / ce_val, 4) if ce_val and ce_val > 0 else None
        rows.append({"date": date, "pcr": pcr})
    return pd.DataFrame(rows)


def compute_max_pain(df: pd.DataFrame, weight_col="open_interest") -> pd.DataFrame:
    rows = []
    for (date, expiry), grp in df.groupby(["date", "expiry"]):
        future = grp[grp["expiry"] >= date]["expiry"].unique()
        if len(future) == 0:
            continue
        if expiry not in sorted(future)[:3]:
            continue

        strikes = grp["strike"].unique()
        if len(strikes) < 3:
            continue

        ce = grp[grp["option_type"] == "CE"][["strike", weight_col]].dropna()
        pe = grp[grp["option_type"] == "PE"][["strike", weight_col]].dropna()
        if ce.empty or pe.empty:
            continue

        ce_arr = ce.values
        pe_arr = pe.values

        losses = []
        for S in strikes:
            ce_loss = np.sum(np.maximum(S - ce_arr[:, 0], 0) * ce_arr[:, 1])
            pe_loss = np.sum(np.maximum(pe_arr[:, 0] - S, 0) * pe_arr[:, 1])
            losses.append(ce_loss + pe_loss)

        best_strike = strikes[np.argmin(losses)]
        ul_vals = grp["underlying_price"].dropna()
        ul = float(ul_vals.iloc[0]) if not ul_vals.empty else None

        rows.append({"date": date, "expiry": expiry,
                     "max_pain": best_strike, "underlying": ul})
    return pd.DataFrame(rows)


def bs_price(S, K, T, r, sigma, option_type):
    if T <= 0 or sigma <= 0:
        return max(0, S - K) if option_type == "CE" else max(0, K - S)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if option_type == "CE":
        return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    else:
        return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def implied_vol(price, S, K, T, r, option_type):
    if T <= 0 or price <= 0:
        return None
    sigma = 0.3
    for _ in range(50):
        p = bs_price(S, K, T, r, sigma, option_type)
        vega = S * norm.pdf((math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))) * math.sqrt(T) if sigma > 0 else 0
        if vega < 1e-6:
            break
        diff = p - price
        if abs(diff) < 1e-4:
            return sigma
        sigma -= diff / vega
        if sigma <= 0 or sigma > 5:
            return None
    return sigma if 0 < sigma < 5 else None


def compute_iv_surface_india(df: pd.DataFrame, max_dates=60) -> pd.DataFrame:
    """Computes IV via Black-Scholes for India options (no IV column available)."""
    rows = []
    dates = sorted(df["date"].unique())[-max_dates:]
    for date in dates:
        day = df[df["date"] == date]
        ul_vals = day["underlying_price"].dropna()
        if ul_vals.empty:
            continue
        ul = float(ul_vals.iloc[0])
        if ul <= 0:
            continue

        near_expiry = day["expiry"].min()
        chain = day[day["expiry"] == near_expiry]
        T = max((near_expiry - date).days / 365.0, 1/365)

        for otype in ["CE", "PE"]:
            sub = chain[chain["option_type"] == otype]
            atm = sub.iloc[(sub["strike"] - ul).abs().argsort()[:1]] if not sub.empty else sub
            if atm.empty:
                continue
            row = atm.iloc[0]
            iv = implied_vol(row["price"], ul, row["strike"], T, RISK_FREE, otype)
            if iv:
                rows.append({"date": date, "option_type": otype, "iv": round(iv, 4)})
    return pd.DataFrame(rows)


def compute_iv_surface_spy(df: pd.DataFrame, max_dates=120) -> pd.DataFrame:
    """SPY already has IV pre-computed — just take median per date/type."""
    rows = []
    dates = sorted(df["date"].unique())[-max_dates:]
    for date in dates:
        day = df[df["date"] == date]
        for otype in ["CE", "PE"]:
            grp = day[day["option_type"] == otype]
            iv_vals = grp["iv"].dropna()
            iv_vals = iv_vals[iv_vals > 0]
            if iv_vals.empty:
                continue
            rows.append({"date": date, "option_type": otype,
                        "iv": round(float(iv_vals.median()), 4)})
    return pd.DataFrame(rows)


def compute_oi_buildup(df: pd.DataFrame):
    """Only meaningful for India (has real OI). Returns empty for SPY."""
    if df.empty:
        return pd.DataFrame(), pd.DataFrame(), None

    latest = df["date"].max()
    day = df[df["date"] == latest]
    if day.empty:
        return pd.DataFrame(), pd.DataFrame(), latest

    near_expiry = day["expiry"].min()
    chain = day[day["expiry"] == near_expiry]
    chain = chain.dropna(subset=["underlying_price"])

    if chain.empty:
        # fall back to most recent date+expiry combo with a valid underlying
        valid = df.dropna(subset=["underlying_price"])
        if valid.empty:
            return pd.DataFrame(), pd.DataFrame(), latest
        latest2 = valid["date"].max()
        day2 = df[df["date"] == latest2]
        near_expiry2 = day2["expiry"].min()
        chain = day2[day2["expiry"] == near_expiry2]

    ul_vals = chain["underlying_price"].dropna()
    ul = float(ul_vals.iloc[0]) if not ul_vals.empty else None

    ce = chain[chain["option_type"] == "CE"][["strike", "open_interest"]].dropna()
    pe = chain[chain["option_type"] == "PE"][["strike", "open_interest"]].dropna()

    if ul and ul > 0:
        ce = ce.copy(); pe = pe.copy()
        ce["pct_atm"] = ((ce["strike"] - ul) / ul * 100).round(2)
        pe["pct_atm"] = ((pe["strike"] - ul) / ul * 100).round(2)
    else:
        ce = ce.copy(); pe = pe.copy()
        ce["pct_atm"] = None
        pe["pct_atm"] = None

    # keep strikes within 10% of ATM for readability
    if ul and ul > 0:
        ce = ce[(ce["strike"] >= ul * 0.9) & (ce["strike"] <= ul * 1.1)]
        pe = pe[(pe["strike"] >= ul * 0.9) & (pe["strike"] <= ul * 1.1)]

    return ce, pe, latest


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 4 — PAYLOAD BUILDER (per symbol)
# ═══════════════════════════════════════════════════════════════════════════

def safe_round(v, n=2):
    if v is None:
        return None
    try:
        if isinstance(v, float) and np.isnan(v):
            return None
    except Exception:
        pass
    return round(float(v), n)


def build_symbol_payload(symbol: str, df: pd.DataFrame, market: str, lookback: int = None) -> dict:
    """market = 'india' or 'us' — controls which compute fns are used."""
    lookback = lookback or LOOKBACK

    if df.empty:
        return {
            "symbol": symbol, "market": market, "has_data": False,
            "pcr": [], "max_pain": [], "iv_ce": [], "iv_pe": [],
            "ce_oi": [], "pe_oi": [],
            "kpi": {"underlying": "N/A", "max_pain": "N/A",
                   "pcr": "N/A", "iv": "N/A"},
            "date_range": "N/A", "row_count": 0
        }

    pcr_by = "oi" if market == "india" else "volume"
    weight_col = "open_interest" if market == "india" else "volume"

    pcr_df = compute_pcr(df, by=pcr_by)

    # Only compute Max Pain over the lookback window — the chart discards
    # everything older anyway, so restrict the expensive loop up front.
    _all_dates = sorted(df["date"].unique())
    _recent_dates = set(_all_dates[-lookback:])
    mp_input = df[df["date"].isin(_recent_dates)]
    mp_df  = compute_max_pain(mp_input, weight_col=weight_col)

    if market == "india":
        iv_df = compute_iv_surface_india(df, max_dates=lookback)
        ce_oi, pe_oi, snap_date = compute_oi_buildup(df)
    else:
        iv_df = compute_iv_surface_spy(df, max_dates=lookback)
        ce_oi, pe_oi, snap_date = pd.DataFrame(), pd.DataFrame(), df["date"].max()

    all_dates = sorted(df["date"].unique())
    recent = all_dates[-lookback:]

    pcr_near = pcr_df[pcr_df["date"].isin(recent)]
    pcr_data = [{"x": str(r["date"])[:10], "y": r["pcr"]}
                for _, r in pcr_near.iterrows() if r["pcr"] is not None]

    mp_near = (mp_df.sort_values("expiry").groupby("date").first().reset_index()
              if not mp_df.empty else pd.DataFrame())
    mp_near = mp_near[mp_near["date"].isin(recent)] if not mp_near.empty else mp_near
    mp_data = [{"x": str(r["date"])[:10],
                "mp": safe_round(r["max_pain"], 0),
                "ul": safe_round(r["underlying"], 2)}
               for _, r in mp_near.iterrows()] if not mp_near.empty else []
    mp_data = [d for d in mp_data if d["mp"] is not None and d["ul"] is not None]

    if not iv_df.empty and "option_type" in iv_df.columns:
        iv_ce = [{"x": str(r["date"])[:10], "y": r["iv"]}
                 for _, r in iv_df[iv_df["option_type"] == "CE"].iterrows()]
        iv_pe = [{"x": str(r["date"])[:10], "y": r["iv"]}
                 for _, r in iv_df[iv_df["option_type"] == "PE"].iterrows()]
    else:
        iv_ce, iv_pe = [], []

    ce_oi_data = ([{"strike": r["strike"], "oi": int(r["open_interest"]), "pct": r["pct_atm"]}
                  for _, r in ce_oi.sort_values("strike").iterrows()]
                  if not ce_oi.empty else [])
    pe_oi_data = ([{"strike": r["strike"], "oi": int(r["open_interest"]), "pct": r["pct_atm"]}
                  for _, r in pe_oi.sort_values("strike").iterrows()]
                  if not pe_oi.empty else [])

    # KPIs
    latest_pcr = pcr_near.iloc[-1]["pcr"] if not pcr_near.empty else None

    _ul_series = mp_near["underlying"].dropna() if not mp_near.empty else pd.Series(dtype=float)
    latest_ul = safe_round(_ul_series.iloc[-1]) if not _ul_series.empty else None

    _mp_series = mp_near["max_pain"].dropna() if not mp_near.empty else pd.Series(dtype=float)
    latest_mp = int(_mp_series.iloc[-1]) if not _mp_series.empty else None

    if not iv_df.empty and "option_type" in iv_df.columns:
        ce_iv_rows = iv_df[iv_df["option_type"] == "CE"]["iv"].dropna()
        latest_iv = safe_round(float(ce_iv_rows.iloc[-1]) * 100) if not ce_iv_rows.empty else None
    else:
        latest_iv = None

    date_range = f"{str(all_dates[0])[:10]} \u2192 {str(all_dates[-1])[:10]}"

    return {
        "symbol": symbol, "market": market, "has_data": True,
        "pcr": pcr_data, "max_pain": mp_data, "iv_ce": iv_ce, "iv_pe": iv_pe,
        "ce_oi": ce_oi_data, "pe_oi": pe_oi_data,
        "kpi": {
            "underlying": latest_ul if latest_ul is not None else "N/A",
            "max_pain": latest_mp if latest_mp is not None else "N/A",
            "pcr": safe_round(latest_pcr) if latest_pcr is not None else "N/A",
            "iv": latest_iv if latest_iv is not None else "N/A"
        },
        "date_range": date_range,
        "row_count": len(df)
    }


def build_pre2020_payload(symbol: str, df: pd.DataFrame) -> dict:
    """Builds the payload for a pre-2020 historical price tab.
    Shows spot vs futures price, basis, and 20-day realized volatility
    instead of the options-chain metrics used elsewhere."""

    empty_shape = {
        "symbol": symbol, "market": "pre2020", "has_data": False,
        "spot": [], "futures": [], "basis": [], "realized_vol": [],
        "kpi": {"spot": "N/A", "basis": "N/A", "vol": "N/A"},
        "date_range": "N/A", "row_count": 0
    }

    if df.empty or df["spot_close"].dropna().empty:
        return empty_shape

    df = compute_pre2020_metrics(df)

    all_dates = sorted(df["date"].unique())
    recent_dates = set(all_dates[-PRE2020_LOOKBACK:])
    view = df[df["date"].isin(recent_dates)]

    def series(col):
        sub = view.dropna(subset=[col])
        return [{"x": str(r["date"])[:10], "y": safe_round(r[col])} for _, r in sub.iterrows()]

    spot_data  = series("spot_close")
    fut_data   = series("futures_close")
    basis_data = series("basis")
    vol_data   = series("realized_vol_20d")

    spot_valid = df["spot_close"].dropna()
    basis_valid = df["basis"].dropna()
    vol_valid = df["realized_vol_20d"].dropna()

    latest_spot  = safe_round(spot_valid.iloc[-1]) if not spot_valid.empty else None
    latest_basis = safe_round(basis_valid.iloc[-1]) if not basis_valid.empty else None
    latest_vol   = safe_round(vol_valid.iloc[-1]) if not vol_valid.empty else None

    valid_dates = df["date"].dropna()
    date_range = (f"{str(valid_dates.min())[:10]} \u2192 {str(valid_dates.max())[:10]}"
                  if not valid_dates.empty else "N/A")

    return {
        "symbol": symbol, "market": "pre2020", "has_data": True,
        "spot": spot_data, "futures": fut_data, "basis": basis_data,
        "realized_vol": vol_data,
        "kpi": {
            "spot": latest_spot if latest_spot is not None else "N/A",
            "basis": latest_basis if latest_basis is not None else "N/A",
            "vol": latest_vol if latest_vol is not None else "N/A"
        },
        "date_range": date_range,
        "row_count": len(df)
    }


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 5 — COMBINED HTML BUILDER
# ═══════════════════════════════════════════════════════════════════════════

CURRENCY = {"NIFTY": "\u20b9", "BANKNIFTY": "\u20b9", "SPY": "$"}

def build_combined_html(payloads: list) -> str:
    # default=str handles any stray pandas Timestamp/NaT/numpy objects that
    # slip through (e.g. from sparse pre-2020 data) instead of crashing.
    payload_json = json.dumps(payloads, default=str)

    tabs_html = "".join(
        f'<button class="tab" data-idx="{i}" onclick="selectTab({i})">{p["symbol"]}</button>'
        for i, p in enumerate(payloads)
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Options Pipeline Dashboard</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
<style>
  :root {{
    --bg: #0d1117; --surface: #161b22; --border: #30363d;
    --text: #e6edf3; --muted: #8b949e;
    --green: #3fb950; --red: #f85149; --blue: #58a6ff; --amber: #d29922;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: var(--bg); color: var(--text); font-family: 'Segoe UI', sans-serif; min-height: 100vh; }}

  header {{
    padding: 24px 32px 0;
    border-bottom: 1px solid var(--border);
  }}
  .header-top {{
    display: flex; justify-content: space-between; align-items: flex-end;
    margin-bottom: 18px;
  }}
  header h1 {{ font-size: 1.6rem; font-weight: 700; letter-spacing: -0.5px; }}
  header h1 span {{ color: var(--blue); }}
  header .meta {{ font-size: 0.78rem; color: var(--muted); text-align: right; }}
  header .subtitle {{ font-size: 0.8rem; color: var(--muted); margin-top: 4px; }}

  .tabs {{ display: flex; gap: 4px; flex-wrap: wrap; }}
  .tab {{
    background: transparent; border: none; color: var(--muted);
    padding: 10px 18px; font-size: 0.85rem; font-weight: 600;
    cursor: pointer; border-bottom: 2px solid transparent;
    transition: color 0.15s, border-color 0.15s;
  }}
  .tab:hover {{ color: var(--text); }}
  .tab.active {{ color: var(--blue); border-bottom-color: var(--blue); }}

  .kpis {{
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 16px;
    padding: 24px 32px;
  }}
  .kpis.kpis-3 {{ grid-template-columns: repeat(3, 1fr); }}
  .kpi {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 18px 20px;
  }}
  .kpi .label {{ font-size: 0.72rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 8px; }}
  .kpi .value {{ font-size: 1.6rem; font-weight: 700; }}
  .kpi .value.green {{ color: var(--green); }}
  .kpi .value.red   {{ color: var(--red); }}
  .kpi .value.blue  {{ color: var(--blue); }}
  .kpi .value.amber {{ color: var(--amber); }}

  .charts {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
    padding: 0 32px 32px;
  }}
  .chart-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 20px;
  }}
  .chart-card.full {{ grid-column: 1 / -1; }}
  .chart-card h2 {{
    font-size: 0.85rem;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    margin-bottom: 16px;
  }}
  canvas {{ width: 100% !important; height: 220px !important; }}

  table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
  table th, table td {{ padding: 6px 10px; border-bottom: 1px solid var(--border); }}
  table thead th {{ position: sticky; top: 0; background: var(--surface); }}

  .no-data {{
    text-align: center; padding: 60px 20px; color: var(--muted); font-size: 0.9rem;
  }}

  footer {{
    text-align: center;
    padding: 20px;
    font-size: 0.75rem;
    color: var(--muted);
    border-top: 1px solid var(--border);
  }}
</style>
</head>
<body>

<header>
  <div class="header-top">
    <div>
      <h1>Options <span>Pipeline</span> Dashboard</h1>
      <div class="subtitle">NIFTY &middot; BANKNIFTY &middot; SPY &middot; Pre-2020 History &mdash; Multi-Market Analysis</div>
    </div>
    <div class="meta" id="metaBox"></div>
  </div>
  <div class="tabs">
    {tabs_html}
  </div>
</header>

<div id="dashboard"></div>

<footer>
  Generated by pattern_analysis.py &mdash; NSE Bhavcopy + Kite (India) &middot; SPY EOD Options (Kaggle) &middot; Pre-2020 Intraday Archive
</footer>

<script>
const PAYLOADS = {payload_json};
const CURRENCY = {{
  "NIFTY": "\\u20b9", "BANKNIFTY": "\\u20b9", "SPY": "$",
  "NIFTY (Pre-2020)": "\\u20b9", "BANKNIFTY (Pre-2020)": "\\u20b9",
  "NIFTY Options (Pre-2020)": "\\u20b9", "BANKNIFTY Options (Pre-2020)": "\\u20b9"
}};
let currentIdx = 0;
let charts = [];

function destroyCharts() {{
  charts.forEach(c => c.destroy());
  charts = [];
}}

function fmtCurrency(sym, val) {{
  if (val === "N/A" || val === null || val === undefined) return "N/A";
  const cur = CURRENCY[sym] || "";
  return cur + val;
}}

function selectTab(idx) {{
  currentIdx = idx;
  document.querySelectorAll('.tab').forEach((el, i) => {{
    el.classList.toggle('active', i === idx);
  }});
  renderSymbol(idx);
}}

function renderSymbol(idx) {{
  const p = PAYLOADS[idx];
  destroyCharts();

  document.getElementById('metaBox').innerHTML =
    `Date range: ${{p.date_range}}<br/>${{p.row_count.toLocaleString()}} rows loaded`;

  const dash = document.getElementById('dashboard');

  if (!p.has_data) {{
    dash.innerHTML = `<div class="no-data">No data available for ${{p.symbol}}. Run the relevant fetch script first.</div>`;
    return;
  }}

  if (p.market === 'pre2020') {{
    renderPre2020(p, dash);
    return;
  }}

  if (p.market === 'all_stocks') {{
    renderAllStocks(p, dash);
    return;
  }}

  const pcrClass = (typeof p.kpi.pcr === 'number' && p.kpi.pcr < 1) ? 'green' : 'red';
  const showOi = p.market === 'india' && (p.ce_oi.length > 0 || p.pe_oi.length > 0);

  dash.innerHTML = `
    <div class="kpis">
      <div class="kpi">
        <div class="label">${{p.symbol}} Last Price</div>
        <div class="value blue">${{fmtCurrency(p.symbol, p.kpi.underlying)}}</div>
      </div>
      <div class="kpi">
        <div class="label">Max Pain Strike</div>
        <div class="value amber">${{fmtCurrency(p.symbol, p.kpi.max_pain)}}</div>
      </div>
      <div class="kpi">
        <div class="label">Put/Call Ratio</div>
        <div class="value ${{pcrClass}}">${{p.kpi.pcr}}</div>
      </div>
      <div class="kpi">
        <div class="label">Avg Call IV</div>
        <div class="value amber">${{p.kpi.iv === "N/A" ? "N/A" : p.kpi.iv + "%"}}</div>
      </div>
    </div>

    <div class="charts">
      <div class="chart-card">
        <h2>Put/Call Ratio</h2>
        <canvas id="pcrChart"></canvas>
      </div>
      <div class="chart-card">
        <h2>Max Pain vs ${{p.symbol}} Price</h2>
        <canvas id="mpChart"></canvas>
      </div>
      <div class="chart-card">
        <h2>Implied Volatility &mdash; Call (CE)</h2>
        <canvas id="ivCeChart"></canvas>
      </div>
      <div class="chart-card">
        <h2>Implied Volatility &mdash; Put (PE)</h2>
        <canvas id="ivPeChart"></canvas>
      </div>
      ${{showOi ? `
      <div class="chart-card">
        <h2>OI Buildup &mdash; Calls (CE)</h2>
        <canvas id="ceOiChart"></canvas>
      </div>
      <div class="chart-card">
        <h2>OI Buildup &mdash; Puts (PE)</h2>
        <canvas id="peOiChart"></canvas>
      </div>` : ''}}
    </div>
  `;

  const GRID = {{ color: 'rgba(48,54,61,0.8)' }};
  const TICK = {{ color: '#8b949e', font: {{ size: 10 }} }};
  const baseOpts = {{
    responsive: true,
    maintainAspectRatio: false,
    plugins: {{ legend: {{ display: false }}, tooltip: {{ mode: 'index', intersect: false }} }},
    scales: {{
      x: {{ grid: GRID, ticks: {{ ...TICK, maxTicksLimit: 8, maxRotation: 0 }} }},
      y: {{ grid: GRID, ticks: TICK }}
    }}
  }};

  charts.push(new Chart(document.getElementById('pcrChart'), {{
    type: 'line',
    data: {{
      labels: p.pcr.map(d => d.x),
      datasets: [
        {{ data: p.pcr.map(d => d.y), borderColor: '#58a6ff', borderWidth: 1.5,
           pointRadius: 0, fill: false, tension: 0.3 }},
        {{ data: p.pcr.map(() => 1.0), borderColor: 'rgba(248,81,73,0.4)', borderWidth: 1,
           borderDash: [4,4], pointRadius: 0, fill: false }}
      ]
    }},
    options: baseOpts
  }}));

  charts.push(new Chart(document.getElementById('mpChart'), {{
    type: 'line',
    data: {{
      labels: p.max_pain.map(d => d.x),
      datasets: [
        {{ label: 'Max Pain', data: p.max_pain.map(d => d.mp), borderColor: '#d29922',
           borderWidth: 1.5, pointRadius: 0, fill: false, tension: 0.3 }},
        {{ label: p.symbol + ' Price', data: p.max_pain.map(d => d.ul), borderColor: '#3fb950',
           borderWidth: 1.5, pointRadius: 0, fill: false, tension: 0.3 }}
      ]
    }},
    options: {{
      ...baseOpts,
      plugins: {{ legend: {{ display: true, labels: {{ color: '#8b949e', font: {{ size: 11 }} }} }},
                 tooltip: {{ mode: 'index', intersect: false }} }}
    }}
  }}));

  charts.push(new Chart(document.getElementById('ivCeChart'), {{
    type: 'line',
    data: {{
      labels: p.iv_ce.map(d => d.x),
      datasets: [{{ data: p.iv_ce.map(d => (d.y * 100).toFixed(2)), borderColor: '#3fb950',
                   borderWidth: 1.5, pointRadius: 0, fill: true,
                   backgroundColor: 'rgba(63,185,80,0.08)', tension: 0.3 }}]
    }},
    options: {{
      ...baseOpts,
      scales: {{ x: baseOpts.scales.x, y: {{ grid: GRID, ticks: {{ ...TICK, callback: v => v + '%' }} }} }}
    }}
  }}));

  charts.push(new Chart(document.getElementById('ivPeChart'), {{
    type: 'line',
    data: {{
      labels: p.iv_pe.map(d => d.x),
      datasets: [{{ data: p.iv_pe.map(d => (d.y * 100).toFixed(2)), borderColor: '#f85149',
                   borderWidth: 1.5, pointRadius: 0, fill: true,
                   backgroundColor: 'rgba(248,81,73,0.08)', tension: 0.3 }}]
    }},
    options: {{
      ...baseOpts,
      scales: {{ x: baseOpts.scales.x, y: {{ grid: GRID, ticks: {{ ...TICK, callback: v => v + '%' }} }} }}
    }}
  }}));

  if (showOi) {{
    charts.push(new Chart(document.getElementById('ceOiChart'), {{
      type: 'bar',
      data: {{
        labels: p.ce_oi.map(d => d.strike),
        datasets: [{{ data: p.ce_oi.map(d => d.oi), backgroundColor: 'rgba(63,185,80,0.6)' }}]
      }},
      options: baseOpts
    }}));
    charts.push(new Chart(document.getElementById('peOiChart'), {{
      type: 'bar',
      data: {{
        labels: p.pe_oi.map(d => d.strike),
        datasets: [{{ data: p.pe_oi.map(d => d.oi), backgroundColor: 'rgba(248,81,73,0.6)' }}]
      }},
      options: baseOpts
    }}));
  }}
}}

function renderPre2020(p, dash) {{
  const volClass = (typeof p.kpi.vol === 'number' && p.kpi.vol > 25) ? 'red' : 'green';

  dash.innerHTML = `
    <div class="kpis kpis-3">
      <div class="kpi">
        <div class="label">${{p.symbol}} Spot Close</div>
        <div class="value blue">${{fmtCurrency(p.symbol, p.kpi.spot)}}</div>
      </div>
      <div class="kpi">
        <div class="label">Futures Basis</div>
        <div class="value amber">${{fmtCurrency(p.symbol, p.kpi.basis)}}</div>
      </div>
      <div class="kpi">
        <div class="label">20D Realized Vol</div>
        <div class="value ${{volClass}}">${{p.kpi.vol === "N/A" ? "N/A" : p.kpi.vol + "%"}}</div>
      </div>
    </div>

    <div class="charts">
      <div class="chart-card full">
        <h2>Spot vs Futures Price</h2>
        <canvas id="spotFutChart"></canvas>
      </div>
      <div class="chart-card">
        <h2>Futures Basis (Futures &minus; Spot)</h2>
        <canvas id="basisChart"></canvas>
      </div>
      <div class="chart-card">
        <h2>20-Day Realized Volatility (Annualized)</h2>
        <canvas id="volChart"></canvas>
      </div>
    </div>
  `;

  const GRID = {{ color: 'rgba(48,54,61,0.8)' }};
  const TICK = {{ color: '#8b949e', font: {{ size: 10 }} }};
  const baseOpts = {{
    responsive: true,
    maintainAspectRatio: false,
    plugins: {{ legend: {{ display: false }}, tooltip: {{ mode: 'index', intersect: false }} }},
    scales: {{
      x: {{ grid: GRID, ticks: {{ ...TICK, maxTicksLimit: 8, maxRotation: 0 }} }},
      y: {{ grid: GRID, ticks: TICK }}
    }}
  }};

  // Merge spot/futures onto a shared date axis so the line chart aligns correctly
  const dateSet = Array.from(new Set([...p.spot.map(d => d.x), ...p.futures.map(d => d.x)])).sort();
  const spotMap = Object.fromEntries(p.spot.map(d => [d.x, d.y]));
  const futMap  = Object.fromEntries(p.futures.map(d => [d.x, d.y]));

  charts.push(new Chart(document.getElementById('spotFutChart'), {{
    type: 'line',
    data: {{
      labels: dateSet,
      datasets: [
        {{ label: 'Spot', data: dateSet.map(x => spotMap[x] ?? null), borderColor: '#3fb950',
           borderWidth: 1.5, pointRadius: 0, fill: false, tension: 0.2, spanGaps: true }},
        {{ label: 'Futures', data: dateSet.map(x => futMap[x] ?? null), borderColor: '#58a6ff',
           borderWidth: 1.5, pointRadius: 0, fill: false, tension: 0.2, spanGaps: true }}
      ]
    }},
    options: {{
      ...baseOpts,
      plugins: {{ legend: {{ display: true, labels: {{ color: '#8b949e', font: {{ size: 11 }} }} }},
                 tooltip: {{ mode: 'index', intersect: false }} }}
    }}
  }}));

  charts.push(new Chart(document.getElementById('basisChart'), {{
    type: 'line',
    data: {{
      labels: p.basis.map(d => d.x),
      datasets: [{{ data: p.basis.map(d => d.y), borderColor: '#d29922',
                   borderWidth: 1.5, pointRadius: 0, fill: true,
                   backgroundColor: 'rgba(210,153,34,0.08)', tension: 0.2 }}]
    }},
    options: baseOpts
  }}));

  charts.push(new Chart(document.getElementById('volChart'), {{
    type: 'line',
    data: {{
      labels: p.realized_vol.map(d => d.x),
      datasets: [{{ data: p.realized_vol.map(d => d.y), borderColor: '#f85149',
                   borderWidth: 1.5, pointRadius: 0, fill: true,
                   backgroundColor: 'rgba(248,81,73,0.08)', tension: 0.2 }}]
    }},
    options: {{
      ...baseOpts,
      scales: {{ x: baseOpts.scales.x, y: {{ grid: GRID, ticks: {{ ...TICK, callback: v => v + '%' }} }} }}
    }}
  }}));
}}

function renderAllStocks(p, dash) {{
  const rows = p.coverage.map(c =>
    `<tr><td>${{c.symbol}}</td><td style="text-align:right">${{c.rows.toLocaleString()}}</td><td>${{c.range}}</td></tr>`
  ).join('');

  const pcrClass = (typeof p.kpi.latest_pcr === 'number' && p.kpi.latest_pcr < 1) ? 'green' : 'red';

  dash.innerHTML = `
    <div class="kpis kpis-3">
      <div class="kpi">
        <div class="label">Total Stocks Covered</div>
        <div class="value blue">${{p.kpi.total_stocks}}</div>
      </div>
      <div class="kpi">
        <div class="label">Latest Combined PCR</div>
        <div class="value ${{pcrClass}}">${{p.kpi.latest_pcr}}</div>
      </div>
      <div class="kpi">
        <div class="label">Total Rows</div>
        <div class="value amber">${{p.row_count.toLocaleString()}}</div>
      </div>
    </div>
    <div class="charts">
      <div class="chart-card full">
        <h2>Combined Put/Call Ratio (All Stocks, OI-weighted)</h2>
        <canvas id="allPcrChart"></canvas>
      </div>
      <div class="chart-card full">
        <h2>Stock Coverage</h2>
        <div style="max-height:400px; overflow-y:auto;">
          <table>
            <thead><tr style="color:var(--muted); text-align:left;">
              <th>Symbol</th><th style="text-align:right">Rows</th><th>Date Range</th>
            </tr></thead>
            <tbody>${{rows}}</tbody>
          </table>
        </div>
      </div>
    </div>
  `;

  const GRID = {{ color: 'rgba(48,54,61,0.8)' }};
  const TICK = {{ color: '#8b949e', font: {{ size: 10 }} }};
  charts.push(new Chart(document.getElementById('allPcrChart'), {{
    type: 'line',
    data: {{
      labels: p.pcr.map(d => d.x),
      datasets: [
        {{ data: p.pcr.map(d => d.y), borderColor: '#58a6ff', borderWidth: 1.5,
           pointRadius: 0, fill: false, tension: 0.2 }},
        {{ data: p.pcr.map(() => 1.0), borderColor: 'rgba(248,81,73,0.4)', borderWidth: 1,
           borderDash: [4,4], pointRadius: 0, fill: false }}
      ]
    }},
    options: {{
      responsive: true, maintainAspectRatio: false,
      plugins: {{ legend: {{ display: false }} }},
      scales: {{
        x: {{ grid: GRID, ticks: {{ ...TICK, maxTicksLimit: 10, maxRotation: 0 }} }},
        y: {{ grid: GRID, ticks: TICK }}
      }}
    }}
  }}));
}}

// init
selectTab(0);
</script>
</body>
</html>"""
    return html


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 6 — MAIN
# ═══════════════════════════════════════════════════════════════════════════

def main():
    payloads = []

    print("=" * 60)
    for symbol in ["NIFTY", "BANKNIFTY"]:
        print(f"Processing {symbol}...")
        df = load_india_data(symbol, date_start="2020-01-01")
        if df.empty:
            print(f"  No data found for {symbol}.")
        else:
            print(f"  Loaded {len(df):,} rows | "
                  f"{str(df['date'].min())[:10]} -> {str(df['date'].max())[:10]}")
        payload = build_symbol_payload(symbol, df, market="india")
        payloads.append(payload)
        print(f"  Done with {symbol}.")

    # ── Pre-2020 index options tabs (NIFTY/BANKNIFTY, full 2012-2019 range) ──
    for symbol in ["NIFTY", "BANKNIFTY"]:
        label = f"{symbol} Options (Pre-2020)"
        print(f"Processing {label}...")
        df = load_india_data(symbol, date_end="2020-01-01")
        if df.empty:
            print(f"  No pre-2020 options data found for {symbol}.")
        else:
            print(f"  Loaded {len(df):,} rows | "
                  f"{str(df['date'].min())[:10]} -> {str(df['date'].max())[:10]}")
        payload = build_symbol_payload(label, df, market="india", lookback=2100)
        payloads.append(payload)
        print(f"  Done with {label}.")

    # ── Aggregate All-Stocks (Pre-2020) view: combined PCR + coverage table ──
    print("Building combined All-Stocks (Pre-2020) view...")
    pcr_df = load_all_stocks_pcr("2012-01-01", "2020-01-01")
    coverage_df = load_stock_coverage("2020-01-01")
    all_stocks_payload = build_all_stocks_payload(pcr_df, coverage_df)
    payloads.append(all_stocks_payload)
    print(f"  {len(coverage_df)} stocks, {len(pcr_df)} days of PCR data")

    print(f"Processing SPY...")
    spy_df = load_spy_data()
    if spy_df.empty:
        print("  No data found for SPY.")
    else:
        print(f"  Loaded {len(spy_df):,} rows | "
              f"{str(spy_df['date'].min())[:10]} -> {str(spy_df['date'].max())[:10]}")
    spy_payload = build_symbol_payload("SPY", spy_df, market="us")
    payloads.append(spy_payload)
    print("  Done with SPY.")

    for symbol in ["NIFTY", "BANKNIFTY"]:
        label = f"{symbol} (Pre-2020)"
        print(f"Processing {label}...")
        df = load_pre2020_data(symbol)
        if df.empty:
            print(f"  No pre-2020 data found for {symbol}.")
        else:
            print(f"  Loaded {len(df):,} daily bars | "
                  f"{str(df['date'].min())[:10]} -> {str(df['date'].max())[:10]}")
        payload = build_pre2020_payload(label, df)
        payloads.append(payload)
        print(f"  Done with {label}.")

    print("Building combined HTML...")
    html = build_combined_html(payloads)

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Saved: {OUT_FILE}")
    print("=" * 60)
    print("Done.")
    webbrowser.open(f"file:///{OUT_FILE}")


if __name__ == "__main__":
    main()