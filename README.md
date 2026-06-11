# Drug Shortage Tracker

Tracks FDA drug shortages using the [openFDA drug shortages API](https://open.fda.gov/apis/drug/drugshortages/),
searchable by drug name, manufacturer, and notice/resolve dates, with change
notifications. See [PLAN.md](PLAN.md) for the full design.

## Status

**Phases 1–2 done.** Data layer (openFDA client, SQLite schema, sync/diff
engine) plus the JSON API and search web UI. Phase 3 (email notifications)
and Phase 4 (dashboard, polish) are next.

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync          # install dependencies
uv run pytest    # run tests
```

## Usage

```sh
uv run python -m app.cli seed    # first import: full pull, no events emitted
uv run python -m app.cli sync    # incremental: pull, diff, record change events
uv run python -m app.cli stats   # record/event counts

uv run uvicorn app.api:app       # serve web UI + API at http://127.0.0.1:8000
```

The web UI supports search by drug/brand/manufacturer, filters for status,
therapeutic category, and posted/resolved date ranges, and a per-drug detail
page with the manufacturer breakdown and change history. Interactive API docs
are at `/docs`.

Environment variables:

- `DATABASE_URL` — SQLAlchemy URL (default `sqlite:///drugshortage.db`)
- `OPENFDA_API_KEY` — optional; raises openFDA rate limits ([get one free](https://open.fda.gov/apis/authentication/))

Run `sync` on a schedule (cron, every 6–12 hours). Each run upserts the full
openFDA dataset and appends `shortage_events` rows (`new_shortage`, `updated`,
`resolved`, `reposted`) that will drive notifications in Phase 3.

## Layout

- `app/openfda.py` — API client: pagination, date parsing, field normalization
  (tolerates known field-name aliases in the openFDA payload)
- `app/models.py` — `shortage_records` (one row per drug × company ×
  presentation) and `shortage_events` (append-only change log)
- `app/sync.py` — upsert + diff engine; record identity is a hash of
  generic name + company + presentation + NDC since openFDA provides no stable ID
- `app/api.py` — FastAPI app: grouped drug search, record search, detail with
  event timeline, stats; serves the static frontend
- `app/static/` — dependency-free HTML/JS/CSS single-page UI
- `app/cli.py` — `seed` / `sync` / `stats` commands
