# Drug Shortage Tracker

Tracks FDA drug shortages using the [openFDA drug shortages API](https://open.fda.gov/apis/drug/drugshortages/),
searchable by drug name, manufacturer, and notice/resolve dates, with change
notifications. See [PLAN.md](PLAN.md) for the full design.

## Status

**Phase 1 (data layer) — done.** openFDA client, SQLite schema, and the
sync/diff engine that detects new, updated, resolved, and reposted shortages.
Phases 2–4 (API + search UI, notifications, dashboard) are next.

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
```

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
- `app/cli.py` — `seed` / `sync` / `stats` commands
