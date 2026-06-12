import sqlite3, pandas as pd
conn = sqlite3.connect("options_data.db")
df = pd.read_sql("""
    SELECT instrument, COUNT(*) as rows, 
           MIN(date) as from_date, 
           MAX(date) as to_date
    FROM options_chain 
    GROUP BY instrument
""", conn)
print(df)
conn.close()