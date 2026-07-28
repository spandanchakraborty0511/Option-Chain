import requests
import pandas as pd
import time

API_KEY = "b84cb36965943c3205baeeec615e70632b9b4b6e"

MISSING = ['AL', 'BK', 'CMA', 'CTRA', 'DAY', 'EXAS', 'FI', 'GES', 'GMRE', 'HBI',
           'HOLX', 'IPG', 'IRBT', 'K', 'LAZR', 'MASI', 'MMC', 'PSTG', 'SCS', 'SEE',
           'SMLR', 'SPR', 'TTFNF', 'VSCO']

headers = {"Content-Type": "application/json"}
results = []
still_failed = []

for ticker in MISSING:
    try:
        url = f"https://api.tiingo.com/tiingo/daily/{ticker}/prices"
        params = {
            "startDate": "2025-11-01",
            "endDate": "2025-12-24",
            "token": API_KEY
        }
        resp = requests.get(url, headers=headers, params=params, timeout=15)

        if resp.status_code != 200:
            still_failed.append(ticker)
            print(f"[{ticker}] FAILED: HTTP {resp.status_code} - {resp.text[:150]}")
            continue

        data = resp.json()
        if not data:
            still_failed.append(ticker)
            print(f"[{ticker}] EMPTY response")
            continue

        for row in data:
            results.append({
                "ticker": ticker,
                "date": row["date"][:10],
                "close": round(float(row["close"]), 4)
            })
        print(f"[{ticker}] OK - {len(data)} rows")

    except Exception as e:
        still_failed.append(ticker)
        print(f"[{ticker}] FAILED: {e}")

    time.sleep(1.5)

retry_df = pd.DataFrame(results)
retry_df.to_csv(r"D:\iisc\project\option data\us_underlying_close_lookup_retry5.csv", index=False)
print(f"\nRecovered: {retry_df['ticker'].nunique() if not retry_df.empty else 0} tickers")
print(f"Still failed: {still_failed}")