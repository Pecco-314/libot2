from __future__ import annotations

from src.db.sqlite import connect_sqlite
from datetime import datetime

def _table_exists(conn, table_name: str) -> bool:
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
    return cur.fetchone() is not None

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


def _merge_and_sort_histories(history_rows: list, event_rows: list) -> list[dict[str, object]]:
    user_data = {}  # 结构: {uid: {uname: min_timestamp}}
    
    # 融合并保留每个名字的最早时间戳
    for uid, uname, ts in history_rows + event_rows:
        if uid not in user_data:
            user_data[uid] = {}
            
        # 如果名字没出现过，或者这次的时间更早，则更新
        if uname not in user_data[uid] or ts < user_data[uid][uname]:
            user_data[uid][uname] = ts
            
    # 格式化输出
    result = []
    for uid, name_ts_map in user_data.items():
        # 将每个用户的曾用名按时间戳升序排序
        sorted_names = [name for name, _ in sorted(name_ts_map.items(), key=lambda x: x[1])]
        result.append({
            "uid": uid,
            "history": sorted_names
        })
        
    # 按 uid 排序返回
    result.sort(key=lambda x: x["uid"])
    return result


def list_name_history_by_uid(uid: int) -> list[dict[str, object]]:
    with connect_sqlite() as conn:
        cur = conn.cursor()

        history_rows = []
        # 可以从本项目外导入曾用名数据到 name_history 表
        if _table_exists(conn, "name_history"):
            cur.execute("SELECT uid, uname, first_seen FROM name_history WHERE uid = ?", (uid,))
            history_rows = cur.fetchall()
        
        cur.execute("SELECT uid, uname, timestamp FROM event WHERE uid = ?", (uid,))
        event_rows = cur.fetchall()
        
    if not history_rows and not event_rows:
        return []
        
    return _merge_and_sort_histories(history_rows, event_rows)


def list_name_history_by_name(target_name: str) -> list[dict[str, object]]:
    with connect_sqlite() as conn:
        cur = conn.cursor()
        
        uids_nh = set()
        if _table_exists(conn, "name_history"):
            cur.execute("SELECT uid FROM name_history WHERE uname = ?", (target_name,))
            uids_nh = {r[0] for r in cur.fetchall()}
        
        cur.execute("SELECT uid FROM event WHERE uname = ?", (target_name,))
        uids_ev = {r[0] for r in cur.fetchall()}
        
        target_uids = list(uids_nh | uids_ev)
        
        if not target_uids:
            return []
            
        history_rows = []
        event_rows = []
        
        chunk_size = 900
        for i in range(0, len(target_uids), chunk_size):
            chunk = target_uids[i:i+chunk_size]
            placeholders = ",".join("?" * len(chunk))
            
            if _table_exists(conn, "name_history"):
                cur.execute(
                    f"SELECT uid, uname, first_seen FROM name_history WHERE uid IN ({placeholders})", 
                    chunk
                )
                history_rows.extend(cur.fetchall())
            
            cur.execute(
                f"SELECT uid, uname, timestamp FROM event WHERE uid IN ({placeholders})", 
                chunk
            )
            event_rows.extend(cur.fetchall())
            
    # 第三步：交由 Python 进行去重和排序
    return _merge_and_sort_histories(history_rows, event_rows)


def list_name_history_by_name_or_uid(query: str) -> list[dict[str, object]]:
    if query.isdigit():
        return list_name_history_by_uid(int(query))
    else:
        return list_name_history_by_name(query)