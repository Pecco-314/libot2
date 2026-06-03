from __future__ import annotations

import json
from typing import Any

from src.db.sqlite import connect_sqlite, execute_write, write_transaction


def init_activity_db() -> None:
    with write_transaction() as conn:
        # 兼容性迁移：检查表字段，如果不存在新版字段，则无损追加字段
        cursor = execute_write(conn, "PRAGMA table_info(activity)")
        columns = [col[1] for col in cursor.fetchall()]
        
        if columns: # 表已存在
            if "item" not in columns:
                execute_write(conn, "ALTER TABLE activity ADD COLUMN item TEXT")
            if "dy_type_str" not in columns:
                execute_write(conn, "ALTER TABLE activity ADD COLUMN dy_type_str TEXT")
        else:
            # 首次运行，直接创建包含新老字段的表
            execute_write(
                conn,
                """
                CREATE TABLE activity (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    activity_id TEXT NOT NULL UNIQUE,
                    room_id INTEGER NOT NULL,
                    uid INTEGER NOT NULL,
                    uname TEXT NOT NULL,
                    timestamp INTEGER NOT NULL,
                    dy_type INTEGER NOT NULL DEFAULT 0,     -- [遗留] 老版动态类型
                    orig_type INTEGER NOT NULL DEFAULT 0,   -- [遗留] 老版源动态类型
                    card TEXT NOT NULL DEFAULT '',          -- [遗留] 老版 JSON
                    emoji_details TEXT,                     -- [遗留] 老版表情
                    item TEXT,                              -- [新版] 完整动态字典 JSON
                    dy_type_str TEXT,                       -- [新版] 字符串动态类型
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
    dy_type_str: str,
    item_dict: dict[str, Any],
) -> bool:
    item_json = json.dumps(item_dict, ensure_ascii=False)
    with write_transaction() as conn:
        # 新数据插入时，遗留字段用 0 或空字符串填充，满足旧版的 NOT NULL 约束
        cursor = execute_write(
            conn,
            """
            INSERT OR IGNORE INTO activity (
                activity_id, room_id, uid, uname, timestamp, 
                dy_type, orig_type, card, emoji_details,
                item, dy_type_str
            ) VALUES (?, ?, ?, ?, ?, 0, 0, '', '[]', ?, ?)
            """,
            (
                activity_id,
                room_id,
                uid,
                uname,
                timestamp,
                item_json,
                dy_type_str,
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
        # 修改 SELECT，显式提取追加的新字段 item 和 dy_type_str
        row = conn.execute(
            """
            SELECT id, activity_id, room_id, uid, uname, timestamp, dy_type,
                   orig_type, card, emoji_details, created_at, item, dy_type_str
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
                   orig_type, card, emoji_details, created_at, item, dy_type_str
            FROM activity
            WHERE id > ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (last_id, limit),
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def _row_to_dict(row) -> dict[str, Any]:
    # 解析新版数据
    item_dict: dict[str, Any] = {}
    raw_item = row[11]
    if raw_item:
        try:
            item_dict = json.loads(raw_item)
        except Exception:
            pass

    return {
        "id": int(row[0]),
        "activity_id": str(row[1]),
        "room_id": int(row[2]),
        "uid": int(row[3]),
        "uname": str(row[4]),
        "timestamp": int(row[5]),
        # 遗留字段予以保留以防系统其他角落引用
        "dy_type": int(row[6]),
        "orig_type": int(row[7]),
        "card": str(row[8]),
        "created_at": str(row[10]) if row[10] is not None else "",
        # 新字段
        "item": item_dict,
        "dy_type_str": str(row[12]) if row[12] is not None else "",
    }