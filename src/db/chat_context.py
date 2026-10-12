"""群内长期称呼映射；聊天正文继续使用 24 小时撤回缓存。"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from src.common.utils import ROOT

DB_PATH = ROOT / "data" / "chat_context.db"
CONTEXT_PATH = ROOT / "src" / "common" / "chat_summary_context.json"


@contextmanager
def connection(db_path=DB_PATH):
    conn = sqlite3.connect(db_path, timeout=5)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db(db_path: Path = DB_PATH) -> None:
    with connection(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS person (
                group_id INTEGER NOT NULL, qq INTEGER NOT NULL,
                display_name TEXT NOT NULL, updated_at INTEGER NOT NULL,
                PRIMARY KEY(group_id, qq)
            );
            CREATE TABLE IF NOT EXISTS alias (
                group_id INTEGER NOT NULL, qq INTEGER NOT NULL,
                name TEXT NOT NULL COLLATE NOCASE, source TEXT NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY(group_id, qq, name)
            );
            CREATE INDEX IF NOT EXISTS idx_chat_alias_name
                ON alias(group_id, name COLLATE NOCASE);
        """)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(person)")}
        if "name_source" not in columns:
            conn.execute(
                "ALTER TABLE person ADD COLUMN name_source TEXT NOT NULL DEFAULT 'observed'"
            )
        for person in json.loads(CONTEXT_PATH.read_text())["people"]:
            # 初始称呼只填补空缺，保留网页维护后的主名。
            if not conn.execute(
                "SELECT 1 FROM person WHERE group_id=? AND qq=?",
                (person["group_id"], person["qq"]),
            ).fetchone():
                write_aliases(
                    conn,
                    person["group_id"],
                    person["qq"],
                    person["name"],
                    person["aliases"],
                    "confirmed",
                )


def set_aliases(group_id, qq, name, aliases, source="confirmed", db_path=DB_PATH):
    with connection(db_path) as conn:
        write_aliases(conn, group_id, qq, name, aliases, source)


def write_aliases(conn, group_id, qq, name, aliases, source="confirmed"):
    """在调用者事务内维护主名和称呼，已确认主名优先于观察到的昵称。"""
    now = int(time.time())
    conn.execute(
        "INSERT INTO person(group_id,qq,display_name,updated_at,name_source) VALUES(?,?,?,?,?) "
        "ON CONFLICT(group_id,qq) DO UPDATE SET "
        "display_name=CASE WHEN excluded.name_source='confirmed' OR person.name_source<>'confirmed' "
        "THEN excluded.display_name ELSE person.display_name END,"
        "name_source=CASE WHEN excluded.name_source='confirmed' THEN 'confirmed' ELSE person.name_source END,"
        "updated_at=excluded.updated_at",
        (group_id, qq, name, now, source),
    )
    conn.executemany(
        "INSERT INTO alias VALUES(?,?,?,?,?) ON CONFLICT(group_id,qq,name) "
        "DO UPDATE SET updated_at=excluded.updated_at,"
        "source=CASE WHEN excluded.source='confirmed' THEN 'confirmed' ELSE alias.source END",
        [
            (group_id, qq, str(a).strip(), source, now)
            for a in {name, *aliases}
            if str(a).strip()
        ],
    )


def observe_names(group_id, rows, db_path=DB_PATH):
    latest = {}
    names = {}
    for row in rows:
        if row["nickname"].strip():
            qq = row["user_id"]
            latest[qq] = row["nickname"]
            names.setdefault(qq, set()).add(row["nickname"])
    with connection(db_path) as conn:
        for qq, name in latest.items():
            write_aliases(conn, group_id, qq, name, names[qq], "observed")


def list_people(group_id, db_path=DB_PATH):
    with connection(db_path) as conn:
        people = {
            qq: {"name": name, "aliases": [], "confirmed_aliases": []}
            for qq, name in conn.execute(
                "SELECT qq,display_name FROM person WHERE group_id=?", (group_id,)
            )
        }
        for qq, name, source in conn.execute(
            "SELECT qq,name,source FROM alias WHERE group_id=? ORDER BY name",
            (group_id,),
        ):
            if qq in people:
                people[qq]["aliases"].append(name)
                if source == "confirmed":
                    people[qq]["confirmed_aliases"].append(name)
    return people


def search_people(group_id, query="", db_path=DB_PATH, limit=30):
    """搜索本群已观察或已确认的昵称与称呼。"""
    query = query.strip().casefold()
    people = list_people(group_id, db_path)
    matches = [
        {"qq": str(qq), **person}
        for qq, person in people.items()
        if not query
        or query in str(qq)
        or any(
            query in name.casefold() for name in [person["name"], *person["aliases"]]
        )
    ]
    matches.sort(
        key=lambda p: (p["name"].casefold() != query, p["name"].casefold(), p["qq"])
    )
    return matches[:limit]


def list_messages(group_id, start, end, cache_path=None):
    from src.db.recall import DB_PATH as RECALL_DB_PATH

    path = cache_path or RECALL_DB_PATH
    conn = sqlite3.connect(
        f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=5
    )
    try:
        conn.row_factory = sqlite3.Row
        return [
            dict(r)
            for r in conn.execute(
                "SELECT message_id,user_id,sent_at,nickname,segments FROM message "
                "WHERE group_id=? AND sent_at>=? AND sent_at<? AND recalled_at IS NULL "
                "ORDER BY sent_at,message_id",
                (group_id, start, end),
            )
        ]
    finally:
        conn.close()


def recalled_ids(group_id, cache_path=None):
    from src.db.recall import DB_PATH as RECALL_DB_PATH

    path = cache_path or RECALL_DB_PATH
    conn = sqlite3.connect(
        f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=5
    )
    try:
        return {
            row[0]
            for row in conn.execute(
                "SELECT message_id FROM message WHERE group_id=? AND recalled_at IS NOT NULL",
                (group_id,),
            )
        }
    finally:
        conn.close()
