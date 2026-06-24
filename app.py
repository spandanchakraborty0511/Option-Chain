"""
app.py  —  Options Chain Web Dashboard
────────────────────────────────────────────────────────────
A simple Flask web app.
Open http://localhost:5000 in your browser.

One-time setup:
    pip install flask kiteconnect pandas numpy scipy

Run:
    python app.py
"""

import os
import re
import json
import time
import sqlite3
import datetime
import threading
import webbrowser
import numpy as np
import pandas as pd

from flask import Flask, render_template_string, jsonify, request, redirect, session
from kiteconnect import KiteConnect
from scipy.optimize import brentq
from scipy.stats import norm

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────

API_KEY    = "ujqgohskrn96s6n3"
API_SECRET = "4chfbivdma7z6n59zyuxgzofu9tvq9zb"

INSTRUMENTS  = ["NIFTY", "BANKNIFTY"]
STRIKE_RANGE = 10
DAYS_BACK    = 60
RISK_FREE    = 0.065
DB_PATH      = "options_data.db"

STRIKE_STEPS = {"NIFTY": 50, "BANKNIFTY": 100}
INDEX_TOKENS = {"NIFTY": 256265, "BANKNIFTY": 260105}

app = Flask(__name__)
app.secret_key = "options_app_secret_123"

# global state
fetch_status = {"running": False, "log": [], "done": False, "error": None}
kite_instance = None

# ─────────────────────────────────────────────
#  DATABASE
# ─────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
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
    conn.close()

# ─────────────────────────────────────────────
#  BLACK-SCHOLES IV
# ─────────────────────────────────────────────

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

# ─────────────────────────────────────────────
#  FETCH LOGIC (runs in background thread)
# ─────────────────────────────────────────────

def log_msg(msg):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    fetch_status["log"].append("[" + ts + "] " + msg)
    print(msg)

def fetch_and_store(kite):
    global fetch_status
    fetch_status["running"] = True
    fetch_status["done"]    = False
    fetch_status["error"]   = None
    fetch_status["log"]     = []

    try:
        to_date   = datetime.date.today() - datetime.timedelta(days=1)
        from_date = to_date - datetime.timedelta(days=DAYS_BACK)
        log_msg("Fetching: " + str(from_date) + " → " + str(to_date))

        conn = sqlite3.connect(DB_PATH)
        instruments_df = pd.DataFrame(kite.instruments("NFO"))

        for inst in INSTRUMENTS:
            log_msg("─── " + inst + " ───")

            opts = instruments_df[
                (instruments_df["name"] == inst) &
                (instruments_df["instrument_type"].isin(["CE", "PE"]))
            ].copy()
            opts["expiry"] = pd.to_datetime(opts["expiry"])
            expiry = opts["expiry"].min()
            opts   = opts[opts["expiry"] == expiry]
            log_msg("Expiry: " + str(expiry.date()) + " | Contracts: " + str(len(opts)))

            spot_hist = kite.historical_data(INDEX_TOKENS[inst], from_date, to_date, "day")
            spot_df   = pd.DataFrame(spot_hist)
            spot_df["date"] = pd.to_datetime(spot_df["date"]).dt.tz_localize(None)
            spot_series  = spot_df.set_index("date")["close"].to_dict()
            latest_spot  = float(spot_df["close"].iloc[-1]) if len(spot_df) else 0
            log_msg("Latest spot: " + str(latest_spot))

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
                    log_msg("[" + str(idx) + "/" + str(total) + "] " + sym)
                except Exception as e:
                    log_msg("Skipped " + sym + ": " + str(e))
                time.sleep(0.35)

            if not records:
                log_msg("No records for " + inst)
                continue

            df = pd.DataFrame(records)
            df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)

            # raw chain
            rows = []
            for _, r in df.iterrows():
                rows.append((
                    inst, str(expiry.date()), str(r["date"].date()),
                    r["strike"], r["type"],
                    r.get("open"), r.get("high"), r.get("low"), r.get("close"),
                    int(r.get("volume", 0) or 0), int(r.get("oi", 0) or 0),
                ))
            conn.executemany("""
                INSERT OR IGNORE INTO options_chain
                (instrument,expiry,date,strike,type,open,high,low,close,volume,oi)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, rows)
            conn.commit()
            log_msg("Saved " + str(len(rows)) + " rows → options_chain")

            # IV
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
            log_msg("Saved " + str(len(iv_rows)) + " rows → iv_data")

            # max pain
            mp_rows = []
            for date_val, grp in df.groupby("date"):
                strikes = sorted(grp["strike"].unique())
                ce_oi = grp[grp["type"] == "CE"].set_index("strike")["oi"].to_dict()
                pe_oi = grp[grp["type"] == "PE"].set_index("strike")["oi"].to_dict()
                best_strike, best_pain = None, float("inf")
                for ep in strikes:
                    pain  = sum((ep - k) * (v or 0) for k, v in ce_oi.items() if ep > k)
                    pain += sum((k - ep) * (v or 0) for k, v in pe_oi.items() if ep < k)
                    if pain < best_pain:
                        best_pain, best_strike = pain, ep
                mp_rows.append((inst, str(expiry.date()), str(date_val.date()), best_strike))
            conn.executemany("""
                INSERT OR IGNORE INTO max_pain (instrument,expiry,date,max_pain_strike)
                VALUES (?,?,?,?)
            """, mp_rows)
            conn.commit()
            log_msg("Saved " + str(len(mp_rows)) + " rows → max_pain")

            # PCR
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
            log_msg("Saved " + str(len(pcr_rows)) + " rows → pcr")
            log_msg("✅ " + inst + " done!")

        conn.close()
        log_msg("🎉 All done! Database updated.")
        fetch_status["done"] = True

    except Exception as e:
        fetch_status["error"] = str(e)
        log_msg("❌ ERROR: " + str(e))
    finally:
        fetch_status["running"] = False


# ─────────────────────────────────────────────
#  ROUTES
# ─────────────────────────────────────────────

@app.route("/")
def index():
    # check if we have a valid token saved today
    token_today = False
    if os.path.exists("kite_token.json"):
        with open("kite_token.json") as f:
            data = json.load(f)
        if data.get("date") == str(datetime.date.today()):
            token_today = True

    # db stats
    stats = []
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("""
            SELECT instrument, COUNT(*) as rows,
                   MIN(date) as from_date, MAX(date) as to_date
            FROM options_chain GROUP BY instrument
        """).fetchall()
        conn.close()
        stats = [{"inst": r[0], "rows": r[1], "from": r[2], "to": r[3]} for r in rows]
    except Exception:
        pass

    return render_template_string(PAGE_HTML,
        token_today=token_today,
        stats=stats,
        login_url=KiteConnect(api_key=API_KEY).login_url()
    )


@app.route("/kite_callback")
def kite_callback():
    """Zerodha redirects here after login with request_token in URL."""
    global kite_instance
    request_token = request.args.get("request_token")
    if not request_token:
        return "No request_token in URL.", 400

    try:
        kite = KiteConnect(api_key=API_KEY)
        sess = kite.generate_session(request_token, api_secret=API_SECRET)
        kite.set_access_token(sess["access_token"])
        kite_instance = kite

        # save token
        with open("kite_token.json", "w") as f:
            json.dump({"access_token": sess["access_token"],
                       "date": str(datetime.date.today())}, f)

        return redirect("/")
    except Exception as e:
        return "Login failed: " + str(e), 500


@app.route("/fetch", methods=["POST"])
def fetch():
    global kite_instance, fetch_status
    if fetch_status["running"]:
        return jsonify({"ok": False, "msg": "Already running"})

    # try saved token
    if kite_instance is None and os.path.exists("kite_token.json"):
        with open("kite_token.json") as f:
            data = json.load(f)
        if data.get("date") == str(datetime.date.today()):
            kite = KiteConnect(api_key=API_KEY)
            kite.set_access_token(data["access_token"])
            kite_instance = kite

    if kite_instance is None:
        return jsonify({"ok": False, "msg": "Not logged in. Click Login first."})

    thread = threading.Thread(target=fetch_and_store, args=(kite_instance,), daemon=True)
    thread.start()
    return jsonify({"ok": True})


@app.route("/status")
def status():
    return jsonify(fetch_status)


@app.route("/logout")
def logout():
    global kite_instance
    kite_instance = None
    if os.path.exists("kite_token.json"):
        os.remove("kite_token.json")
    return redirect("/")


# ─────────────────────────────────────────────
#  HTML PAGE
# ─────────────────────────────────────────────

PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Options Fetcher</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: #0d1117; color: #e6edf3;
  font-family: 'Segoe UI', system-ui, sans-serif;
  min-height: 100vh;
}
header {
  background: #161b22; border-bottom: 1px solid #30363d;
  padding: 16px 32px; display: flex;
  align-items: center; justify-content: space-between;
}
header h1 { font-size: 1.1rem; font-weight: 600; }
.logout-btn {
  background: none; border: 1px solid #30363d;
  color: #8b949e; padding: 5px 14px; border-radius: 6px;
  cursor: pointer; font-size: .8rem;
  text-decoration: none;
}
.logout-btn:hover { border-color: #f85149; color: #f85149; }

main { max-width: 820px; margin: 40px auto; padding: 0 20px; }

.card {
  background: #161b22; border: 1px solid #30363d;
  border-radius: 10px; padding: 28px; margin-bottom: 20px;
}
.card h2 { font-size: .95rem; font-weight: 600; margin-bottom: 16px; color: #8b949e; letter-spacing: .05em; text-transform: uppercase; }

.status-row {
  display: flex; align-items: center; gap: 10px; margin-bottom: 20px;
}
.dot {
  width: 10px; height: 10px; border-radius: 50%;
  background: #30363d;
}
.dot.green  { background: #3fb950; box-shadow: 0 0 6px #3fb950; }
.dot.yellow { background: #d29922; box-shadow: 0 0 6px #d29922; animation: pulse 1s infinite; }
.dot.red    { background: #f85149; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }

.status-text { font-size: .9rem; }

.btn {
  display: inline-block; padding: 10px 28px;
  border-radius: 8px; border: none; cursor: pointer;
  font-size: .9rem; font-weight: 600;
  text-decoration: none; text-align: center;
  transition: opacity .15s, transform .1s;
}
.btn:hover { opacity: .85; transform: translateY(-1px); }
.btn:active { transform: translateY(0); }
.btn-blue   { background: #1f6feb; color: #fff; }
.btn-green  { background: #238636; color: #fff; }
.btn-gray   { background: #21262d; color: #8b949e; border: 1px solid #30363d; }
.btn:disabled { opacity: .4; cursor: not-allowed; transform: none; }

.btn-row { display: flex; gap: 12px; flex-wrap: wrap; }

.log-box {
  background: #0d1117; border: 1px solid #21262d;
  border-radius: 6px; padding: 14px;
  height: 280px; overflow-y: auto;
  font-family: 'Consolas', monospace; font-size: .78rem;
  line-height: 1.6; color: #8b949e;
  margin-top: 16px;
}
.log-box .ok   { color: #3fb950; }
.log-box .err  { color: #f85149; }
.log-box .info { color: #58a6ff; }

table { width: 100%; border-collapse: collapse; font-size: .85rem; }
th { text-align: left; padding: 8px 12px; color: #8b949e; border-bottom: 1px solid #21262d; font-weight: 500; }
td { padding: 8px 12px; border-bottom: 1px solid #161b22; }
tr:last-child td { border-bottom: none; }
.badge {
  display: inline-block; padding: 2px 10px; border-radius: 12px;
  font-size: .75rem; font-weight: 600;
}
.badge-green { background: rgba(63,185,80,.15); color: #3fb950; }
.badge-blue  { background: rgba(88,166,255,.15); color: #58a6ff; }

.step-row {
  display: flex; align-items: flex-start; gap: 14px; margin-bottom: 14px;
}
.step-num {
  background: #1f6feb; color: #fff;
  width: 24px; height: 24px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  font-size: .75rem; font-weight: 700; flex-shrink: 0; margin-top: 2px;
}
.step-num.done { background: #238636; }
.step-text { font-size: .87rem; line-height: 1.5; color: #c9d1d9; }
.step-text strong { color: #e6edf3; }
</style>
</head>
<body>

<header>
  <h1>⚡ Options Chain Fetcher</h1>
  <a href="/logout" class="logout-btn">Logout</a>
</header>

<main>

  <!-- ── HOW IT WORKS ── -->
  <div class="card">
    <h2>How it works</h2>
    <div class="step-row">
      <div class="step-num {% if token_today %}done{% endif %}">1</div>
      <div class="step-text">
        <strong>Login with Zerodha</strong> — Click the Login button below.
        It opens Kite in this browser. Log in normally (User ID + Password + TOTP from your phone).
        You are redirected back here automatically. <em>Do this once per day.</em>
      </div>
    </div>
    <div class="step-row">
      <div class="step-num">2</div>
      <div class="step-text">
        <strong>Fetch Data</strong> — Click "Fetch Now". The script runs in the background,
        pulls 60 days of NIFTY + BANKNIFTY options data from Kite, computes IV / Max Pain / PCR,
        and saves everything to <code>options_data.db</code>. Watch the live log below.
      </div>
    </div>
    <div class="step-row">
      <div class="step-num">3</div>
      <div class="step-text">
        <strong>View Dashboard</strong> — After fetch completes, run
        <code>python pattern_analysis.py</code> to open the full chart dashboard.
        Or just repeat Step 2 daily to keep data fresh.
      </div>
    </div>
  </div>

  <!-- ── LOGIN / FETCH ── -->
  <div class="card">
    <h2>Controls</h2>

    <div class="status-row">
      <div class="dot {% if token_today %}green{% else %}red{% endif %}" id="login-dot"></div>
      <span class="status-text" id="login-status">
        {% if token_today %}
          ✅ Logged in — token valid for today
        {% else %}
          ⚠️ Not logged in
        {% endif %}
      </span>
    </div>

    <div class="btn-row">
      {% if not token_today %}
      <a href="{{ login_url }}" class="btn btn-blue">🔐 Login with Zerodha</a>
      {% else %}
      <button class="btn btn-green" id="fetch-btn" onclick="startFetch()">⬇️ Fetch Data Now</button>
      <a href="{{ login_url }}" class="btn btn-gray" style="font-size:.8rem;padding:10px 16px;">🔄 Re-login</a>
      {% endif %}
    </div>

    <div class="log-box" id="log-box">
      <span style="color:#30363d;">Logs will appear here when fetch starts...</span>
    </div>
  </div>

  <!-- ── DB STATS ── -->
  <div class="card">
    <h2>Database Status — options_data.db</h2>
    {% if stats %}
    <table>
      <tr>
        <th>Instrument</th>
        <th>Rows</th>
        <th>From</th>
        <th>To</th>
        <th>Status</th>
      </tr>
      {% for s in stats %}
      <tr>
        <td><strong>{{ s.inst }}</strong></td>
        <td>{{ s.rows }}</td>
        <td>{{ s.from }}</td>
        <td>{{ s.to }}</td>
        <td><span class="badge badge-green">✓ Data available</span></td>
      </tr>
      {% endfor %}
    </table>
    {% else %}
    <p style="color:#8b949e;font-size:.87rem;">No data yet. Login and click Fetch Data to populate the database.</p>
    {% endif %}
  </div>

</main>

<script>
var polling = null;

function startFetch() {
  document.getElementById('fetch-btn').disabled = true;
  document.getElementById('fetch-btn').textContent = '⏳ Fetching...';
  document.getElementById('log-box').innerHTML = '';

  fetch('/fetch', { method: 'POST' })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (!d.ok) {
        appendLog('❌ ' + d.msg, 'err');
        document.getElementById('fetch-btn').disabled = false;
        document.getElementById('fetch-btn').textContent = '⬇️ Fetch Data Now';
        return;
      }
      polling = setInterval(pollStatus, 1500);
    });
}

function pollStatus() {
  fetch('/status')
    .then(function(r) { return r.json(); })
    .then(function(d) {
      var box = document.getElementById('log-box');
      box.innerHTML = '';
      d.log.forEach(function(line) {
        var cls = '';
        if (line.indexOf('✅') >= 0 || line.indexOf('🎉') >= 0 || line.indexOf('Saved') >= 0) cls = 'ok';
        else if (line.indexOf('❌') >= 0 || line.indexOf('ERROR') >= 0) cls = 'err';
        else if (line.indexOf('───') >= 0 || line.indexOf('Expiry') >= 0) cls = 'info';
        appendLog(line, cls);
      });

      if (!d.running) {
        clearInterval(polling);
        polling = null;
        var btn = document.getElementById('fetch-btn');
        if (btn) {
          btn.disabled = false;
          btn.textContent = '⬇️ Fetch Data Now';
        }
        if (d.done) {
          setTimeout(function() { location.reload(); }, 2000);
        }
      }
    });
}

function appendLog(msg, cls) {
  var box = document.getElementById('log-box');
  var line = document.createElement('div');
  if (cls) line.className = cls;
  line.textContent = msg;
  box.appendChild(line);
  box.scrollTop = box.scrollHeight;
}
</script>
</body>
</html>"""


# ─────────────────────────────────────────────
#  STARTUP
# ─────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    print("\n" + "=" * 50)
    print("  Options Fetcher running!")
    print("  Open this in your browser:")
    print("  http://localhost:5000")
    print("=" * 50 + "\n")
    webbrowser.open("http://localhost:5000")
    app.run(port=5000, debug=False)