from __future__ import annotations

from typing import Any

from src.db.sqlite import connect_sqlite, execute_write, write_transaction


def init_liver_stats_db() -> None:
    with write_transaction() as conn:
        execute_write(
            conn,
            """
            CREATE TABLE IF NOT EXISTS liver_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id INTEGER NOT NULL,
                uid INTEGER NOT NULL,
                uname TEXT NOT NULL,
                fans_num INTEGER NOT NULL,
                guard_num INTEGER NOT NULL,
                fan_club_num INTEGER NOT NULL,
                recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
        )
        execute_write(
            conn,
            """
            CREATE INDEX IF NOT EXISTS idx_liver_stats_room_time
            ON liver_stats(room_id, recorded_at)
            """,
        )


def insert_liver_stats(
    room_id: int,
    uid: int,
    uname: str,
    fans_num: int,
    guard_num: int,
    fan_club_num: int,
) -> None:
    with write_transaction() as conn:
        execute_write(
            conn,
            """
            INSERT INTO liver_stats (
                room_id, uid, uname, fans_num, guard_num, fan_club_num
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (room_id, uid, uname, fans_num, guard_num, fan_club_num),
        )


def list_liver_stats(room_id: int, limit: int = 50) -> list[dict[str, Any]]:
    with connect_sqlite() as conn:
        rows = conn.execute(
            """
            SELECT room_id, uid, uname, fans_num, guard_num, fan_club_num, recorded_at
            FROM liver_stats
            WHERE room_id = ?
            ORDER BY recorded_at DESC, id DESC
            LIMIT ?
            """,
            (room_id, limit),
        ).fetchall()
    return [
        {
            "room_id": int(row[0]),
            "uid": int(row[1]),
            "uname": str(row[2]),
            "fans_num": int(row[3]),
            "guard_num": int(row[4]),
            "fan_club_num": int(row[5]),
            "recorded_at": str(row[6]),
        }
        for row in rows
    ]
