#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.db.event_search import (  # noqa: E402
    DEFAULT_BATCH_SIZE,
    DEFAULT_SEARCH_DB_PATH,
    get_event_search_status,
    index_pending_events,
    optimize_event_search_index,
    reset_event_search_db,
)
from src.db.sqlite import DEFAULT_DB_PATH  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build or incrementally update the event FTS5 search index."
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--search-database", type=Path, default=DEFAULT_SEARCH_DB_PATH)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="discard and rebuild only the derived search database",
    )
    parser.add_argument(
        "--optimize",
        action="store_true",
        help="merge FTS segments and reclaim free pages after an incremental run",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="show status without indexing",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    db_path = args.database.resolve()
    search_db_path = args.search_database.resolve()
    if args.status:
        print(
            json.dumps(
                get_event_search_status(db_path, search_db_path),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.rebuild:
        reset_event_search_db(search_db_path)

    started = time.monotonic()
    total_scanned = 0
    total_indexed = 0
    rounds = 0
    while True:
        result = index_pending_events(
            db_path,
            search_db_path,
            batch_size=args.batch_size,
            max_batches=20,
        )
        total_scanned += result["scanned_events"]
        total_indexed += result["indexed_documents"]
        rounds += 1
        if result["scanned_events"] or rounds == 1:
            print(
                "scanned={scanned} indexed={indexed} last_id={last_id} "
                "source_max={source_max} pending={pending} elapsed={elapsed:.1f}s".format(
                    scanned=total_scanned,
                    indexed=total_indexed,
                    last_id=result["last_indexed_event_id"],
                    source_max=result["source_max_event_id"],
                    pending=result["pending_events"],
                    elapsed=time.monotonic() - started,
                ),
                flush=True,
            )
        if result["pending_events"] == 0 or result["scanned_events"] == 0:
            break

    if args.rebuild or args.optimize:
        print("optimizing FTS segments...", flush=True)
        optimize_event_search_index(search_db_path)

    status = get_event_search_status(db_path, search_db_path)
    status["elapsed_seconds"] = round(time.monotonic() - started, 1)
    status["search_database_bytes"] = (
        search_db_path.stat().st_size if search_db_path.exists() else 0
    )
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
