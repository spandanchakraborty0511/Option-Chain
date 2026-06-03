from kiteconnect import KiteConnect
from config import API_KEY, ACCESS_TOKEN
import pandas as pd

kite = KiteConnect(api_key=API_KEY)
kite.set_access_token("quVgqNCllQJ52Z2fNbUKrn8ffExCtUxw")

instruments = kite.instruments()

df = pd.DataFrame(instruments)

df.to_csv("all_instruments.csv", index=False)

print(df.head())
print(f"\nTotal Instruments: {len(df)}")