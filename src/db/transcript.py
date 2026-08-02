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

def get_recent_transcripts(room_id: int, end_ts: int, window_seconds: int = 60, limit: int = 15) -> list[str]:
    """获取指定时间窗口内的 ASR 记录，按时间倒序"""
    from src.db.sqlite import connect_sqlite
    with connect_sqlite() as conn:
        rows = conn.execute(
            """
            SELECT content FROM transcript
            WHERE room_id = ? AND timestamp >= ? AND timestamp <= ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (room_id, end_ts - window_seconds, end_ts + 2, limit)
        ).fetchall()
    return [row[0] for row in rows if row[0]]


def list_transcripts_in_range(
    room_id: int,
    start_ts: int,
    end_ts: int,
) -> list[dict[str, Any]]:
    """按时间顺序返回指定范围内的 ASR 记录。"""
    with connect_sqlite() as conn:
        rows = conn.execute(
            """
            SELECT id, content, timestamp
            FROM transcript
            WHERE room_id = ? AND timestamp >= ? AND timestamp <= ?
            ORDER BY timestamp ASC, id ASC
            """,
            (room_id, start_ts, end_ts),
        ).fetchall()
    return [
        {
            "id": int(row[0]),
            "content": str(row[1]),
            "timestamp": int(row[2]),
        }
        for row in rows
        if row[1]
    ]
