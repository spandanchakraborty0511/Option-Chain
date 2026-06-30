"""
sp500_analysis.py
──────────────────────────────────────────────────────────────────────────────
SPY Options Chain Pattern Analysis Dashboard
Reads from sp500_options table — computes PCR, Max Pain, and IV surface.
Outputs a single index.html dashboard.
"""

import sys, io, sqlite3, json, webbrowser, os
import pandas as pd
import numpy as np

if sys.stdout.encoding is None or sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ── Config ────────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, "sp500_data.db")
OUT_FILE = os.path.join(BASE_DIR, "index.html")
SYMBOL   = "SPY"
LOOKBACK = 120   # trading days to show in charts


# ── Data loading ──────────────────────────────────────────────────────────────
def load_data() -> pd.DataFrame:
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql("""
        SELECT date, expiry, strike, option_type,
               underlying, price, bid, ask, volume, iv, dte
        FROM sp500_options
        WHERE (price > 0 OR volume > 0)
        ORDER BY date, expiry, strike
    """, conn)
    conn.close()
    df["date"]   = pd.to_datetime(df["date"])
    df["expiry"] = pd.to_datetime(df["expiry"])
    df["strike"] = df["strike"].astype(float)
    return df


# ── PCR (Put/Call Ratio by volume) ───────────────────────────────────────────
def compute_pcr(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for date, grp in df.groupby("date"):
        ce_vol = grp[grp["option_type"] == "CE"]["volume"].sum()
        pe_vol = grp[grp["option_type"] == "PE"]["volume"].sum()
        pcr = round(pe_vol / ce_vol, 4) if ce_vol > 0 else None
        rows.append({"date": date, "pcr": pcr,
                     "ce_vol": ce_vol, "pe_vol": pe_vol})
    return pd.DataFrame(rows)


# ── Max Pain ──────────────────────────────────────────────────────────────────
def compute_max_pain(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (date, expiry), grp in df.groupby(["date", "expiry"]):
        # Only process nearest 3 expiries per date
        future = grp[grp["expiry"] >= date]["expiry"].unique() if hasattr(grp["expiry"].iloc[0], 'date') else []
        strikes = grp["strike"].unique()
        if len(strikes) < 3:
            continue

        ce = grp[grp["option_type"] == "CE"][["strike", "volume"]].dropna()
        pe = grp[grp["option_type"] == "PE"][["strike", "volume"]].dropna()

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
        ul_vals = grp["underlying"].dropna()
        ul = float(ul_vals.iloc[0]) if not ul_vals.empty else None

        rows.append({
            "date": date, "expiry": expiry,
            "max_pain": best_strike, "underlying": ul
        })

    return pd.DataFrame(rows)


# ── IV Surface ────────────────────────────────────────────────────────────────
def compute_iv(df: pd.DataFrame, max_dates: int = 60) -> pd.DataFrame:
    """IV is already in the data — just aggregate by date and option_type."""
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
            avg_iv = round(float(iv_vals.median()), 4)
            rows.append({"date": date, "option_type": otype, "iv": avg_iv})

    return pd.DataFrame(rows)


# ── HTML builder ──────────────────────────────────────────────────────────────
def build_html(pcr_df, mp_df, iv_df) -> str:

    # Restrict to last LOOKBACK trading days
    all_dates = sorted(pcr_df["date"].unique())
    recent    = all_dates[-LOOKBACK:]
    pcr_near  = pcr_df[pcr_df["date"].isin(recent)]

    # Max pain — use nearest expiry per date
    mp_near = (mp_df.sort_values("expiry")
               .groupby("date").first().reset_index())
    mp_near = mp_near[mp_near["date"].isin(recent)]

    # PCR chart data
    pcr_data = [{"x": str(r["date"])[:10], "y": round(r["pcr"], 4)}
                for _, r in pcr_near.iterrows()
                if r["pcr"] is not None and not (isinstance(r["pcr"], float) and np.isnan(r["pcr"]))]

    # Max Pain chart data
    mp_data = [{"x": str(r["date"])[:10],
                "mp": round(r["max_pain"], 0),
                "ul": round(r["underlying"], 2)}
               for _, r in mp_near.iterrows()
               if r["max_pain"] is not None and r["underlying"] is not None
               and not np.isnan(r["max_pain"]) and not np.isnan(r["underlying"])]

    # IV chart data
    if not iv_df.empty and "option_type" in iv_df.columns:
        iv_ce = [{"x": str(r["date"])[:10], "y": round(r["iv"], 4)}
                 for _, r in iv_df[iv_df["option_type"] == "CE"].iterrows()
                 if r["iv"] and not np.isnan(r["iv"])]
        iv_pe = [{"x": str(r["date"])[:10], "y": round(r["iv"], 4)}
                 for _, r in iv_df[iv_df["option_type"] == "PE"].iterrows()
                 if r["iv"] and not np.isnan(r["iv"])]
    else:
        iv_ce, iv_pe = [], []

    # KPIs
    latest_pcr = round(pcr_near.iloc[-1]["pcr"], 2) if not pcr_near.empty and pcr_near.iloc[-1]["pcr"] else "N/A"

    _ul_series = mp_near["underlying"].dropna()
    _ul_series = _ul_series[~_ul_series.apply(lambda x: isinstance(x, float) and np.isnan(x))]
    latest_ul  = round(float(_ul_series.iloc[-1]), 2) if not _ul_series.empty else "N/A"

    _mp_series = mp_near["max_pain"].dropna()
    latest_mp  = int(_mp_series.iloc[-1]) if not _mp_series.empty else "N/A"

    if not iv_df.empty and "option_type" in iv_df.columns:
        ce_iv_rows = iv_df[iv_df["option_type"] == "CE"]["iv"].dropna()
        latest_ce_iv = round(float(ce_iv_rows.iloc[-1]) * 100, 2) if not ce_iv_rows.empty else "N/A"
    else:
        latest_ce_iv = "N/A"

    latest_date = str(all_dates[-1])[:10] if all_dates else "N/A"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>SPY Options Dashboard</title>
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
    padding: 24px 32px 16px;
    border-bottom: 1px solid var(--border);
    display: flex; justify-content: space-between; align-items: flex-end;
  }}
  header h1 {{ font-size: 1.6rem; font-weight: 700; letter-spacing: -0.5px; }}
  header h1 span {{ color: var(--blue); }}
  header .meta {{ font-size: 0.78rem; color: var(--muted); text-align: right; }}

  .kpis {{
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 16px;
    padding: 24px 32px;
  }}
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
  .chart-card h2 {{
    font-size: 0.85rem;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    margin-bottom: 16px;
  }}
  canvas {{ width: 100% !important; height: 220px !important; }}

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
  <div>
    <h1><span>SPY</span> Options Dashboard</h1>
    <div style="font-size:0.8rem;color:var(--muted);margin-top:4px;">
      S&amp;P 500 ETF &mdash; EOD Options Chain Analysis (2010&ndash;2023)
    </div>
  </div>
  <div class="meta">
    Last date: {latest_date}<br/>
    {LOOKBACK} trading days shown
  </div>
</header>

<div class="kpis">
  <div class="kpi">
    <div class="label">SPY Last Price</div>
    <div class="value blue">${latest_ul}</div>
  </div>
  <div class="kpi">
    <div class="label">Max Pain Strike</div>
    <div class="value amber">${latest_mp}</div>
  </div>
  <div class="kpi">
    <div class="label">Put/Call Ratio (Vol)</div>
    <div class="value {'green' if isinstance(latest_pcr, float) and latest_pcr < 1 else 'red'}">{latest_pcr}</div>
  </div>
  <div class="kpi">
    <div class="label">Avg Call IV</div>
    <div class="value amber">{latest_ce_iv}{'%' if latest_ce_iv != 'N/A' else ''}</div>
  </div>
</div>

<div class="charts">

  <div class="chart-card">
    <h2>Put/Call Ratio (Volume)</h2>
    <canvas id="pcrChart"></canvas>
  </div>

  <div class="chart-card">
    <h2>Max Pain vs SPY Price</h2>
    <canvas id="mpChart"></canvas>
  </div>

  <div class="chart-card">
    <h2>Implied Volatility — Call (CE)</h2>
    <canvas id="ivCeChart"></canvas>
  </div>

  <div class="chart-card">
    <h2>Implied Volatility — Put (PE)</h2>
    <canvas id="ivPeChart"></canvas>
  </div>

</div>

<footer>
  Data: SPY EOD Options 2010&ndash;2023 &mdash; Kaggle &mdash; Generated by sp500_analysis.py
</footer>

<script>
const PCR_DATA  = {json.dumps(pcr_data)};
const MP_DATA   = {json.dumps(mp_data)};
const IV_CE     = {json.dumps(iv_ce)};
const IV_PE     = {json.dumps(iv_pe)};

const GRID  = {{ color: 'rgba(48,54,61,0.8)' }};
const TICK  = {{ color: '#8b949e', font: {{ size: 10 }} }};
const baseOpts = {{
  responsive: true,
  maintainAspectRatio: false,
  plugins: {{ legend: {{ display: false }}, tooltip: {{ mode: 'index', intersect: false }} }},
  scales: {{
    x: {{ grid: GRID, ticks: {{ ...TICK, maxTicksLimit: 8, maxRotation: 0 }} }},
    y: {{ grid: GRID, ticks: TICK }}
  }}
}};

// PCR Chart
new Chart(document.getElementById('pcrChart'), {{
  type: 'line',
  data: {{
    labels: PCR_DATA.map(d => d.x),
    datasets: [{{
      data: PCR_DATA.map(d => d.y),
      borderColor: '#58a6ff', borderWidth: 1.5,
      pointRadius: 0, fill: false, tension: 0.3
    }},
    {{
      data: PCR_DATA.map(() => 1.0),
      borderColor: 'rgba(248,81,73,0.4)', borderWidth: 1,
      borderDash: [4,4], pointRadius: 0, fill: false
    }}]
  }},
  options: {{ ...baseOpts }}
}});

// Max Pain Chart
new Chart(document.getElementById('mpChart'), {{
  type: 'line',
  data: {{
    labels: MP_DATA.map(d => d.x),
    datasets: [
      {{
        label: 'Max Pain',
        data: MP_DATA.map(d => d.mp),
        borderColor: '#d29922', borderWidth: 1.5,
        pointRadius: 0, fill: false, tension: 0.3
      }},
      {{
        label: 'SPY Price',
        data: MP_DATA.map(d => d.ul),
        borderColor: '#3fb950', borderWidth: 1.5,
        pointRadius: 0, fill: false, tension: 0.3
      }}
    ]
  }},
  options: {{
    ...baseOpts,
    plugins: {{
      legend: {{ display: true, labels: {{ color: '#8b949e', font: {{ size: 11 }} }} }},
      tooltip: {{ mode: 'index', intersect: false }}
    }}
  }}
}});

// IV CE Chart
new Chart(document.getElementById('ivCeChart'), {{
  type: 'line',
  data: {{
    labels: IV_CE.map(d => d.x),
    datasets: [{{
      data: IV_CE.map(d => (d.y * 100).toFixed(2)),
      borderColor: '#3fb950', borderWidth: 1.5,
      pointRadius: 0, fill: true,
      backgroundColor: 'rgba(63,185,80,0.08)', tension: 0.3
    }}]
  }},
  options: {{
    ...baseOpts,
    scales: {{
      x: {{ grid: GRID, ticks: {{ ...TICK, maxTicksLimit: 8, maxRotation: 0 }} }},
      y: {{ grid: GRID, ticks: {{ ...TICK, callback: v => v + '%' }} }}
    }}
  }}
}});

// IV PE Chart
new Chart(document.getElementById('ivPeChart'), {{
  type: 'line',
  data: {{
    labels: IV_PE.map(d => d.x),
    datasets: [{{
      data: IV_PE.map(d => (d.y * 100).toFixed(2)),
      borderColor: '#f85149', borderWidth: 1.5,
      pointRadius: 0, fill: true,
      backgroundColor: 'rgba(248,81,73,0.08)', tension: 0.3
    }}]
  }},
  options: {{
    ...baseOpts,
    scales: {{
      x: {{ grid: GRID, ticks: {{ ...TICK, maxTicksLimit: 8, maxRotation: 0 }} }},
      y: {{ grid: GRID, ticks: {{ ...TICK, callback: v => v + '%' }} }}
    }}
  }}
}});
</script>
</body>
</html>"""
    return html


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print(f"Processing {SYMBOL}...")

    print(f"  Loading data from DB...")
    df = load_data()
    print(f"  Loaded {len(df):,} rows | {str(df['date'].min())[:10]} -> {str(df['date'].max())[:10]}")

    print("  Computing PCR...")
    pcr_df = compute_pcr(df)

    print("  Computing Max Pain...")
    mp_df = compute_max_pain(df)

    print("  Computing IV surface...")
    iv_df = compute_iv(df, max_dates=LOOKBACK)

    print("  Building HTML...")
    html = build_html(pcr_df, mp_df, iv_df)

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"  Saved: {OUT_FILE}")
    print("=" * 60)
    print("Done.")
    webbrowser.open(f"file:///{OUT_FILE}")


if __name__ == "__main__":
    main()