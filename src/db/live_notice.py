"""逐群持久化直播通知；扫描游标与待发送记录在同一事务中保存。"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from src.db.sqlite import DEFAULT_DB_PATH, connect_sqlite, write_transaction

MAX_NOTICE_AGE = 3600


def init_live_notice_db(db_path: Path = DEFAULT_DB_PATH) -> None:
    with write_transaction(db_path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS live_notice_delivery (
                event_id INTEGER NOT NULL,
                group_id INTEGER NOT NULL,
                room_id INTEGER NOT NULL,
                cmd TEXT NOT NULL,
                title TEXT,
                timestamp INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                retry_after INTEGER NOT NULL DEFAULT 0,
                sent_at INTEGER,
                message_id INTEGER,
                PRIMARY KEY(event_id, group_id)
            );
            CREATE INDEX IF NOT EXISTS idx_live_notice_pending
                ON live_notice_delivery(status, retry_after, event_id);
        """)


def stage_live_notices(rows, through_id: int, db_path: Path = DEFAULT_DB_PATH) -> None:
    """为当前订阅群入队，再推进扫描游标；崩溃时两项一同提交或回滚。"""
    with write_transaction(db_path) as conn:
        for row in rows:
            conn.execute(
                """
                INSERT OR IGNORE INTO live_notice_delivery
                    (event_id,group_id,room_id,cmd,title,timestamp)
                SELECT ?,group_id,room_id,?,?,? FROM subscription WHERE room_id=?
                """,
                (row['id'], row['cmd'], row.get('title'), row['timestamp'], row['room_id']),
            )
        conn.execute(
            "INSERT INTO state(key,value) VALUES('last_event_id',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP",
            (str(through_id),),
        )


def list_pending_live_notices(db_path: Path = DEFAULT_DB_PATH, *, now=None, limit=50):
    now = int(time.time()) if now is None else now
    with connect_sqlite(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT d.* FROM live_notice_delivery d
            JOIN subscription s ON s.group_id=d.group_id AND s.room_id=d.room_id
            WHERE d.status='pending' AND d.retry_after<=? AND d.timestamp>=?
              AND NOT EXISTS (
                  SELECT 1 FROM live_notice_delivery older
                  WHERE older.group_id=d.group_id AND older.status='pending'
                    AND older.room_id=d.room_id
                    AND older.event_id<d.event_id AND older.timestamp>=?
              )
            ORDER BY d.event_id,d.group_id LIMIT ?
            """,
            (now, now - MAX_NOTICE_AGE, now - MAX_NOTICE_AGE, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def mark_live_notice_sent(event_id, group_id, message_id=None, db_path: Path = DEFAULT_DB_PATH):
    with write_transaction(db_path) as conn:
        conn.execute(
            "UPDATE live_notice_delivery SET status='sent',sent_at=?,message_id=? "
            "WHERE event_id=? AND group_id=? AND status='pending'",
            (int(time.time()), message_id, event_id, group_id),
        )


def retry_live_notice(event_id, group_id, db_path: Path = DEFAULT_DB_PATH, *, now=None):
    now = int(time.time()) if now is None else now
    with write_transaction(db_path) as conn:
        row = conn.execute(
            "SELECT attempts FROM live_notice_delivery WHERE event_id=? AND group_id=? AND status='pending'",
            (event_id, group_id),
        ).fetchone()
        if row:
            delay = min(300, 5 * 2 ** min(int(row[0]), 6))
            conn.execute(
                "UPDATE live_notice_delivery SET attempts=attempts+1,retry_after=? "
                "WHERE event_id=? AND group_id=? AND status='pending'",
                (now + delay, event_id, group_id),
            )


def prune_live_notices(db_path: Path = DEFAULT_DB_PATH, *, now=None):
    """短期发送记录保留七天；过期通知不会继续发送。"""
    now = int(time.time()) if now is None else now
    with write_transaction(db_path) as conn:
        conn.execute('DELETE FROM live_notice_delivery WHERE timestamp<?', (now - 7 * 86400,))
