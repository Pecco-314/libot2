from __future__ import annotations

from typing import Any

from src.db.sqlite import connect_sqlite, execute_write, write_transaction


def init_transcript_db() -> None:
    with write_transaction() as conn:
        execute_write(
            conn,
            """
            CREATE TABLE IF NOT EXISTS transcript (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
        )
        execute_write(
            conn,
            """
            CREATE INDEX IF NOT EXISTS idx_transcript_room_time
            ON transcript(room_id, timestamp)
            """,
        )


def insert_transcript(
    *,
    room_id: int,
    content: str,
    timestamp: int,
) -> bool:
    with write_transaction() as conn:
        cursor = execute_write(
            conn,
            """
            INSERT INTO transcript (room_id, content, timestamp)
            VALUES (?, ?, ?)
            """,
            (room_id, content, timestamp),
        )
    return cursor.rowcount > 0


def list_transcripts_after(last_id: int, limit: int = 100) -> list[dict[str, Any]]:
    with connect_sqlite() as conn:
        rows = conn.execute(
            """
            SELECT id, room_id, content, timestamp, created_at
            FROM transcript
            WHERE id > ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (last_id, limit),
        ).fetchall()
    return [
        {
            "id": int(row[0]),
            "room_id": int(row[1]),
            "content": str(row[2]),
            "timestamp": int(row[3]),
            "created_at": str(row[4]) if row[4] is not None else None,
        }
        for row in rows
    ]
