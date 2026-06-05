import sqlite3, pandas as pd
conn = sqlite3.connect("options_data.db")
for table in ["options_chain", "iv_data", "max_pain", "pcr"]:
    df = pd.read_sql(f"SELECT * FROM {table} LIMIT 2", conn)
    print(f"\n── {table} ──")
    print(df.to_string(index=False))