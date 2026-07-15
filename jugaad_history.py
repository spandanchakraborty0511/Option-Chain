import jugaad_data.nse as nse_module
from datetime import date
import pandas as pd
import io
import os
import time

def fetch_bhavcopy_fo_with_retry(d, max_retries=4, wait_seconds=3):
    for attempt in range(1, max_retries + 1):
        try:
            raw = nse_module.bhavcopy_fo_raw(d)
            return raw
        except Exception as e:
            print(f"  Attempt {attempt}/{max_retries} failed for {d}: {e}")
            if attempt < max_retries:
                time.sleep(wait_seconds)
            else:
                print(f"  Giving up on {d} after {max_retries} attempts")
                return None

root_dir = r"D:\nifty_intraday"
sample_day = os.path.join(root_dir, "2018", "AUG", "29AUG", "29AUG")
all_files = os.listdir(sample_day)
stock_symbols = set(f.replace(".txt", "") for f in all_files if not f.endswith("_F1.txt") and f != "NIFTY.txt")

test_dates = [date(2013, 6, 27), date(2015, 6, 25), date(2017, 6, 29), date(2019, 6, 27)]

matched_any_year = set()
for d in test_dates:
    raw = fetch_bhavcopy_fo_with_retry(d)
    if raw is None:
        continue
    df = pd.read_csv(io.StringIO(raw))
    opt = df[df['INSTRUMENT'] == 'OPTSTK']
    matched = opt[opt['SYMBOL'].isin(stock_symbols)]
    matched_any_year.update(matched['SYMBOL'].unique())
    print(f"{d}: OK - {opt['SYMBOL'].nunique()} symbols")
    time.sleep(1)  # be polite between successful calls too

unmatched = stock_symbols - matched_any_year
print(f"\nTotal stock symbols: {len(stock_symbols)}")
print(f"Matched: {len(matched_any_year)}")
print(f"\nUnmatched ({len(unmatched)}):")
print(sorted(unmatched))