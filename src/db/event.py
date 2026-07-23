from __future__ import annotations

from src.db.sqlite import connect_sqlite
from datetime import datetime
from typing import Any

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


def get_latest_live_cmd(room_id: int) -> str | None:
    with connect_sqlite() as conn:
        row = conn.execute(
            """
            SELECT cmd FROM event
            WHERE room_id = ? AND cmd IN ('LIVE', 'PREPARING')
            ORDER BY id DESC LIMIT 1
            """,
            (room_id,),
        ).fetchone()
    if row is None:
        return None
    return str(row[0])


def get_latest_live_event_id(room_ids: list[int]) -> int:
    if not room_ids:
        return 0
    placeholders = ",".join("?" for _ in room_ids)
    sql = (
        "SELECT MAX(id) FROM event "
        "WHERE cmd IN ('LIVE', 'PREPARING') "
        f"AND room_id IN ({placeholders})"
    )
    with connect_sqlite() as conn:
        row = conn.execute(sql, room_ids).fetchone()
    if row is None or row[0] is None:
        return 0
    return int(row[0])


def list_live_events_after(room_ids: list[int], last_id: int) -> list[dict[str, object]]:
    if not room_ids:
        return []

    placeholders = ",".join("?" for _ in room_ids)
    sql = (
        "SELECT id, cmd, room_id, title, timestamp "
        "FROM event "
        "WHERE cmd IN ('LIVE', 'PREPARING') "
        f"AND room_id IN ({placeholders}) "
        "AND id > ? "
        "ORDER BY id ASC"
    )
    params: list[object] = list(room_ids) + [last_id]
    with connect_sqlite() as conn:
        rows = conn.execute(sql, params).fetchall()

    return [
        {
            "id": int(row[0]),
            "cmd": str(row[1]),
            "room_id": int(row[2]),
            "title": str(row[3]) if row[3] is not None else None,
            "timestamp": int(row[4]) if row[4] is not None else 0,
        }
        for row in rows
    ]

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


def list_superchat_events_by_uid(room_id: int, uid: int) -> list[dict[str, object]]:
    with connect_sqlite() as conn:
        rows = conn.execute(
            """
            SELECT uname, content, total_coin, timestamp FROM event
            WHERE room_id = ? AND cmd = 'SUPER_CHAT_MESSAGE' AND uid = ?
            ORDER BY timestamp ASC, id ASC
            """,
            (room_id, uid),
        ).fetchall()
    return [
        {
            "uname": row[0],
            "content": row[1],
            "price": row[2],
            "timestamp": row[3],
        }
        for row in rows
    ]


def list_superchat_event_by_day(room_id: int, day: datetime) -> list[dict[str, object]]:
    start_of_day = int(day.replace(hour=0, minute=0, second=0).timestamp())
    end_of_day = int(day.replace(hour=23, minute=59, second=59).timestamp())
    return list_superchat_events(room_id, start_of_day, end_of_day)


def get_latest_uid_by_uname(room_id: int, uname: str) -> int | None:
    if not uname:
        return None
    with connect_sqlite() as conn:
        row = conn.execute(
            """
            SELECT uid
            FROM event
            WHERE room_id = ? AND uname = ? COLLATE NOCASE AND uid IS NOT NULL
            ORDER BY timestamp DESC, id DESC
            LIMIT 1
            """,
            (room_id, uname),
        ).fetchone()
    if row is None or row[0] is None:
        return None
    return int(row[0])


def list_events_by_uid(room_id: int, uid: int, start_ts: int, end_ts: int) -> list[dict[str, Any]]:
    with connect_sqlite() as conn:
        rows = conn.execute(
            """
            SELECT cmd, content, gift_name, gift_num, total_coin, title, timestamp
            FROM event
            WHERE room_id = ? AND uid = ? AND timestamp >= ? AND timestamp <= ?
            ORDER BY timestamp ASC, id ASC
            """,
            (room_id, uid, start_ts, end_ts),
        ).fetchall()

    return [
        {
            "cmd": str(row[0]),
            "content": row[1],
            "gift_name": row[2],
            "gift_num": row[3],
            "total_coin": row[4],
            "title": row[5],
            "timestamp": int(row[6]) if row[6] is not None else 0,
        }
        for row in rows
    ]


def list_session_events(room_id: int, start_ts: int, end_ts: int) -> list[dict[str, Any]]:
    with connect_sqlite() as conn:
        rows = conn.execute(
            """
            SELECT uid, uname, content
            FROM event
            WHERE room_id = ? AND timestamp >= ? AND timestamp <= ?
              AND uid IS NOT NULL
            ORDER BY timestamp ASC, id ASC
            """,
            (room_id, start_ts, end_ts),
        ).fetchall()

    return [
        {
            "uid": int(row[0]) if row[0] is not None else 0,
            "uname": str(row[1]) if row[1] is not None else "",
            "content": row[2],
        }
        for row in rows
    ]


def list_online_counts(room_id: int, start_ts: int, end_ts: int) -> list[dict[str, Any]]:
    with connect_sqlite() as conn:
        rows = conn.execute(
            """
            SELECT content, timestamp, id
            FROM event
            WHERE room_id = ? AND cmd = 'ONLINE_RANK_COUNT'
              AND timestamp >= ? AND timestamp <= ?
            ORDER BY timestamp ASC, id ASC
            """,
            (room_id, start_ts, end_ts),
        ).fetchall()

    result: list[dict[str, Any]] = []
    for content, ts, _row_id in rows:
        value = None
        if isinstance(content, int):
            value = content
        elif isinstance(content, str):
            if content.isdigit():
                value = int(content)
            else:
                try:
                    value = int(float(content))
                except Exception:
                    value = None
        if value is None:
            continue
        result.append({
            "count": value,
            "timestamp": int(ts) if ts is not None else 0,
        })
    return result


def list_live_sessions(room_id: int) -> list[dict[str, Any]]:
    with connect_sqlite() as conn:
        rows = conn.execute(
            """
            SELECT cmd, timestamp, id
            FROM event
            WHERE room_id = ? AND cmd IN ('LIVE', 'PREPARING')
            ORDER BY timestamp ASC, id ASC
            """,
            (room_id,),
        ).fetchall()

    sessions: list[dict[str, Any]] = []
    current_start: int | None = None
    for cmd, ts, _row_id in rows:
        if ts is None:
            continue
        ts_int = int(ts)
        if cmd == "LIVE":
            current_start = ts_int
        elif cmd == "PREPARING":
            if current_start is None:
                continue
            sessions.append({
                "start_ts": current_start,
                "end_ts": ts_int,
                "ongoing": False,
            })
            current_start = None

    if current_start is not None:
        sessions.append({
            "start_ts": current_start,
            "end_ts": None,
            "ongoing": True,
        })

    return sessions


def list_live_sessions_by_date(room_id: int, date_str: str) -> list[dict[str, Any]]:
    target = datetime.strptime(date_str, "%Y-%m-%d").date()
    result: list[dict[str, Any]] = []
    for session in list_live_sessions(room_id):
        start_dt = datetime.fromtimestamp(session["start_ts"])
        if start_dt.date() == target:
            result.append({
                **session,
                "start_dt": start_dt,
            })
    return result


def get_latest_live_session(room_id: int) -> dict[str, Any] | None:
    sessions = list_live_sessions(room_id)
    if not sessions:
        return None
    return sessions[-1]


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
            cur.execute("SELECT uid FROM name_history WHERE uname = ? COLLATE NOCASE", (target_name,))
            uids_nh = {r[0] for r in cur.fetchall()}
        
        cur.execute("SELECT uid FROM event WHERE uname = ? COLLATE NOCASE", (target_name,))
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
            
    return _merge_and_sort_histories(history_rows, event_rows)


def list_name_history_by_name_or_uid(query: str) -> list[dict[str, object]]:
    if query.isdigit():
        return list_name_history_by_uid(int(query))
    else:
        return list_name_history_by_name(query)


def list_recent_events_by_uid(
    room_id: int,
    uid: int,
    limit: int,
    end_ts: int | None = None,
) -> list[dict[str, Any]]:
    end_condition = "AND timestamp <= ?" if end_ts is not None else ""
    params: tuple[Any, ...] = (
        (room_id, uid, end_ts, limit)
        if end_ts is not None
        else (room_id, uid, limit)
    )
    with connect_sqlite() as conn:
        rows = conn.execute(
            f"""
            SELECT cmd, content, gift_name, gift_num, total_coin, title, timestamp
            FROM event
            WHERE room_id = ? AND uid = ?
              {end_condition}
            ORDER BY timestamp DESC, id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()

    return [
        {
            "cmd": str(row[0]),
            "content": row[1],
            "gift_name": row[2],
            "gift_num": row[3],
            "total_coin": row[4],
            "title": row[5],
            "timestamp": int(row[6]) if row[6] is not None else 0,
        }
        for row in reversed(rows)
    ]