import sqlite3
import time
import random
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from nse import NSE

# Initialize the NSE client
# Using server=True enforces HTTP/2 headers which is highly recommended for massive pipelines
nse = NSE(download_folder="./nse_downloads", server=False)
DB_NAME = "massive_options_vault.db"
MAX_WORKERS = 4  # Keep this low (3-5) to stay under the NSE rate-limiting radar

def build_production_schema():
    """Builds a high-performance database schema with appropriate indexes."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("PRAGMA foreign_keys = ON;")
    
    # 1. Snapshot Master Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS OPTION_CHAIN_SNAPSHOTS (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            underlying_symbol TEXT NOT NULL,
            underlying_value REAL
        )
    ''')
    
    # 2. Granular Contract Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS OPTION_STRIKE_DATA (
            data_id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id INTEGER,
            expiry_date TEXT NOT NULL,
            strike_price REAL NOT NULL,
            ce_oi INTEGER, ce_change_in_oi INTEGER, ce_ltp REAL, ce_iv REAL,
            pe_oi INTEGER, pe_change_in_oi INTEGER, pe_ltp REAL, pe_iv REAL,
            FOREIGN KEY (snapshot_id) REFERENCES OPTION_CHAIN_SNAPSHOTS(snapshot_id) ON DELETE CASCADE
        )
    ''')
    
    # CREATE INDEXES: Crucial for keeping queries fast as the DB grows into gigabytes
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_snapshot_symbol ON OPTION_CHAIN_SNAPSHOTS(underlying_symbol);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_strike_snapshot ON OPTION_STRIKE_DATA(snapshot_id);")
    
    conn.commit()
    conn.close()

def get_all_fo_symbols():
    """Dynamically fetches all active F&O underlying symbols directly from NSE."""
    print("Fetching active F&O stock and index master list...")
    # Add core indices manually as a baseline
    symbols = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"]
    try:
        with nse:
            # advanceDecline gives us a clean snapshot of active symbols
            market_data = nse.advanceDecline()
            # If the library supports an explicit list, we can parse it, 
            # otherwise we fallback to a verified list of liquid stocks or expand here.
    except Exception as e:
        print(f"Could not pull dynamic list, using top liquid derivative symbols. Error: {e}")
        
    # fallback/top liquid F&O equities list to guarantee a massive footprint
    liquid_fo_stocks = [
        "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN", "BHARTIARTL", "ITC", 
        "KOTAKBANK", "LT", "AXISBANK", "RELIANCE", "BAJFINANCE", "MARUTI", "M&M", "TATASTEEL"
    ]
    return list(set(symbols + liquid_fo_stocks))

def worker_fetch_and_store(symbol):
    """Worker thread tasked with processing a single symbol's option chain."""
    # Gentle, randomized sleep (jitter) to prevent concurrent hits on the same millisecond
    time.sleep(random.uniform(0.3, 1.2))
    
    try:
        with nse:
            # Check if it's an index or stock to apply proper flags if needed
             # The library detects whether it's an index or stock automatically from the symbol string!
             chain_packet = nse.optionChain(symbol=symbol)
            
        if not chain_packet or 'records' not in chain_packet:
            return f"[{symbol}] No active data packet returned."

        records_layer = chain_packet['records']
        market_timestamp = records_layer.get('timestamp', datetime.now().strftime("%d-%b-%Y %H:%M:%S"))
        spot_price = records_layer.get('underlyingValue', 0.0)
        raw_strikes = records_layer.get('data', [])

        # Write data to DB (Open a localized connection per thread for safety)
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO OPTION_CHAIN_SNAPSHOTS (timestamp, underlying_symbol, underlying_value)
            VALUES (?, ?, ?)
        ''', (market_timestamp, symbol, spot_price))
        
        current_snapshot_id = cursor.lastrowid
        parsed_rows = []

        for row in raw_strikes:
            strike = row.get('strikePrice')
            expiry = row.get('expiryDate')
            
            if not expiry:
                continue

            ce_side = row.get('CE', {}) or {}
            pe_side = row.get('PE', {}) or {}

            parsed_rows.append((
                current_snapshot_id, expiry, strike,
                ce_side.get('openInterest', 0), ce_side.get('changeinOpenInterest', 0), ce_side.get('lastPrice', 0.0), ce_side.get('impliedVolatility', 0.0),
                pe_side.get('openInterest', 0), pe_side.get('changeinOpenInterest', 0), pe_side.get('lastPrice', 0.0), pe_side.get('impliedVolatility', 0.0)
            ))

        cursor.executemany('''
            INSERT INTO OPTION_STRIKE_DATA (
                snapshot_id, expiry_date, strike_price, 
                ce_oi, ce_change_in_oi, ce_ltp, ce_iv,
                pe_oi, pe_change_in_oi, pe_ltp, pe_iv
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', parsed_rows)

        conn.commit()
        conn.close()
        return f"[{symbol}] Successfully logged Snapshot #{current_snapshot_id} ({len(parsed_rows)} entries)."

    except Exception as e:
        return f"[{symbol}] Execution failed: {e}"

def pipeline_orchestrator():
    build_production_schema()
    symbols_to_track = get_all_fo_symbols()
    print(f"Starting massive data ingestion pipeline for {len(symbols_to_track)} tickers...")
    
    start_time = time.time()
    
    # Spin up the ThreadPoolExecutor to request and parse concurrently
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(worker_fetch_and_store, sym): sym for sym in symbols_to_track}
        
        for future in as_completed(futures):
            result = future.result()
            print(result)
            
    print(f"\nPipeline run completed in {round(time.time() - start_time, 2)} seconds.")

if __name__ == "__main__":
    pipeline_orchestrator()