#!/usr/bin/env bash
#
# RestoreGuard — live demo, three scenarios.
#
#   1. healthy        -> PASS   (all four checks green)
#   2. corrupt backup -> ERROR  (pipeline broke; scratch STILL dropped)
#   3. stale source   -> FAIL   (pipeline fine, data is stale)
#
# Watch the ledger in a second terminal:  tail -f logs/runs.jsonl
# Or open the dashboard:                  make web  ->  http://127.0.0.1:5001/dashboard

set -u
cd "$(dirname "$0")"

psql_src() { PGPASSWORD=devpass psql -h 127.0.0.1 -p 5433 -U rgadmin -d appdb "$@"; }
run() { .venv/bin/python -m restoreguard.verify; }
step() { echo; echo "============================================================"; echo "  $*"; echo "============================================================"; }

# --- 1. healthy ------------------------------------------------------------
step "1. HEALTHY  (expect PASS)"
psql_src -f sql/schema.sql >/dev/null 2>&1
run; echo "exit=$?"

# --- 2. corrupt backup -----------------------------------------------------
step "2. CORRUPT BACKUP  (expect ERROR — scratch dropped anyway)"
RESTOREGUARD_DEMO_CORRUPT=1 run; echo "exit=$?"

# --- 3. stale source -------------------------------------------------------
step "3. STALE SOURCE  (expect FAIL — freshness only)"
psql_src -f sql/schema.sql >/dev/null 2>&1
psql_src -c "UPDATE order_events SET occurred_at = now() - interval '7 days'" >/dev/null
run; echo "exit=$?"

# --- reset -----------------------------------------------------------------
step "RESET  (expect PASS)"
psql_src -f sql/refresh.sql >/dev/null 2>&1
run; echo "exit=$?"
