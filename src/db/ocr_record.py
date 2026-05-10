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


def get_recent_ocr_texts(room_id: int, end_ts: int, window_seconds: int = 60, limit: int = 5) -> list[str]:
    """获取指定时间窗口内的 OCR 文本片段，自动解析 JSON 列表并平铺展平"""
    from src.db.sqlite import connect_sqlite
    with connect_sqlite() as conn:
        rows = conn.execute(
            """
            SELECT content FROM ocr_record
            WHERE room_id = ? AND timestamp >= ? AND timestamp <= ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (room_id, end_ts - window_seconds, end_ts + 2, limit)
        ).fetchall()
    
    texts = []
    for row in rows:
        try:
            content_list = json.loads(row[0])
            if isinstance(content_list, list):
                texts.extend(content_list)
        except json.JSONDecodeError:
            pass
    return texts