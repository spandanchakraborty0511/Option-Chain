"""
Options Chain Pattern Analysis Dashboard
Reads from nse_options table only — computes PCR, Max Pain, and IV on the fly.
Python 3.11 compatible.
"""

import sys
import io

# Force UTF-8 stdout/stderr so Unicode characters (₹, →, etc.) don't crash
# when this script runs as a subprocess on Windows (which defaults stdout
# to cp1252 in that case instead of the console's UTF-8 codepage).
if sys.stdout.encoding is None or sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import sqlite3
import json
import webbrowser
import os
import pandas as pd
import numpy as np
from scipy.stats import norm
import math

DB_PATH = r"D:\iisc\project\options_data.db"
OUTPUT_HTML = "index.html"

# ─────────────────────────────────────────────
# BLACK-SCHOLES IV (Newton-Raphson)
# ─────────────────────────────────────────────

def bs_price(S, K, T, r, sigma, opt):
    if T <= 0 or sigma <= 0:
        return 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if opt == "CE":
        return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    else:
        return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)

def calc_iv(market_price, S, K, T, r=0.065, opt="CE"):
    if T <= 0 or market_price <= 0 or S <= 0 or K <= 0:
        return np.nan
    intrinsic = max(S - K, 0) if opt == "CE" else max(K - S, 0)
    if market_price < intrinsic * 0.99:
        return np.nan
    sigma = 0.3
    for _ in range(100):
        price = bs_price(S, K, T, r, sigma, opt)
        d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
        vega = S * norm.pdf(d1) * math.sqrt(T)
        if vega < 1e-10:
            break
        diff = price - market_price
        sigma -= diff / vega
        if sigma <= 0:
            sigma = 0.001
        if abs(diff) < 1e-6:
            break
    return round(sigma * 100, 2) if 0 < sigma < 5 else np.nan

# ─────────────────────────────────────────────
# DATA LOADING
# ─────────────────────────────────────────────

def _load_table(conn, table, symbol, sym_candidates, otype_candidates,
                 price_candidates, ul_candidates, oi_candidates, vol_candidates):
    """Generic loader for a single table, auto-detecting its column names."""
    cols_raw = conn.execute(f"PRAGMA table_info({table})").fetchall()
    col_names = [c[1] for c in cols_raw]
    if not col_names:
        return pd.DataFrame()

    def pick(candidates):
        for c in candidates:
            if c in col_names:
                return c
        return None

    sym_col    = pick(sym_candidates)
    date_col   = pick(["date", "TradDt", "trade_date"])
    expiry_col = pick(["expiry", "XpryDt", "expiry_date"])
    strike_col = pick(["strike", "StrkPric", "strike_price"])

    otype_col = None
    for candidate in otype_candidates:
        if candidate in col_names:
            result = conn.execute(
                f"SELECT {candidate} FROM {table} WHERE {candidate} IS NOT NULL LIMIT 1"
            ).fetchone()
            if result:
                otype_col = candidate
                break

    price_col = pick(price_candidates)
    ul_col     = pick(ul_candidates)
    oi_col     = pick(oi_candidates)
    vol_col    = pick(vol_candidates)

    missing = [n for n, c in [
        ("symbol", sym_col), ("date", date_col), ("expiry", expiry_col),
        ("strike", strike_col), ("option_type", otype_col),
        ("price", price_col), ("OI", oi_col)
    ] if c is None]
    if missing:
        return pd.DataFrame()  # table doesn't have what we need, skip it

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
        WHERE {sym_col} = ?
          AND {otype_col} IN ('CE','PE')
          AND CAST({price_col} AS REAL) > 0
        ORDER BY {date_col}, {expiry_col}, {strike_col}
    """
    df = pd.read_sql_query(query, conn, params=(symbol,))
    if df.empty:
        return df

    df["date"]        = pd.to_datetime(df["date"]).dt.normalize()
    df["expiry"]      = pd.to_datetime(df["expiry"]).dt.normalize()
    df["option_type"] = df["option_type"].str.upper().str.strip()
    df["open_interest"] = df["open_interest"].fillna(0)
    if "volume" not in df.columns:
        df["volume"] = 0
    df["source"] = table
    return df


def load_data(symbol: str):
    """
    Merge nse_options (primary, Jan 2025 -> present) with options_chain (Kite,
    used only to fill in dates NOT already covered by nse_options).
    """
    conn = sqlite3.connect(DB_PATH)

    nse_df = _load_table(
        conn, "nse_options", symbol,
        sym_candidates=["symbol", "instrument", "TckrSymb"],
        otype_candidates=["option_type", "type", "OptnTp"],
        price_candidates=["close", "ClsPric", "settle_price", "SttlmPric", "last", "LastPric"],
        ul_candidates=["underlying_close", "underlying_price", "UndrlygPric", "ul_price"],
        oi_candidates=["oi", "open_interest", "OpnIntrst"],
        vol_candidates=["volume", "TtlTradgVol", "traded_volume"],
    )

    kite_df = _load_table(
        conn, "options_chain", symbol,
        sym_candidates=["instrument", "symbol", "TckrSymb"],
        otype_candidates=["type", "option_type", "OptnTp"],
        price_candidates=["close", "ClsPric", "settle_price", "last"],
        ul_candidates=["underlying_close", "underlying_price", "ul_price"],
        oi_candidates=["oi", "open_interest", "OpnIntrst"],
        vol_candidates=["volume", "TtlTradgVol", "traded_volume"],
    )

    conn.close()

    if nse_df.empty and kite_df.empty:
        raise ValueError(f"No data found for symbol '{symbol}' in either nse_options or options_chain.")

    if not kite_df.empty and not nse_df.empty:
        # Only keep Kite rows for dates NOT already covered by NSE data
        nse_dates = set(nse_df["date"].unique())
        kite_df = kite_df[~kite_df["date"].isin(nse_dates)]

    df = pd.concat([nse_df, kite_df], ignore_index=True)
    df = df.sort_values(["date", "expiry", "strike"]).reset_index(drop=True)

    if df.empty:
        raise ValueError(f"No usable data found for symbol '{symbol}' after merging sources.")

    return df

# ─────────────────────────────────────────────
# METRICS
# ─────────────────────────────────────────────

def compute_pcr(df):
    """Daily Put-Call Ratio by OI."""
    rows = []
    for date, grp in df.groupby("date"):
        ce = grp[grp["option_type"] == "CE"]["open_interest"].sum()
        pe = grp[grp["option_type"] == "PE"]["open_interest"].sum()
        pcr = round(pe / ce, 4) if ce > 0 else np.nan
        rows.append({"date": date, "pcr": pcr, "ce_oi": ce, "pe_oi": pe})
    return pd.DataFrame(rows).dropna()


def compute_max_pain(df):
    """
    Vectorized Max Pain — strike where total option buyer loss is minimized.
    Only processes nearest 3 expiries per date for speed.
    """
    rows = []

    # Pre-split CE and PE
    ce_all = df[df["option_type"] == "CE"][["date", "expiry", "strike", "open_interest", "underlying_price"]]
    pe_all = df[df["option_type"] == "PE"][["date", "expiry", "strike", "open_interest"]]

    for (date, expiry), ce_grp in ce_all.groupby(["date", "expiry"]):
        # Only process nearest 3 expiries per date
        future = df[(df["date"] == date) & (df["expiry"] >= date)]["expiry"].unique()
        if len(future) == 0:
            continue
        if expiry not in sorted(future)[:3]:
            continue

        pe_grp = pe_all[(pe_all["date"] == date) & (pe_all["expiry"] == expiry)]

        strikes = np.union1d(ce_grp["strike"].values, pe_grp["strike"].values)
        if len(strikes) < 3:
            continue

        ce_s = ce_grp["strike"].values
        ce_o = ce_grp["open_interest"].values
        pe_s = pe_grp["strike"].values
        pe_o = pe_grp["open_interest"].values

        # Vectorized loss computation across all candidate strikes
        # Shape: (n_strikes, n_options)
        ce_loss = np.sum(np.maximum(strikes[:, None] - ce_s[None, :], 0) * ce_o[None, :], axis=1)
        pe_loss = np.sum(np.maximum(pe_s[None, :] - strikes[:, None], 0) * pe_o[None, :], axis=1)
        total_loss = ce_loss + pe_loss
        best_strike = strikes[np.argmin(total_loss)]

        ul_vals = ce_grp["underlying_price"].dropna()
        ul = ul_vals.iloc[0] if not ul_vals.empty else None

        rows.append({"date": date, "expiry": expiry,
                     "max_pain": best_strike, "underlying": ul})

    return pd.DataFrame(rows)


def compute_iv_surface(df, sample_dates=None, max_dates=60):
    """Compute daily ATM IV for CE and PE (nearest-expiry only)."""
    rows = []
    dates = df["date"].unique()
    if sample_dates:
        dates = sample_dates
    if len(dates) > max_dates:
        step = len(dates) // max_dates
        dates = dates[::step]

    for date in dates:
        day = df[df["date"] == date]
        future_expiries = day[day["expiry"] >= date]["expiry"].unique()
        if len(future_expiries) == 0:
            continue
        expiry = sorted(future_expiries)[0]
        chain = day[day["expiry"] == expiry].dropna(subset=["underlying_price"])
        if chain.empty:
            continue
        ul = chain["underlying_price"].iloc[0]
        if not ul or ul <= 0:
            continue
        T = (pd.Timestamp(expiry) - pd.Timestamp(date)).days / 365.0
        if T <= 0:
            continue
        atm_strike = chain.iloc[(chain["strike"] - ul).abs().argsort()].iloc[0]["strike"]
        for otype in ["CE", "PE"]:
            row = chain[(chain["option_type"] == otype) & (chain["strike"] == atm_strike)]
            if row.empty:
                continue
            iv = calc_iv(row["price"].iloc[0], ul, atm_strike, T, opt=otype)
            rows.append({"date": date, "option_type": otype, "iv": iv,
                         "underlying": ul, "atm_strike": atm_strike})
    return pd.DataFrame(rows).dropna()


def compute_oi_buildup(df, top_n=10):
    """Latest snapshot: top strikes by OI for CE and PE."""
    latest = df["date"].max()
    day = df[df["date"] == latest]
    future = day[day["expiry"] >= latest]["expiry"].unique()
    if len(future) == 0:
        return pd.DataFrame(), pd.DataFrame(), latest
    expiry = sorted(future)[0]
    chain = day[day["expiry"] == expiry]
    if chain.empty:
        return pd.DataFrame(), pd.DataFrame(), latest

    # Try to get underlying price from the latest day; if missing
    # (e.g. source table has no underlying column), fall back to the
    # most recent earlier date that DOES have one. This is only used
    # for the pct_atm column — OI bars themselves don't need it.
    ul = None
    same_day_ul = chain["underlying_price"].dropna()
    if not same_day_ul.empty and same_day_ul.iloc[0] > 0:
        ul = same_day_ul.iloc[0]
    else:
        prior = (df[(df["date"] < latest) & df["underlying_price"].notna()]
                 .sort_values("date"))
        if not prior.empty:
            ul = prior["underlying_price"].iloc[-1]

    ce = (chain[chain["option_type"] == "CE"]
          .groupby("strike")["open_interest"].sum()
          .nlargest(top_n).reset_index())
    pe = (chain[chain["option_type"] == "PE"]
          .groupby("strike")["open_interest"].sum()
          .nlargest(top_n).reset_index())

    if ul and ul > 0:
        ce["pct_atm"] = ((ce["strike"] - ul) / ul * 100).round(2)
        pe["pct_atm"] = ((pe["strike"] - ul) / ul * 100).round(2)
    else:
        ce["pct_atm"] = None
        pe["pct_atm"] = None

    return ce, pe, latest

# ─────────────────────────────────────────────
# HTML GENERATION
# ─────────────────────────────────────────────

def build_symbol_payload(symbol, pcr_df, mp_df, iv_df, ce_oi, pe_oi, snapshot_date):
    """Build the JS-ready data payload for one symbol (no HTML)."""
    pcr_data = [{"x": str(r["date"])[:10], "y": round(r["pcr"], 3)}
                for _, r in pcr_df.iterrows()]

    mp_near = mp_df.copy()
    if not mp_near.empty:
        mp_near = (mp_near.sort_values("expiry")
                   .groupby("date").first().reset_index())
    mp_data = [{"x": str(r["date"])[:10], "mp": round(r["max_pain"], 0),
                "ul": round(r["underlying"], 2)}
               for _, r in mp_near.iterrows()
               if r["max_pain"] is not None and not pd.isna(r["max_pain"])
               and r["underlying"] is not None and not pd.isna(r["underlying"])]

    if not iv_df.empty and "option_type" in iv_df.columns:
        iv_ce = [{"x": str(r["date"])[:10], "y": round(r["iv"], 2)}
                 for _, r in iv_df[iv_df["option_type"] == "CE"].iterrows()
                 if r["iv"] and not np.isnan(r["iv"])]
        iv_pe = [{"x": str(r["date"])[:10], "y": round(r["iv"], 2)}
                 for _, r in iv_df[iv_df["option_type"] == "PE"].iterrows()
                 if r["iv"] and not np.isnan(r["iv"])]
    else:
        iv_ce, iv_pe = [], []

    ce_oi_data = ([{"strike": r["strike"], "oi": int(r["open_interest"]), "pct": r["pct_atm"]}
                   for _, r in ce_oi.sort_values("strike").iterrows()]
                  if not ce_oi.empty else [])
    pe_oi_data = ([{"strike": r["strike"], "oi": int(r["open_interest"]), "pct": r["pct_atm"]}
                   for _, r in pe_oi.sort_values("strike").iterrows()]
                  if not pe_oi.empty else [])

    if not mp_near.empty:
        valid_ul = mp_near[mp_near["underlying"].notna()]
        _ul = valid_ul.iloc[-1]["underlying"] if not valid_ul.empty else None
    else:
        _ul = None
    latest_ul  = round(_ul, 2) if _ul is not None and not pd.isna(_ul) else None
    latest_pcr = round(pcr_df.iloc[-1]["pcr"], 3) if not pcr_df.empty else None
    _mp = mp_near.iloc[-1]["max_pain"] if not mp_near.empty else None
    latest_mp  = int(_mp) if _mp is not None and not pd.isna(_mp) else None
    if not iv_df.empty and "option_type" in iv_df.columns and \
            not iv_df[iv_df["option_type"] == "CE"].empty:
        _iv = iv_df[iv_df["option_type"] == "CE"].iloc[-1]["iv"]
        latest_ce_iv = round(_iv, 2) if not pd.isna(_iv) else None
    else:
        latest_ce_iv = None

    return {
        "symbol": symbol,
        "snapshotDate": str(snapshot_date)[:10],
        "pcrData": pcr_data,
        "mpData": mp_data,
        "ivCeData": iv_ce,
        "ivPeData": iv_pe,
        "ceOiData": ce_oi_data,
        "peOiData": pe_oi_data,
        "kpi": {
            "underlying": latest_ul,
            "pcr": latest_pcr,
            "maxPain": latest_mp,
            "ceIv": latest_ce_iv,
        }
    }


def build_combined_html(payloads):
    """Build one HTML page with a tab switcher between symbols."""
    symbols = [p["symbol"] for p in payloads]
    payload_json = json.dumps(payloads)
    tabs_html = "\n".join(
        f'<button class="tab{" active" if i == 0 else ""}" data-symbol="{p["symbol"]}">{p["symbol"]}</button>'
        for i, p in enumerate(payloads)
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Options Chain Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
          background: #f5f5f5; color: #1a1a1a; }}
  .header {{ background: #fff; border-bottom: 1px solid #e0e0e0;
             padding: 20px 32px; display: flex; align-items: center; justify-content: space-between;
             flex-wrap: wrap; gap: 16px; }}
  .header h1 {{ font-size: 22px; font-weight: 600; }}
  .header .sub {{ font-size: 13px; color: #666; margin-top: 2px; }}
  .tabs {{ display: flex; gap: 8px; }}
  .tab {{ background: #f0f0f0; border: 1px solid #e0e0e0; border-radius: 8px;
          padding: 10px 20px; font-size: 14px; font-weight: 600; color: #555;
          cursor: pointer; transition: all 0.15s; }}
  .tab:hover {{ background: #e8e8e8; }}
  .tab.active {{ background: #5b6ef5; color: #fff; border-color: #5b6ef5; }}
  .kpi-row {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px;
              padding: 24px 32px 0; }}
  .kpi {{ background: #fff; border-radius: 10px; padding: 16px 20px;
          border: 1px solid #e8e8e8; }}
  .kpi .label {{ font-size: 12px; color: #888; text-transform: uppercase;
                 letter-spacing: 0.5px; margin-bottom: 6px; }}
  .kpi .value {{ font-size: 26px; font-weight: 600; color: #1a1a1a; }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px;
           padding: 24px 32px 32px; }}
  .card {{ background: #fff; border-radius: 10px; border: 1px solid #e8e8e8;
           padding: 20px; }}
  .card h2 {{ font-size: 14px; font-weight: 600; margin-bottom: 16px; color: #333; }}
  .canvas-wrap {{ position: relative; height: 220px; }}
  .full {{ grid-column: 1 / -1; }}
  @media (max-width: 800px) {{
    .grid {{ grid-template-columns: 1fr; }}
    .kpi-row {{ grid-template-columns: 1fr 1fr; }}
    .full {{ grid-column: 1; }}
  }}
</style>
</head>
<body>

<div class="header">
  <div>
    <h1 id="pageTitle">Options Chain Dashboard</h1>
    <div class="sub" id="pageSub">Historical data from NSE Bhavcopy + Kite</div>
  </div>
  <div class="tabs" id="tabBar">
    {tabs_html}
  </div>
</div>

<div class="kpi-row">
  <div class="kpi"><div class="label">Underlying</div>
    <div class="value" id="kpiUl">—</div></div>
  <div class="kpi"><div class="label">PCR (latest)</div>
    <div class="value" id="kpiPcr">—</div></div>
  <div class="kpi"><div class="label">Max Pain</div>
    <div class="value" id="kpiMp">—</div></div>
  <div class="kpi"><div class="label">ATM CE IV</div>
    <div class="value" id="kpiIv">—</div></div>
</div>

<div class="grid">

  <div class="card">
    <h2>Put-Call Ratio (OI based)</h2>
    <div class="canvas-wrap"><canvas id="pcrChart"></canvas></div>
  </div>

  <div class="card">
    <h2>ATM Implied Volatility</h2>
    <div class="canvas-wrap"><canvas id="ivChart"></canvas></div>
  </div>

  <div class="card">
    <h2>Max Pain vs Underlying</h2>
    <div class="canvas-wrap"><canvas id="mpChart"></canvas></div>
  </div>

  <div class="card">
    <h2>OI Buildup — CE (top strikes, latest day)</h2>
    <div class="canvas-wrap"><canvas id="ceOiChart"></canvas></div>
  </div>

  <div class="card full">
    <h2>OI Buildup — PE (top strikes, latest day)</h2>
    <div class="canvas-wrap"><canvas id="peOiChart"></canvas></div>
  </div>

</div>

<script>
const ALL_DATA = {payload_json};

const timeAxis = {{
  type: 'time',
  time: {{ unit: 'month', tooltipFormat: 'dd MMM yyyy' }},
  ticks: {{ maxTicksLimit: 12, color: '#888' }},
  grid: {{ color: '#f0f0f0' }}
}};

let charts = {{}};

function fmtKpi(val, prefix = '', suffix = '') {{
  return (val === null || val === undefined) ? 'N/A' : `${{prefix}}${{val}}${{suffix}}`;
}}

function destroyCharts() {{
  Object.values(charts).forEach(c => c && c.destroy());
  charts = {{}};
}}

function renderSymbol(symbol) {{
  const d = ALL_DATA.find(x => x.symbol === symbol);
  if (!d) return;

  document.getElementById('pageTitle').textContent = symbol + ' — Options Chain Dashboard';
  document.getElementById('pageSub').textContent =
    'Snapshot date: ' + d.snapshotDate + '  |  Historical data from NSE Bhavcopy + Kite';

  document.getElementById('kpiUl').textContent  = fmtKpi(d.kpi.underlying, '₹');
  document.getElementById('kpiPcr').textContent = fmtKpi(d.kpi.pcr);
  document.getElementById('kpiMp').textContent  = fmtKpi(d.kpi.maxPain, '₹');
  document.getElementById('kpiIv').textContent  = fmtKpi(d.kpi.ceIv, '', '%');

  destroyCharts();

  charts.pcr = new Chart(document.getElementById('pcrChart'), {{
    type: 'line',
    data: {{ datasets: [{{
      label: 'PCR', data: d.pcrData,
      borderColor: '#5b6ef5', backgroundColor: 'rgba(91,110,245,0.08)',
      fill: true, tension: 0.3, pointRadius: 0, borderWidth: 2
    }}] }},
    options: {{
      responsive: true, maintainAspectRatio: false,
      parsing: {{ xAxisKey: 'x', yAxisKey: 'y' }},
      plugins: {{ legend: {{ display: false }}, tooltip: {{ mode: 'index', intersect: false }} }},
      scales: {{
        x: timeAxis,
        y: {{ ticks: {{ color: '#888' }}, grid: {{ color: '#f0f0f0' }},
              title: {{ display: true, text: 'PCR', color: '#888', font: {{ size: 11 }} }} }}
      }}
    }}
  }});

  charts.iv = new Chart(document.getElementById('ivChart'), {{
    type: 'line',
    data: {{ datasets: [
      {{ label: 'CE IV %', data: d.ivCeData, borderColor: '#10b981',
         pointRadius: 0, borderWidth: 2, tension: 0.3 }},
      {{ label: 'PE IV %', data: d.ivPeData, borderColor: '#ef4444',
         pointRadius: 0, borderWidth: 2, tension: 0.3 }}
    ] }},
    options: {{
      responsive: true, maintainAspectRatio: false,
      parsing: {{ xAxisKey: 'x', yAxisKey: 'y' }},
      plugins: {{ legend: {{ position: 'top', labels: {{ boxWidth: 12, font: {{ size: 11 }} }} }},
                  tooltip: {{ mode: 'index', intersect: false }} }},
      scales: {{
        x: timeAxis,
        y: {{ ticks: {{ color: '#888' }}, grid: {{ color: '#f0f0f0' }},
              title: {{ display: true, text: 'IV %', color: '#888', font: {{ size: 11 }} }} }}
      }}
    }}
  }});

  charts.mp = new Chart(document.getElementById('mpChart'), {{
    type: 'line',
    data: {{ datasets: [
      {{ label: 'Max Pain', data: d.mpData.map(x => ({{ x: x.x, y: x.mp }})),
         borderColor: '#f59e0b', pointRadius: 0, borderWidth: 2, tension: 0.2 }},
      {{ label: 'Underlying', data: d.mpData.map(x => ({{ x: x.x, y: x.ul }})),
         borderColor: '#6b7280', pointRadius: 0, borderWidth: 1.5,
         borderDash: [4, 3], tension: 0.2 }}
    ] }},
    options: {{
      responsive: true, maintainAspectRatio: false,
      plugins: {{ legend: {{ position: 'top', labels: {{ boxWidth: 12, font: {{ size: 11 }} }} }},
                  tooltip: {{ mode: 'index', intersect: false }} }},
      scales: {{ x: timeAxis, y: {{ ticks: {{ color: '#888' }}, grid: {{ color: '#f0f0f0' }} }} }}
    }}
  }});

  charts.ceOi = new Chart(document.getElementById('ceOiChart'), {{
    type: 'bar',
    data: {{
      labels: d.ceOiData.map(x => x.strike),
      datasets: [{{ label: 'CE OI', data: d.ceOiData.map(x => x.oi),
                    backgroundColor: 'rgba(16,185,129,0.7)', borderRadius: 4 }}]
    }},
    options: {{
      responsive: true, maintainAspectRatio: false,
      plugins: {{ legend: {{ display: false }} }},
      scales: {{
        x: {{ ticks: {{ color: '#888', font: {{ size: 10 }} }}, grid: {{ display: false }} }},
        y: {{ ticks: {{ color: '#888' }}, grid: {{ color: '#f0f0f0' }} }}
      }}
    }}
  }});

  charts.peOi = new Chart(document.getElementById('peOiChart'), {{
    type: 'bar',
    data: {{
      labels: d.peOiData.map(x => x.strike),
      datasets: [{{ label: 'PE OI', data: d.peOiData.map(x => x.oi),
                    backgroundColor: 'rgba(239,68,68,0.7)', borderRadius: 4 }}]
    }},
    options: {{
      responsive: true, maintainAspectRatio: false,
      plugins: {{ legend: {{ display: false }} }},
      scales: {{
        x: {{ ticks: {{ color: '#888', font: {{ size: 10 }} }}, grid: {{ display: false }} }},
        y: {{ ticks: {{ color: '#888' }}, grid: {{ color: '#f0f0f0' }} }}
      }}
    }}
  }});
}}

document.getElementById('tabBar').addEventListener('click', (e) => {{
  const btn = e.target.closest('.tab');
  if (!btn) return;
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  btn.classList.add('active');
  renderSymbol(btn.dataset.symbol);
}});

renderSymbol(ALL_DATA[0].symbol);
</script>
</body>
</html>"""
    return html

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    symbols = ["BANKNIFTY", "NIFTY"]
    payloads = []

    for symbol in symbols:
        print(f"\n{'='*60}")
        print(f"Processing {symbol}...")

        try:
            df = load_data(symbol)
        except ValueError as e:
            print(f"  ERROR: {e}")
            continue

        print(f"  Loaded {len(df):,} rows | "
              f"{df['date'].min().date()} → {df['date'].max().date()}")

        print("  Computing PCR...")
        pcr_df = compute_pcr(df)

        print("  Computing Max Pain...")
        mp_df = compute_max_pain(df)

        print("  Computing IV (sampled)...")
        iv_df = compute_iv_surface(df, max_dates=60)

        print("  Computing OI buildup snapshot...")
        ce_oi, pe_oi, snap_date = compute_oi_buildup(df)

        payload = build_symbol_payload(symbol, pcr_df, mp_df, iv_df, ce_oi, pe_oi, snap_date)
        payloads.append(payload)

    if not payloads:
        print("\nNo data processed for any symbol. Exiting.")
        return

    html = build_combined_html(payloads)
    out_file = OUTPUT_HTML
    with open(out_file, "w", encoding="utf-8") as f:
        f.write(html)

    abs_path = os.path.abspath(out_file)
    print(f"\nSaved combined dashboard: {abs_path}")
    webbrowser.open(f"file:///{abs_path}")

    print("\nDone.")

if __name__ == "__main__":
    main()