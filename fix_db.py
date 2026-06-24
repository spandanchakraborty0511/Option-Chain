import sqlite3
DB_PATH = "options_data.db"
conn = sqlite3.connect(DB_PATH)

# Check existing columns
cols = [r[1] for r in conn.execute("PRAGMA table_info(options_chain)").fetchall()]
print("Existing columns:", cols)

# Add any missing columns
needed = {
    "symbol":           "TEXT",
    "expiry":           "TEXT",
    "strike":           "REAL",
    "option_type":      "TEXT",
    "open":             "REAL",
    "high":             "REAL",
    "low":              "REAL",
    "close":            "REAL",
    "last":             "REAL",
    "underlying_close": "REAL",
    "volume":           "INTEGER",
    "oi":               "INTEGER",
    "oi_change":        "INTEGER",
    "turnover":         "REAL",
}
for col, dtype in needed.items():
    if col not in cols:
        conn.execute(f"ALTER TABLE options_chain ADD COLUMN {col} {dtype}")
        print(f"  Added column: {col}")

conn.commit()
print("Done — table is ready.")
conn.close()