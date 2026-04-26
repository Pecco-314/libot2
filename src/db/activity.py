from __future__ import annotations

import json
from typing import Any

from src.db.sqlite import connect_sqlite, execute_write, write_transaction


def init_activity_db() -> None:
    with write_transaction() as conn:
        execute_write(
            conn,
            """
            CREATE TABLE IF NOT EXISTS activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                activity_id TEXT NOT NULL UNIQUE,
                room_id INTEGER NOT NULL,
                uid INTEGER NOT NULL,
                uname TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                dy_type INTEGER NOT NULL,
                orig_type INTEGER NOT NULL,
                card TEXT NOT NULL,
                emoji_details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
        )
        execute_write(
            conn,
            """
            CREATE INDEX IF NOT EXISTS idx_activity_room_time
            ON activity(room_id, id)
            """,
        )


def insert_activity(
    *,
    activity_id: str,
    room_id: int,
    uid: int,
    uname: str,
    timestamp: int,
    dy_type: int,
    orig_type: int,
    card: dict[str, Any],
    emoji_details: list[dict[str, Any]] | None,
) -> bool:
    emoji_details_json = json.dumps(emoji_details or [], ensure_ascii=False)
    with write_transaction() as conn:
        cursor = execute_write(
            conn,
            """
            INSERT OR IGNORE INTO activity (
                activity_id, room_id, uid, uname, timestamp, dy_type,
                orig_type, card, emoji_details
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                activity_id,
                room_id,
                uid,
                uname,
                timestamp,
                dy_type,
                orig_type,
                card,
                emoji_details_json,
            ),
        )
    return cursor.rowcount > 0


def get_max_activity_id() -> int:
    with connect_sqlite() as conn:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM activity").fetchone()
    if row is None:
        return 0
    return int(row[0])


def get_newest_activity() -> dict[str, Any] | None:
    with connect_sqlite() as conn:
        row = conn.execute(
            """
            SELECT id, activity_id, room_id, uid, uname, timestamp, dy_type,
                   orig_type, card, emoji_details, created_at
            FROM activity
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    if row is None:
        return None
    return _row_to_dict(row)


def list_activities_after(last_id: int, limit: int = 100) -> list[dict[str, Any]]:
    with connect_sqlite() as conn:
        rows = conn.execute(
            """
            SELECT id, activity_id, room_id, uid, uname, timestamp, dy_type,
                   orig_type, card, emoji_details, created_at
            FROM activity
            WHERE id > ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (last_id, limit),
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def _row_to_dict(row) -> dict[str, Any]:
    emoji_details: list[dict[str, Any]] = []
    raw_emoji_details = row[9]
    if raw_emoji_details:
        try:
            parsed = json.loads(raw_emoji_details)
            if isinstance(parsed, list):
                emoji_details = parsed
        except Exception:
            emoji_details = []

    return {
        "id": int(row[0]),
        "activity_id": str(row[1]),
        "room_id": int(row[2]),
        "uid": int(row[3]),
        "uname": str(row[4]),
        "timestamp": int(row[5]),
        "dy_type": int(row[6]),
        "orig_type": int(row[7]),
        "card": str(row[8]),
        "emoji_details": emoji_details,
        "created_at": str(row[10]) if row[10] is not None else "",
    }
