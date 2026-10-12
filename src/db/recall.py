"""Short-lived QQ message cache, separate from the live-event database."""
import json
import sqlite3
from contextlib import contextmanager

from src.common.utils import ROOT

DB_PATH = ROOT / 'data' / 'recall_cache.db'
TTL = 86400


@contextmanager
def connection(db_path=None):
    conn = sqlite3.connect(db_path or DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db():
    with connection() as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS message (
                group_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL, sent_at INTEGER NOT NULL,
                nickname TEXT NOT NULL DEFAULT '', segments TEXT,
                recalled_at INTEGER, operator_id INTEGER,
                PRIMARY KEY(group_id,message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_recall_user ON message(user_id,recalled_at);
            CREATE INDEX IF NOT EXISTS idx_recall_expiry ON message(sent_at);
        ''')


def save_message(group, mid, uid, sent_at, nickname, segments):
    with connection() as conn:
        conn.execute('''INSERT INTO message(group_id,message_id,user_id,sent_at,nickname,segments)
            VALUES(?,?,?,?,?,?) ON CONFLICT(group_id,message_id) DO UPDATE SET
            sent_at=excluded.sent_at,nickname=excluded.nickname,segments=excluded.segments
        ''', (group,mid,uid,sent_at,nickname,json.dumps(segments,ensure_ascii=False)))


def save_history_messages(rows, db_path=None):
    """补入缺失消息，保留实时缓存、已撤回标记和现有图片信息。"""
    with connection(db_path) as conn:
        before = conn.total_changes
        conn.executemany('''
            INSERT INTO message(group_id,message_id,user_id,sent_at,nickname,segments)
            VALUES(?,?,?,?,?,?) ON CONFLICT(group_id,message_id) DO NOTHING
        ''', [(r['group_id'],r['message_id'],r['user_id'],r['sent_at'],r['nickname'],
               json.dumps(r['segments'],ensure_ascii=False)) for r in rows])
        return conn.total_changes-before


def mark_recalled(group, mid, uid, timestamp, operator):
    with connection() as conn:
        conn.execute('''INSERT INTO message(group_id,message_id,user_id,sent_at,recalled_at,operator_id)
            VALUES(?,?,?,?,?,?) ON CONFLICT(group_id,message_id) DO UPDATE SET
            recalled_at=excluded.recalled_at,operator_id=excluded.operator_id
        ''', (group,mid,uid,timestamp,timestamp,operator))


def list_recalled(uid, groups, now):
    if not groups:
        return []
    with connection() as conn:
        return [dict(row) for row in conn.execute(
            'SELECT * FROM message WHERE user_id=? AND sent_at>=? AND recalled_at>=? '
            'AND group_id IN (' + ','.join('?' for _ in groups) + ') ORDER BY recalled_at DESC',
            (uid,now-TTL,now-TTL,*groups))]


def expire(now):
    with connection() as conn:
        conn.execute('DELETE FROM message WHERE sent_at < ?', (now-TTL,))
