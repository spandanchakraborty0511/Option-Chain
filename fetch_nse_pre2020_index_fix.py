import sqlite3
import pandas as pd

conn = sqlite3.connect(r"D:\iisc\project\options_data.db")

# Check yearly row counts - should be ~245-252 per year, evenly spread
yearly = pd.read_sql("""
    SELECT symbol, instrument_type, date/10000 as year, COUNT(*) as cnt
    FROM pre2020_price_history
    WHERE symbol IN ('NIFTY', 'BANKNIFTY')
    GROUP BY symbol, instrument_type, year
    ORDER BY symbol, instrument_type, year;
""", conn)
print("Yearly breakdown:")
print(yearly.to_string())

# Check for duplicate dates (should be exactly 1 row per date per symbol per instrument_type)
dupes = pd.read_sql("""
    SELECT symbol, instrument_type, date, COUNT(*) as cnt
    FROM pre2020_price_history
    WHERE symbol IN ('NIFTY', 'BANKNIFTY')
    GROUP BY symbol, instrument_type, date
    HAVING COUNT(*) > 1;
""", conn)
print("\nDuplicate date entries (should be empty):")
print(dupes)

# Check the actual close price trend makes sense - NIFTY should show
# roughly 5000 in 2012 rising to ~12000 by 2019, not random jumps
trend = pd.read_sql("""
    SELECT date, close FROM pre2020_price_history
    WHERE symbol = 'NIFTY' AND instrument_type = 'SPOT'
    AND date IN (20120103, 20140102, 20160104, 20180102, 20191231)
    ORDER BY date;
""", conn)
print("\nNIFTY SPOT price trend sanity check:")
print(trend)

conn.close()