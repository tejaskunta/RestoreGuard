"""Unit tests for the check logic — no live database needed.

WHY MOCKS? (for your viva):
  CI (GitHub Actions) has no RDS. So we fake the DB connection with
  tiny FakeConn/FakeCursor classes that return canned answers.
  Each test says: 'IF the DB answered X, THEN the check must say Y.'
  The real SQL still runs against real Postgres in Step 6 (end-to-end).
"""

from datetime import datetime, timedelta, timezone

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from checks import (
    check_tables_exist,
    check_row_counts,
    check_referential_integrity,
    check_freshness,
    parse_log_line,
)


class FakeCursor:
    """Pretends to be a psycopg2 cursor. Returns scripted answers."""

    def __init__(self, answers):
        # answers: list of values fetchone()/fetchall() should return, in order
        self.answers = list(answers)
        self.queries = []

    def execute(self, query):
        self.queries.append(query)

    def fetchall(self):
        return self.answers.pop(0)

    def fetchone(self):
        return self.answers.pop(0)

    def close(self):
        pass


class FakeConn:
    def __init__(self, answers):
        self.cursor_obj = FakeCursor(answers)

    def cursor(self):
        return self.cursor_obj


def test_tables_pass():
    conn = FakeConn([[("users",), ("orders",), ("payments",)]])
    passed, detail = check_tables_exist(conn)
    assert passed, detail


def test_tables_missing():
    conn = FakeConn([[("users",), ("orders",)]])  # payments missing
    passed, detail = check_tables_exist(conn)
    assert not passed
    assert "payments" in detail


def test_row_counts_pass():
    conn = FakeConn([(3,), (4,), (4,)])
    passed, detail = check_row_counts(conn)
    assert passed, detail


def test_row_counts_truncated():
    conn = FakeConn([(3,), (0,), (4,)])  # orders emptied
    passed, detail = check_row_counts(conn)
    assert not passed
    assert "orders" in detail


def test_referential_integrity_pass():
    conn = FakeConn([(0,), (0,)])  # zero orphans
    passed, detail = check_referential_integrity(conn)
    assert passed, detail


def test_referential_integrity_orphan():
    conn = FakeConn([(2,), (0,)])  # 2 orders point nowhere
    passed, detail = check_referential_integrity(conn)
    assert not passed
    assert "2" in detail


def test_freshness_pass():
    fresh = datetime.now(timezone.utc) - timedelta(hours=1)
    conn = FakeConn([(fresh,)])
    passed, detail = check_freshness(conn)
    assert passed, detail


def test_freshness_stale():
    old = datetime.now(timezone.utc) - timedelta(days=5)
    conn = FakeConn([(old,)])
    passed, detail = check_freshness(conn)
    assert not passed
    assert "stale" in detail


def test_parse_log_good():
    parsed = parse_log_line("2026-09-15T10:00:00Z | PASS | all checks ok")
    assert parsed == {
        "timestamp": "2026-09-15T10:00:00Z",
        "outcome": "PASS",
        "detail": "all checks ok",
    }


def test_parse_log_malformed():
    assert parse_log_line("garbage line") is None
    assert parse_log_line("") is None
