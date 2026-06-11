# Drug Shortage Tracker

Tracks FDA drug shortages using the [openFDA drug shortages API](https://open.fda.gov/apis/drug/drugshortages/):
searchable by drug name, manufacturer, therapeutic category, and notice/resolve
dates, with email alerts when shortages are posted, updated, or resolved.
See [PLAN.md](PLAN.md) for the full design.

## Features

- **Search & browse** — grouped by drug (one shortage spans many
  manufacturer/presentation records), filterable by status, manufacturer,
  category, and date-of-notice / date-of-resolve ranges; per-drug detail page
  with manufacturer breakdown and a change-history timeline.
- **Change detection** — each sync diffs the openFDA feed against the local
  database and records `new_shortage` / `updated` / `resolved` / `reposted`
  events.
- **Email alerts** — subscribe by drug name, manufacturer, category, or all
  shortages; double-opt-in verification, one-click unsubscribe, and one digest
  email per sync run (never per event).
- **Dashboard & RSS** — current/resolved counts, changes this week, top
  affected categories, longest-running shortages, and an RSS feed of recent
  changes at `/feed.xml`.
- **JSON API** — everything above is served from a documented API (`/docs`).

## Quick start

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync                          # install dependencies
uv run pytest                    # run tests (43)

uv run python -m app.cli seed    # first import from openFDA (no events emitted)
uv run uvicorn app.api:app       # serve UI + API at http://127.0.0.1:8000
```

Then keep data fresh either way:

- set `SYNC_INTERVAL_HOURS=6` before starting uvicorn for an in-process
  scheduler (sync + notify), or
- run `uv run python -m app.cli sync` from cron every 6–12 hours.

### Docker

```sh
docker build -t drugshortage .
docker run -p 8000:8000 -v drugshortage-data:/data \
  -e SMTP_HOST=... -e SMTP_USERNAME=... -e SMTP_PASSWORD=... \
  -e MAIL_FROM=alerts@example.com -e BASE_URL=https://your.host \
  drugshortage
```

## Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:///drugshortage.db` | SQLAlchemy URL; Postgres works unchanged |
| `OPENFDA_API_KEY` | – | optional; raises openFDA rate limits ([free](https://open.fda.gov/apis/authentication/)) |
| `SYNC_INTERVAL_HOURS` | off | in-process sync + notify scheduler |
| `BASE_URL` | `http://localhost:8000` | absolute links in emails |
| `SMTP_HOST/PORT/USERNAME/PASSWORD` | – | any SMTP provider (SES, SendGrid, Resend…) |
| `SMTP_STARTTLS` | `1` | set `0` to disable STARTTLS |
| `MAIL_FROM` | `drugshortage@localhost` | From address |

Without `SMTP_HOST`, emails are printed to stdout (development mode).

## CLI

```sh
uv run python -m app.cli seed    # initial import, suppresses events
uv run python -m app.cli sync    # pull, diff, record events, send digests
uv run python -m app.cli notify  # send digests for pending events only
uv run python -m app.cli stats   # record/event counts
```

## Layout

- `app/openfda.py` — API client: pagination, date parsing, field normalization
  (tolerates known field-name aliases in the openFDA payload)
- `app/models.py` — `shortage_records` (one row per drug × company ×
  presentation), `shortage_events` (append-only change log), `subscriptions`
- `app/sync.py` — upsert + diff engine; record identity is a hash of
  generic name + company + presentation + NDC since openFDA provides no stable ID
- `app/notify.py` — matches pending events to subscriptions, builds digests
- `app/mailer.py` — SMTP (env-configured) or console fallback
- `app/api.py` — FastAPI app: search, detail, stats, subscriptions, RSS,
  optional scheduler; serves the static frontend
- `app/static/` — dependency-free HTML/JS/CSS single-page UI
- `app/cli.py` — `seed` / `sync` / `notify` / `stats`

## Notes

- Data is FDA's Drug Shortages database via openFDA, refreshed daily on
  business days — this tracker can't be more current than FDA's reporting.
  Not medical advice.
- No migration tooling yet; the schema is created with `create_all`. If you
  pull a schema-changing update, recreate the database (re-`seed`) or add
  Alembic before deploying anything you can't rebuild.
