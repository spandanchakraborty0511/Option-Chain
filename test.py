import pandas as pd

files = [
    r"D:\iisc\project\option data\us_underlying_close_lookup.csv",
    r"D:\iisc\project\option data\us_underlying_close_lookup_retry2.csv",
    r"D:\iisc\project\option data\us_underlying_close_lookup_retry5.csv",
]

dfs = []
for f in files:
    try:
        df = pd.read_csv(f)
        print(f"{f}: {len(df)} rows, {df['ticker'].nunique()} tickers")
        dfs.append(df)
    except FileNotFoundError:
        print(f"{f}: NOT FOUND, skipping")

combined = pd.concat(dfs, ignore_index=True)

# BF-B / BRK-B were fetched with hyphens; normalize back to match your JSON filenames (BF.B / BRK.B)
combined['ticker'] = combined['ticker'].replace({'BF-B': 'BF.B', 'BRK-B': 'BRK.B'})

# Remove any duplicate (ticker, date) pairs, keeping the first occurrence
before = len(combined)
combined = combined.drop_duplicates(subset=['ticker', 'date'], keep='first')
after = len(combined)
print(f"\nDropped {before - after} duplicate rows")

combined.to_csv(r"D:\iisc\project\option data\us_underlying_close_MASTER.csv", index=False)

print(f"\nFinal master file: {len(combined)} rows, {combined['ticker'].nunique()} unique tickers")
print(f"Date range: {combined['date'].min()} to {combined['date'].max()}")