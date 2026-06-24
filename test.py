import sqlite3
conn = sqlite3.connect(r"D:\iisc\project\options_data.db")

print("nse_options:", conn.execute("SELECT COUNT(*), MIN(date), MAX(date) FROM nse_options").fetchone())
print("options_chain:", conn.execute("SELECT COUNT(*), MIN(date), MAX(date) FROM options_chain").fetchone())

conn.close()