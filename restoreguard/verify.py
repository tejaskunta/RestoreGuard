"""RestoreGuard — the engine.

One run does six things, in order:
  0. check the pg_dump client matches the server
  1. dump the source database
  2. recreate the scratch database and restore into it
  3. run four checks against the restored copy
  4. drop the scratch database (ALWAYS, even on failure)
  5. log one line to logs/runs.jsonl, alert Discord if it failed
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Settings. Read once, at import time, from the .env next to the project root.
# ROOT comes from THIS FILE's location, not the current directory, so cron can
# invoke us from anywhere.
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

HOST = os.getenv("SOURCE_DB_HOST", "127.0.0.1")
PORT = int(os.getenv("SOURCE_DB_PORT", "5432"))
DB = os.getenv("SOURCE_DB_NAME", "appdb")
USER = os.getenv("SOURCE_DB_USER", "rgadmin")
PASSWORD = os.getenv("SOURCE_DB_PASSWORD", "")

# The Postgres client directory we must use. cron never reads ~/.zshrc, and
# zsh caches command locations, so we pin the directory and prepend it to PATH
# ourselves rather than trusting whatever `pg_dump` happens to resolve to.
PG_CLIENT_BIN = os.getenv("PG_CLIENT_BIN", "").strip()

SCRATCH = "scratch_verify"     # the disposable copy
MAINTENANCE = "postgres"       # connect here to CREATE/DROP another database

FRESH_MAX_AGE_MINUTES = int(os.getenv("FRESH_MAX_AGE_MINUTES", "60"))
WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

LOG_PATH = ROOT / "logs" / "runs.jsonl"
DUMP_PATH = Path("/tmp/restoreguard/backup.dump")

# Single source of truth for the existence check and the row-count check.
# Minimums only — data grows, so a floor never false-alarms.
EXPECTED_TABLES = {
    "customers": 10,
    "orders": 100,
    "order_events": 500,
}

# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def pg_env() -> dict:
    """Environment for pg_dump/pg_restore.

    PGPASSWORD stops the password prompt. PG_CLIENT_BIN goes first on PATH so
    the pinned client always wins — this is the fix for the v17/v16 mixup.
    """
    env = dict(os.environ)
    env["PGPASSWORD"] = PASSWORD
    if PG_CLIENT_BIN:
        env["PATH"] = PG_CLIENT_BIN + os.pathsep + env.get("PATH", "")
    return env


def connect(database: str) -> psycopg.Connection:
    """Open a connection. autocommit so one failed statement can't poison the rest."""
    return psycopg.connect(
        host=HOST,
        port=PORT,
        dbname=database,
        user=USER,
        password=PASSWORD,
        autocommit=True,
        connect_timeout=10,
    )

def _run(cmd: list, env: dict) -> None:
    """Run a pg command; on failure raise with the REAL stderr.

    subprocess.run(check=True) raises CalledProcessError whose str() is only
    "returned non-zero exit status 1" — it discards the stderr that says WHY.
    We re-raise with the last few lines of stderr so the log is diagnosable.
    """
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-5:])
        raise RuntimeError(f"{cmd[0]} failed (exit {proc.returncode}):\n{tail}")

def _client_major() -> int:
    """Major version of the pg_dump we would actually run, e.g. 16.

    Note: we ask the binary THROUGH pg_env(), so this reports what will really
    execute — not what the ambient shell PATH says.
    """
    out = subprocess.run(
        ["pg_dump", "--version"], env=pg_env(), capture_output=True, text=True, check=True
    ).stdout.strip()                                  # "pg_dump (PostgreSQL) 16.15 (Homebrew)"
    return int(out.split()[2].split(".")[0])


def _server_major() -> int:
    """Major version of the server we would restore into.

    server_version_num is 160015 for 16.15, so integer-dividing by 10000 gives 16.
    """
    with connect(MAINTENANCE) as conn:
        row = conn.execute("SHOW server_version_num").fetchone()
    if row is None:
        raise RuntimeError("Unable to determine PostgreSQL server version")
    num = row[0]
    return int(num) // 10000


def check_client_version() -> None:
    """Fail early and clearly if the client and server majors disagree.

    Learned the hard way: a v17 pg_dump writes `SET transaction_timeout = 0;`
    into the archive and a v16 server rejects it with
    'unrecognized configuration parameter "transaction_timeout"'.
    The opposite (too-old client) gives 'unsupported version (1.15) in file header'.
    Insisting the majors match avoids both.
    """
    client = _client_major()
    server = _server_major()
    print(f"   client {client} vs server {server}")
    if client != server:
        raise RuntimeError(
            f"Postgres client major {client} does not match server major {server}. "
            f"Install the matching client and point PG_CLIENT_BIN at it.\n"
            f"  brew install postgresql@{server}\n"
            f"  PG_CLIENT_BIN=/usr/local/opt/postgresql@{server}/bin"
        )


def dump() -> int:
    """Take a custom-format dump. Returns the file size in bytes.

    --format=custom is load-bearing: it makes a truncated or damaged file
    detectably invalid at restore time. A plain .sql file would happily
    half-restore and look fine.
    """
    DUMP_PATH.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "pg_dump",
        "-h", HOST, "-p", str(PORT), "-U", USER, "-d", DB,
        "--format=custom", "--no-owner", "--no-password",
        "-f", str(DUMP_PATH),
    ]
    _run(cmd, pg_env())

    # Demo switch: deliberately corrupt the archive so the restore must fail.
    if os.getenv("RESTOREGUARD_DEMO_CORRUPT"):
        with open(DUMP_PATH, "r+b") as f:
            f.truncate(1024)
        print("   DEMO: truncated the dump to 1024 bytes")

    return DUMP_PATH.stat().st_size


def create_scratch() -> None:
    """Drop any leftover scratch database, then create a fresh empty one."""
    with connect(MAINTENANCE) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{SCRATCH}"')
        conn.execute(f'CREATE DATABASE "{SCRATCH}"')


def restore() -> None:
    """Restore the dump. --exit-on-error turns a bad dump into a loud failure."""
    cmd = [
        "pg_restore",
        "-h", HOST, "-p", str(PORT), "-U", USER, "-d", SCRATCH,
        "--exit-on-error", "--no-owner", "--no-password",
        str(DUMP_PATH),
    ]
    _run(cmd, pg_env())


def drop_scratch() -> None:
    """Fully remove the scratch database.

    DROP DATABASE refuses while any connection is open, so we kick everyone
    else out first — the same two-step you ran by hand.
    """
    with connect(MAINTENANCE) as conn:
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (SCRATCH,),
        )
        conn.execute(f'DROP DATABASE IF EXISTS "{SCRATCH}"')


# ---------------------------------------------------------------------------
# The four checks. Each takes an open connection and returns one small dict.
# ---------------------------------------------------------------------------


def _result(name: str, passed: bool, detail: str) -> dict:
    return {"name": name, "passed": passed, "detail": detail}


def check_tables(conn) -> dict:
    """FR4 — every expected table exists in the restored copy."""
    rows = conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    ).fetchall()
    present = {r[0] for r in rows}
    missing = sorted(set(EXPECTED_TABLES) - present)
    return _result(
        "tables_exist",
        not missing,
        "all 3 present" if not missing else "missing: " + ", ".join(missing),
    )


def check_row_counts(conn) -> dict:
    """FR5 — no table is suspiciously empty."""
    problems = []
    for table, minimum in EXPECTED_TABLES.items():
        try:
            n = conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
        except Exception as exc:
            problems.append(f"{table}: unreadable ({exc.__class__.__name__})")
            continue
        if n < minimum:
            problems.append(f"{table}={n} (needs >= {minimum})")
    return _result(
        "row_counts",
        not problems,
        "all above minimum" if not problems else "; ".join(problems),
    )


def check_integrity(conn) -> dict:
    """FR6 — no orphan rows. A LEFT JOIN that finds no match IS the orphan.

    Guarded: if the restored copy is missing one of the tables, the query
    raises. We want that to be a clean FAIL, not blow the whole run into ERROR.
    """
    try:
        orphan_orders = conn.execute(
            "SELECT count(*) FROM orders o "
            "LEFT JOIN customers c ON c.id = o.customer_id "
            "WHERE c.id IS NULL"
        ).fetchone()[0]
        orphan_events = conn.execute(
            "SELECT count(*) FROM order_events e "
            "LEFT JOIN orders o ON o.id = e.order_id "
            "WHERE o.id IS NULL"
        ).fetchone()[0]
    except Exception as exc:
        return _result("integrity", False, f"unreadable ({exc.__class__.__name__})")
    total = orphan_orders + orphan_events
    return _result("integrity", total == 0, f"{total} orphan row(s)")


def check_freshness(conn) -> dict:
    """FR7 — is the backup recent enough to be worth anything?

    Only looks at order_events, the table that writes constantly. customers
    could be quiet for weeks and that would be perfectly normal.
    """
    try:
        newest = conn.execute("SELECT max(occurred_at) FROM order_events").fetchone()[0]
    except Exception as exc:
        return _result("freshness", False, f"unreadable ({exc.__class__.__name__})")
    if newest is None:
        return _result("freshness", False, "order_events is empty")
    age_minutes = (datetime.now(timezone.utc) - newest).total_seconds() / 60
    ok = age_minutes <= FRESH_MAX_AGE_MINUTES
    return _result(
        "freshness",
        ok,
        f"newest event {age_minutes:.0f} min old (limit {FRESH_MAX_AGE_MINUTES})",
    )


CHECKS = [check_tables, check_row_counts, check_integrity, check_freshness]

# ---------------------------------------------------------------------------
# Logging and alerting
# ---------------------------------------------------------------------------


def log_run(record: dict) -> None:
    """Append one JSON line. Append-only means a crash can never lose history."""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")


def send_alert(record: dict) -> None:
    """Post to Discord if a webhook is configured. Never fatal."""
    if not WEBHOOK:
        print("   (no DISCORD_WEBHOOK_URL set — skipping alert)")
        return
    lines = "\n".join(f"- {c['name']}: {c['detail']}" for c in record["checks"])
    text = (
        f"**RestoreGuard {record['outcome']}** on {record['host']}\n"
        f"{lines or '- no checks ran'}\n"
        f"error: {record['error'] or 'none'}"
    )
    try:
        requests.post(WEBHOOK, json={"content": text}, timeout=10)
        print("   alert sent")
    except Exception as exc:
        print(f"   alert failed: {exc}")


# ---------------------------------------------------------------------------
# One full run
# ---------------------------------------------------------------------------


def run() -> int:
    """Run the whole check. Returns 0 on PASS, 1 otherwise."""
    started = time.time()
    record = {
        "run_id": uuid.uuid4().hex[:12],
        "started_at": datetime.now(timezone.utc).isoformat(),
        "host": os.uname().nodename,
        "outcome": "ERROR",
        "error": None,
        "checks": [],
        "scratch_db": SCRATCH,
        "scratch_dropped": False,
        "dump_bytes": 0,
        "duration_seconds": 0.0,
    }

    conn = None
    try:
        print("0. checking client version...")
        check_client_version()

        print("1. dumping source database...")
        record["dump_bytes"] = dump()
        print(f"   wrote {record['dump_bytes']} bytes")

        print("2. recreating scratch database...")
        create_scratch()

        print("3. restoring into scratch database...")
        restore()

        print("4. running checks...")
        conn = connect(SCRATCH)
        for check in CHECKS:
            result = check(conn)
            record["checks"].append(result)
            mark = "PASS" if result["passed"] else "FAIL"
            print(f"   [{mark}] {result['name']}: {result['detail']}")

        record["outcome"] = "PASS" if all(c["passed"] for c in record["checks"]) else "FAIL"

    except Exception as exc:
        record["error"] = str(exc)
        record["outcome"] = "ERROR"
        print(f"   ERROR: {exc}")

    finally:
        # Runs no matter what happened above. That is FR10.
        if conn is not None:
            conn.close()
        print("5. dropping scratch database (always)...")
        try:
            drop_scratch()
            record["scratch_dropped"] = True
            print("   dropped")
        except Exception as exc:
            print(f"   could not drop: {exc}")

        record["duration_seconds"] = round(time.time() - started, 2)
        log_run(record)
        if record["outcome"] != "PASS":
            send_alert(record)

    print(f"\nOutcome: {record['outcome']} in {record['duration_seconds']}s")
    return 0 if record["outcome"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(run())