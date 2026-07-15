import os
import re
import sqlite3
import time

root_dir = r"D:\nifty_intraday"
db_path = r"D:\iisc\project\options_data.db"

OPTION_RE = re.compile(r"^([A-Za-z]+)(\d{2}[A-Z]{3})([\d.]+)(CE|PE)$", re.IGNORECASE)

def is_option_filename(basename):
    return OPTION_RE.match(basename)

def classify_symbol(symbol):
    s = symbol.upper()
    if s == "INDIAVIX":
        return "INDEX_VIX"
    if any(s.startswith(c) for c in ("CRUDEOIL", "GOLD")) and s.endswith("_F1"):
        return "COMMODITY_FUTURES_1"
    if any(s.startswith(c) for c in ("CRUDEOIL", "GOLD")) and s.endswith("_F2"):
        return "COMMODITY_FUTURES_2"
    if re.match(r"^(EUR|USD|GBP|JPY)INR", s):
        return "CURRENCY_FUTURES"
    if s.endswith("_F2"):
        return "FUTURES_2"
    if s.endswith("_F1"):
        return "FUTURES_1"
    return "SPOT"

def parse_line(line, is_option, filename_symbol=None):
    parts = line.strip().split(",")
    if is_option:
        if len(parts) < 6 or not parts[0].isdigit():
            return None
        date_str, time_str = parts[0], parts[1]
        o, h, l, c = parts[2:6]
        volume = parts[6] if len(parts) > 6 else None
        oi = parts[7] if len(parts) > 7 else None
        symbol = filename_symbol
        instrument_type = "OPTIONS"
    else:
        if len(parts) < 7 or not parts[1].isdigit():
            return None
        symbol, date_str, time_str = parts[0], parts[1], parts[2]
        o, h, l, c = parts[3:7]
        volume = parts[7] if len(parts) > 7 else None
        oi = parts[8] if len(parts) > 8 else None
        instrument_type = classify_symbol(symbol)

    try:
        return (
            int(date_str), time_str, symbol, instrument_type,
            float(o), float(h), float(l), float(c),
            float(volume) if volume not in (None, "") else None,
            float(oi) if oi not in (None, "") else None,
        )
    except ValueError:
        return None  # malformed line, skip rather than crash the whole run

def build_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pre2020_price_history (
            date INTEGER,
            time TEXT,
            symbol TEXT,
            instrument_type TEXT,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL,
            open_interest REAL,
            source_file TEXT,
            UNIQUE(date, time, symbol, instrument_type)
        )
    """)
    conn.commit()

def main(subset_years=None):
    print("Script starting...", flush=True)
    conn = sqlite3.connect(db_path)

    # Fresh start: drop and rebuild, since we're replacing the table with
    # a clean 2013-2019 load using the corrected classification logic.
    conn.execute("DROP TABLE IF EXISTS pre2020_price_history")
    build_db(conn)

    file_count = 0
    attempted_rows = 0
    inserted_rows = 0
    failed_files = 0
    start = time.time()
    folder_stats = {}  # relative folder path -> [attempted, inserted]

    for dirpath, dirnames, filenames in os.walk(root_dir):
        if ".git" in dirpath:
            continue
        rel = os.path.relpath(dirpath, root_dir)
        top = rel.split(os.sep)[0] if rel != "." else "ROOT"

        if subset_years and top not in subset_years:
            continue

        data_files = [f for f in filenames if f.lower().endswith((".txt", ".csv"))]
        if not data_files:
            continue

        key = rel if rel != "." else "ROOT"

        batch = []
        for fname in data_files:
            file_count += 1
            basename = os.path.splitext(fname)[0]
            opt_match = is_option_filename(basename)
            fpath = os.path.join(dirpath, fname)
            try:
                with open(fpath, "r", errors="replace") as f:
                    for line in f:
                        if not line.strip():
                            continue
                        row = parse_line(
                            line, bool(opt_match),
                            filename_symbol=basename if opt_match else None
                        )
                        if row:
                            batch.append(row + (fname,))
            except Exception as e:
                failed_files += 1
                print(f"  Failed to read {fpath}: {e}", flush=True)
                continue

            if len(batch) >= 20000:
                attempted_rows += len(batch)
                cur = conn.executemany(
                    """INSERT OR IGNORE INTO pre2020_price_history
                       (date,time,symbol,instrument_type,open,high,low,close,volume,open_interest,source_file)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    batch
                )
                inserted_this_batch = cur.rowcount if cur.rowcount != -1 else 0
                inserted_rows += inserted_this_batch
                a, i = folder_stats.get(key, (0, 0))
                folder_stats[key] = (a + len(batch), i + inserted_this_batch)
                batch = []
                conn.commit()

            if file_count % 2000 == 0:
                elapsed = time.time() - start
                print(f"Processed {file_count} files ({failed_files} failed) - {elapsed:.0f}s elapsed", flush=True)

        if batch:
            attempted_rows += len(batch)
            cur = conn.executemany(
                """INSERT OR IGNORE INTO pre2020_price_history
                   (date,time,symbol,instrument_type,open,high,low,close,volume,open_interest,source_file)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                batch
            )
            inserted_this_batch = cur.rowcount if cur.rowcount != -1 else 0
            inserted_rows += inserted_this_batch
            a, i = folder_stats.get(key, (0, 0))
            folder_stats[key] = (a + len(batch), i + inserted_this_batch)
            conn.commit()

    elapsed = time.time() - start
    print(f"\nDone. {file_count} files processed, {failed_files} failed, {elapsed:.0f}s elapsed", flush=True)
    print(f"Attempted rows: {attempted_rows}, actually inserted (post-dedup): {inserted_rows}", flush=True)
    print("\nPer-folder attempted vs inserted (fixed accounting):")
    for k, (a, i) in sorted(folder_stats.items()):
        dup_pct = 100 * (1 - i / a) if a else 0
        print(f"  {k}: attempted={a}, inserted={i}, duplicate_rate={dup_pct:.1f}%")

    conn.close()

if __name__ == "__main__":
    # 2013-2019: skips 2012, whose folder structure (JAN_OCT_2012, NOV_2012,
    # DEC2012, no plain month folders) was the messiest we found and isn't
    # worth the added complexity for one extra year.
    main(subset_years=["2013", "2014", "2015", "2016", "2017", "2018", "2019"])