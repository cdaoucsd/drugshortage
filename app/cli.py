"""Command-line entry points.

    python -m app.cli seed    # first import: full pull, no events emitted
    python -m app.cli sync    # incremental: full pull, diff, emit events
    python -m app.cli stats   # quick record/event counts

DATABASE_URL and OPENFDA_API_KEY are read from the environment.
"""

import argparse
import os
import sys

from sqlalchemy import func, select

from app.db import get_engine, init_db, make_session_factory
from app.models import ShortageEvent, ShortageRecord
from app.openfda import OpenFDAClient
from app.sync import sync_records


def _session_factory():
    engine = get_engine()
    init_db(engine)
    return make_session_factory(engine)


def cmd_sync(seed: bool) -> int:
    client = OpenFDAClient(api_key=os.environ.get("OPENFDA_API_KEY"))
    try:
        records = client.fetch_all()
    finally:
        client.close()
    print(f"fetched {len(records)} records from openFDA")

    factory = _session_factory()
    with factory() as session:
        if seed:
            has_rows = session.execute(select(ShortageRecord.id).limit(1)).first()
            if has_rows:
                print("database already seeded; use `sync` instead", file=sys.stderr)
                return 1
        result = sync_records(session, records, emit_events=not seed)
        session.commit()
    print(result.summary())
    return 0


def cmd_stats() -> int:
    factory = _session_factory()
    with factory() as session:
        by_status = session.execute(
            select(ShortageRecord.status, func.count()).group_by(ShortageRecord.status)
        ).all()
        n_events = session.execute(select(func.count(ShortageEvent.id))).scalar()
    for status, count in by_status:
        print(f"{status or '(no status)'}: {count}")
    print(f"events: {n_events}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="drugshortage")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("seed", help="initial full import (no events emitted)")
    sub.add_parser("sync", help="pull latest data, diff, and record events")
    sub.add_parser("stats", help="print record and event counts")
    args = parser.parse_args(argv)

    if args.command == "seed":
        return cmd_sync(seed=True)
    if args.command == "sync":
        return cmd_sync(seed=False)
    return cmd_stats()


if __name__ == "__main__":
    raise SystemExit(main())
