"""用直播间 API 的实际开播时间补回缺失标记，并保留恢复依据。"""

from __future__ import annotations

import time
from pathlib import Path

from src.db.sqlite import DEFAULT_DB_PATH, connect_sqlite, write_transaction


def _has_session_marker(conn, room_id: int, started_at: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM event INDEXED BY idx_event_cmd_time "
        "WHERE cmd IN ('LIVE','PREPARING') AND room_id=? AND timestamp>=? LIMIT 1",
        (room_id, started_at - 10),
    ).fetchone() is not None


def recover_live_start(
    room_id: int, started_at: int, *, title: str = "",
    suppress_notice: bool = False, observed_at: int | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> int | None:
    """缺失时原子写入实际开播标记；已存在本场标记或下播记录时不重复补。"""
    observed_at = int(time.time()) if observed_at is None else observed_at
    if room_id <= 0 or started_at <= 0 or started_at > observed_at + 5:
        raise ValueError("直播间或开播时间无效")
    with connect_sqlite(db_path) as conn:
        if _has_session_marker(conn, room_id, started_at):
            return None
    with write_transaction(db_path) as conn:
        if _has_session_marker(conn, room_id, started_at):
            return None
        conn.execute("""
            CREATE TABLE IF NOT EXISTS live_event_recovery (
                event_id INTEGER PRIMARY KEY REFERENCES event(id),
                source TEXT NOT NULL,
                observed_at INTEGER NOT NULL,
                suppress_notice INTEGER NOT NULL
            )
        """)
        event_id = conn.execute(
            "INSERT INTO event(room_id,cmd,title,timestamp) VALUES(?,'LIVE',?,?)",
            (room_id, title, started_at),
        ).lastrowid
        conn.execute(
            "INSERT INTO live_event_recovery VALUES(?,'bilibili_room_info',?,?)",
            (event_id, observed_at, int(suppress_notice)),
        )
        if suppress_notice:
            conn.execute(
                "INSERT INTO live_notice_delivery(event_id,group_id,room_id,cmd,title,timestamp,status) "
                "SELECT ?,group_id,room_id,'LIVE',?,?,'suppressed' FROM subscription WHERE room_id=?",
                (event_id, title, started_at, room_id),
            )
    return int(event_id)
