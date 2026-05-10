from __future__ import annotations

import json
from typing import Any

from src.db.sqlite import connect_sqlite, execute_write, write_transaction


def init_ocr_db() -> None:
    with write_transaction() as conn:
        execute_write(
            conn,
            """
            CREATE TABLE IF NOT EXISTS ocr_record (
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
            CREATE INDEX IF NOT EXISTS idx_ocr_room_time
            ON ocr_record(room_id, timestamp)
            """,
        )


def insert_ocr_record(
    *,
    room_id: int,
    content: list[str],  # 参数类型改为 list[str]
    timestamp: int,
) -> bool:
    # 将列表序列化为 JSON 字符串，保留中文
    json_content = json.dumps(content, ensure_ascii=False)
    
    with write_transaction() as conn:
        cursor = execute_write(
            conn,
            """
            INSERT INTO ocr_record (room_id, content, timestamp)
            VALUES (?, ?, ?)
            """,
            (room_id, json_content, timestamp),
        )
    return cursor.rowcount > 0