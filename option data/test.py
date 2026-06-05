import os
import zipfile
import json
import sqlite3
import re
import time
import requests

# --- CONFIGURATION ---
FOLDER_URL = "https://drive.google.com/drive/folders/1a7afPF3k-I0kjA3aybJWR1-rIQTNK_ef"
DB_NAME = "option_chain_database.db"
OUTPUT_DIR = "./option_chain_cache"
COOKIE_FILE = "cookies.txt"
# ---------------------

def extract_folder_id(url):
    match = re.search(r'/folders/([a-zA-Z0-9_-]+)', url)
    if not match:
        raise ValueError("Invalid Google Drive folder URL structure.")
    return match.group(1)

def init_database(db_path):
    """Initializes local SQLite database structure with performance indexing."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS option_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_zip TEXT,
            internal_json TEXT,
            strike_price REAL,
            option_type TEXT,
            open_interest INTEGER,
            volume INTEGER,
            implied_volatility REAL,
            last_traded_price REAL,
            timestamp TEXT,
            raw_payload TEXT
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_strike_ts ON option_data (strike_price, timestamp);')
    conn.commit()
    return conn

def load_cookies_to_session(session, cookie_file_path):
    """Parses a Netscape format cookies.txt file into a requests Session."""
    if not os.path.exists(cookie_file_path):
        return False
    
    print("[+] Extrapolating session credentials from cookies.txt...")
    count = 0
    with open(cookie_file_path, 'r', encoding='utf-8') as f:
        for line in f:
            if line.startswith('#') or not line.strip():
                continue
            parts = line.strip().split('\t')
            if len(parts) >= 7:
                domain, _, path, secure, _, name, value = parts[:7]
                session.cookies.set(name, value, domain=domain, path=path)
                count += 1
    print(f"[+] Loaded {count} authorization tokens into the pipeline session context.")
    return True

def extract_file_manifest(folder_id, session):
    """Scrapes the embedded folder view via authenticated session to isolate target asset arrays."""
    url = f"https://drive.google.com/embeddedfolderview?id={folder_id}"
    print("[+] Mapping folder array map from root resource node...")
    
    response = session.get(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
    if response.status_code != 200:
        print(f"[-] Root directory verification failed. Status code: {response.status_code}")
        return []
        
    # Isolate string layouts matching: ["ID", "YYYY-MM-DD.zip"]
    matches = re.findall(r'\["([a-zA-Z0-9_-]{28,35})",\s*"([^"]+\.zip)"', response.text)
    
    unique_files = []
    seen = set()
    for fid, name in matches:
        if name not in seen:
            seen.add(name)
            unique_files.append({'id': fid, 'name': name})
            
    # Sort chronological items ascending
    unique_files.sort(key=lambda x: x['name'])
    print(f"[+] Isolated {len(unique_files)} discrete target archives inside directory manifest.")
    return unique_files

def download_file_stream(file_id, output_path, session):
    """Streams data arrays using direct confirmation tokens to prevent block alerts."""
    base_url = "https://drive.google.com/uc"
    params = {'export': 'download', 'id': file_id}
    
    # Send primary transaction header query
    response = session.get(base_url, params=params, stream=True)
    
    # Intercept Google Drive's classic large-file virus scan bypass redirection prompt
    confirm_token = None
    for k, v in response.cookies.items():
        if k.startswith('download_warning'):
            confirm_token = v
            break
            
    if not confirm_token:
        match = re.search(r'confirm=([a-zA-Z0-9_-]+)', response.text)
        if match:
            confirm_token = match.group(1)
            
    if confirm_token:
        params['confirm'] = confirm_token
        response = session.get(base_url, params=params, stream=True)
        
    if response.status_code != 200:
        raise RuntimeError(f"Server rejected binary stream pipeline execution with code: {response.status_code}")
        
    # Pipe download block data straight onto disk
    with open(output_path, 'wb') as f:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)

def parse_and_insert(json_data, zip_name, json_name, conn):
    """Decouples JSON payload structures into the optimization schema rows."""
    cursor = conn.cursor()
    try:
        if isinstance(json_data, list):
            records = json_data
        elif "records" in json_data and "data" in json_data["records"]:
            records = json_data["records"]["data"]
        else:
            records = [json_data]

        for item in records:
            strike = item.get('strikePrice') or item.get('strike_price')
            opt_type = item.get('type') or item.get('option_type')
            oi = item.get('openInterest') or item.get('OI')
            vol = item.get('volume') or item.get('totalTradedVolume')
            iv = item.get('impliedVolatility') or item.get('IV')
            ltp = item.get('lastPrice') or item.get('ltp')
            ts = item.get('timestamp') or json_data.get('timestamp')

            cursor.execute('''
                INSERT INTO option_data (
                    source_zip, internal_json, strike_price, option_type, 
                    open_interest, volume, implied_volatility, last_traded_price, timestamp, raw_payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (zip_name, json_name, strike, opt_type, oi, vol, iv, ltp, ts, json.dumps(item)))
    except Exception:
        pass

def pipeline():
    folder_id = extract_folder_id(FOLDER_URL)
    conn = init_database(DB_NAME)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    session = requests.Session()
    if not load_cookies_to_session(session, COOKIE_FILE):
        print("[-] Aborting: Valid authenticated 'cookies.txt' is mandatory in this mode.")
        return
        
    files_to_download = extract_file_manifest(folder_id, session)
    if not files_to_download:
        print("[-] Zero valid files resolved. Check if the shared GDrive folder links match.")
        return
        
    downloaded_paths = []
    
    print("\n[+] Processing item downloading block with protective pacing loops...")
    for idx, f in enumerate(files_to_download, 1):
        target_path = os.path.join(OUTPUT_DIR, f['name'])
        downloaded_paths.append(target_path)
        
        # Avoid processing items already written to disk
        if os.path.exists(target_path) and os.path.getsize(target_path) > 1024 * 1024:
            print(f" -> [{idx}/{len(files_to_download)}] Present on disk: {f['name']} (Skipping download)")
            continue
            
        print(f" -> [{idx}/{len(files_to_download)}] Downloading file data chunk: {f['name']}")
        retries = 0
        while retries < 3:
            try:
                download_file_stream(f['id'], target_path, session)
                time.sleep(5)  # Safe 5-second sleep to stay completely under the anti-bot radar
                break
            except Exception as err:
                retries += 1
                wait_time = retries * 60
                print(f"    [!] Error pulling stream. Backing off for {wait_time} seconds. ({err})")
                time.sleep(wait_time)
                
    print(f"\n[+] Extraction Phase: Commencing database ingestion into '{DB_NAME}'...")
    for file_path in downloaded_paths:
        if os.path.exists(file_path) and file_path.endswith('.zip'):
            zip_filename = os.path.basename(file_path)
            print(f" -> Unpacking and indexing records: {zip_filename}")
            
            try:
                with zipfile.ZipFile(file_path, 'r') as archive:
                    json_files = [fn for fn in archive.namelist() if fn.endswith('.json')]
                    
                    for json_file_name in json_files:
                        with archive.open(json_file_name) as json_file:
                            try:
                                content = json.load(json_file)
                                parse_and_insert(content, zip_filename, json_file_name, conn)
                            except json.JSONDecodeError:
                                continue
                conn.commit()
            except zipfile.BadZipFile:
                print(f" [!] File block evaluation anomaly at: {zip_filename}")
                
    conn.close()
    print(f"\n[+] Processing loop successfully written out! Local option database created at '{DB_NAME}'.")

if __name__ == "__main__":
    pipeline()