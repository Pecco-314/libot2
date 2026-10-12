from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from src.common.blind_box import BLIND_BOX_COST_COIN, BLIND_BOX_RESULT_ORDER
from src.db.event_search import (
    EventSearchUnavailable,
    list_recent_events_by_content_indexed,
)
from src.db.name_history import is_name_history_index_complete
from src.db.sqlite import DEFAULT_DB_PATH, connect_sqlite


logger = logging.getLogger("libot.db.event")


def _table_exists(conn, table_name: str) -> bool:
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
    return cur.fetchone() is not None


def _find_uids_by_indexed_name(conn, target_name: str) -> set[int]:
    uids: set[int] = set()
    if _table_exists(conn, "name_history"):
        rows = conn.execute(
            "SELECT uid FROM name_history WHERE uname = ? COLLATE NOCASE",
            (target_name,),
        ).fetchall()
        uids.update(int(row[0]) for row in rows if row[0] is not None)
    if _table_exists(conn, "fan_club_name_history"):
        rows = conn.execute(
            """
            SELECT member_uid FROM fan_club_name_history
            WHERE uname = ? COLLATE NOCASE
            """,
            (target_name,),
        ).fetchall()
        uids.update(int(row[0]) for row in rows if row[0] is not None)
    if _table_exists(conn, "liver"):
        rows = conn.execute(
            """
            SELECT uid FROM liver
            WHERE uid IS NOT NULL AND (uname = ? COLLATE NOCASE OR nickname = ? COLLATE NOCASE)
            """,
            (target_name, target_name),
        ).fetchall()
        uids.update(int(row[0]) for row in rows if row[0] is not None)
    return uids

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


def get_max_event_id(db_path: Path = DEFAULT_DB_PATH) -> int:
    with connect_sqlite(db_path) as conn:
        row = conn.execute("SELECT MAX(id) FROM event").fetchone()
    if row is None or row[0] is None:
        return 0
    return int(row[0])


def list_live_notice_events_after(
    last_id: int, through_id: int, since: int, db_path: Path = DEFAULT_DB_PATH,
) -> list[dict[str, object]]:
    """按入库顺序读取有效时限内的全部直播通知，不扫描普通弹幕。"""
    if through_id <= last_id:
        return []
    with connect_sqlite(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id,cmd,room_id,title,timestamp FROM event INDEXED BY idx_event_cmd_time "
            "WHERE cmd IN ('LIVE','PREPARING','ROOM_CHANGE') AND timestamp>=? "
            "AND id>? AND id<=? ORDER BY id",
            (since, last_id, through_id),
        ).fetchall()
    return [dict(row) for row in rows]


def list_streamer_danmaku_events_after(
    last_id: int,
    through_id: int,
    db_path: Path = DEFAULT_DB_PATH,
) -> list[dict[str, object]]:
    """List new danmaku sent by a monitored room's own streamer."""
    if through_id <= last_id:
        return []
    with connect_sqlite(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
                event.id,
                event.room_id,
                event.uid,
                COALESCE(
                    NULLIF(liver.nickname, ''),
                    NULLIF(liver.uname, ''),
                    NULLIF(event.uname, ''),
                    CAST(event.uid AS TEXT)
                ) AS display_name,
                event.content,
                event.timestamp
            FROM event
            JOIN liver
              ON liver.room_id = event.room_id
             AND liver.uid = event.uid
            WHERE event.id > ?
              AND event.id <= ?
              AND event.cmd = 'DANMU_MSG'
              AND event.content IS NOT NULL
              AND TRIM(event.content) != ''
              AND EXISTS (
                  SELECT 1
                  FROM subscription
                  WHERE subscription.room_id = event.room_id
              )
            ORDER BY event.id ASC
            """,
            (last_id, through_id),
        ).fetchall()
    return [
        {
            "id": int(row[0]),
            "room_id": int(row[1]),
            "uid": int(row[2]),
            "uname": str(row[3]),
            "content": str(row[4]),
            "timestamp": int(row[5] or 0),
        }
        for row in rows
    ]


def get_latest_live_cmd(room_id: int, db_path: Path = DEFAULT_DB_PATH) -> str | None:
    with connect_sqlite(db_path) as conn:
        row = conn.execute(
            """
            SELECT cmd FROM event INDEXED BY idx_event_cmd_time
            WHERE room_id = ? AND cmd IN ('LIVE', 'PREPARING')
            ORDER BY id DESC LIMIT 1
            """,
            (room_id,),
        ).fetchone()
    if row is None:
        return None
    return str(row[0])


def get_latest_live_event_id(room_ids: list[int], db_path: Path = DEFAULT_DB_PATH) -> int:
    if not room_ids:
        return 0
    placeholders = ",".join("?" for _ in room_ids)
    sql = (
        "SELECT MAX(id) FROM event INDEXED BY idx_event_cmd_time "
        "WHERE cmd IN ('LIVE', 'PREPARING') "
        f"AND room_id IN ({placeholders})"
    )
    with connect_sqlite(db_path) as conn:
        row = conn.execute(sql, room_ids).fetchone()
    if row is None or row[0] is None:
        return 0
    return int(row[0])


def list_live_events_after(
    room_ids: list[int], last_id: int, db_path: Path = DEFAULT_DB_PATH,
) -> list[dict[str, object]]:
    """扫描少量开下播标记，避免按房间扫描全部弹幕。"""
    if not room_ids:
        return []

    placeholders = ",".join("?" for _ in room_ids)
    sql = (
        "SELECT id, cmd, room_id, title, timestamp "
        "FROM event INDEXED BY idx_event_cmd_time "
        "WHERE cmd IN ('LIVE', 'PREPARING') "
        f"AND room_id IN ({placeholders}) "
        "AND id > ? "
        "ORDER BY id ASC"
    )
    params: list[object] = list(room_ids) + [last_id]
    with connect_sqlite(db_path) as conn:
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
            SELECT cmd FROM event INDEXED BY idx_event_cmd_time
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
            SELECT title FROM event INDEXED BY idx_event_cmd_time
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


def get_latest_uid_by_uname(
    room_id: int,
    uname: str,
    db_path: Path = DEFAULT_DB_PATH,
) -> int | None:
    if not uname:
        return None
    with connect_sqlite(db_path) as conn:
        candidates = _find_uids_by_indexed_name(conn, uname)
        # The daily index covers a prefix; resolve fresh names from the tail
        # by primary key, including names shared with an indexed user.
        complete = is_name_history_index_complete(conn)
        if complete:
            cursor_row = conn.execute(
                "SELECT value FROM name_history_index_meta WHERE key='cursor_event_id'"
            ).fetchone()
            cursor = int(cursor_row[0]) if cursor_row else 0
            candidates.update(int(row[0]) for row in conn.execute(
                "SELECT DISTINCT uid FROM event NOT INDEXED "
                "WHERE id > ? AND room_id = ? AND uname = ? COLLATE NOCASE "
                "AND uid IS NOT NULL", (cursor, room_id, uname)
            ))
        candidate_uids = sorted(candidates)
        if candidate_uids:
            latest_uid: int | None = None
            latest_key: tuple[int, int] | None = None
            chunk_size = 900
            for index in range(0, len(candidate_uids), chunk_size):
                chunk = candidate_uids[index : index + chunk_size]
                placeholders = ",".join("?" for _ in chunk)
                row = conn.execute(
                    f"""
                    SELECT uid, timestamp, id
                    FROM event
                    WHERE room_id = ?
                      AND uid IN ({placeholders})
                    ORDER BY timestamp DESC, id DESC
                    LIMIT 1
                    """,
                    (room_id, *chunk),
                ).fetchone()
                if row is None or row[0] is None:
                    continue
                key = (int(row[1] or 0), int(row[2] or 0))
                if latest_key is None or key > latest_key:
                    latest_key = key
                    latest_uid = int(row[0])
            if latest_uid is not None:
                return latest_uid

        if complete:
            return None

        # An incomplete name_history is not the source of truth yet.
        # Old rows and data imported while a writer was running may be absent
        # from it, so an index miss must fall back to the event table.
        row = conn.execute(
            """
            SELECT uid
            FROM event INDEXED BY idx_event_room_time
            WHERE room_id = ? AND uname = ? COLLATE NOCASE
              AND uid IS NOT NULL
            ORDER BY timestamp DESC, id DESC
            LIMIT 1
            """,
            (room_id, uname),
        ).fetchone()
        return int(row[0]) if row is not None and row[0] is not None else None


def list_events_by_uid(room_id: int, uid: int, start_ts: int, end_ts: int) -> list[dict[str, Any]]:
    with connect_sqlite() as conn:
        rows = conn.execute(
            """
            SELECT cmd, content, gift_name, gift_num, total_coin, title, timestamp, uname
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
            "uname": str(row[7]) if row[7] is not None else None,
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


def list_heart_blind_box_events(
    room_id: int,
    start_ts: int,
    end_ts: int,
) -> list[dict[str, Any]]:
    """List 心动盲盒 results, excluding directly sent gifts with the same names."""
    result_names = BLIND_BOX_RESULT_ORDER
    placeholders = ",".join("?" for _ in result_names)
    with connect_sqlite() as conn:
        rows = conn.execute(
            f"""
            SELECT uid, uname, gift_name, gift_num, timestamp
            FROM event INDEXED BY idx_event_room_time
            WHERE room_id = ?
              AND timestamp BETWEEN ? AND ?
              AND cmd = 'SEND_GIFT'
              AND gift_name IN ({placeholders})
              AND gift_num > 0
              AND total_coin = ? * gift_num
            ORDER BY timestamp, id
            """,
            (room_id, start_ts, end_ts, *result_names, BLIND_BOX_COST_COIN),
        ).fetchall()
    return [
        {
            "uid": int(row[0]) if row[0] is not None else 0,
            "uname": str(row[1] or ""),
            "gift_name": str(row[2]),
            "gift_num": int(row[3]),
            "timestamp": int(row[4]),
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
            FROM event INDEXED BY idx_event_cmd_time
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


def get_live_end_near_start(
    room_id: int,
    start_ts: int,
    *,
    tolerance_seconds: int = 10,
    max_duration_seconds: int = 48 * 60 * 60,
) -> int | None:
    """Return the matching PREPARING timestamp without scanning all room events."""
    with connect_sqlite() as conn:
        live_row = conn.execute(
            """
            SELECT timestamp FROM event INDEXED BY idx_event_cmd_time
            WHERE cmd = 'LIVE' AND room_id = ? AND timestamp BETWEEN ? AND ?
            ORDER BY ABS(timestamp - ?), id
            LIMIT 1
            """,
            (
                room_id,
                start_ts - tolerance_seconds,
                start_ts + tolerance_seconds,
                start_ts,
            ),
        ).fetchone()
        if live_row is None:
            return None

        local_start = int(live_row[0])
        preparing_row = conn.execute(
            """
            SELECT timestamp FROM event INDEXED BY idx_event_cmd_time
            WHERE cmd = 'PREPARING' AND room_id = ?
              AND timestamp > ? AND timestamp <= ?
            ORDER BY timestamp, id
            LIMIT 1
            """,
            (room_id, local_start, local_start + max_duration_seconds),
        ).fetchone()
    if preparing_row is None:
        return None
    return int(preparing_row[0])


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
        if _table_exists(conn, "fan_club_name_history"):
            cur.execute(
                """
                SELECT member_uid, uname, first_seen
                FROM fan_club_name_history WHERE member_uid = ?
                """,
                (uid,),
            )
            history_rows.extend(cur.fetchall())
        
        cur.execute("SELECT uid, uname, timestamp FROM event WHERE uid = ?", (uid,))
        event_rows = cur.fetchall()
        
    if not history_rows and not event_rows:
        return []
        
    return _merge_and_sort_histories(history_rows, event_rows)


def _collect_uids_by_name(conn, target_name: str) -> list[int]:
    """合并每日名字索引与未覆盖的事件，保留所有同名 UID。"""
    candidate_uids = _find_uids_by_indexed_name(conn, target_name)
    cursor = None
    if _table_exists(conn, "name_history") and is_name_history_index_complete(conn):
        row = conn.execute(
            "SELECT value FROM name_history_index_meta WHERE key = 'cursor_event_id'"
        ).fetchone()
        if row is not None:
            cursor = int(row[0])
    sql = "SELECT DISTINCT uid FROM event NOT INDEXED WHERE uname = ? COLLATE NOCASE AND uid IS NOT NULL"
    params = (target_name,)
    if cursor is not None:
        sql += " AND id > ?"
        params = (target_name, cursor)
    candidate_uids.update(int(row[0]) for row in conn.execute(sql, params))
    return sorted(candidate_uids)


def list_uids_by_uname(
    target_name: str, db_path: Path = DEFAULT_DB_PATH,
) -> list[int]:
    """查询所有同名用户，仅解析 UID，不加载用户的完整弹幕或曾用名记录。"""
    with connect_sqlite(db_path) as conn:
        conn.execute("BEGIN")
        return _collect_uids_by_name(conn, target_name)


def list_name_history_by_name(target_name: str) -> list[dict[str, object]]:
    with connect_sqlite() as conn:
        # 索引进度与名字记录使用同一读快照，避免每日更新期间遗漏数据。
        conn.execute("BEGIN")
        cur = conn.cursor()
        target_uids = _collect_uids_by_name(conn, target_name)
        
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
            if _table_exists(conn, "fan_club_name_history"):
                cur.execute(
                    f"""
                    SELECT member_uid, uname, first_seen
                    FROM fan_club_name_history
                    WHERE member_uid IN ({placeholders})
                    """,
                    chunk,
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
    db_path: Path = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    end_condition = "AND timestamp <= ?" if end_ts is not None else ""
    params: tuple[Any, ...] = (
        (room_id, uid, end_ts, limit)
        if end_ts is not None
        else (room_id, uid, limit)
    )
    with connect_sqlite(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT cmd, content, gift_name, gift_num, total_coin, title, timestamp, uname
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
            "uname": str(row[7]) if row[7] is not None else None,
        }
        for row in reversed(rows)
    ]


def _list_recent_events_by_content_legacy(
    room_id: int,
    keyword: str,
    limit: int = 100,
    excluded_range: tuple[int, int] | None = None,
    end_ts: int | None = None,
) -> list[dict[str, Any]]:
    exclude_condition = (
        "AND NOT (timestamp >= ? AND timestamp < ?)"
        if excluded_range is not None
        else ""
    )
    end_condition = "AND timestamp <= ?" if end_ts is not None else ""
    params: list[Any] = [room_id, keyword]
    if excluded_range is not None:
        params.extend(excluded_range)
    if end_ts is not None:
        params.append(end_ts)
    params.append(limit)

    with connect_sqlite() as conn:
        rows = conn.execute(
            f"""
            SELECT uid, uname, cmd, content, gift_name, gift_num,
                   total_coin, title, timestamp
            FROM event
            WHERE room_id = ?
              AND uid IS NOT NULL
              AND content IS NOT NULL
              AND instr(content, ?) > 0
              {exclude_condition}
              {end_condition}
            ORDER BY timestamp DESC, id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()

    return [
        {
            "uid": int(row[0]) if row[0] is not None else 0,
            "uname": str(row[1]) if row[1] is not None else "",
            "cmd": str(row[2]),
            "content": row[3],
            "gift_name": row[4],
            "gift_num": row[5],
            "total_coin": row[6],
            "title": row[7],
            "timestamp": int(row[8]) if row[8] is not None else 0,
        }
        for row in reversed(rows)
    ]


def list_recent_events_by_content(
    room_id: int,
    keyword: str,
    limit: int = 100,
    excluded_range: tuple[int, int] | None = None,
    end_ts: int | None = None,
) -> list[dict[str, Any]]:
    try:
        return list_recent_events_by_content_indexed(
            room_id,
            keyword,
            limit,
            excluded_range,
            end_ts,
        )
    except EventSearchUnavailable as exc:
        logger.warning("event FTS unavailable, using legacy scan: %s", exc)
        return _list_recent_events_by_content_legacy(
            room_id,
            keyword,
            limit,
            excluded_range,
            end_ts,
        )
