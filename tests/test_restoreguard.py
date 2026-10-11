"""RestoreGuard test suite.

Strategy
--------
The interesting logic — the client/server version guard, the four checks,
the log reader, the routing — is testable without a real database. The
checks just take a connection and return a dict, so we hand them a fake
connection scripted to return chosen values. The web layer reads a JSONL
file, so we point it at a temp file. That keeps the suite fast and means
CI needs no Postgres.

Run:  .venv/bin/python -m pytest -q
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from restoreguard import verify, web

# ---------------------------------------------------------------------------
# A stand-in for a psycopg connection. Scripted: hand it a list of return
# values and it hands them back one per execute().
# ---------------------------------------------------------------------------


class FakeCursor:
    def __init__(self, value):
        self._value = value

    def fetchone(self):
        return (self._value,)

    def fetchall(self):
        return self._value


class FakeConn:
    def __init__(self, results):
        self._results = list(results)
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append(sql)
        value = self._results.pop(0) if self._results else None
        return FakeCursor(value)


class BoomConn(FakeConn):
    """A connection whose queries always explode (a missing table)."""

    def execute(self, sql, params=None):
        raise RuntimeError("relation does not exist")


# ---------------------------------------------------------------------------
# Version guard — the bug that cost the most time, now pinned by tests
# ---------------------------------------------------------------------------


def test_version_guard_rejects_mismatch(monkeypatch):
    monkeypatch.setattr(verify, "_client_major", lambda: 17)
    monkeypatch.setattr(verify, "_server_major", lambda: 16)
    with pytest.raises(RuntimeError, match="does not match"):
        verify.check_client_version()


def test_version_guard_accepts_match(monkeypatch):
    monkeypatch.setattr(verify, "_client_major", lambda: 16)
    monkeypatch.setattr(verify, "_server_major", lambda: 16)
    verify.check_client_version()          # must not raise


# ---------------------------------------------------------------------------
# The four checks
# ---------------------------------------------------------------------------


def test_tables_check_flags_missing():
    conn = FakeConn([[("customers",), ("orders",)]])     # order_events absent
    result = verify.check_tables(conn)
    assert result["passed"] is False
    assert "order_events" in result["detail"]


def test_tables_check_all_present():
    conn = FakeConn([[("customers",), ("orders",), ("order_events",)]])
    assert verify.check_tables(conn)["passed"] is True


def test_row_counts_flags_too_few():
    conn = FakeConn([10, 100, 3])                        # order_events below floor
    result = verify.check_row_counts(conn)
    assert result["passed"] is False
    assert "order_events=3" in result["detail"]


def test_row_counts_all_good():
    conn = FakeConn([50, 200, 600])
    assert verify.check_row_counts(conn)["passed"] is True


def test_integrity_flags_orphans():
    conn = FakeConn([7, 0])                              # 7 orphan orders
    result = verify.check_integrity(conn)
    assert result["passed"] is False
    assert "7 orphan" in result["detail"]


def test_integrity_clean():
    assert verify.check_integrity(FakeConn([0, 0]))["passed"] is True


def test_integrity_survives_missing_table():
    result = verify.check_integrity(BoomConn([]))
    assert result["passed"] is False
    assert "unreadable" in result["detail"]


def test_freshness_fails_when_stale():
    old = datetime.now(timezone.utc) - timedelta(minutes=10080)   # 7 days
    result = verify.check_freshness(FakeConn([old]))
    assert result["passed"] is False
    assert "min old" in result["detail"]


def test_freshness_passes_when_recent():
    result = verify.check_freshness(FakeConn([datetime.now(timezone.utc)]))
    assert result["passed"] is True


def test_freshness_survives_missing_table():
    result = verify.check_freshness(BoomConn([]))
    assert result["passed"] is False
    assert "unreadable" in result["detail"]


def test_freshness_handles_empty_table():
    result = verify.check_freshness(FakeConn([None]))
    assert result["passed"] is False
    assert "empty" in result["detail"]


# ---------------------------------------------------------------------------
# Web layer — log reader and status builder (no Postgres, just a file)
# ---------------------------------------------------------------------------


def test_read_runs_newest_first(tmp_path, monkeypatch):
    log = tmp_path / "runs.jsonl"
    log.write_text(
        json.dumps({"outcome": "FAIL", "checks": []}) + "\n"
        + json.dumps({"outcome": "PASS", "checks": []}) + "\n"
    )
    monkeypatch.setattr(web, "LOG_PATH", log)
    assert [r["outcome"] for r in web.read_runs()] == ["PASS", "FAIL"]


def test_read_runs_skips_malformed_lines(tmp_path, monkeypatch):
    log = tmp_path / "runs.jsonl"
    log.write_text('{"outcome": "PASS", "checks": []}\nnot json at all\n')
    monkeypatch.setattr(web, "LOG_PATH", log)
    assert len(web.read_runs()) == 1


def test_status_unknown_when_no_log(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "LOG_PATH", tmp_path / "missing.jsonl")
    assert web.build_status()["state"] == "unknown"


def test_status_reports_latest_outcome(tmp_path, monkeypatch):
    log = tmp_path / "runs.jsonl"
    log.write_text(json.dumps({
        "outcome": "PASS", "checks": [], "started_at": "2026-01-01T00:00:00Z",
    }) + "\n")
    monkeypatch.setattr(web, "LOG_PATH", log)
    status = web.build_status()
    assert status["state"] == "PASS"
    assert status["last_run"]["outcome"] == "PASS"


# ---------------------------------------------------------------------------
# Routing — the URLs actually resolve and render
# ---------------------------------------------------------------------------


def test_routes_are_registered():
    rules = {r.rule for r in web.app.url_map.iter_rules()}
    assert "/status" in rules
    assert "/dashboard" in rules


def test_status_endpoint_returns_json(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "LOG_PATH", tmp_path / "missing.jsonl")
    resp = web.app.test_client().get("/status")
    assert resp.status_code == 200
    assert resp.get_json()["state"] == "unknown"


def test_dashboard_endpoint_renders(tmp_path, monkeypatch):
    log = tmp_path / "runs.jsonl"
    log.write_text(json.dumps({
        "outcome": "PASS",
        "started_at": "2026-01-01T00:00:00Z",
        "duration_seconds": 1.2,
        "dump_bytes": 12345,
        "scratch_dropped": True,
        "checks": [{"name": "tables_exist", "passed": True, "detail": "all 3 present"}],
    }) + "\n")
    monkeypatch.setattr(web, "LOG_PATH", log)
    resp = web.app.test_client().get("/dashboard")
    assert resp.status_code == 200
    assert b"RestoreGuard" in resp.data