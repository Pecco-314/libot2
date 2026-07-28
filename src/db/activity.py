from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from src.db.sqlite import DEFAULT_DB_PATH, connect_sqlite, write_transaction


ACTIVITY_COLUMNS = (
    "id",
    "activity_id",
    "room_id",
    "uid",
    "uname",
    "timestamp",
    "item",
    "dy_type_str",
    "item_remote",
    "assets_localized",
    "created_at",
)
ACTIVITY_SELECT = ", ".join(ACTIVITY_COLUMNS)


def _create_activity_table(
    conn: sqlite3.Connection,
    table_name: str = "activity",
) -> None:
    if table_name not in {"activity", "activity_new"}:
        raise ValueError(f"unsupported activity table name: {table_name}")
    conn.execute(
        f"""
        CREATE TABLE {table_name} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            activity_id TEXT NOT NULL UNIQUE,
            room_id INTEGER NOT NULL,
            uid INTEGER NOT NULL,
            uname TEXT NOT NULL,
            timestamp INTEGER NOT NULL,
            item TEXT NOT NULL,
            dy_type_str TEXT NOT NULL DEFAULT '',
            item_remote TEXT NOT NULL,
            assets_localized INTEGER NOT NULL DEFAULT 0
                CHECK (assets_localized IN (0, 1)),
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def _activity_columns(conn: sqlite3.Connection) -> tuple[str, ...]:
    return tuple(
        str(row[1]) for row in conn.execute("PRAGMA table_info(activity)")
    )


def _migrate_activity_table(conn: sqlite3.Connection) -> None:
    source_columns = set(_activity_columns(conn))
    required = {
        "id",
        "activity_id",
        "room_id",
        "uid",
        "uname",
        "timestamp",
        "item",
        "dy_type_str",
        "item_remote",
        "assets_localized",
        "created_at",
    }
    missing = sorted(required - source_columns)
    if missing:
        raise RuntimeError(
            "activity cannot be migrated to the item-only schema; "
            f"missing columns: {', '.join(missing)}"
        )

    conn.execute("DROP TABLE IF EXISTS activity_new")
    _create_activity_table(conn, "activity_new")
    conn.execute(
        """
        INSERT INTO activity_new (
            id, activity_id, room_id, uid, uname, timestamp,
            item, dy_type_str, item_remote, assets_localized, created_at
        )
        SELECT
            id, activity_id, room_id, uid, uname, timestamp,
            item, COALESCE(dy_type_str, ''), item_remote,
            assets_localized, COALESCE(created_at, CURRENT_TIMESTAMP)
        FROM activity
        ORDER BY id
        """
    )
    conn.execute("DROP TABLE activity")
    conn.execute("ALTER TABLE activity_new RENAME TO activity")


def init_activity_db(db_path: Path = DEFAULT_DB_PATH) -> None:
    conn = connect_sqlite(db_path)
    try:
        # Rebuilding a referenced table requires foreign keys to be disabled
        # before the transaction. The asset rows are retained and checked before
        # commit.
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN IMMEDIATE")
        columns = _activity_columns(conn)
        if not columns:
            _create_activity_table(conn)
        elif columns != ACTIVITY_COLUMNS:
            _migrate_activity_table(conn)

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_activity_room_time
            ON activity(room_id, id)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_activity_room_timestamp
            ON activity(room_id, timestamp, activity_id)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS activity_asset (
                activity_id TEXT NOT NULL,
                remote_url TEXT NOT NULL,
                local_path TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                content_type TEXT,
                size_bytes INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (activity_id, remote_url),
                FOREIGN KEY (activity_id)
                    REFERENCES activity(activity_id)
                    ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_activity_asset_sha256
            ON activity_asset(content_sha256)
            """
        )
        violations = list(conn.execute("PRAGMA foreign_key_check"))
        if violations:
            raise RuntimeError(
                f"activity migration produced foreign key violations: "
                f"{violations[:3]}"
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.close()


def insert_activity(
    *,
    activity_id: str,
    room_id: int,
    uid: int,
    uname: str,
    timestamp: int,
    dy_type_str: str,
    item_dict: dict[str, Any],
    item_remote_dict: dict[str, Any] | None = None,
    assets: Sequence[dict[str, Any]] = (),
    assets_localized: bool = False,
    db_path: Path = DEFAULT_DB_PATH,
) -> bool:
    item_json = json.dumps(item_dict, ensure_ascii=False)
    remote_json = json.dumps(
        item_remote_dict if item_remote_dict is not None else item_dict,
        ensure_ascii=False,
    )
    with write_transaction(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO activity (
                activity_id, room_id, uid, uname, timestamp,
                item, dy_type_str, item_remote, assets_localized
            )
            SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?
            WHERE NOT EXISTS (
                SELECT 1 FROM activity WHERE activity_id = ?
            )
            """,
            (
                activity_id,
                room_id,
                uid,
                uname,
                timestamp,
                item_json,
                dy_type_str,
                remote_json,
                int(assets_localized),
                activity_id,
            ),
        )
        if cursor.rowcount > 0 and assets:
            conn.executemany(
                """
                INSERT OR REPLACE INTO activity_asset (
                    activity_id, remote_url, local_path, content_sha256,
                    content_type, size_bytes
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        activity_id,
                        str(asset["remote_url"]),
                        str(asset["local_path"]),
                        str(asset["content_sha256"]),
                        str(asset.get("content_type") or ""),
                        int(asset["size_bytes"]),
                    )
                    for asset in assets
                ],
            )
    return cursor.rowcount > 0


def get_max_activity_id() -> int:
    with connect_sqlite() as conn:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM activity").fetchone()
    return int(row[0]) if row is not None else 0


def activity_exists(
    activity_id: str,
    db_path: Path = DEFAULT_DB_PATH,
) -> bool:
    with connect_sqlite(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM activity WHERE activity_id = ? LIMIT 1",
            (activity_id,),
        ).fetchone()
    return row is not None


def get_newest_activity() -> dict[str, Any] | None:
    with connect_sqlite() as conn:
        row = conn.execute(
            f"""
            SELECT {ACTIVITY_SELECT}
            FROM activity
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    return _row_to_dict(row) if row is not None else None


def list_activities_after(last_id: int, limit: int = 100) -> list[dict[str, Any]]:
    with connect_sqlite() as conn:
        rows = conn.execute(
            f"""
            SELECT {ACTIVITY_SELECT}
            FROM activity
            WHERE id > ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (last_id, limit),
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def list_activities_by_month(
    room_id: int,
    year: int,
    month: int,
    db_path: Path = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    start = datetime(year, month, 1)
    if month == 12:
        end = datetime(year + 1, 1, 1)
    else:
        end = datetime(year, month + 1, 1)

    with connect_sqlite(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT {ACTIVITY_SELECT}
            FROM activity
            WHERE room_id = ?
              AND timestamp >= ?
              AND timestamp < ?
              AND dy_type_str != 'DYNAMIC_TYPE_LIVE_RCMD'
            ORDER BY timestamp ASC, activity_id ASC
            """,
            (room_id, int(start.timestamp()), int(end.timestamp())),
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def list_activities_by_date(
    room_id: int,
    target_date: str,
    db_path: Path = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    start = datetime.strptime(target_date, "%Y-%m-%d")
    end = start + timedelta(days=1)
    with connect_sqlite(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT {ACTIVITY_SELECT}
            FROM activity
            WHERE room_id = ?
              AND timestamp >= ?
              AND timestamp < ?
              AND dy_type_str != 'DYNAMIC_TYPE_LIVE_RCMD'
            ORDER BY timestamp ASC, activity_id ASC
            """,
            (
                room_id,
                int(start.timestamp()),
                int(end.timestamp()),
            ),
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def get_activity_by_date_index(
    room_id: int,
    target_date: str,
    index: int,
    db_path: Path = DEFAULT_DB_PATH,
) -> dict[str, Any] | None:
    if index < 1:
        return None

    rows = list_activities_by_date(room_id, target_date, db_path)
    return rows[index - 1] if index <= len(rows) else None


def _parse_item(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _row_to_dict(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": int(row[0]),
        "activity_id": str(row[1]),
        "room_id": int(row[2]),
        "uid": int(row[3]),
        "uname": str(row[4]),
        "timestamp": int(row[5]),
        "item": _parse_item(row[6]),
        "dy_type_str": str(row[7]),
        "item_remote": _parse_item(row[8]),
        "assets_localized": bool(row[9]),
        "created_at": str(row[10]),
    }
