from __future__ import annotations

from typing import Any
from datetime import datetime, timedelta

from src.db.sqlite import connect_sqlite, execute_write, write_transaction


def _table_columns(conn, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row[1]) for row in rows}


def _ensure_stats_schema(conn) -> None:
    columns = _table_columns(conn, "stats")
    if not columns:
        execute_write(
            conn,
            """
            CREATE TABLE stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id INTEGER NOT NULL,
                uid INTEGER NOT NULL,
                uname TEXT NOT NULL,
                fans_num INTEGER NOT NULL,
                guard_num INTEGER NOT NULL,
                fan_club_num INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
        )
    elif "created_at" not in columns:
        execute_write(
            conn,
            "ALTER TABLE stats ADD COLUMN created_at TIMESTAMP",
        )

    execute_write(
        conn,
        "DROP INDEX IF EXISTS idx_stats_room_id",
    )
    execute_write(
        conn,
        """
        CREATE INDEX IF NOT EXISTS idx_stats_room_time
        ON stats(room_id, created_at)
        """,
    )


def init_stats_db() -> None:
    with write_transaction() as conn:
        _ensure_stats_schema(conn)


def insert_stats(
    room_id: int,
    uid: int,
    uname: str,
    fans_num: int,
    guard_num: int,
    fan_club_num: int,
    created_at: str,
) -> None:
    with write_transaction() as conn:
        execute_write(
            conn,
            """
            INSERT INTO stats (
                room_id, uid, uname, fans_num, guard_num, fan_club_num, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (room_id, uid, uname, fans_num, guard_num, fan_club_num, created_at),
        )


def list_stats(room_id: int, limit: int = 50) -> list[dict[str, Any]]:
    with connect_sqlite() as conn:
        rows = conn.execute(
            """
            SELECT room_id, uid, uname, fans_num, guard_num, fan_club_num, created_at, id
            FROM stats
            WHERE room_id = ?
            ORDER BY created_at DESC, id DESC
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
            "created_at": str(row[6]) if row[6] is not None else "",
            "id": int(row[7]),
        }
        for row in rows
    ]


def list_stats(room_id: int, days: int) -> list[dict[str, Any]]:
    now_local = datetime.now()
    start_local = (now_local - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    
    with connect_sqlite() as conn:
        rows = conn.execute(
            """
            SELECT 
                fans_num, 
                guard_num, 
                fan_club_num, 
                datetime(created_at, '+8 hours') as local_time
            FROM stats
            WHERE room_id = ? AND local_time >= ?
            ORDER BY local_time ASC
            """,
            (room_id, start_local),
        ).fetchall()
        
    return [
        {
            "fans_num": row[0],
            "guard_num": row[1],
            "fan_club_num": row[2],
            "created_at": row[3],
        }
        for row in rows
    ]