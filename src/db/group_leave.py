import sqlite3

from src.db.sqlite import connect_sqlite, write_transaction


def init_group_leave_db():
    with write_transaction() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS group_leave (
                id INTEGER PRIMARY KEY,
                group_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                timestamp INTEGER NOT NULL,
                subtype TEXT NOT NULL,
                nickname TEXT,
                avatar_path TEXT,
                UNIQUE(group_id,user_id,timestamp,subtype)
            );
            CREATE INDEX IF NOT EXISTS idx_group_leave_time
                ON group_leave(group_id,timestamp);
        ''')


def record_leave(group_id, user_id, timestamp, subtype):
    with write_transaction() as conn:
        conn.execute('INSERT OR IGNORE INTO group_leave(group_id,user_id,timestamp,subtype) VALUES (?,?,?,?)',
                     (group_id,user_id,timestamp,subtype))
        return conn.execute('SELECT id FROM group_leave WHERE group_id=? AND user_id=? AND timestamp=? AND subtype=?',
                            (group_id,user_id,timestamp,subtype)).fetchone()[0]


def enrich_leave(record_id, nickname, avatar_path):
    with write_transaction() as conn:
        conn.execute('UPDATE group_leave SET nickname=?,avatar_path=? WHERE id=?', (nickname,avatar_path,record_id))


def recent_leaves(groups, limit):
    if not groups:
        return []
    with connect_sqlite() as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(
            'SELECT * FROM group_leave WHERE group_id IN (' + ','.join('?' for _ in groups) +
            ') ORDER BY timestamp DESC,id DESC LIMIT ?', (*groups,limit))]
