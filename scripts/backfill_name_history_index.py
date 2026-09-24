#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.db.name_history import init_name_history_index
from src.db.sqlite import DEFAULT_DB_PATH, connect_sqlite, write_transaction


def _meta(conn, key: str) -> int:
    row = conn.execute(
        "SELECT value FROM name_history_index_meta WHERE key = ?", (key,)
    ).fetchone()
    return int(row[0]) if row else 0


def backfill(db_path: Path, batch_size: int) -> None:
    init_name_history_index(db_path)
    with connect_sqlite(db_path) as conn:
        complete = _meta(conn, "complete")
        cursor = _meta(conn, "cursor_event_id")
        target = _meta(conn, "target_event_id")
        if complete or target <= 0:
            target = int(
                conn.execute("SELECT COALESCE(MAX(id), 0) FROM event").fetchone()[0]
            )
            with write_transaction(db_path) as write_conn:
                write_conn.execute(
                    "UPDATE name_history_index_meta SET value = ? WHERE key = 'target_event_id'",
                    (target,),
                )

    while cursor < target:
        upper = min(target, cursor + batch_size)
        with write_transaction(db_path) as conn:
            conn.execute(
                """
                INSERT INTO name_history(uid, uname, first_seen)
                SELECT uid, uname, MIN(timestamp)
                FROM event
                WHERE id > ? AND id <= ?
                  AND uid IS NOT NULL AND uname IS NOT NULL
                  AND TRIM(uname) <> '' AND timestamp IS NOT NULL
                GROUP BY uid, uname
                ON CONFLICT(uid, uname) DO UPDATE SET
                    first_seen = MIN(name_history.first_seen, excluded.first_seen)
                """,
                (cursor, upper),
            )
            conn.execute(
                "UPDATE name_history_index_meta SET value = ? WHERE key = 'cursor_event_id'",
                (upper,),
            )
        cursor = upper
        print(f"backfilled={cursor}/{target}", flush=True)

    with write_transaction(db_path) as conn:
        conn.execute(
            "UPDATE name_history_index_meta SET value = 1 WHERE key = 'complete'"
        )
        indexed = int(conn.execute("SELECT COUNT(*) FROM name_history").fetchone()[0])
    print(f"complete target={target} indexed_names={indexed}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill the transactional name index")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--batch-size", type=int, default=50_000)
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("--batch-size must be positive")
    try:
        backfill(args.db.resolve(), args.batch_size)
    except sqlite3.Error as exc:
        print(f"backfill failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
