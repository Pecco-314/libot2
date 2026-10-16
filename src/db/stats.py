from __future__ import annotations

from typing import Any
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.db.sqlite import DEFAULT_DB_PATH, connect_sqlite, execute_write, write_transaction


def _table_columns(conn, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row[1]) for row in rows}


def _ensure_stats_schema(conn) -> None:
    columns = _table_columns(conn, "stats")
    if not columns:
        execute_write(
            conn,
            """
            CREATE TABLE stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id INTEGER NOT NULL,
                uid INTEGER NOT NULL,
                uname TEXT NOT NULL,
                fans_num INTEGER NOT NULL,
                guard_num INTEGER NOT NULL,
                fan_club_num INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
        )
    elif "created_at" not in columns:
        execute_write(
            conn,
            "ALTER TABLE stats ADD COLUMN created_at TIMESTAMP",
        )

    execute_write(
        conn,
        "DROP INDEX IF EXISTS idx_stats_room_id",
    )
    execute_write(
        conn,
        """
        CREATE INDEX IF NOT EXISTS idx_stats_room_time
        ON stats(room_id, created_at)
        """,
    )


def init_stats_db() -> None:
    with write_transaction() as conn:
        _ensure_stats_schema(conn)


def insert_stats(
    room_id: int,
    uid: int,
    uname: str,
    fans_num: int,
    guard_num: int,
    fan_club_num: int,
    created_at: str,
) -> None:
    with write_transaction() as conn:
        execute_write(
            conn,
            """
            INSERT INTO stats (
                room_id, uid, uname, fans_num, guard_num, fan_club_num, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (room_id, uid, uname, fans_num, guard_num, fan_club_num, created_at),
        )


def list_stats(
    room_id: int,
    days: int | None = None,
    *,
    start_ts: int | None = None,
    end_ts: int | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    """按 UTC 时间索引读取 [start_ts, end_ts)，输出北京时间供绘图。"""
    if start_ts is None and end_ts is None:
        if days is None or days <= 0:
            raise ValueError("查询天数必须为正数")
        now = datetime.now(timezone.utc)
        start_ts = int((now - timedelta(days=days)).timestamp())
        end_ts = int(now.timestamp()) + 1
    if start_ts is None or end_ts is None or start_ts >= end_ts:
        raise ValueError("统计时间范围无效")
    start_utc = datetime.fromtimestamp(start_ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    end_utc = datetime.fromtimestamp(end_ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    with connect_sqlite(db_path) as conn:
        rows = conn.execute(
            """
            SELECT 
                fans_num, 
                guard_num, 
                fan_club_num, 
                datetime(created_at, '+8 hours') as local_time
            FROM stats
            WHERE room_id = ? AND created_at >= ? AND created_at < ?
            ORDER BY created_at ASC, id ASC
            """,
            (room_id, start_utc, end_utc),
        ).fetchall()
        
    return [
        {
            "fans_num": row[0],
            "guard_num": row[1],
            "fan_club_num": row[2],
            "created_at": row[3],
        }
        for row in rows
    ]


_STAT_VALUE_COLUMNS = {
    "fans": "fans_num",
    "guards": "guard_num",
    "club": "fan_club_num",
}


def get_stat_start_date(room_id: int, stat_type: str = "fans") -> datetime:
    value_column = _STAT_VALUE_COLUMNS.get(stat_type)
    if value_column is None:
        raise ValueError(f"未知统计类型: {stat_type}")
    with connect_sqlite() as conn:
        row = conn.execute(
            f"""
            SELECT datetime(created_at, '+8 hours') as local_time
            FROM stats
            WHERE room_id = ? AND {value_column} != -1
            ORDER BY local_time ASC
            LIMIT 1
            """,
            (room_id,),
        ).fetchone()
    if row and row[0]:
        return datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
    return datetime.now()
