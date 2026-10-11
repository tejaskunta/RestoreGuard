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
└── .github/workflows/ci.yml
```

## Setup

Postgres client major **must equal** the server major (see *The version trap*
below). Local dev uses a Postgres 16 container:

```
docker run -d --name rgdev -e POSTGRES_USER=rgadmin -e POSTGRES_PASSWORD=devpass -e POSTGRES_DB=appdb -p 5433:5432 postgres:16
brew install postgresql@16
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
PGPASSWORD=devpass psql -h 127.0.0.1 -p 5433 -U rgadmin -d appdb -f sql/schema.sql
cp .env.example .env        # then edit PG_CLIENT_BIN and the DB values
```

> **Port drift:** on this machine the database is on **5433**, not 5432 —
> 5432 belongs to another project (`aeroshield-db`). Never assume the default
> port. Everything reads the port from `.env` (`SOURCE_DB_PORT`), so it is
> configured in exactly one place.

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

`demo.sh` plays all five scenarios back-to-back so you can show the tool
changing its mind:

```
./demo.sh
```

| Scenario | Trigger | Outcome |
|---|---|---|
| Healthy | reseed → run | `4 PASS` |
| Corrupt backup | `RESTOREGUARD_DEMO_CORRUPT=1` | `ERROR` (scratch still dropped) |
| Missing table | `DROP TABLE order_events CASCADE` | `4 FAIL` |
| Stale source | `UPDATE ... now() - interval '7 days'` | `1 FAIL` (freshness only) |
| Reset | `sql/refresh.sql` → run | `4 PASS` |

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
.venv/bin/python -m pytest -q        # 22 tests, no database required
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