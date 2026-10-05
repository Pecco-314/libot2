"""QQ 群消息表情回应；独立存储事件与作者信息，不保存聊天正文。"""
import sqlite3
from contextlib import contextmanager

from src.common.utils import ROOT

DB_PATH = ROOT / "data" / "qq_reactions.db"


@contextmanager
def connection(db_path=None):
    conn = sqlite3.connect(db_path or DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db(db_path=None):
    with connection(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS message_author (
                self_id INTEGER NOT NULL, group_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                sent_at INTEGER NOT NULL,
                PRIMARY KEY(self_id,group_id,message_id)
            );
            CREATE TABLE IF NOT EXISTS member (
                group_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                nickname TEXT NOT NULL, updated_at INTEGER NOT NULL,
                PRIMARY KEY(group_id,user_id)
            );
            CREATE TABLE IF NOT EXISTS reaction (
                id INTEGER PRIMARY KEY, self_id INTEGER NOT NULL,
                group_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL, emoji_id TEXT NOT NULL,
                timestamp INTEGER NOT NULL, is_add INTEGER NOT NULL,
                UNIQUE(self_id,group_id,message_id,user_id,emoji_id,timestamp,is_add)
            );
            CREATE INDEX IF NOT EXISTS reaction_group_time ON reaction(group_id,timestamp);
            CREATE INDEX IF NOT EXISTS reaction_message ON reaction(self_id,group_id,message_id);
        """)


def save_author(conn, self_id, group_id, message_id, user_id, sent_at=0, nickname=""):
    if not user_id:
        return
    conn.execute("""INSERT INTO message_author VALUES(?,?,?,?,?)
        ON CONFLICT(self_id,group_id,message_id) DO UPDATE SET
        user_id=excluded.user_id,sent_at=MAX(sent_at,excluded.sent_at)""",
        (self_id, group_id, message_id, user_id, sent_at))
    if nickname:
        conn.execute("""INSERT INTO member VALUES(?,?,?,?)
            ON CONFLICT(group_id,user_id) DO UPDATE SET
            nickname=excluded.nickname,updated_at=excluded.updated_at
            WHERE excluded.updated_at>=member.updated_at""",
            (group_id, user_id, nickname, sent_at))


def store_author(*args, db_path=None):
    with connection(db_path) as conn:
        save_author(conn, *args)


def get_author(self_id, group_id, message_id, db_path=None):
    with connection(db_path) as conn:
        row = conn.execute("SELECT user_id FROM message_author WHERE self_id=? AND group_id=? AND message_id=?",
                           (self_id, group_id, message_id)).fetchone()
        return row[0] if row else None


def save_notice(conn, notice):
    """count 是消息上表情的累计数量，不参与单人计数。"""
    if not notice.get("user_id") or not notice.get("group_id"):
        return 0
    if not isinstance(notice.get("is_add"), bool):
        return 0
    inserted = 0
    for like in notice.get("likes") or []:
        emoji_id = str(like.get("emoji_id", ""))
        if not emoji_id.isdigit() or len(emoji_id) > 10:
            continue
        inserted += conn.execute("""INSERT OR IGNORE INTO reaction
            (self_id,group_id,message_id,user_id,emoji_id,timestamp,is_add)
            VALUES(?,?,?,?,?,?,?)""", (
                int(notice["self_id"]), int(notice["group_id"]), int(notice["message_id"]),
                int(notice["user_id"]), emoji_id, int(notice["time"]), int(notice["is_add"]),
            )).rowcount
    return inserted


def store_notice(notice, db_path=None):
    with connection(db_path) as conn:
        return save_notice(conn, notice)


def ranking(group_id, start, end, received=False, limit=30, db_path=None):
    """按区间终点的有效状态统计，重复 add 不改变首次有效添加时间。"""
    with connection(db_path) as conn:
        rows = conn.execute("""
            WITH history AS (
                SELECT *, LAG(is_add) OVER (
                    PARTITION BY self_id,group_id,message_id,user_id,emoji_id
                    ORDER BY timestamp,id
                ) AS previous FROM reaction WHERE group_id=? AND timestamp<?
            ), changes AS (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY self_id,group_id,message_id,user_id,emoji_id
                    ORDER BY timestamp DESC,id DESC
                ) AS latest FROM history WHERE previous IS NULL OR previous<>is_add
            )
            SELECT r.user_id AS actor,a.user_id AS target,r.emoji_id
            FROM changes r LEFT JOIN message_author a
                ON a.self_id=r.self_id AND a.group_id=r.group_id AND a.message_id=r.message_id
            WHERE latest=1 AND is_add=1 AND timestamp>=?
        """, (group_id, end, start)).fetchall()
        names = dict(conn.execute("SELECT user_id,nickname FROM member WHERE group_id=?", (group_id,)))
        first = conn.execute("SELECT MIN(timestamp) FROM reaction WHERE group_id=?", (group_id,)).fetchone()[0]
    counts = {}
    unknown = 0
    for row in rows:
        if row["target"] is None:
            unknown += 1
        uid = row["target"] if received else row["actor"]
        if uid is None:
            continue
        entry = counts.setdefault(uid, {"uid": uid, "name": names.get(uid, str(uid)), "count": 0, "emoji": {}})
        entry["count"] += 1
        entry["emoji"][row["emoji_id"]] = entry["emoji"].get(row["emoji_id"], 0) + 1
    items = sorted(counts.values(), key=lambda x: (-x["count"], x["uid"]))[:limit]
    return {"items": items, "total": len(rows), "unknown": unknown, "first": first}


def cleanup_authors(before, db_path=None):
    """仅清理过期且从未出现在回应记录中的消息作者。"""
    with connection(db_path) as conn:
        conn.execute("""DELETE FROM message_author WHERE sent_at<? AND NOT EXISTS (
            SELECT 1 FROM reaction r WHERE r.self_id=message_author.self_id
            AND r.group_id=message_author.group_id AND r.message_id=message_author.message_id
        )""", (before,))
