"""动态逐群发送状态；发表后24小时内的漏抓动态可在恢复后补发。"""

from __future__ import annotations

import time
from pathlib import Path

from src.db.activity import _row_to_dict
from src.db.sqlite import DEFAULT_DB_PATH, connect_sqlite, write_transaction

MAX_ACTIVITY_NOTICE_AGE = 86400


def init_activity_notice_db(db_path: Path = DEFAULT_DB_PATH):
    with write_transaction(db_path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS activity_notice_delivery (
                activity_record_id INTEGER NOT NULL,
                group_id INTEGER NOT NULL,
                room_id INTEGER NOT NULL,
                timestamp INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                retry_after INTEGER NOT NULL DEFAULT 0,
                sent_at INTEGER,
                message_id INTEGER,
                PRIMARY KEY(activity_record_id,group_id)
            );
            CREATE INDEX IF NOT EXISTS idx_activity_notice_pending
                ON activity_notice_delivery(status,retry_after,activity_record_id);
        """)


def stage_activity_notices(rows, through_id: int, db_path: Path = DEFAULT_DB_PATH):
    with write_transaction(db_path) as conn:
        for row in rows:
            conn.execute(
                'INSERT OR IGNORE INTO activity_notice_delivery(activity_record_id,group_id,room_id,timestamp) '
                'SELECT ?,group_id,room_id,? FROM subscription WHERE room_id=?',
                (row['id'], row['timestamp'], row['room_id']),
            )
        conn.execute(
            "INSERT INTO state(key,value) VALUES('last_activity_id',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP",
            (str(through_id),),
        )


def list_pending_activity_notices(db_path: Path = DEFAULT_DB_PATH, *, now=None):
    now = int(time.time()) if now is None else now
    with connect_sqlite(db_path) as conn:
        rows = conn.execute(
            """
            SELECT a.*,d.group_id FROM activity_notice_delivery d
            JOIN activity a ON a.id=d.activity_record_id
            JOIN subscription s ON s.group_id=d.group_id AND s.room_id=d.room_id
            WHERE d.status='pending' AND d.retry_after<=? AND d.timestamp>=?
              AND NOT EXISTS (
                  SELECT 1 FROM activity_notice_delivery older
                  WHERE older.group_id=d.group_id AND older.room_id=d.room_id
                    AND older.status='pending' AND older.activity_record_id<d.activity_record_id
                    AND older.timestamp>=?
              )
            ORDER BY d.activity_record_id,d.group_id LIMIT 50
            """,
            (now, now-MAX_ACTIVITY_NOTICE_AGE, now-MAX_ACTIVITY_NOTICE_AGE),
        ).fetchall()
    return [{**_row_to_dict(row), 'group_id': int(row[-1])} for row in rows]


def mark_activity_notice_sent(record_id, group_id, message_id=None, db_path: Path = DEFAULT_DB_PATH):
    with write_transaction(db_path) as conn:
        conn.execute(
            "UPDATE activity_notice_delivery SET status='sent',sent_at=?,message_id=? "
            "WHERE activity_record_id=? AND group_id=? AND status='pending'",
            (int(time.time()), message_id, record_id, group_id),
        )


def retry_activity_notice(record_id, group_id, db_path: Path = DEFAULT_DB_PATH):
    with write_transaction(db_path) as conn:
        row = conn.execute(
            "SELECT attempts FROM activity_notice_delivery WHERE activity_record_id=? AND group_id=? AND status='pending'",
            (record_id, group_id),
        ).fetchone()
        if row:
            delay = min(300, 5*2**min(int(row[0]), 6))
            conn.execute(
                'UPDATE activity_notice_delivery SET attempts=attempts+1,retry_after=? '
                'WHERE activity_record_id=? AND group_id=?',
                (int(time.time())+delay, record_id, group_id),
            )


def prune_activity_notices(db_path: Path = DEFAULT_DB_PATH):
    with write_transaction(db_path) as conn:
        conn.execute('DELETE FROM activity_notice_delivery WHERE timestamp<?', (int(time.time())-7*86400,))
