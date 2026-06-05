from nse import NSE
import os

# Create a downloads folder if it doesn't exist
os.makedirs("./nse_downloads", exist_ok=True)

# Note: If running this on a cloud server (AWS, DigitalOcean), 
# set server=True to enforce HTTP/2 which prevents immediate IP blocks.
nse = NSE(download_folder="./nse_downloads", server=False)

try:
    with nse:
        print("--- Fetching Market Advance/Decline Ratio ---")
        adv_dec = nse.advanceDecline()
        print(adv_dec)
        
        print("\n--- Fetching Live Market Status ---")
        status = nse.status()
        print(status)

except Exception as e:
    print(f"An error occurred: {e}")