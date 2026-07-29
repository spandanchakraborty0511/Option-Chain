import pandas as pd
import zipfile, os

# Full expected ticker list from the zip snapshots
CACHE_DIR = r"D:\iisc\project\option data\option_chain_cache"
all_tickers = set()
for fname in sorted(os.listdir(CACHE_DIR)):
    if not fname.endswith(".zip"):
        continue
    with zipfile.ZipFile(os.path.join(CACHE_DIR, fname), 'r') as z:
        for n in z.namelist():
            if n.endswith(".json"):
                all_tickers.add(os.path.basename(n).replace(".json", ""))

master = pd.read_csv(r"D:\iisc\project\option data\us_underlying_close_MASTER.csv")
covered = set(master['ticker'].unique())

missing = sorted(all_tickers - covered)

print(f"Total expected tickers: {len(all_tickers)}")
print(f"Covered in master: {len(covered)}")
print(f"Still missing: {len(missing)}")
print(missing)