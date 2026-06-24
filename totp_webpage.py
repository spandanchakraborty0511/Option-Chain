"""
totp_webpage.py
─────────────────────────────────────────────────────────────
Tiny local webpage that pops up during Kite login and waits
for you to type in the TOTP code from your authenticator app.

Used by auto_fetch.py — you normally don't need to run this directly.
"""

import threading
import webbrowser
from flask import Flask, request

_app = Flask(__name__)
_totp_holder = {"code": None}

PAGE_HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Enter TOTP</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: #f5f5f5;
    display: flex;
    align-items: center;
    justify-content: center;
    height: 100vh;
  }
  .card {
    background: #fff;
    border-radius: 12px;
    border: 1px solid #e0e0e0;
    padding: 36px 40px;
    width: 340px;
    text-align: center;
    box-shadow: 0 4px 20px rgba(0,0,0,0.06);
  }
  h1 { font-size: 18px; font-weight: 600; margin-bottom: 8px; color: #1a1a1a; }
  p { font-size: 13px; color: #777; margin-bottom: 24px; }
  input {
    width: 100%;
    font-size: 28px;
    text-align: center;
    letter-spacing: 8px;
    padding: 14px;
    border: 1px solid #ddd;
    border-radius: 8px;
    margin-bottom: 16px;
    outline: none;
  }
  input:focus { border-color: #5b6ef5; }
  button {
    width: 100%;
    padding: 12px;
    background: #5b6ef5;
    color: #fff;
    border: none;
    border-radius: 8px;
    font-size: 15px;
    font-weight: 600;
    cursor: pointer;
  }
  button:hover { background: #4a5de0; }
  .status { margin-top: 16px; font-size: 13px; color: #16a34a; display: none; }
</style>
</head>
<body>
  <div class="card">
    <h1>Zerodha TOTP</h1>
    <p>Open your authenticator app and enter the 6-digit code</p>
    <form id="f">
      <input type="text" id="code" maxlength="6" inputmode="numeric"
             autocomplete="one-time-code" autofocus placeholder="000000">
      <button type="submit">Submit</button>
    </form>
    <div class="status" id="status">Submitted — you can close this tab.</div>
  </div>
<script>
document.getElementById('f').addEventListener('submit', async (e) => {
  e.preventDefault();
  const code = document.getElementById('code').value.trim();
  if (code.length !== 6) return;
  await fetch('/submit', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({code})
  });
  document.getElementById('f').style.display = 'none';
  document.getElementById('status').style.display = 'block';
});
</script>
</body>
</html>
"""


@_app.route("/")
def home():
    return PAGE_HTML


@_app.route("/submit", methods=["POST"])
def submit():
    data = request.get_json()
    _totp_holder["code"] = data.get("code", "").strip()
    return {"ok": True}


@_app.route("/status")
def status():
    return {"code": _totp_holder["code"]}


def get_totp_via_webpage(port=5005, timeout=120):
    """
    Starts a local Flask server, opens it in the browser, and blocks
    until the user submits a 6-digit TOTP code (or timeout is reached).
    Returns the code as a string.
    """
    import time
    import logging

    werkzeug_log = logging.getLogger("werkzeug")
    werkzeug_log.setLevel(logging.ERROR)  # silence Flask's request logs

    _totp_holder["code"] = None

    server_thread = threading.Thread(
        target=lambda: _app.run(port=port, debug=False, use_reloader=False),
        daemon=True
    )
    server_thread.start()

    time.sleep(0.5)  # let server boot
    webbrowser.open(f"http://127.0.0.1:{port}")

    print(f"Waiting for TOTP code at http://127.0.0.1:{port} ...")

    waited = 0
    while _totp_holder["code"] is None:
        time.sleep(0.5)
        waited += 0.5
        if waited >= timeout:
            raise TimeoutError(f"No TOTP submitted within {timeout} seconds.")

    code = _totp_holder["code"]
    print(f"Received TOTP: {code}")
    return code


if __name__ == "__main__":
    # quick manual test
    code = get_totp_via_webpage()
    print("Got code:", code)