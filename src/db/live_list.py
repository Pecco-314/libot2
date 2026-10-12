from __future__ import annotations

import json
from pathlib import Path

from src.db.sqlite import DEFAULT_DB_PATH, connect_sqlite, execute_write, write_transaction


def init_live_list_db() -> None:
    with write_transaction() as conn:
        execute_write(
            conn,
            """
            CREATE TABLE IF NOT EXISTS global_live_list (
                room_id INTEGER PRIMARY KEY
            )
            """,
        )
        # 兼容旧版本数据库，追加列
        try:
            conn.execute("ALTER TABLE global_live_list ADD COLUMN uname TEXT")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE global_live_list ADD COLUMN adder_uid INTEGER")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE global_live_list ADD COLUMN tags TEXT")
        except Exception:
            pass


def add_live_list(room_id: int, uname: str | None, adder_uid: int, tags: list[str]) -> bool:
    tags_str = json.dumps(tags, ensure_ascii=False)
    with write_transaction() as conn:
        row = conn.execute(
            "SELECT 1 FROM global_live_list WHERE room_id = ?",
            (room_id,),
        ).fetchone()
        if row is not None:
            return False
        execute_write(
            conn,
            "INSERT INTO global_live_list (room_id, uname, adder_uid, tags) VALUES (?, ?, ?, ?)",
            (room_id, uname, adder_uid, tags_str),
        )
        return True


def remove_live_list(room_id: int) -> bool:
    with write_transaction() as conn:
        row = conn.execute(
            "SELECT 1 FROM global_live_list WHERE room_id = ?",
            (room_id,),
        ).fetchone()
        if row is None:
            return False
        execute_write(
            conn,
            "DELETE FROM global_live_list WHERE room_id = ?",
            (room_id,),
        )
        return True


def update_live_list_uname(room_id: int, uname: str) -> None:
    with write_transaction() as conn:
        execute_write(
            conn,
            "UPDATE global_live_list SET uname = ? WHERE room_id = ?",
            (uname, room_id),
        )


def update_live_list_tags(room_id: int, tags: list[str]) -> None:
    tags_str = json.dumps(tags, ensure_ascii=False)
    with write_transaction() as conn:
        execute_write(
            conn,
            "UPDATE global_live_list SET tags = ? WHERE room_id = ?",
            (tags_str, room_id),
        )


def _parse_tags(tags_raw: str | None) -> list[str]:
    if not tags_raw:
        return []
    try:
        parsed = json.loads(tags_raw)
        return [tag for tag in parsed if isinstance(tag, str)] if isinstance(parsed, list) else []
    except Exception:
        return []


def get_live_list_info(room_id: int) -> dict | None:
    with connect_sqlite() as conn:
        row = conn.execute(
            "SELECT room_id, uname, adder_uid, tags FROM global_live_list WHERE room_id = ?",
            (room_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "room_id": int(row[0]),
            "uname": row[1],
            "adder_uid": int(row[2]) if row[2] else 0,
            "tags": _parse_tags(row[3]),
        }


def get_live_list(db_path: Path = DEFAULT_DB_PATH) -> list[dict]:
    with connect_sqlite(db_path) as conn:
        rows = conn.execute(
            "SELECT room_id, uname, adder_uid, tags FROM global_live_list",
        ).fetchall()
    return [
        {
            "room_id": int(r[0]),
            "uname": r[1],
            "adder_uid": int(r[2]) if r[2] else 0,
            "tags": _parse_tags(r[3]),
        }
        for r in rows
    ]


def get_live_list_by_tag(tag: str, db_path: Path = DEFAULT_DB_PATH) -> list[dict]:
    """Return every tracked room with an exact, case-insensitive tag match."""
    normalized_tag = tag.strip().casefold()
    if not normalized_tag:
        return []

    rooms = get_live_list(db_path)
    matched = [
        room
        for room in rooms
        if any(item.casefold() == normalized_tag for item in room["tags"])
    ]
    return sorted(
        matched,
        key=lambda room: ((room.get("uname") or "").casefold(), room["room_id"]),
    )
