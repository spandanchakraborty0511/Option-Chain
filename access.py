from kiteconnect import KiteConnect

api_key = "ujqgohskrn96s6n3"
api_secret = "4chfbivdma7z6n59zyuxgzofu9tvq9zb"
request_token = "Ud4cajaL6i75b1Lc5iLQXNOqJWepbH9P"

kite = KiteConnect(api_key=api_key)

data = kite.generate_session(
    request_token,
    api_secret=api_secret
)

print(data["access_token"])