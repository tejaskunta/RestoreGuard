#!/usr/bin/env bash
#
# RestoreGuard — live demo script.
#
# Plays the verifier five times in a row so you can watch it change its mind:
#   0. healthy           -> 4 PASS
#   A. corrupt backup     -> ERROR        (and still drops the scratch DB)
#   C. missing table      -> 4 FAIL       (no crash, all four checks recorded)
#   B. stale source       -> 1 FAIL       (only freshness; the pipeline was fine)
#   Z. reset              -> 4 PASS       (back to green)
#
# Two ways to watch it:
#   1. Open a second terminal and run:  tail -f logs/runs.jsonl
#   2. Or watch the dashboard:          .venv/bin/python -m restoreguard.web
#                                       then open http://127.0.0.1:5001/dashboard
#
# Run with:  ./demo.sh

set -u
cd "$(dirname "$0")"

# --- helpers ---------------------------------------------------------------
# psql against the RestoreGuard source database (container rgdev, host port 5433)
psql_src() { PGPASSWORD=devpass psql -h 127.0.0.1 -p 5433 -U rgadmin -d appdb "$@"; }

# the verifier, exactly as cron will call it
run() { .venv/bin/python -m restoreguard.verify; }

# big visible banner between scenarios
step() { echo; echo "============================================================"; echo "  $*"; echo "============================================================"; }

# --- 0. healthy baseline ---------------------------------------------------
step "0. HEALTHY  (expect 4 PASS)"
psql_src -f sql/schema.sql >/dev/null 2>&1       # reseed fresh data
run; echo "exit=$?"

# --- A. corrupt backup -----------------------------------------------------
step "A. CORRUPT BACKUP  (expect ERROR — but scratch STILL dropped)"
RESTOREGUARD_DEMO_CORRUPT=1 run; echo "exit=$?"

# --- C. missing table ------------------------------------------------------
step "C. MISSING TABLE  (expect 4 FAIL — NOT an ERROR)"
psql_src -c "DROP TABLE order_events CASCADE" >/dev/null
run; echo "exit=$?"

# --- B. stale source -------------------------------------------------------
step "B. STALE SOURCE  (expect only freshness FAIL)"
psql_src -f sql/schema.sql >/dev/null 2>&1       # rebuild the dropped table
psql_src -c "UPDATE order_events SET occurred_at = now() - interval '7 days'" >/dev/null
run; echo "exit=$?"

# --- Z. reset and recover --------------------------------------------------
step "Z. RESET  (expect 4 PASS)"
psql_src -f sql/refresh.sql >/dev/null 2>&1      # simulate fresh writes
run; echo "exit=$?"

# --- summary ---------------------------------------------------------------
echo
echo "=================== LEDGER (logs/runs.jsonl) ==================="
.venv/bin/python - <<'PY'
import json, collections
recs = [json.loads(l) for l in open("logs/runs.jsonl")]
print(f"{len(recs)} records:", dict(collections.Counter(r["outcome"] for r in recs)))
print("every run dropped its scratch DB:",
      all(r["scratch_dropped"] for r in recs))
print()
for r in recs[-5:]:
    marks = " ".join(("+" if c["passed"] else "-") + c["name"] for c in r["checks"])
    print(f'  {r["outcome"]:5}  {r["duration_seconds"]:>5}s  {marks or r["error"]}')
PY