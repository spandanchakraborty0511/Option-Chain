from kiteconnect import KiteConnect

api_key = "ujqgohskrn96s6n3"
api_secret = "4chfbivdma7z6n59zyuxgzofu9tvq9zb"
request_token = "ghGAwiH8SR31gQuxNlI1UZ9QWZ7VTjrE"

kite = KiteConnect(api_key=api_key)

data = kite.generate_session(
    request_token,
    api_secret=api_secret
)

print(data["access_token"])