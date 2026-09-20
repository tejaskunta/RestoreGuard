"""RestoreGuard check logic — the 4 checks that decide PASS / FAIL.

WHY THIS FILE EXISTS (for your viva):
  A backup file existing proves nothing. These 4 checks prove the backup
  actually restores COMPLETE (tables), CORRECT (row counts + foreign keys),
  and CURRENT (freshness). Each function takes a live DB connection to the
  SCRATCH database only — never the source — and returns (passed, detail).

  They are pure logic over SQL results, so pytest can test them with a
  fake connection (see tests/test_checks.py) — no live DB needed for CI.
"""

from datetime import datetime, timezone

EXPECTED_TABLES = ["users", "orders", "payments"]

# Minimum rows we expect. Catches a truncated table.
# (Seed has 3 users, 4 orders, 4 payments — so min 1 is a sane tripwire.)
MIN_COUNTS = {"users": 1, "orders": 1, "payments": 1}

# Freshness: newest order must be newer than this many hours ago.
# Catches a backup job that silently stopped running.
FRESHNESS_HOURS = 48


def check_tables_exist(conn, expected=EXPECTED_TABLES):
    """RG-FR-004: do all expected tables exist after restore?"""
    cur = conn.cursor()
    cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public';")
    found = {row[0] for row in cur.fetchall()}
    missing = [t for t in expected if t not in found]
    cur.close()
    if missing:
        return False, f"missing tables: {missing}"
    return True, f"all tables present: {expected}"


def check_row_counts(conn, minimums=MIN_COUNTS):
    """RG-FR-005: does every table have a sane number of rows?"""
    cur = conn.cursor()
    problems = []
    for table, minimum in minimums.items():
        cur.execute(f"SELECT COUNT(*) FROM {table};")
        count = cur.fetchone()[0]
        if count < minimum:
            problems.append(f"{table} has {count} rows, expected >= {minimum}")
    cur.close()
    if problems:
        return False, "; ".join(problems)
    return True, "all row counts sane"


def check_referential_integrity(conn):
    """RG-FR-006: zero orphaned foreign keys?

    An orphan = an order pointing at a user that does not exist.
    A restore can 'succeed' (exit code 0) yet still lose rows —
    this query catches that silent corruption.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT COUNT(*) FROM orders o
        LEFT JOIN users u ON o.user_id = u.id
        WHERE u.id IS NULL;
        """
    )
    orphan_orders = cur.fetchone()[0]
    cur.execute(
        """
        SELECT COUNT(*) FROM payments p
        LEFT JOIN orders o ON p.order_id = o.id
        WHERE o.id IS NULL;
        """
    )
    orphan_payments = cur.fetchone()[0]
    cur.close()
    if orphan_orders or orphan_payments:
        return False, (
            f"orphaned FKs: {orphan_orders} orders without user, "
            f"{orphan_payments} payments without order"
        )
    return True, "no orphaned foreign keys"


def check_freshness(conn, max_age_hours=FRESHNESS_HOURS):
    """RG-FR-007: is the newest record recent enough?

    Even a perfect restore is useless if the backup itself is stale
    (backup cron died weeks ago). We check MAX(created_at) in orders —
    the table expected to change most often.
    """
    cur = conn.cursor()
    cur.execute("SELECT MAX(created_at) FROM orders;")
    newest = cur.fetchone()[0]
    cur.close()
    if newest is None:
        return False, "orders table is empty, cannot judge freshness"
    # psycopg2 returns timezone-aware datetimes; make NOW aware too.
    now = datetime.now(timezone.utc)
    if newest.tzinfo is None:
        newest = newest.replace(tzinfo=timezone.utc)
    age_hours = (now - newest).total_seconds() / 3600
    if age_hours > max_age_hours:
        return False, f"stale: newest order is {age_hours:.1f}h old (limit {max_age_hours}h)"
    return True, f"fresh: newest order is {age_hours:.1f}h old"


def run_all_checks(conn):
    """Run every check in order. Returns list of (name, passed, detail).

    Order matters for your demo story: tables -> rows -> FKs -> freshness,
    from 'did it restore at all' to 'is the data actually useful'.
    """
    results = []
    for name, fn in [
        ("tables", check_tables_exist),
        ("row_counts", check_row_counts),
        ("referential_integrity", check_referential_integrity),
        ("freshness", check_freshness),
    ]:
        passed, detail = fn(conn)
        results.append((name, passed, detail))
    return results


def parse_log_line(line):
    """Parse one verification_log.txt line. Shared by /status + /dashboard.

    Format: '2026-09-15T10:00:00Z | PASS | all checks ok'
    Returns dict or None if the line is malformed (never crash on bad input).
    """
    try:
        parts = [p.strip() for p in line.strip().split("|")]
        if len(parts) != 3:
            return None
        timestamp, outcome, detail = parts
        if outcome not in ("PASS", "FAIL"):
            return None
        return {"timestamp": timestamp, "outcome": outcome, "detail": detail}
    except Exception:
        return None
