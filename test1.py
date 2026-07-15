import pandas as pd
import re

catalog = pd.read_csv(r"D:\nifty_intraday\pre2020_symbol_catalog.csv")

# Fixed pattern: allows optional decimal point in the strike (e.g. 67.5CE, 152.5PE)
option_pattern = re.compile(r"\d{2}[A-Z]{3}\d+(\.\d+)?(CE|PE)$")

def is_real_stock_symbol(sym):
    return not option_pattern.search(sym)

spot_only = catalog[catalog["instrument_type"] == "SPOT"].copy()
spot_only["is_real"] = spot_only["symbol"].apply(is_real_stock_symbol)

real_stocks = spot_only[spot_only["is_real"]].copy()
print("After regex fix:", len(real_stocks), "SPOT symbols")

# Belt-and-suspenders: also apply a frequency floor, since the option dump
# proved unpredictable in naming - anything appearing fewer than 100 times
# across 8 years definitely isn't a continuously-traded stock we care about
real_stocks_filtered = real_stocks[real_stocks["file_count"] >= 100]
print("After frequency floor (>=100 files):", len(real_stocks_filtered), "symbols")

print("\nAny remaining low-frequency stragglers between the two filters:")
stragglers = real_stocks[(real_stocks["file_count"] < 100)]
print(stragglers[["symbol", "file_count"]].to_string())

real_stocks_filtered[["symbol", "instrument_type", "file_count"]].to_csv(
    r"D:\nifty_intraday\pre2020_symbol_catalog_clean.csv", index=False
)
print(f"\nSaved {len(real_stocks_filtered)} clean symbols to pre2020_symbol_catalog_clean.csv")