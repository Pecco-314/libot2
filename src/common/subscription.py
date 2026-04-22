from __future__ import annotations

import sqlite3
from typing import Any

from src.common.sqlite import connect_sqlite, execute_write, write_transaction


def init_subscription_db() -> None:
    with write_transaction() as conn:
        execute_write(
            conn,
            """
            CREATE TABLE IF NOT EXISTS subscription (
                group_id INTEGER PRIMARY KEY,
                room_id INTEGER NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
        )


def set_subscription(group_id: int, room_id: int) -> None:
    with write_transaction() as conn:
        execute_write(
            conn,
            """
            INSERT INTO subscription (group_id, room_id)
            VALUES (?, ?)
            ON CONFLICT(group_id)
            DO UPDATE SET room_id = excluded.room_id, updated_at = CURRENT_TIMESTAMP
            """,
            (group_id, room_id),
        )


def get_subscription(group_id: int) -> int | None:
    with connect_sqlite() as conn:
        row = conn.execute(
            "SELECT room_id FROM subscription WHERE group_id = ?",
            (group_id,),
        ).fetchone()
    if row is None:
        return None
    return int(row[0])


def remove_subscription(group_id: int) -> bool:
    with write_transaction() as conn:
        cur = execute_write(conn, "DELETE FROM subscription WHERE group_id = ?", (group_id,))
    return cur.rowcount > 0


def list_subscribed_room_ids() -> list[int]:
    with connect_sqlite() as conn:
        rows = conn.execute("SELECT DISTINCT room_id FROM subscription ORDER BY room_id ASC").fetchall()
    return [int(row[0]) for row in rows]
