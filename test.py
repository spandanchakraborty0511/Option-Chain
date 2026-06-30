import sqlite3
conn = sqlite3.connect(r"D:\iisc\project\sp500_data.db")
print("Rows:", conn.execute("SELECT COUNT(*) FROM sp500_options").fetchone())
print("Range:", conn.execute("SELECT MIN(date), MAX(date) FROM sp500_options").fetchone())
print("Years:", conn.execute("SELECT strftime('%Y', date), COUNT(*) FROM sp500_options GROUP BY strftime('%Y', date) ORDER BY 1").fetchall())
conn.close()