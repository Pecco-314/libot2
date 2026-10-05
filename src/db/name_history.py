from __future__ import annotations

import sqlite3
from pathlib import Path

from src.db.sqlite import DEFAULT_DB_PATH, write_transaction


def init_name_history_index(db_path: Path = DEFAULT_DB_PATH) -> None:
    """Initialize the daily name index and remove per-event synchronization."""
    with write_transaction(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS name_history (
                uid INTEGER,
                uname TEXT,
                first_seen INTEGER,
                UNIQUE(uid, uname)
            );
            CREATE INDEX IF NOT EXISTS idx_nh_name_uid
                ON name_history(uname, uid);
            CREATE INDEX IF NOT EXISTS idx_nh_name_uid_nocase
                ON name_history(uname COLLATE NOCASE, uid);
            CREATE INDEX IF NOT EXISTS idx_nh_uid_time
                ON name_history(uid, first_seen);
            CREATE TABLE IF NOT EXISTS name_history_index_meta (
                key TEXT PRIMARY KEY,
                value INTEGER NOT NULL
            );
            INSERT OR IGNORE INTO name_history_index_meta(key, value)
                VALUES ('complete', 0), ('cursor_event_id', 0), ('target_event_id', 0);
            DROP TRIGGER IF EXISTS trg_event_name_history_insert;
            """
        )


def is_name_history_index_complete(conn) -> bool:
    try:
        meta = conn.execute(
            """
            SELECT value FROM name_history_index_meta
            WHERE key = 'complete'
            """
        ).fetchone()
    except sqlite3.Error:
        return False
    return bool(meta and int(meta[0]) == 1)
