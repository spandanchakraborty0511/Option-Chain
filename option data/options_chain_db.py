import sqlite3
from datetime import datetime
from nse import NSE

# Initialize the client (Enabling server mode handles cloud/datacenter IP tracking safely)
nse = NSE(download_folder="./nse_downloads", server=False)
DB_NAME = "options_chain_vault.db"

def build_schema():
    """Builds an optimized schema structured specifically for Options Chains."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # Enable foreign key support in SQLite
    cursor.execute("PRAGMA foreign_keys = ON;")
    
    # Table 1: High-level market state at the exact capture instance
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS OPTION_CHAIN_SNAPSHOTS (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            underlying_index TEXT NOT NULL,
            underlying_value REAL
        )
    ''')
    
    # Table 2: Deep Options matrix tracking contract metrics side-by-side
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS OPTION_STRIKE_DATA (
            data_id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id INTEGER,
            expiry_date TEXT NOT NULL,
            strike_price REAL NOT NULL,
            ce_oi INTEGER,
            ce_change_in_oi INTEGER,
            ce_ltp REAL,
            ce_iv REAL,
            pe_oi INTEGER,
            pe_change_in_oi INTEGER,
            pe_ltp REAL,
            pe_iv REAL,
            FOREIGN KEY (snapshot_id) REFERENCES OPTION_CHAIN_SNAPSHOTS(snapshot_id) ON DELETE CASCADE
        )
    ''')
    
    conn.commit()
    conn.close()

def log_options_chain(symbol="NIFTY"):
    """Pulls the nested options chain and maps it into structural rows."""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Connecting to NSE for {symbol} chain...")
    
    try:
        with nse:
            chain_packet = nse.optionChain(symbol=symbol)
    except Exception as e:
        print(f"Network processing failed: {e}")
        return

    if not chain_packet or 'records' not in chain_packet:
        print("Data layer isolated or unavailable. Aborting write sequence.")
        return

    records_layer = chain_packet['records']
    market_timestamp = records_layer.get('timestamp', datetime.now().strftime("%d-%b-%Y %H:%M:%S"))
    spot_price = records_layer.get('underlyingValue', 0.0)
    raw_strikes = records_layer.get('data', [])

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    try:
        # Insert general snapshot block
        cursor.execute('''
            INSERT INTO OPTION_CHAIN_SNAPSHOTS (timestamp, underlying_index, underlying_value)
            VALUES (?, ?, ?)
        ''', (market_timestamp, symbol, spot_price))
        
        current_snapshot_id = cursor.lastrowid
        parsed_rows = []

        # Iterate through the matrix structure
        for row in raw_strikes:
            strike = row.get('strikePrice')
            expiry = row.get('expiryDate')
            
            # --- FIX: Skip empty summary rows or rows without an expiry date ---
            if not expiry:
                continue
            
            # Map Call metrics securely
            ce_side = row.get('CE', {}) or {}
            c_oi = ce_side.get('openInterest', 0)
            c_chg_oi = ce_side.get('changeinOpenInterest', 0)
            c_ltp = ce_side.get('lastPrice', 0.0)
            c_iv = ce_side.get('impliedVolatility', 0.0)

            # Map Put metrics securely
            pe_side = row.get('PE', {}) or {}
            p_oi = pe_side.get('openInterest', 0)
            p_chg_oi = pe_side.get('changeinOpenInterest', 0)
            p_ltp = pe_side.get('lastPrice', 0.0)
            p_iv = pe_side.get('impliedVolatility', 0.0)

            parsed_rows.append((
                current_snapshot_id, expiry, strike,
                c_oi, c_chg_oi, c_ltp, c_iv,
                p_oi, p_chg_oi, p_ltp, p_iv
            ))

        # Batch insert for rapid disk writing
        cursor.executemany('''
            INSERT INTO OPTION_STRIKE_DATA (
                snapshot_id, expiry_date, strike_price, 
                ce_oi, ce_change_in_oi, ce_ltp, ce_iv,
                pe_oi, pe_change_in_oi, pe_ltp, pe_iv
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', parsed_rows)

        conn.commit()
        print(f"Successfully committed Snapshot #{current_snapshot_id} | logged {len(parsed_rows)} unique derivative strike points.")
        
    except sqlite3.Error as err:
        print(f"Database transaction failure: {err}")
        conn.rollback()
    finally:
        conn.close()
if __name__ == "__main__":
    build_schema()
    log_options_chain(symbol="NIFTY")