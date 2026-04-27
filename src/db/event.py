from __future__ import annotations

from src.db.sqlite import connect_sqlite
from datetime import datetime
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


def list_name_history_by_uid(uid: int) -> list[str]:
    """
    根据 uid 查询用户的所有曾用名，按出现时间先后排序。
    """
    with connect_sqlite() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT uname 
            FROM event 
            WHERE uid = ? 
            ORDER BY timestamp ASC
            """,
            (uid,),
        ).fetchall()

    if not rows:
        return []
    else:
        return [{
            "uid": uid,
            "history": [row[0] for row in rows],
        }]


def list_name_history_by_name(target_name: str) -> list[dict[str, object]]:
    """
    通过一个曾用名（或当前名）查询所有使用过该名字的用户及其完整的改名历史。
    由于可能存在重名情况，返回一个包含多个用户信息的列表。
    """
    with connect_sqlite() as conn:
        rows = conn.execute(
            """
            SELECT uid, uname
            FROM event
            WHERE uid IN (
                SELECT DISTINCT uid 
                FROM event 
                WHERE uname = ?
            )
            GROUP BY uid, uname
            ORDER BY uid ASC, timestamp ASC
            """,
            (target_name,),
        ).fetchall()

    # 将结果按 uid 分组处理
    result = []
    current_uid = None
    user_entry = None

    for uid, uname in rows:
        if uid != current_uid:
            if user_entry:
                result.append(user_entry)
            
            current_uid = uid
            user_entry = {
                "uid": uid,
                "history": []
            }

        user_entry["history"].append(uname)

    if user_entry:
        result.append(user_entry)

    return result


def list_name_history_by_name_or_uid(query: str) -> list[dict[str, object]]:
    if query.isdigit():
        return list_name_history_by_uid(int(query))
    else:
        return list_name_history_by_name(query)


if __name__ == "__main__":
    # name_history = list_name_history_by_name("_Misuzu")
    name_history = list_name_history_by_name_or_uid("1")
    
    print(f"曾用名历史：{name_history}")