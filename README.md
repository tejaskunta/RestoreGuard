# RestoreGuard

**A backup you have never restored is not a backup — it's a hope.**

RestoreGuard takes a real dump of your Postgres database, restores it into a
throwaway copy, runs four checks against the copy, and then deletes the copy.
If anything is wrong it exits non-zero and (optionally) pings Discord. It runs
on a schedule, so a broken backup is discovered by a cron job at 3am — not by
you, during an outage, when you actually need the backup.

> **Studying or presenting this project?** See **[REVIEW.md](REVIEW.md)** — a
> tour of the architecture, routing, outcome model, and a scripted live demo.

## The loop

One run does six things, in order:

```
0. check the pg_dump client matches the server major version
1. pg_dump the source  ->  /tmp/restoreguard/backup.dump   (custom format)
2. drop + recreate a disposable database named scratch_verify
3. pg_restore the dump into scratch_verify  (--exit-on-error)
4. run four checks against the restored copy
5. ALWAYS drop scratch_verify, then log one line and alert on failure
```

Step 5 happens in a `try/finally`, so the scratch database is dropped even when
step 3 or 4 blew up. **A run never leaves a database behind.**

## The four checks

| Check | Question it answers | Catches |
|---|---|---|
| `tables_exist` | Are all expected tables in the restore? | a dump that restored only partly |
| `row_counts` | Is any table suspiciously empty? | truncation, a bad `DELETE`, a broken seed |
| `integrity` | Are there orphan rows (broken foreign keys)? | corrupted or half-written data |
| `freshness` | Is the newest high-write row recent enough? | a **frozen source** (app down, replication stopped) |

`freshness` is the one no other tool covers. A dump can be perfectly
restorable *and* worthless — if the source stopped writing three days ago, the
backup faithfully preserved stale data.

## Outcomes — PASS / FAIL / ERROR

RestoreGuard reports three distinct things, and the distinction matters:

- **PASS** — dump, restore and all four checks succeeded.
- **FAIL** — the pipeline worked; a **check** said the data is bad (stale,
  empty, orphaned, missing table). This is the product working correctly.
- **ERROR** — the **pipeline** broke (corrupt dump, wrong client version,
  database unreachable). The checks may not have run at all.

`FAIL` means "your data needs attention". `ERROR` means "the tool itself
couldn't do its job". Conflating them is how alerting becomes noise.

## Layout

```
RestoreGuard/
├── restoreguard/
│   ├── verify.py            # the engine — one full run
│   ├── web.py               # Flask: /status (JSON) + /dashboard (HTML)
│   ├── templates/dashboard.html
│   └── static/style.css
├── sql/
│   ├── schema.sql           # sample source: 3 linked tables + data
│   └── refresh.sql          # simulate fresh writes (used by the demo)
├── tests/test_restoreguard.py
├── deploy/restoreguard.cron # the crontab line
├── logs/runs.jsonl          # append-only ledger (gitignored)
├── demo.sh
├── requirements.txt
├── .env.example
├── docker-compose.yml       # postgres 16 on :5433, schema auto-seeds
├── Makefile                 # up / run / demo / web / test
└── .github/workflows/ci.yml
```

## Quick start

    make up      # postgres 16 on :5433, schema auto-seeds on first boot
    make run     # one verification run
    make demo    # three scenarios: PASS / ERROR / FAIL
    make web     # dashboard on http://127.0.0.1:5001
    make test    # 20 tests, no database required

## Setup

Prerequisites: Docker, Python 3.12, and a Postgres **client** whose major
matches the server (see *The version trap*).

    make up                              # starts postgres 16, seeds schema
    python3.12 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    cp .env.example .env                 # edit if your port differs

`make up` uses `docker-compose.yml`. Postgres runs `sql/schema.sql` from
`/docker-entrypoint-initdb.d/` **on first boot only**, so the database seeds
itself. `make clean` (`docker compose down -v`) wipes the volume to re-seed.

## Running it

```
.venv/bin/python -m restoreguard.verify      # one verification run
.venv/bin/python -m restoreguard.web         # dashboard on http://127.0.0.1:5001
```

The web layer is read-only and has exactly two routes:

- `GET /status` → JSON for machines (a monitor, a health probe, `curl`).
- `GET /dashboard` → an HTML "inspection certificate" for humans, with a
  `meta refresh` so it updates itself. No JavaScript, no build step.

Both read the same file — `logs/runs.jsonl` — so there is nothing to keep in
sync. `verify.py` writes it; `web.py` reads it.

## The demo

`make demo` plays all three scenarios back-to-back so you can show the tool
changing its mind:

```
make demo
```

| Scenario | Trigger | Outcome |
|---|---|---|
| Healthy | reseed → run | `PASS` |
| Corrupt backup | `RESTOREGUARD_DEMO_CORRUPT=1` | `ERROR` (scratch still dropped) |
| Stale source | `UPDATE ... now() - interval '7 days'` | `FAIL` (freshness only) |

Watch the ledger grow in a second terminal with `tail -f logs/runs.jsonl`.

## Scheduling it (cron)

```
*/30 * * * * cd /Users/tejaskunta/RestoreGuard && .venv/bin/python -m restoreguard.verify >> logs/cron.log 2>&1
```

See `deploy/restoreguard.cron`. Notes:

- **Absolute paths, always.** cron does not read your shell profile, so it has
  a minimal `PATH`. `verify.py` therefore pins the Postgres client via
  `PG_CLIENT_BIN` in `.env` and prepends it to `PATH` itself.
- `verify.py` resolves its own root from `Path(__file__)`, so it works no
  matter what directory cron starts in.
- `>> logs/cron.log 2>&1` captures both stdout and stderr.

## Tests & CI

```
.venv/bin/python -m pytest -q        # 20 tests, no database required
```

The tests cover the version guard, the four checks, the log reader, and both
routes — using a fake connection and a temp log file, so they are fast and
CI needs no Postgres. `.github/workflows/ci.yml` runs them on Python 3.12
against a `postgres:16` service (matching the pinned production major).

## Design decisions

- **Orchestrate in Python, not bash.** Cleanup is a real `try/finally`, which
  bash makes awkward to get right.
- **`pg_dump --format=custom` + `pg_restore --exit-on-error`.** A plain `.sql`
  dump would happily half-restore and look fine. The custom archive format
  makes a truncated or damaged file *detectably* invalid (this is Demo A).
- **`logs/runs.jsonl` is append-only and is the single source of truth** for
  both `/status` and `/dashboard`. A crash can never lose history.
- **The version guard fails fast and loudly** rather than letting Postgres
  emit a cryptic archive error halfway through a restore.
- **`web.py` builds `/status` field-by-field** from an explicit allow-list
  rather than spreading the raw record, so nothing leaks if the log grows.

## The version trap

A Postgres client can read a *newer* server's data over the wire, but
`pg_dump`/`pg_restore` enforce the **archive format**, which is versioned:

- too-old client → `unsupported version (1.15) in file header`
- too-new client → writes `SET transaction_timeout = 0;`, a PG17-only
  setting a PG16 server rejects with `unrecognized configuration parameter`

So RestoreGuard requires the client and server **majors to match**, checks
this at the top of every run, and tells you the exact `brew install` /
`PG_CLIENT_BIN` fix if they don't. On EC2 the same class of bug appears when
Amazon Linux ships a client older than RDS — install `postgresql16` and pin it.

## Environment check

```
sw_vers -productVersion && uname -m
docker ps -a --format "{{.Names}} {{.Status}} {{.Ports}}" | grep rgdev
pg_dump --version && pg_restore --version
PGPASSWORD=devpass psql -h 127.0.0.1 -p 5433 -U rgadmin -d appdb -c "SELECT count(*) FROM order_events"
PGPASSWORD=devpass psql -h 127.0.0.1 -p 5433 -U rgadmin -d postgres -tAc "SELECT count(*) FROM pg_database WHERE datname='scratch_verify'"
```

Expect: `x86_64`, `rgdev` up on `5433`, client 16, 600 events, **0** scratch
databases.
