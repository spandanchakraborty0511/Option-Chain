"""
server.py
─────────────────────────────────────────────────────────────
Local control panel for the options chain pipeline.

Serves index.html with two actions:
  - "Fetch Latest Data"  -> runs auto_fetch.py as a subprocess
  - "View Dashboard"     -> runs pattern_analysis.py, then serves
                            the generated options_patterns.html

Run with:
  python server.py

Then open:
  http://127.0.0.1:5000
"""

import os
import sys
import subprocess
import threading
import uuid

from flask import Flask, jsonify, send_from_directory, Response

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PYTHON   = sys.executable  # use whichever python is running this server

app = Flask(__name__, static_folder=BASE_DIR, static_url_path="")

# In-memory job tracking: job_id -> {"status": ..., "log": [...]}
JOBS = {}


def _run_script(job_id, script_name, extra_args=None):
    """Runs a script as a subprocess, streaming output into JOBS[job_id]['log']."""
    JOBS[job_id] = {"status": "running", "log": [], "returncode": None}
    cmd = [PYTHON, os.path.join(BASE_DIR, script_name)]
    if extra_args:
        cmd.extend(extra_args)

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=BASE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        for line in proc.stdout:
            JOBS[job_id]["log"].append(line.rstrip())
        proc.wait()
        JOBS[job_id]["returncode"] = proc.returncode
        JOBS[job_id]["status"] = "done" if proc.returncode == 0 else "error"
    except Exception as e:
        JOBS[job_id]["log"].append(f"ERROR: {e}")
        JOBS[job_id]["status"] = "error"
        JOBS[job_id]["returncode"] = -1


@app.route("/")
def home():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/fetch", methods=["POST"])
def start_fetch():
    """Kicks off auto_fetch.py in the background, returns a job id."""
    job_id = uuid.uuid4().hex[:8]
    t = threading.Thread(target=_run_script, args=(job_id, "auto_fetch.py"), daemon=True)
    t.start()
    return jsonify({"job_id": job_id})


@app.route("/analyze", methods=["POST"])
def start_analyze():
    """Kicks off pattern_analysis.py in the background, returns a job id."""
    job_id = uuid.uuid4().hex[:8]
    t = threading.Thread(target=_run_script, args=(job_id, "pattern_analysis.py"), daemon=True)
    t.start()
    return jsonify({"job_id": job_id})


@app.route("/job/<job_id>")
def job_status(job_id):
    """Poll this to get live log lines + status for a running job."""
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"status": "unknown", "log": []}), 404
    return jsonify(job)


@app.route("/dashboard")
def dashboard():
    """Serves the most recently generated options_patterns.html."""
    path = os.path.join(BASE_DIR, "options_patterns.html")
    if not os.path.exists(path):
        return Response(
            "<p style='font-family:sans-serif;padding:40px'>"
            "No dashboard yet — click 'View Dashboard' from the home page first.</p>",
            mimetype="text/html"
        )
    return send_from_directory(BASE_DIR, "options_patterns.html")


if __name__ == "__main__":
    print(f"Serving from: {BASE_DIR}")
    print("Open http://127.0.0.1:5000 in your browser.")
    app.run(host="127.0.0.1", port=5000, debug=False)