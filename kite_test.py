"""
Kite Connect — Connection Test
===============================
Run this first to verify your API credentials and login work
before running the full pipeline.
"""

from kiteconnect import KiteConnect

# ── Fill these in ──
API_KEY    = "ujqgohskrn96s6n3"
API_SECRET = "4chfbivdma7z6n59zyuxgzofu9tvq9zb"
# ──────────────────

kite = KiteConnect(api_key=API_KEY)

# Step 1: Get login URL
print("\n" + "─"*50)
print("  Step 1: Open this URL in your browser")
print("─"*50)
print(kite.login_url())

# Step 2: Paste token
print("\n" + "─"*50)
print("  Step 2: After login, copy the request_token")
print("  from the browser URL bar and paste it below")
print("─"*50)
request_token = input("\n  Paste request_token: ").strip()

# Step 3: Generate session
print("\n  Generating session...")
try:
    session = kite.generate_session(request_token, api_secret=API_SECRET)
    kite.set_access_token(session["access_token"])
    print("  ✅  Login successful!")
except Exception as e:
    print(f"  ❌  Login failed: {e}")
    exit()

# Step 4: Quick sanity checks
print("\n" + "─"*50)
print("  Step 3: Running sanity checks")
print("─"*50)

# Profile
try:
    profile = kite.profile()
    print(f"  👤  Logged in as : {profile['user_name']} ({profile['user_id']})")
    print(f"  📧  Email        : {profile['email']}")
except Exception as e:
    print(f"  ⚠  Could not fetch profile: {e}")

# Margins
try:
    margins = kite.margins()
    equity = margins.get("equity", {})
    print(f"  💰  Available    : ₹{equity.get('available', {}).get('live_balance', 0):,.2f}")
except Exception as e:
    print(f"  ⚠  Could not fetch margins: {e}")

# Instruments count
try:
    import pandas as pd
    instruments = pd.DataFrame(kite.instruments("NFO"))
    options = instruments[instruments["instrument_type"].isin(["CE", "PE"])]
    print(f"  📋  NFO options  : {len(options):,} contracts available")
    print(f"\n  Sample instruments:")
    sample = options[options["name"] == "NIFTY"].head(5)[
        ["tradingsymbol", "strike", "instrument_type", "expiry"]
    ]
    print(sample.to_string(index=False))
except Exception as e:
    print(f"  ⚠  Could not fetch instruments: {e}")

print("\n" + "─"*50)
print("  ✅  All good! You can now run options_pipeline.py")
print("─"*50 + "\n")