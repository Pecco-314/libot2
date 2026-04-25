from __future__ import annotations

from src.db.sqlite import connect_sqlite
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

local_tz = ZoneInfo("Asia/Shanghai")


def get_newest_live_event() -> dict[str, object] | None:
    live_cmds = ["LIVE", "PREPARING", "ROOM_CHANGE"]
    with connect_sqlite() as conn:
        row = conn.execute(
            "SELECT id, cmd, room_id, title, timestamp FROM event WHERE cmd IN (?, ?, ?) ORDER BY timestamp DESC, id DESC LIMIT 1",
            tuple(live_cmds),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": int(row[0]),
        "cmd": str(row[1]),
        "room_id": int(row[2]),
        "title": str(row[3]) if row[3] is not None else None,
        "timestamp": int(row[4])
    }

def is_streaming_event(row) -> bool:
    """判断LIVE事件是否是推流而非真的开播"""
    cmd = row.get("cmd")
    room_id = row.get("room_id")
    event_id = row.get("id")
    if cmd != "LIVE":
        return False
    with connect_sqlite() as conn:
        row = conn.execute(
            """
            SELECT cmd FROM event
            WHERE room_id = ? AND cmd IN ('LIVE', 'PREPARING') AND id < ?
            ORDER BY id DESC LIMIT 1
            """,
            (room_id, event_id),
        ).fetchone()
    if row is None:
        return False
    return row[0] == "LIVE"

def is_duplicate_room_change(row) -> bool:
    """判断是否出现重复的ROOM_CHANGE事件"""
    room_id = row.get("room_id")
    event_id = row.get("id")
    cmd = row.get("cmd")
    title = row.get("title")
    if cmd != "ROOM_CHANGE":
        return False
    with connect_sqlite() as conn:
        row = conn.execute(
            """
            SELECT title FROM event
            WHERE room_id = ? AND cmd = 'ROOM_CHANGE' AND id < ?
            ORDER BY id DESC LIMIT 1
            """,
            (room_id, event_id),
        ).fetchone()
    if row is None:
        return False
    return title == row[0]


def list_superchat_events(room_id: int, from_time: int, to_time: int) -> list[dict[str, object]]:
    with connect_sqlite() as conn:
        rows = conn.execute(
            """
            SELECT uname, content, total_coin, timestamp FROM event
            WHERE room_id = ? AND cmd = 'SUPER_CHAT_MESSAGE' AND timestamp >= ? AND timestamp <= ?
            ORDER BY timestamp ASC, id ASC
            """,
            (room_id, from_time, to_time),
        ).fetchall()
    return [
        {
            "uname": row[0],
            "content": row[1],
            "price": row[2],
            "timestamp": row[3]
        }
        for row in rows
    ]

def list_superchat_event_by_day(room_id: int, day: datetime) -> list[dict[str, object]]:
    start_of_day = int(day.replace(hour=0, minute=0, second=0).timestamp())
    end_of_day = int(day.replace(hour=23, minute=59, second=59).timestamp())
    return list_superchat_events(room_id, start_of_day, end_of_day)


if __name__ == "__main__":
    d = list_superchat_event_by_day(1967216004, datetime.now(local_tz) - timedelta(days=2))
    print(d)