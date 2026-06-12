"""
Options Chain Pattern Analysis Dashboard
Python 3.11 compatible — no backslashes inside f-strings
"""

import sqlite3
import json
import webbrowser
import os
import pandas as pd
import numpy as np

DB_PATH      = "options_data.db"
OUTPUT_HTML  = "options_patterns.html"

# ─────────────────────────────────────────────
#  Load data from SQLite
# ─────────────────────────────────────────────

def load_data(db_path):
    conn = sqlite3.connect(db_path)

    chain   = pd.read_sql("SELECT * FROM options_chain ORDER BY instrument, date, strike, type", conn)
    iv_data = pd.read_sql("SELECT * FROM iv_data      ORDER BY instrument, date, strike, type", conn)
    mp      = pd.read_sql("SELECT * FROM max_pain     ORDER BY instrument, date", conn)
    pcr     = pd.read_sql("SELECT * FROM pcr          ORDER BY instrument, date", conn)

    conn.close()

    # normalise date columns
    for df in [chain, iv_data, mp, pcr]:
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")

    instruments = sorted(chain["instrument"].unique().tolist())
    return chain, iv_data, mp, pcr, instruments


# ─────────────────────────────────────────────
#  Build per-instrument chart data
# ─────────────────────────────────────────────

def build_instrument_data(inst, chain, iv_data, mp, pcr):
    c  = chain[chain["instrument"]   == inst].copy()
    iv = iv_data[iv_data["instrument"] == inst].copy()
    m  = mp[mp["instrument"]          == inst].copy()
    p  = pcr[pcr["instrument"]        == inst].copy()

    dates   = sorted(c["date"].unique().tolist())
    strikes = sorted(c["strike"].unique().tolist())

    # ── options chain: one snapshot per date ──
    chain_by_date = {}
    for d in dates:
        day = c[c["date"] == d]
        ce  = day[day["type"] == "CE"].sort_values("strike")
        pe  = day[day["type"] == "PE"].sort_values("strike")
        iv_day = iv[iv["date"] == d]
        iv_ce  = iv_day[iv_day["type"] == "CE"].sort_values("strike")
        iv_pe  = iv_day[iv_day["type"] == "PE"].sort_values("strike")

        spot_val = float(iv_day["spot"].iloc[0]) if len(iv_day) > 0 else None

        chain_by_date[d] = {
            "strikes_ce":   ce["strike"].tolist(),
            "close_ce":     ce["close"].tolist(),
            "oi_ce":        ce["oi"].tolist(),
            "volume_ce":    ce["volume"].tolist(),
            "strikes_pe":   pe["strike"].tolist(),
            "close_pe":     pe["close"].tolist(),
            "oi_pe":        pe["oi"].tolist(),
            "volume_pe":    pe["volume"].tolist(),
            "iv_strikes_ce": iv_ce["strike"].tolist(),
            "iv_ce":         (iv_ce["iv"] * 100).round(2).tolist(),
            "iv_strikes_pe": iv_pe["strike"].tolist(),
            "iv_pe":         (iv_pe["iv"] * 100).round(2).tolist(),
            "spot":          spot_val,
        }

    # ── OI heatmap: strike × date matrices ──
    ce_hm = c[c["type"] == "CE"].pivot_table(index="strike", columns="date", values="oi", aggfunc="sum").fillna(0)
    pe_hm = c[c["type"] == "PE"].pivot_table(index="strike", columns="date", values="oi", aggfunc="sum").fillna(0)

    # ── max pain vs spot ──
    spot_series = {}
    for d in dates:
        day_iv = iv[iv["date"] == d]
        if len(day_iv) > 0:
            spot_series[d] = float(day_iv["spot"].iloc[0])

    mp_dates  = m["date"].tolist()
    mp_values = m["max_pain_strike"].tolist()
    spot_for_mp = [spot_series.get(d) for d in mp_dates]

    # ── PCR ──
    pcr_dates  = p["date"].tolist()
    pcr_oi     = p["pcr_oi"].tolist()
    pcr_vol    = p["pcr_volume"].tolist()

    # ── insights: latest date ──
    latest = dates[-1] if dates else None
    insights = {}
    if latest:
        day = c[c["date"] == latest]
        ce_day = day[day["type"] == "CE"].sort_values("oi", ascending=False)
        pe_day = day[day["type"] == "PE"].sort_values("oi", ascending=False)
        insights["top_ce_oi"]  = ce_day.head(3)["strike"].tolist()
        insights["top_pe_oi"]  = pe_day.head(3)["strike"].tolist()
        insights["latest_date"] = latest

        mp_row = m[m["date"] == latest]
        insights["max_pain"] = float(mp_row["max_pain_strike"].iloc[0]) if len(mp_row) > 0 else None

        iv_latest = iv[iv["date"] == latest]
        insights["spot"] = float(iv_latest["spot"].iloc[0]) if len(iv_latest) > 0 else None

        pcr_row = p[p["date"] == latest]
        if len(pcr_row) > 0 and pd.notna(pcr_row["pcr_oi"].iloc[0]):
            pval = float(pcr_row["pcr_oi"].iloc[0])
            insights["pcr_oi"] = round(pval, 3)
            if pval > 1.3:
                insights["pcr_signal"] = "Bearish (PCR > 1.3)"
                insights["pcr_color"]  = "#ef4444"
            elif pval < 0.7:
                insights["pcr_signal"] = "Bullish (PCR < 0.7)"
                insights["pcr_color"]  = "#22c55e"
            else:
                insights["pcr_signal"] = "Neutral"
                insights["pcr_color"]  = "#f59e0b"
        else:
            insights["pcr_oi"]     = None
            insights["pcr_signal"] = "N/A"
            insights["pcr_color"]  = "#6b7280"

    return {
        "dates":         dates,
        "strikes":       strikes,
        "chain_by_date": chain_by_date,
        "heatmap_ce": {
            "strikes": ce_hm.index.tolist(),
            "dates":   ce_hm.columns.tolist(),
            "z":       ce_hm.values.tolist(),
        },
        "heatmap_pe": {
            "strikes": pe_hm.index.tolist(),
            "dates":   pe_hm.columns.tolist(),
            "z":       pe_hm.values.tolist(),
        },
        "mp_dates":    mp_dates,
        "mp_values":   mp_values,
        "spot_for_mp": spot_for_mp,
        "pcr_dates":   pcr_dates,
        "pcr_oi":      pcr_oi,
        "pcr_vol":     pcr_vol,
        "insights":    insights,
    }


# ─────────────────────────────────────────────
#  Print terminal insights
# ─────────────────────────────────────────────

def print_insights(instruments, all_data):
    print("\n" + "=" * 58)
    print("  OPTIONS CHAIN PATTERN INSIGHTS")
    print("=" * 58)
    for inst in instruments:
        d  = all_data[inst]
        ins = d["insights"]
        print("\n  [" + inst + "]  as of " + str(ins.get("latest_date", "N/A")))
        print("  Spot       : " + str(ins.get("spot", "N/A")))
        print("  Max Pain   : " + str(ins.get("max_pain", "N/A")))
        print("  PCR (OI)   : " + str(ins.get("pcr_oi", "N/A")) + "  → " + ins.get("pcr_signal", ""))
        print("  Top CE OI (Resistance): " + str(ins.get("top_ce_oi", [])))
        print("  Top PE OI (Support)   : " + str(ins.get("top_pe_oi", [])))
    print("\n" + "=" * 58)


# ─────────────────────────────────────────────
#  Build HTML
# ─────────────────────────────────────────────

def build_html(instruments, all_data):

    # serialise to JS
    js_data = "const ALL_DATA = " + json.dumps(all_data, default=str) + ";\n"
    js_data += "const INSTRUMENTS = " + json.dumps(instruments) + ";\n"

    # instrument buttons (Python 3.11 safe — no backslash in f-string)
    btn_html = ""
    for i, inst in enumerate(instruments):
        active_cls = " active" if i == 0 else ""
        btn_html += (
            '<button class="inst-btn' + active_cls + '" '
            'onclick="selectInstrument(this, \'' + inst + '\')">'
            + inst + '</button>\n'
        )

    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Options Chain Dashboard</title>
<script src="https://cdn.plot.ly/plotly-2.32.0.min.js"></script>
<style>
  :root {
    --bg:      #0d1117;
    --surface: #161b22;
    --border:  #30363d;
    --text:    #e6edf3;
    --muted:   #8b949e;
    --green:   #3fb950;
    --red:     #f85149;
    --yellow:  #d29922;
    --blue:    #58a6ff;
    --purple:  #bc8cff;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif; }

  header {
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 14px 24px;
    display: flex; align-items: center; justify-content: space-between;
  }
  header h1 { font-size: 1.1rem; font-weight: 600; letter-spacing: .03em; }
  #clock { font-size: .85rem; color: var(--muted); font-variant-numeric: tabular-nums; }

  .inst-bar {
    padding: 12px 24px;
    display: flex; gap: 8px; flex-wrap: wrap;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
  }
  .inst-btn {
    background: var(--bg);
    border: 1px solid var(--border);
    color: var(--muted);
    padding: 6px 18px; border-radius: 20px;
    cursor: pointer; font-size: .85rem; font-weight: 500;
    transition: all .15s;
  }
  .inst-btn:hover  { border-color: var(--blue); color: var(--blue); }
  .inst-btn.active { background: var(--blue); border-color: var(--blue); color: #fff; }

  .insights-bar {
    display: flex; flex-wrap: wrap; gap: 16px;
    padding: 10px 24px;
    background: #111820;
    border-bottom: 1px solid var(--border);
    font-size: .8rem;
  }
  .ins-item { display: flex; align-items: center; gap: 6px; }
  .ins-label { color: var(--muted); }
  .ins-val   { color: var(--text); font-weight: 500; }

  .tabs {
    display: flex; gap: 2px;
    padding: 12px 24px 0;
    border-bottom: 1px solid var(--border);
  }
  .tab-btn {
    background: none; border: none;
    color: var(--muted); padding: 8px 18px;
    font-size: .88rem; cursor: pointer;
    border-bottom: 2px solid transparent;
    transition: all .15s;
  }
  .tab-btn:hover  { color: var(--text); }
  .tab-btn.active { color: var(--blue); border-bottom-color: var(--blue); }

  .tab-content { display: none; padding: 20px 24px; }
  .tab-content.active { display: block; }

  .chart-controls {
    display: flex; align-items: center; gap: 12px;
    margin-bottom: 14px; flex-wrap: wrap;
  }
  .ctrl-label { font-size: .8rem; color: var(--muted); }
  select, input[type=range] {
    background: var(--surface);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 5px 10px; border-radius: 6px;
    font-size: .82rem; cursor: pointer;
  }
  .view-btns { display: flex; gap: 4px; }
  .view-btn {
    background: var(--surface); border: 1px solid var(--border);
    color: var(--muted); padding: 4px 12px; border-radius: 4px;
    cursor: pointer; font-size: .78rem;
    transition: all .12s;
  }
  .view-btn:hover  { border-color: var(--blue); color: var(--blue); }
  .view-btn.active { background: var(--blue); border-color: var(--blue); color: #fff; }

  .play-btn {
    background: var(--green); border: none; color: #000;
    padding: 5px 14px; border-radius: 6px;
    cursor: pointer; font-size: .82rem; font-weight: 600;
  }
  .play-btn.stop { background: var(--red); }

  .date-display { font-size: .82rem; color: var(--yellow); font-weight: 600; min-width: 90px; }

  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  .grid2-full { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px; }

  .chart-box {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px; padding: 14px;
  }
  .chart-box h3 {
    font-size: .82rem; font-weight: 600;
    color: var(--muted); letter-spacing: .06em;
    text-transform: uppercase; margin-bottom: 10px;
  }

  @media (max-width: 700px) {
    .grid2, .grid2-full { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>

<header>
  <h1>⚡ Options Chain Dashboard</h1>
  <span id="clock"></span>
</header>

<div class="inst-bar">
""" + btn_html + """</div>

<div class="insights-bar" id="insights-bar">
  <div class="ins-item"><span class="ins-label">Date:</span>     <span class="ins-val" id="ins-date">—</span></div>
  <div class="ins-item"><span class="ins-label">Spot:</span>     <span class="ins-val" id="ins-spot">—</span></div>
  <div class="ins-item"><span class="ins-label">Max Pain:</span> <span class="ins-val" id="ins-mp">—</span></div>
  <div class="ins-item"><span class="ins-label">PCR (OI):</span> <span class="ins-val" id="ins-pcr">—</span></div>
  <div class="ins-item"><span class="ins-label">CE Resistance:</span> <span class="ins-val" id="ins-ce">—</span></div>
  <div class="ins-item"><span class="ins-label">PE Support:</span>    <span class="ins-val" id="ins-pe">—</span></div>
</div>

<div class="tabs">
  <button class="tab-btn active" onclick="switchTab(this,'tab-chain')">📊 Options Chain</button>
  <button class="tab-btn"        onclick="switchTab(this,'tab-pattern')">🔍 Pattern Analysis</button>
  <button class="tab-btn"        onclick="switchTab(this,'tab-heatmap')">🌡️ OI Heatmap</button>
</div>

<!-- ── TAB 1: OPTIONS CHAIN ── -->
<div id="tab-chain" class="tab-content active">
  <div class="chart-controls">
    <span class="ctrl-label">Date:</span>
    <input type="range" id="date-slider" min="0" value="0" oninput="onSlider(this)">
    <span class="date-display" id="slider-date">—</span>
    <span class="ctrl-label">View:</span>
    <div class="view-btns">
      <button class="view-btn active" onclick="setView(this,'price')">Price</button>
      <button class="view-btn"        onclick="setView(this,'oi')">OI</button>
      <button class="view-btn"        onclick="setView(this,'iv')">IV Smile</button>
      <button class="view-btn"        onclick="setView(this,'all')">All</button>
    </div>
    <button class="play-btn" id="play-btn" onclick="togglePlay()">▶ Play</button>
  </div>
  <div id="chain-chart" style="height:480px;"></div>
</div>

<!-- ── TAB 2: PATTERN ANALYSIS ── -->
<div id="tab-pattern" class="tab-content">
  <div class="grid2-full">
    <div class="chart-box">
      <h3>OI Buildup — Latest Date</h3>
      <div id="oi-chart" style="height:280px;"></div>
    </div>
    <div class="chart-box">
      <h3>IV Smile — Latest Date</h3>
      <div id="iv-chart" style="height:280px;"></div>
    </div>
  </div>
  <div class="grid2">
    <div class="chart-box">
      <h3>Max Pain vs Spot (Over Time)</h3>
      <div id="mp-chart" style="height:260px;"></div>
    </div>
    <div class="chart-box">
      <h3>PCR Trend</h3>
      <div id="pcr-chart" style="height:260px;"></div>
    </div>
  </div>
</div>

<!-- ── TAB 3: HEATMAP ── -->
<div id="tab-heatmap" class="tab-content">
  <div class="grid2">
    <div class="chart-box">
      <h3>CE Open Interest Heatmap</h3>
      <div id="hm-ce" style="height:420px;"></div>
    </div>
    <div class="chart-box">
      <h3>PE Open Interest Heatmap</h3>
      <div id="hm-pe" style="height:420px;"></div>
    </div>
  </div>
</div>

<script>
""" + js_data + """

// ── State ──
let currentInst  = INSTRUMENTS[0];
let currentView  = 'price';
let currentDateIdx = 0;
let playTimer    = null;

const PLOTLY_CFG = { responsive: true, displayModeBar: false };
const DARK_LAYOUT = {
  paper_bgcolor: 'transparent',
  plot_bgcolor:  'transparent',
  font:  { color: '#e6edf3', size: 11 },
  xaxis: { gridcolor: '#21262d', linecolor: '#30363d', zerolinecolor: '#30363d' },
  yaxis: { gridcolor: '#21262d', linecolor: '#30363d', zerolinecolor: '#30363d' },
  margin: { t: 20, b: 40, l: 50, r: 20 },
  legend: { bgcolor: 'transparent', bordercolor: 'transparent' },
};

function dl(id, extra) { return Object.assign({}, DARK_LAYOUT, extra || {}, { uirevision: id }); }

// ── Clock ──
function tick() {
  const now = new Date();
  document.getElementById('clock').textContent =
    now.toLocaleDateString('en-IN') + '  ' + now.toLocaleTimeString('en-IN');
}
tick(); setInterval(tick, 1000);

// ── Tab switching ──
function switchTab(btn, id) {
  document.querySelectorAll('.tab-btn').forEach(function(b) { b.classList.remove('active'); });
  document.querySelectorAll('.tab-content').forEach(function(t) { t.classList.remove('active'); });
  btn.classList.add('active');
  document.getElementById(id).classList.add('active');
  renderAllCharts();
}

// ── Instrument switching ──
function selectInstrument(btn, inst) {
  document.querySelectorAll('.inst-btn').forEach(function(b) { b.classList.remove('active'); });
  btn.classList.add('active');
  currentInst   = inst;
  currentDateIdx = 0;
  initSlider();
  updateInsights();
  renderAllCharts();
}

// ── Slider ──
function initSlider() {
  var dates  = ALL_DATA[currentInst].dates;
  var slider = document.getElementById('date-slider');
  slider.max   = dates.length - 1;
  slider.value = dates.length - 1;   // start at latest date
  currentDateIdx = dates.length - 1;
  document.getElementById('slider-date').textContent = dates[currentDateIdx] || '—';
}

function onSlider(el) {
  currentDateIdx = parseInt(el.value);
  var dates = ALL_DATA[currentInst].dates;
  document.getElementById('slider-date').textContent = dates[currentDateIdx] || '—';
  renderChainChart();
}

// ── View buttons ──
function setView(btn, view) {
  document.querySelectorAll('.view-btn').forEach(function(b) { b.classList.remove('active'); });
  btn.classList.add('active');
  currentView = view;
  renderChainChart();
}

// ── Play ──
function togglePlay() {
  var btn = document.getElementById('play-btn');
  if (playTimer) {
    clearInterval(playTimer);
    playTimer = null;
    btn.textContent = '▶ Play';
    btn.classList.remove('stop');
  } else {
    var dates = ALL_DATA[currentInst].dates;
    if (currentDateIdx >= dates.length - 1) currentDateIdx = 0;
    btn.textContent = '⏹ Stop';
    btn.classList.add('stop');
    playTimer = setInterval(function() {
      if (currentDateIdx >= dates.length - 1) {
        togglePlay(); return;
      }
      currentDateIdx++;
      var slider = document.getElementById('date-slider');
      slider.value = currentDateIdx;
      document.getElementById('slider-date').textContent = dates[currentDateIdx];
      renderChainChart();
    }, 500);
  }
}

// ── Insights bar ──
function updateInsights() {
  var ins = ALL_DATA[currentInst].insights;
  document.getElementById('ins-date').textContent = ins.latest_date || '—';
  document.getElementById('ins-spot').textContent = ins.spot ? '₹' + ins.spot.toLocaleString('en-IN') : '—';
  document.getElementById('ins-mp').textContent   = ins.max_pain ? '₹' + ins.max_pain.toLocaleString('en-IN') : '—';

  var pcrEl = document.getElementById('ins-pcr');
  pcrEl.textContent = ins.pcr_oi ? ins.pcr_oi + ' (' + ins.pcr_signal + ')' : '—';
  pcrEl.style.color = ins.pcr_color || '#e6edf3';

  document.getElementById('ins-ce').textContent = (ins.top_ce_oi || []).join(', ') || '—';
  document.getElementById('ins-pe').textContent = (ins.top_pe_oi || []).join(', ') || '—';
}

// ── Chain chart ──
function renderChainChart() {
  var d    = ALL_DATA[currentInst];
  var date = d.dates[currentDateIdx];
  if (!date) return;
  var snap = d.chain_by_date[date];
  if (!snap) return;

  var traces = [];
  var layout = dl('chain', { xaxis: { title: 'Strike' } });

  if (currentView === 'price' || currentView === 'all') {
    layout.yaxis = Object.assign({}, layout.yaxis, { title: 'Premium (₹)' });
    traces.push({
      x: snap.strikes_ce, y: snap.close_ce,
      name: 'CE Price', type: 'scatter', mode: 'lines+markers',
      line: { color: '#58a6ff', width: 2 }, marker: { size: 5 }
    });
    traces.push({
      x: snap.strikes_pe, y: snap.close_pe,
      name: 'PE Price', type: 'scatter', mode: 'lines+markers',
      line: { color: '#f85149', width: 2 }, marker: { size: 5 }
    });
  }

  if (currentView === 'oi' || currentView === 'all') {
    var yaxis_oi = currentView === 'all' ? 'y2' : 'y';
    traces.push({
      x: snap.strikes_ce, y: snap.oi_ce,
      name: 'CE OI', type: 'bar', yaxis: yaxis_oi,
      marker: { color: 'rgba(88,166,255,0.45)' }
    });
    traces.push({
      x: snap.strikes_pe, y: snap.oi_pe,
      name: 'PE OI', type: 'bar', yaxis: yaxis_oi,
      marker: { color: 'rgba(248,81,73,0.45)' }
    });
    if (currentView === 'all') {
      layout.yaxis2 = {
        title: 'OI', overlaying: 'y', side: 'right',
        gridcolor: 'transparent', linecolor: '#30363d'
      };
      layout.barmode = 'group';
    } else {
      layout.yaxis = Object.assign({}, layout.yaxis, { title: 'Open Interest' });
      layout.barmode = 'group';
    }
  }

  if (currentView === 'iv' || currentView === 'all') {
    traces.push({
      x: snap.iv_strikes_ce, y: snap.iv_ce,
      name: 'CE IV%', type: 'scatter', mode: 'lines',
      line: { color: '#d29922', width: 2, dash: 'dot' }
    });
    traces.push({
      x: snap.iv_strikes_pe, y: snap.iv_pe,
      name: 'PE IV%', type: 'scatter', mode: 'lines',
      line: { color: '#bc8cff', width: 2, dash: 'dot' }
    });
    if (currentView === 'iv') {
      layout.yaxis = Object.assign({}, layout.yaxis, { title: 'IV (%)' });
    }
  }

  if (snap.spot) {
    layout.shapes = [{
      type: 'line', x0: snap.spot, x1: snap.spot, y0: 0, y1: 1,
      xref: 'x', yref: 'paper',
      line: { color: '#3fb950', width: 1.5, dash: 'dash' }
    }];
    layout.annotations = [{
      x: snap.spot, y: 1, xref: 'x', yref: 'paper',
      text: 'Spot ' + snap.spot.toLocaleString('en-IN'),
      showarrow: false, yanchor: 'bottom',
      font: { color: '#3fb950', size: 10 }
    }];
  }

  layout.title = {
    text: currentInst + ' Options Chain — ' + date,
    font: { size: 13, color: '#e6edf3' }
  };

  Plotly.react('chain-chart', traces, layout, PLOTLY_CFG);
}

// ── OI Buildup (latest) ──
function renderOIChart() {
  var d    = ALL_DATA[currentInst];
  if (!d.dates.length) return;
  var date = d.dates[d.dates.length - 1];
  var snap = d.chain_by_date[date];
  if (!snap) return;

  var traces = [
    { x: snap.strikes_ce, y: snap.oi_ce, name: 'CE OI', type: 'bar', marker: { color: 'rgba(88,166,255,0.6)' } },
    { x: snap.strikes_pe, y: snap.oi_pe, name: 'PE OI', type: 'bar', marker: { color: 'rgba(248,81,73,0.6)' } },
  ];
  var layout = dl('oi', { xaxis: { title: 'Strike' }, yaxis: { title: 'OI' }, barmode: 'group' });

  if (snap.spot) {
    layout.shapes = [{
      type: 'line', x0: snap.spot, x1: snap.spot, y0: 0, y1: 1,
      xref: 'x', yref: 'paper', line: { color: '#3fb950', width: 1.5, dash: 'dash' }
    }];
  }
  Plotly.react('oi-chart', traces, layout, PLOTLY_CFG);
}

// ── IV Smile (latest) ──
function renderIVChart() {
  var d    = ALL_DATA[currentInst];
  if (!d.dates.length) return;
  var date = d.dates[d.dates.length - 1];
  var snap = d.chain_by_date[date];
  if (!snap) return;

  var traces = [
    { x: snap.iv_strikes_ce, y: snap.iv_ce, name: 'CE IV%', type: 'scatter', mode: 'lines+markers',
      line: { color: '#58a6ff', width: 2 }, marker: { size: 5 } },
    { x: snap.iv_strikes_pe, y: snap.iv_pe, name: 'PE IV%', type: 'scatter', mode: 'lines+markers',
      line: { color: '#f85149', width: 2 }, marker: { size: 5 } },
  ];
  var layout = dl('iv', { xaxis: { title: 'Strike' }, yaxis: { title: 'IV (%)' } });
  if (snap.spot) {
    layout.shapes = [{
      type: 'line', x0: snap.spot, x1: snap.spot, y0: 0, y1: 1,
      xref: 'x', yref: 'paper', line: { color: '#3fb950', width: 1.5, dash: 'dash' }
    }];
  }
  Plotly.react('iv-chart', traces, layout, PLOTLY_CFG);
}

// ── Max Pain vs Spot ──
function renderMPChart() {
  var d = ALL_DATA[currentInst];
  var traces = [
    { x: d.mp_dates, y: d.spot_for_mp, name: 'Spot', type: 'scatter', mode: 'lines',
      line: { color: '#3fb950', width: 2 } },
    { x: d.mp_dates, y: d.mp_values,   name: 'Max Pain', type: 'scatter', mode: 'lines+markers',
      line: { color: '#d29922', width: 2, dash: 'dot' }, marker: { size: 5 } },
  ];
  Plotly.react('mp-chart', traces,
    dl('mp', { xaxis: { title: 'Date' }, yaxis: { title: 'Level' } }),
    PLOTLY_CFG
  );
}

// ── PCR Trend ──
function renderPCRChart() {
  var d = ALL_DATA[currentInst];
  var traces = [
    { x: d.pcr_dates, y: d.pcr_oi,  name: 'PCR OI',     type: 'scatter', mode: 'lines',
      line: { color: '#58a6ff', width: 2 } },
    { x: d.pcr_dates, y: d.pcr_vol, name: 'PCR Volume',  type: 'scatter', mode: 'lines',
      line: { color: '#bc8cff', width: 1.5, dash: 'dot' } },
  ];
  var layout = dl('pcr', {
    xaxis: { title: 'Date' },
    yaxis: { title: 'PCR', zeroline: false },
    shapes: [
      { type: 'rect', x0: d.pcr_dates[0], x1: d.pcr_dates[d.pcr_dates.length-1],
        y0: 1.3, y1: 3, xref: 'x', yref: 'y',
        fillcolor: 'rgba(248,81,73,0.08)', line: { width: 0 } },
      { type: 'rect', x0: d.pcr_dates[0], x1: d.pcr_dates[d.pcr_dates.length-1],
        y0: 0, y1: 0.7, xref: 'x', yref: 'y',
        fillcolor: 'rgba(63,185,80,0.08)', line: { width: 0 } },
      { type: 'line', x0: d.pcr_dates[0], x1: d.pcr_dates[d.pcr_dates.length-1],
        y0: 1.0, y1: 1.0, xref: 'x', yref: 'y',
        line: { color: '#d29922', width: 1, dash: 'dash' } },
    ]
  });
  Plotly.react('pcr-chart', traces, layout, PLOTLY_CFG);
}

// ── Heatmaps ──
function renderHeatmaps() {
  var d = ALL_DATA[currentInst];

  var hce = d.heatmap_ce;
  Plotly.react('hm-ce', [{
    x: hce.dates, y: hce.strikes.map(String), z: hce.z,
    type: 'heatmap', colorscale: 'Blues', reversescale: false,
    showscale: true, hoverongaps: false,
    colorbar: { tickfont: { color: '#e6edf3', size: 10 } }
  }], dl('hm-ce', { xaxis: { title: 'Date' }, yaxis: { title: 'Strike', type: 'category' } }), PLOTLY_CFG);

  var hpe = d.heatmap_pe;
  Plotly.react('hm-pe', [{
    x: hpe.dates, y: hpe.strikes.map(String), z: hpe.z,
    type: 'heatmap', colorscale: 'Reds', reversescale: false,
    showscale: true, hoverongaps: false,
    colorbar: { tickfont: { color: '#e6edf3', size: 10 } }
  }], dl('hm-pe', { xaxis: { title: 'Date' }, yaxis: { title: 'Strike', type: 'category' } }), PLOTLY_CFG);
}

// ── Render all ──
function renderAllCharts() {
  renderChainChart();
  renderOIChart();
  renderIVChart();
  renderMPChart();
  renderPCRChart();
  renderHeatmaps();
}

// ── Init ──
(function() {
  initSlider();
  updateInsights();
  renderAllCharts();
})();
</script>
</body>
</html>"""
    return html


# ─────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────

def main():
    print("Loading data from", DB_PATH, "...")
    chain, iv_data, mp, pcr, instruments = load_data(DB_PATH)

    if not instruments:
        print("No data found in options_data.db. Run options_pipeline.py first.")
        return

    print("Instruments found:", instruments)

    all_data = {}
    for inst in instruments:
        print("  Building charts for", inst, "...")
        all_data[inst] = build_instrument_data(inst, chain, iv_data, mp, pcr)

    print_insights(instruments, all_data)

    html = build_html(instruments, all_data)

    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)

    print("\n✅  Dashboard saved → " + OUTPUT_HTML)

    abs_path = os.path.abspath(OUTPUT_HTML)
    webbrowser.open("file://" + abs_path.replace("\\", "/"))
    print("   Opened in browser.")


if __name__ == "__main__":
    main()