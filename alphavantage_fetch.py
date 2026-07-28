import requests
import pandas as pd
import time

API_KEY = "FDNA2QXZYIWFRAG2"  # paste your free key

MISSING = ['AL', 'BK', 'CMA', 'CTRA', 'DAY', 'EXAS', 'FI', 'GES', 'GMRE', 'HBI',
           'HOLX', 'IPG', 'IRBT', 'K', 'LAZR', 'MASI', 'MMC', 'PSTG', 'SCS', 'SEE',
           'SMLR', 'SPR', 'TTFNF', 'VSCO']

results = []
still_failed = []

for ticker in MISSING:
    try:
        url = "https://www.alphavantage.co/query"
        params = {
            "function": "TIME_SERIES_DAILY",
            "symbol": ticker,
            "outputsize": "full",
            "apikey": API_KEY
        }
        resp = requests.get(url, params=params, timeout=20)
        data = resp.json()

        if "Time Series (Daily)" not in data:
            still_failed.append(ticker)
            print(f"[{ticker}] FAILED: {data.get('Note') or data.get('Error Message') or data}")
            continue

        series = data["Time Series (Daily)"]
        count = 0
        for date_str, values in series.items():
            if "2025-11-01" <= date_str <= "2025-12-24":
                results.append({"ticker": ticker, "date": date_str,
                                 "close": round(float(values["4. close"]), 4)})
                count += 1
        print(f"[{ticker}] OK - {count} rows in target window")

    except Exception as e:
        still_failed.append(ticker)
        print(f"[{ticker}] FAILED: {e}")

    time.sleep(13)  # 5 req/min limit = ~12s minimum between calls; 13s for safety margin

retry_df = pd.DataFrame(results)
retry_df.to_csv(r"D:\iisc\project\option data\us_underlying_close_lookup_retry4.csv", index=False)
print(f"\nRecovered: {retry_df['ticker'].nunique() if not retry_df.empty else 0} tickers")
print(f"Still failed: {still_failed}")