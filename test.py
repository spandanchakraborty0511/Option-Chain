import sqlite3
import pandas as pd

conn = sqlite3.connect(r"D:\iisc\project\options_data.db")

# Total distinct symbols with pre-2020 options data
all_symbols = pd.read_sql("""
    SELECT DISTINCT symbol
    FROM nse_options
    WHERE date < '2020-01-01' AND option_type IN ('CE','PE');
""", conn)
print(f"Total distinct symbols (options, pre-2020): {len(all_symbols)}")
print(sorted(all_symbols['symbol'].tolist()))

conn.close()