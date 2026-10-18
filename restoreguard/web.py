"""RestoreGuard — the read-only web view.

/status     -> JSON, for machines
/dashboard  -> HTML certificate, for humans

Both read the same file: logs/runs.jsonl. Nothing to keep in sync.
"""

import json
from pathlib import Path

from flask import Flask, jsonify, render_template

ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = ROOT / "logs" / "runs.jsonl"

app = Flask(__name__)


def read_runs(limit: int = 10) -> list:
    """Newest `limit` records, newest first. Bad lines are skipped, not fatal."""
    if not LOG_PATH.exists():
        return []
    runs = []
    with open(LOG_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                runs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    runs.reverse()
    return runs[:limit]


def build_status() -> dict:
    """Summarise the latest run.

    Fields are listed explicitly on purpose — a raw **spread would leak
    whatever anyone later adds to the log, including anything sensitive.
    """
    recent = read_runs(limit=1)
    if not recent:
        return {"state": "unknown", "message": "no runs recorded yet", "last_run": None}

    last = recent[0]
    return {
        "state": last.get("outcome", "unknown"),
        "message": f"last run {last.get('outcome')} at {last.get('started_at')}",
        "last_run": {
            "run_id": last.get("run_id"),
            "started_at": last.get("started_at"),
            "outcome": last.get("outcome"),
            "duration_seconds": last.get("duration_seconds"),
            "dump_bytes": last.get("dump_bytes"),
            "scratch_dropped": last.get("scratch_dropped"),
            "checks": last.get("checks", []),
            "error": last.get("error"),
        },
    }


@app.route("/status")
def status():
    return jsonify(build_status())


@app.route("/dashboard")
def dashboard():
    return render_template(
        "dashboard.html",
        status=build_status(),
        runs=read_runs(limit=10),
        refresh=30,          # seconds; 0 disables the meta refresh
    )


if __name__ == "__main__":
    # Port 5001, not 5000 — 5000 is taken by an unrelated container.
    app.run(host="127.0.0.1", port=5001, debug=False)
