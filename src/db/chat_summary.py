"""按消息批次保存群聊总结和人工维护的词义，不保存长期聊天原文。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from src.db import chat_context
from src.common.chat_summary_commands import identify_command, low_information_command

DB_PATH = chat_context.DB_PATH
BATCH_SIZE = 500
QUERY_THRESHOLD = 50
MAX_ATTEMPTS = 3
RAW_TTL = 86400
LEASE_SECONDS = 7200


@contextmanager
def connection(db_path=DB_PATH):
    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def seed_terms():
    context = json.loads(chat_context.CONTEXT_PATH.read_text())
    terms = list(context["terms"])
    for key, meaning in context.get("notation", {}).items():
        terms.append({"term": key, "meaning": meaning})
    for entity in context["entities"]:
        definition = entity["name"]
        if entity.get("organization"):
            definition += f"，{entity['organization']} {entity['generation']}"
        if entity.get("type"):
            definition += f"，{entity['type']}"
        for name in {entity["name"], *entity.get("aliases", [])}:
            terms.append({"term": name, "meaning": definition})
    return terms


def init_db(db_path=DB_PATH):
    chat_context.init_db(db_path)
    with connection(db_path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS chat_glossary (
                term TEXT PRIMARY KEY COLLATE NOCASE, definition TEXT NOT NULL,
                keep_original INTEGER NOT NULL DEFAULT 0,
                needs_context INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chat_context_meta (
                key TEXT PRIMARY KEY, value INTEGER NOT NULL
            );
            INSERT OR IGNORE INTO chat_context_meta VALUES('revision', 1);
            CREATE TABLE IF NOT EXISTS chat_summary_batch (
                id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER NOT NULL,
                start_ts INTEGER NOT NULL, end_ts INTEGER NOT NULL,
                message_count INTEGER NOT NULL, data_hash TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', result TEXT,
                context_revision INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0, retry_after INTEGER NOT NULL DEFAULT 0,
                started_at INTEGER, finished_at INTEGER, error TEXT,
                created_at INTEGER NOT NULL,
                UNIQUE(group_id, data_hash)
            );
            CREATE INDEX IF NOT EXISTS idx_chat_batch_group_time
                ON chat_summary_batch(group_id, start_ts, end_ts, status);
            CREATE INDEX IF NOT EXISTS idx_chat_batch_complete_latest
                ON chat_summary_batch(group_id, end_ts DESC, id DESC)
                WHERE status='complete';
            CREATE TABLE IF NOT EXISTS chat_summary_member (
                batch_id INTEGER NOT NULL, group_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL, sent_at INTEGER NOT NULL,
                PRIMARY KEY(batch_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_chat_summary_member_group
                ON chat_summary_member(group_id, sent_at, message_id);
            CREATE TABLE IF NOT EXISTS chat_summary_payload (
                batch_id INTEGER PRIMARY KEY, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chat_unknown_term (
                group_id INTEGER NOT NULL, term TEXT NOT NULL COLLATE NOCASE,
                guess TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
                evidence TEXT NOT NULL DEFAULT '[]', occurrence_count INTEGER NOT NULL DEFAULT 1,
                first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL,
                PRIMARY KEY(group_id, term)
            );
            CREATE TABLE IF NOT EXISTS chat_term_mention (
                group_id INTEGER NOT NULL, term TEXT NOT NULL COLLATE NOCASE,
                message_id INTEGER NOT NULL, sent_at INTEGER NOT NULL,
                PRIMARY KEY(group_id, term, sent_at, message_id)
            );
            CREATE TABLE IF NOT EXISTS chat_query_cache (
                cache_key TEXT PRIMARY KEY, result TEXT NOT NULL, created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chat_summary_cycle (
                group_id INTEGER PRIMARY KEY, cursor_ts INTEGER NOT NULL DEFAULT 0,
                cursor_mid INTEGER NOT NULL DEFAULT 0, result TEXT,
                context_revision INTEGER NOT NULL DEFAULT 0, latest_batch_id INTEGER,
                updated_at INTEGER NOT NULL DEFAULT 0, claim_token TEXT, claimed_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS chat_summary_revision (
                id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL,
                context_revision INTEGER NOT NULL, result TEXT NOT NULL,
                saved_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chat_summary_recovery (
                batch_id INTEGER PRIMARY KEY, payload TEXT NOT NULL,
                expires_at INTEGER NOT NULL, retry_after INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0, claim_token TEXT, claimed_at INTEGER
            );
        """)
        columns = {r[1] for r in conn.execute("PRAGMA table_info(chat_summary_batch)")}
        if "claim_token" not in columns:
            conn.execute("ALTER TABLE chat_summary_batch ADD COLUMN claim_token TEXT")
        if "origin" not in columns:
            conn.execute(
                "ALTER TABLE chat_summary_batch ADD COLUMN origin TEXT NOT NULL DEFAULT 'automatic'"
            )
        term_columns = {
            r[1] for r in conn.execute("PRAGMA table_info(chat_unknown_term)")
        }
        if "mention_count" not in term_columns:
            conn.execute(
                "ALTER TABLE chat_unknown_term ADD COLUMN mention_count INTEGER NOT NULL DEFAULT 0"
            )
        # OneBot消息ID可能跨天复用，时间戳属于长期批次的消息身份。
        conn.execute("DROP INDEX IF EXISTS idx_chat_member_once")
        conn.execute("DROP INDEX IF EXISTS idx_chat_member_identity")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_member_lookup ON chat_summary_member(group_id,message_id,sent_at)"
        )
        for batch in conn.execute(
            "SELECT b.* FROM chat_summary_batch b JOIN ("
            "SELECT id,ROW_NUMBER() OVER(PARTITION BY group_id ORDER BY end_ts DESC,id DESC) AS latest_rank "
            "FROM chat_summary_batch WHERE status='complete') latest ON latest.id=b.id "
            "WHERE latest.latest_rank=1"
        ).fetchall():
            last = conn.execute(
                "SELECT sent_at,message_id FROM chat_summary_member WHERE batch_id=? ORDER BY sent_at DESC,message_id DESC LIMIT 1",
                (batch["id"],),
            ).fetchone()
            if last:
                conn.execute(
                    "INSERT INTO chat_summary_cycle(group_id,cursor_ts,cursor_mid,result,context_revision,latest_batch_id,updated_at) "
                    "VALUES(?,?,?,?,?,?,?) ON CONFLICT(group_id) DO UPDATE SET cursor_ts=excluded.cursor_ts,cursor_mid=excluded.cursor_mid,"
                    "result=excluded.result,context_revision=excluded.context_revision,latest_batch_id=excluded.latest_batch_id,updated_at=excluded.updated_at "
                    "WHERE chat_summary_cycle.claim_token IS NULL AND (chat_summary_cycle.cursor_ts,chat_summary_cycle.cursor_mid)<(excluded.cursor_ts,excluded.cursor_mid)",
                    (
                        batch["group_id"],
                        last["sent_at"],
                        last["message_id"],
                        batch["result"],
                        batch["context_revision"],
                        batch["id"],
                        batch["finished_at"] or 0,
                    ),
                )
        now = int(time.time())
        conn.executemany(
            "INSERT OR IGNORE INTO chat_glossary VALUES(?,?,?,?,?)",
            [
                (
                    t["term"],
                    t["meaning"],
                    int(t.get("keep_original", False)),
                    int(t.get("needs_context", False)),
                    now,
                )
                for t in seed_terms()
            ],
        )
        updated = 0
        for term in seed_terms():
            meaning = term['meaning']
            old = re.sub(r'\d+期生（\d+/(VR\d+)）', r'\1', meaning)
            if term['term'] == 'VRxx7':
                old = 'VirtuaReal的期生写法，xx表示第xx期：VR197是19期生，VR257是25期生，VR287是28期生。'
            if old != meaning:
                updated += conn.execute(
                    'UPDATE chat_glossary SET definition=?,updated_at=? WHERE term=? AND definition=?',
                    (meaning, now, term['term'], old),
                ).rowcount
        if updated:
            conn.execute("UPDATE chat_context_meta SET value=value+1 WHERE key='revision'")
            conn.execute('DELETE FROM chat_query_cache')


def load_context(db_path=DB_PATH, group_id=None):
    if Path(db_path).exists():
        with connection(db_path) as conn:
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='chat_glossary'"
            ).fetchone():
                context = {
                    "terms": [
                        {
                            "term": r["term"],
                            "meaning": r["definition"],
                            "keep_original": bool(r["keep_original"]),
                            "needs_context": bool(r["needs_context"]),
                        }
                        for r in conn.execute(
                            "SELECT * FROM chat_glossary ORDER BY term COLLATE NOCASE"
                        )
                    ]
                }
                if group_id is not None:
                    context["ignored_terms"] = [
                        r[0]
                        for r in conn.execute(
                            "SELECT term FROM chat_unknown_term WHERE group_id=? AND status='ignored'",
                            (group_id,),
                        )
                    ]
                # 固定记法用于解释各词条中的数字简称，不依赖某一个别名是否被人工编辑。
                context['notation'] = json.loads(chat_context.CONTEXT_PATH.read_text()).get('notation', {})
                return context
    return {"terms": seed_terms()}


def revision(db_path=DB_PATH):
    with connection(db_path) as conn:
        return conn.execute(
            "SELECT value FROM chat_context_meta WHERE key='revision'"
        ).fetchone()[0]


def save_definition(
    term,
    definition,
    *,
    keep_original=False,
    needs_context=False,
    group_id=None,
    qq=None,
    canonical_name=None,
    db_path=DB_PATH,
):
    term, definition = str(term).strip(), str(definition).strip()
    if not term or len(term) > 100 or not definition or len(definition) > 2000:
        raise ValueError("词条需为1—100字，解释需为1—2000字")
    if qq is not None:
        if (
            not group_id
            or int(group_id) <= 0
            or int(qq) <= 0
            or not str(canonical_name or "").strip()
        ):
            raise ValueError("绑定群友时需填写群号、QQ号和主名")
        canonical_name = str(canonical_name).strip()
        if len(canonical_name) > 100:
            raise ValueError("主名最多100字")
    with connection(db_path) as conn:
        if qq is not None:
            chat_context.write_aliases(
                conn, int(group_id), int(qq), canonical_name, [term]
            )
        conn.execute(
            "INSERT INTO chat_glossary VALUES(?,?,?,?,?) ON CONFLICT(term) DO UPDATE SET "
            "definition=excluded.definition,keep_original=excluded.keep_original,"
            "needs_context=excluded.needs_context,updated_at=excluded.updated_at",
            (
                term,
                definition,
                int(keep_original),
                int(needs_context),
                int(time.time()),
            ),
        )
        conn.execute(
            "UPDATE chat_unknown_term SET status='resolved' WHERE term=? COLLATE NOCASE",
            (term,),
        )
        conn.execute("UPDATE chat_context_meta SET value=value+1 WHERE key='revision'")
        conn.execute("DELETE FROM chat_query_cache")


def save_person(group_id, qq, name, aliases=(), db_path=DB_PATH):
    """人工指定摘要主名并追加已确认代号，保留观察到的昵称。"""
    if not (0 < int(group_id) < 2**63 and 0 < int(qq) < 2**63):
        raise ValueError("群号和QQ号需为正整数")
    name = str(name).strip()
    aliases = list(dict.fromkeys(str(alias).strip() for alias in aliases if str(alias).strip()))
    if not name or len(name) > 100:
        raise ValueError("主名需为1—100字")
    if len(aliases) > 30 or any(len(alias) > 100 for alias in aliases):
        raise ValueError("一次最多添加30个代号，每个代号最多100字")
    with connection(db_path) as conn:
        chat_context.write_aliases(conn, int(group_id), int(qq), name, aliases)
        conn.execute("UPDATE chat_context_meta SET value=value+1 WHERE key='revision'")
        conn.execute("DELETE FROM chat_query_cache")


def get_definition(term, db_path=DB_PATH):
    with connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM chat_glossary WHERE term=? COLLATE NOCASE", (term,)
        ).fetchone()
    return dict(row) if row else None


def get_bindings(term, db_path=DB_PATH):
    with connection(db_path) as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT p.group_id,p.qq,p.display_name FROM person p JOIN alias a "
                "ON p.group_id=a.group_id AND p.qq=a.qq WHERE a.name=? COLLATE NOCASE AND a.source='confirmed'",
                (term,),
            )
        ]


def get_unknown(group_id, term, db_path=DB_PATH):
    with connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM chat_unknown_term WHERE group_id=? AND term=? COLLATE NOCASE",
            (group_id, term),
        ).fetchone()
    return dict(row) if row else None


def dismiss_term(group_id, term, db_path=DB_PATH):
    with connection(db_path) as conn:
        return conn.execute(
            "UPDATE chat_unknown_term SET status='ignored' WHERE group_id=? AND term=? COLLATE NOCASE",
            (group_id, term),
        ).rowcount


def glossary_page(query="", page=1, db_path=DB_PATH):
    page = max(1, page)
    with connection(db_path) as conn:
        pattern = (
            "%"
            + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            + "%"
        )
        where = "WHERE term LIKE ? ESCAPE '\\' OR definition LIKE ? ESCAPE '\\'"
        total = conn.execute(
            f"SELECT COUNT(*) FROM chat_glossary {where}", (pattern, pattern)
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM chat_glossary {where} ORDER BY term COLLATE NOCASE LIMIT 30 OFFSET ?",
            (pattern, pattern, (page - 1) * 30),
        ).fetchall()
    return {
        "items": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "pages": max(1, (total + 29) // 30),
    }


def unknown_page(query="", page=1, db_path=DB_PATH, *, min_mentions=1):
    min_mentions = max(0, min(1000000, int(min_mentions)))
    # 未找到可追溯原消息的历史词条也保留在默认列表中。
    minimum = min_mentions if min_mentions > 1 else 0
    with connection(db_path) as conn:
        pattern = (
            "%"
            + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            + "%"
        )
        total = conn.execute(
            "SELECT COUNT(*) FROM chat_unknown_term WHERE status='pending' AND term LIKE ? ESCAPE '\\' AND mention_count>=?",
            (pattern, minimum),
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT * FROM chat_unknown_term WHERE status='pending' AND term LIKE ? ESCAPE '\\' "
            "AND mention_count>=? ORDER BY mention_count DESC,last_seen DESC,term COLLATE NOCASE,group_id LIMIT 30 OFFSET ?",
            (pattern, minimum, (max(1, page) - 1) * 30),
        ).fetchall()
    return {
        "items": [dict(r) for r in rows],
        "total": total,
        "page": max(1, page),
        "pages": max(1, (total + 29) // 30),
    }


def _add_mention_ids(conn, group_id, term, identities):
    added = conn.executemany(
        "INSERT OR IGNORE INTO chat_term_mention VALUES(?,?,?,?)",
        [(group_id, term, mid, ts) for mid, ts in identities],
    ).rowcount
    if added:
        conn.execute(
            "UPDATE chat_unknown_term SET mention_count=mention_count+? WHERE group_id=? AND term=? COLLATE NOCASE",
            (added, group_id, term),
        )


def _record_term_mentions(conn, group_id, rows):
    texts = []
    for row in rows:
        text = "".join(
            str(s.get("text", ""))
            for s in json.loads(row["segments"] or "[]")
            if s["type"] == "text"
        ).strip()
        if (
            text
            and not low_information_command(text)
            and identify_command(text) != "在聊什么"
        ):
            texts.append((row["message_id"], row["sent_at"], text.casefold()))
    for item in conn.execute(
        "SELECT term FROM chat_unknown_term WHERE group_id=? AND status='pending'",
        (group_id,),
    ).fetchall():
        term = item[0]
        variants = [
            name.strip().casefold() for name in re.split(r"[/／]", term) if name.strip()
        ]
        patterns = [
            re.compile(
                (r"(?<![a-z0-9_])" if re.match(r"[a-z0-9_]", name) else "")
                + re.escape(name)
                + (r"(?![a-z0-9_])" if re.search(r"[a-z0-9_]$", name) else "")
            )
            for name in variants
        ]
        _add_mention_ids(
            conn,
            group_id,
            term,
            [
                (mid, ts)
                for mid, ts, text in texts
                if any(p.search(text) for p in patterns)
            ],
        )


def record_term_mentions(group_id, rows, db_path=DB_PATH):
    """按包含词条的去重消息条数累计，不保存消息正文。"""
    with connection(db_path) as conn:
        _record_term_mentions(conn, group_id, rows)


def backfill_term_mentions(*, db_path=DB_PATH, cache_path=None, bot_qq="", now=None):
    """仅从尚未过期的原文回补实际提及，不将摘要关联证据视为提及。"""
    init_db(db_path)
    now = int(time.time()) if now is None else int(now)
    with connection(db_path) as conn:
        groups = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT group_id FROM chat_unknown_term WHERE status='pending'"
            )
        ]
        before = conn.execute("SELECT COUNT(*) FROM chat_term_mention").fetchone()[0]
    for group_id in groups:
        rows = chat_context.list_messages(group_id, now - RAW_TTL, now + 1, cache_path)
        record_term_mentions(
            group_id,
            [row for row in rows if str(row["user_id"]) != str(bot_qq)],
            db_path,
        )
    with connection(db_path) as conn:
        after = conn.execute("SELECT COUNT(*) FROM chat_term_mention").fetchone()[0]
    return {"groups": len(groups), "added_mentions": after - before}


def record_unknown_terms(
    group_id, result, batch_id=None, db_path=DB_PATH, *, rows=None
):
    with connection(db_path) as conn:
        for item in result.get("terms_to_check", []):
            term = str(item.get("term", "")).strip()[:100]
            if (
                not term
                or conn.execute(
                    "SELECT 1 FROM chat_glossary WHERE term=? COLLATE NOCASE", (term,)
                ).fetchone()
            ):
                continue
            mids = [str(mid) for mid in item.get("message_ids", [])[:12]]
            previous = conn.execute(
                "SELECT evidence FROM chat_unknown_term WHERE group_id=? AND term=? COLLATE NOCASE",
                (group_id, term),
            ).fetchone()
            old_evidence = json.loads(previous[0]) if previous else {}
            same_evidence = sorted(old_evidence.get("message_ids", [])) == sorted(mids)
            # 查询复用同一批证据时保留批次位置，不重复累计发现次数。
            if same_evidence and batch_id is None:
                batch_id_for_term = old_evidence.get("batch_id")
            else:
                batch_id_for_term = batch_id
            evidence = json.dumps(
                {"batch_id": batch_id_for_term, "message_ids": mids}, ensure_ascii=False
            )
            now = int(time.time())
            conn.execute(
                "INSERT INTO chat_unknown_term(group_id,term,guess,evidence,first_seen,last_seen) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(group_id,term) DO UPDATE SET guess=excluded.guess,evidence=excluded.evidence,"
                "last_seen=excluded.last_seen,occurrence_count=occurrence_count+?",
                (
                    group_id,
                    term,
                    str(item.get("possible_meaning", ""))[:2000],
                    evidence,
                    now,
                    now,
                    0 if same_evidence else 1,
                ),
            )
        if rows is not None:
            _record_term_mentions(conn, group_id, rows)


def assigned_ids(group_id, since, db_path=DB_PATH):
    with connection(db_path) as conn:
        return {
            r[0]
            for r in conn.execute(
                "SELECT m.message_id FROM chat_summary_member m JOIN chat_summary_batch b ON b.id=m.batch_id "
                "WHERE m.group_id=? AND m.sent_at>=? AND b.status<>'cancelled'",
                (group_id, since),
            )
        }


def stage_batch(group_id, rows, db_path=DB_PATH):
    if len(rows) != BATCH_SIZE or len({r["message_id"] for r in rows}) != BATCH_SIZE:
        raise ValueError("自动批次需要500条不同的消息")
    digest = hashlib.sha256(
        json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    with connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT id FROM chat_summary_batch WHERE group_id=? AND data_hash=?",
            (group_id, digest),
        ).fetchone()
        if existing:
            return existing[0]
        identities = [value for r in rows for value in (r["message_id"], r["sent_at"])]
        if conn.execute(
            "SELECT 1 FROM chat_summary_member m JOIN chat_summary_batch b ON b.id=m.batch_id "
            "WHERE m.group_id=? AND b.status<>'cancelled' AND (m.message_id,m.sent_at) IN (VALUES "
            + ",".join("(?,?)" for _ in rows)
            + ") LIMIT 1",
            (group_id, *identities),
        ).fetchone():
            return None
        conn.execute(
            "INSERT INTO chat_summary_batch(group_id,start_ts,end_ts,message_count,data_hash,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                group_id,
                rows[0]["sent_at"],
                rows[-1]["sent_at"],
                len(rows),
                digest,
                int(time.time()),
            ),
        )
        bid = conn.execute(
            "SELECT id FROM chat_summary_batch WHERE group_id=? AND data_hash=?",
            (group_id, digest),
        ).fetchone()[0]
        conn.executemany(
            "INSERT OR IGNORE INTO chat_summary_member VALUES(?,?,?,?)",
            [(bid, group_id, r["message_id"], r["sent_at"]) for r in rows],
        )
        conn.execute(
            "INSERT OR IGNORE INTO chat_summary_payload VALUES(?,?)",
            (bid, json.dumps(rows, ensure_ascii=False)),
        )
    return bid


def get_batch(batch_id, db_path=DB_PATH):
    with connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM chat_summary_batch WHERE id=?", (batch_id,)
        ).fetchone()
    return dict(row) if row else None


def get_cycle(group_id, db_path=DB_PATH):
    with connection(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO chat_summary_cycle(group_id) VALUES(?)", (group_id,)
        )
        row = conn.execute(
            "SELECT * FROM chat_summary_cycle WHERE group_id=?", (group_id,)
        ).fetchone()
    return dict(row)


def active_batch(group_id, db_path=DB_PATH):
    with connection(db_path) as conn:
        return (
            conn.execute(
                "SELECT id FROM chat_summary_batch WHERE group_id=? AND status IN ('pending','running','failed') LIMIT 1",
                (group_id,),
            ).fetchone()
            is not None
        )


def _claim_cycle(conn, group_id, token, now):
    conn.execute(
        "UPDATE chat_summary_cycle SET claim_token=NULL WHERE claim_token IS NOT NULL AND claimed_at<?",
        (now - LEASE_SECONDS,),
    )
    conn.execute(
        "INSERT OR IGNORE INTO chat_summary_cycle(group_id) VALUES(?)", (group_id,)
    )
    if (
        conn.execute(
            "SELECT (SELECT COUNT(*) FROM chat_summary_cycle WHERE claim_token IS NOT NULL) "
            "+ (SELECT COUNT(*) FROM chat_summary_recovery WHERE claim_token IS NOT NULL)"
        ).fetchone()[0]
        >= 2
    ):
        return False
    return bool(
        conn.execute(
            "UPDATE chat_summary_cycle SET claim_token=?,claimed_at=? WHERE group_id=? AND claim_token IS NULL",
            (token, now, group_id),
        ).rowcount
    )


def claim_cycle(group_id, db_path=DB_PATH):
    with connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        token = uuid.uuid4().hex
        if not _claim_cycle(conn, group_id, token, int(time.time())):
            return None
        return dict(
            conn.execute(
                "SELECT * FROM chat_summary_cycle WHERE group_id=?", (group_id,)
            ).fetchone()
        )


def release_cycle(group_id, token, db_path=DB_PATH):
    with connection(db_path) as conn:
        conn.execute(
            "UPDATE chat_summary_cycle SET claim_token=NULL WHERE group_id=? AND claim_token=?",
            (group_id, token),
        )


def _finish_cycle(conn, group_id, last, result, rev, bid, token):
    changed = conn.execute(
        "UPDATE chat_summary_cycle SET cursor_ts=?,cursor_mid=?,result=?,context_revision=?,latest_batch_id=?,updated_at=?,claim_token=NULL "
        "WHERE group_id=? AND claim_token=?",
        (
            last["sent_at"],
            last["message_id"],
            json.dumps(result, ensure_ascii=False),
            rev,
            bid,
            int(time.time()),
            group_id,
            token,
        ),
    ).rowcount
    if changed:
        conn.execute(
            "UPDATE chat_summary_batch SET status='cancelled',claim_token=NULL,error='已被按需总结覆盖' "
            "WHERE group_id=? AND id<>? AND status IN ('pending','failed','running') "
            "AND EXISTS(SELECT 1 FROM chat_summary_member m WHERE m.batch_id=chat_summary_batch.id AND (m.sent_at,m.message_id)<=(?,?))",
            (group_id, bid, last["sent_at"], last["message_id"]),
        )
        conn.execute(
            "DELETE FROM chat_summary_payload WHERE batch_id IN (SELECT id FROM chat_summary_batch WHERE status='cancelled')"
        )
    return bool(changed)


def finish_query(group_id, rows, result, rev, token, db_path=DB_PATH):
    """按需总结保存为完整批次，游标推进到本轮已分析的最后一条消息。"""
    if not rows:
        raise ValueError("没有可消费的消息")
    digest = hashlib.sha256(
        json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    now = int(time.time())
    with connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute(
            "SELECT 1 FROM chat_summary_cycle WHERE group_id=? AND claim_token=?",
            (group_id, token),
        ).fetchone():
            return False
        conn.execute(
            "INSERT INTO chat_summary_batch(group_id,start_ts,end_ts,message_count,data_hash,status,result,context_revision,finished_at,created_at,origin) "
            "VALUES(?,?,?,?,?,'complete',?,?,?,?, 'query') ON CONFLICT(group_id,data_hash) DO UPDATE SET "
            "status='complete',result=excluded.result,context_revision=excluded.context_revision,finished_at=excluded.finished_at,origin='query',claim_token=NULL,error=NULL",
            (
                group_id,
                rows[0]["sent_at"],
                rows[-1]["sent_at"],
                len(rows),
                digest,
                json.dumps(result, ensure_ascii=False),
                rev,
                now,
                now,
            ),
        )
        bid = conn.execute(
            "SELECT id FROM chat_summary_batch WHERE group_id=? AND data_hash=?",
            (group_id, digest),
        ).fetchone()[0]
        conn.executemany(
            "INSERT OR IGNORE INTO chat_summary_member VALUES(?,?,?,?)",
            [(bid, group_id, r["message_id"], r["sent_at"]) for r in rows],
        )
        conn.execute("DELETE FROM chat_summary_payload WHERE batch_id=?", (bid,))
        _retain_fallback(conn, bid, rows, result)
        _finish_cycle(conn, group_id, rows[-1], result, rev, bid, token)
    record_unknown_terms(group_id, result, bid, db_path, rows=rows)
    return bid


def save_reanalysis(batch_id, result, rev, db_path=DB_PATH, *, rows=None, expected_result=None):
    """保留旧版总结；重新解释历史批次不会移动当前消息计数起点。"""
    with connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        old = conn.execute(
            "SELECT * FROM chat_summary_batch WHERE id=? AND status='complete'",
            (batch_id,),
        ).fetchone()
        if not old:
            raise ValueError("该批次还没有成功的总结")
        if expected_result is not None and old['result'] != expected_result:
            return False
        conn.execute(
            "INSERT INTO chat_summary_revision(batch_id,context_revision,result,saved_at) VALUES(?,?,?,?)",
            (batch_id, old["context_revision"], old["result"], int(time.time())),
        )
        encoded = json.dumps(result, ensure_ascii=False)
        conn.execute(
            "UPDATE chat_summary_batch SET result=?,context_revision=? WHERE id=?",
            (encoded, rev, batch_id),
        )
        conn.execute(
            "UPDATE chat_summary_cycle SET result=?,context_revision=? WHERE latest_batch_id=?",
            (encoded, rev, batch_id),
        )
        conn.execute("DELETE FROM chat_query_cache")
        if rows is not None:
            _retain_fallback(conn, batch_id, rows, result)
    record_unknown_terms(old["group_id"], result, batch_id, db_path, rows=rows)
    return True


def _retain_fallback(conn, batch_id, rows, result):
    """兜底原文最多暂存24小时，供主模型恢复后重新分析；不延长原文生命周期。"""
    if result.get('needs_primary_reanalysis') and rows:
        conn.execute(
            "INSERT INTO chat_summary_recovery(batch_id,payload,expires_at) VALUES(?,?,?) "
            "ON CONFLICT(batch_id) DO UPDATE SET payload=excluded.payload,expires_at=excluded.expires_at",
            (batch_id, json.dumps(rows, ensure_ascii=False), min(r['sent_at'] for r in rows) + RAW_TTL))
    else:
        conn.execute("DELETE FROM chat_summary_recovery WHERE batch_id=?", (batch_id,))


def recovery_candidates(db_path=DB_PATH, *, now=None):
    now = int(time.time()) if now is None else now
    with connection(db_path) as conn:
        conn.execute("DELETE FROM chat_summary_recovery WHERE expires_at<=?", (now,))
        conn.execute("UPDATE chat_summary_recovery SET claim_token=NULL WHERE claimed_at<?",
                     (now - LEASE_SECONDS,))
        return [dict(row) for row in conn.execute(
            "SELECT r.batch_id,b.group_id FROM chat_summary_recovery r "
            "JOIN chat_summary_batch b ON b.id=r.batch_id "
            "WHERE r.retry_after<=? AND r.claim_token IS NULL AND b.status='complete' "
            "ORDER BY b.end_ts DESC LIMIT 20", (now,))]


def claim_recovery(batch_id, db_path=DB_PATH):
    now = int(time.time())
    with connection(db_path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        if conn.execute(
            "SELECT (SELECT COUNT(*) FROM chat_summary_cycle WHERE claim_token IS NOT NULL) "
            "+ (SELECT COUNT(*) FROM chat_summary_recovery WHERE claim_token IS NOT NULL)"
        ).fetchone()[0] >= 2:
            return None
        token = uuid.uuid4().hex
        if not conn.execute(
            "UPDATE chat_summary_recovery SET claim_token=?,claimed_at=?,attempts=attempts+1 "
            "WHERE batch_id=? AND expires_at>? AND retry_after<=? AND claim_token IS NULL",
            (token, now, batch_id, now, now)).rowcount:
            return None
        row = conn.execute(
            "SELECT r.*,b.group_id,b.result FROM chat_summary_recovery r "
            "JOIN chat_summary_batch b ON b.id=r.batch_id WHERE r.batch_id=?", (batch_id,)).fetchone()
        if not row or not json.loads(row['result'] or '{}').get('needs_primary_reanalysis'):
            conn.execute('DELETE FROM chat_summary_recovery WHERE batch_id=?', (batch_id,))
            return None
        value = dict(row)
        value['rows'] = json.loads(value.pop('payload'))
        return value


def release_recovery(batch_id, token, db_path=DB_PATH, *, finished=False):
    with connection(db_path) as conn:
        if finished:
            conn.execute('DELETE FROM chat_summary_recovery WHERE batch_id=? AND claim_token=?',
                         (batch_id, token))
        else:
            retry_seconds = max(60, int(os.environ.get('AI_CODEX_RETRY_SECONDS', '300')))
            conn.execute(
                'UPDATE chat_summary_recovery SET claim_token=NULL,retry_after=? WHERE batch_id=? AND claim_token=?',
                (int(time.time()) + retry_seconds, batch_id, token))


def save_participant_metadata(batch_id, original_json, result, db_path=DB_PATH):
    """为旧摘要补头像身份；并发重分析后的内容不会被覆盖。"""
    encoded = json.dumps(result, ensure_ascii=False)
    with connection(db_path) as conn:
        conn.execute(
            "UPDATE chat_summary_batch SET result=? WHERE id=? AND result=?",
            (encoded, batch_id, original_json),
        )
        conn.execute(
            "UPDATE chat_summary_cycle SET result=? WHERE latest_batch_id=? AND result=?",
            (encoded, batch_id, original_json),
        )


def claim_batch(batch_id, lease_seconds=LEASE_SECONDS, db_path=DB_PATH):
    now = int(time.time())
    with connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _expire_pending(conn, now, lease_seconds)
        if (
            conn.execute(
                "SELECT COUNT(*) FROM chat_summary_batch WHERE status='running'"
            ).fetchone()[0]
            >= 2
        ):
            return None
        token = uuid.uuid4().hex
        candidate = conn.execute(
            "SELECT * FROM chat_summary_batch WHERE id=? AND status IN ('pending','failed') AND retry_after<=?",
            (batch_id, now),
        ).fetchone()
        if not candidate:
            return None
        state = conn.execute(
            "SELECT cursor_ts,cursor_mid FROM chat_summary_cycle WHERE group_id=?",
            (candidate["group_id"],),
        ).fetchone()
        first = conn.execute(
            "SELECT sent_at,message_id FROM chat_summary_member WHERE batch_id=? ORDER BY sent_at,message_id LIMIT 1",
            (batch_id,),
        ).fetchone()
        if state and first and tuple(first) <= tuple(state):
            conn.execute(
                "UPDATE chat_summary_batch SET status='cancelled',error='已被按需总结覆盖' WHERE id=?",
                (batch_id,),
            )
            conn.execute(
                "DELETE FROM chat_summary_payload WHERE batch_id=?", (batch_id,)
            )
            return None
        if not _claim_cycle(conn, candidate["group_id"], token, now):
            return None
        changed = conn.execute(
            "UPDATE chat_summary_batch SET status='running',started_at=?,attempts=attempts+1,claim_token=? "
            "WHERE id=? AND status IN ('pending','failed') AND retry_after<=?",
            (now, token, batch_id, now),
        ).rowcount
        if not changed:
            return None
        row = conn.execute(
            "SELECT payload FROM chat_summary_payload WHERE batch_id=?", (batch_id,)
        ).fetchone()
        if row is None:
            conn.execute(
                "UPDATE chat_summary_batch SET status='cancelled',claim_token=NULL,error='消息暂存不存在' WHERE id=?",
                (batch_id,),
            )
            conn.execute(
                "UPDATE chat_summary_cycle SET claim_token=NULL WHERE claim_token=?",
                (token,),
            )
            return None
        return {"rows": json.loads(row[0]), "token": token}


def finish_batch(
    batch_id, result, context_revision, token, db_path=DB_PATH, *, rows=None
):
    with connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        owner = conn.execute(
            "SELECT group_id FROM chat_summary_batch WHERE id=?", (batch_id,)
        ).fetchone()
        if (
            not owner
            or not conn.execute(
                "SELECT 1 FROM chat_summary_cycle WHERE group_id=? AND claim_token=?",
                (owner[0], token),
            ).fetchone()
        ):
            return False
        changed = conn.execute(
            "UPDATE chat_summary_batch SET status='complete',result=?,context_revision=?,finished_at=?,error=NULL,claim_token=NULL "
            "WHERE id=? AND status='running' AND claim_token=?",
            (
                json.dumps(result, ensure_ascii=False),
                context_revision,
                int(time.time()),
                batch_id,
                token,
            ),
        ).rowcount
        if not changed:
            return False
        if rows is None:
            payload = conn.execute(
                "SELECT payload FROM chat_summary_payload WHERE batch_id=?", (batch_id,)
            ).fetchone()
            rows = json.loads(payload[0]) if payload else []
        conn.execute("DELETE FROM chat_summary_payload WHERE batch_id=?", (batch_id,))
        _retain_fallback(conn, batch_id, rows, result)
        last = conn.execute(
            "SELECT sent_at,message_id FROM chat_summary_member WHERE batch_id=? ORDER BY sent_at DESC,message_id DESC LIMIT 1",
            (batch_id,),
        ).fetchone()
        _finish_cycle(
            conn, owner[0], dict(last), result, context_revision, batch_id, token
        )
    record_unknown_terms(
        get_batch(batch_id, db_path)["group_id"], result, batch_id, db_path, rows=rows
    )
    return True


def release_batch(batch_id, token, db_path=DB_PATH):
    """已取消的执行退回待处理状态，令牌校验保护其他执行者。"""
    with connection(db_path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        changed = conn.execute(
            "UPDATE chat_summary_batch SET status='pending',claim_token=NULL,started_at=NULL,retry_after=0,"
            "attempts=MAX(0,attempts-1) WHERE id=? AND status='running' AND claim_token=?",
            (batch_id,token),
        ).rowcount
        if changed:
            conn.execute('UPDATE chat_summary_cycle SET claim_token=NULL WHERE claim_token=?',(token,))
        return bool(changed)


def fail_batch(batch_id, error, token, db_path=DB_PATH):
    with connection(db_path) as conn:
        conn.execute(
            "UPDATE chat_summary_batch SET status=CASE WHEN attempts>=? THEN 'cancelled' ELSE 'failed' END,"
            "error=?,retry_after=?,claim_token=NULL WHERE id=? AND status='running' AND claim_token=?",
            (MAX_ATTEMPTS, error, int(time.time()) + 900, batch_id, token),
        )
        conn.execute(
            "UPDATE chat_summary_cycle SET claim_token=NULL WHERE claim_token=?",
            (token,),
        )
        conn.execute(
            "DELETE FROM chat_summary_payload WHERE batch_id IN (SELECT id FROM chat_summary_batch WHERE status='cancelled')"
        )


def _expire_pending(conn, now, lease_seconds=LEASE_SECONDS):
    conn.execute(
        "UPDATE chat_summary_cycle SET claim_token=NULL WHERE claim_token IS NOT NULL AND claimed_at<?",
        (now - lease_seconds,),
    )
    conn.execute(
        "UPDATE chat_summary_batch SET status='pending',claim_token=NULL WHERE status='running' AND started_at<?",
        (now - lease_seconds,),
    )
    conn.execute(
        "UPDATE chat_summary_batch SET status='cancelled',claim_token=NULL,error='消息暂存已过期或重试次数已用尽' "
        "WHERE status IN ('pending','failed') AND (MIN(start_ts,created_at)<? OR attempts>=?)",
        (now - RAW_TTL, MAX_ATTEMPTS),
    )
    conn.execute(
        "DELETE FROM chat_summary_payload WHERE batch_id IN (SELECT id FROM chat_summary_batch WHERE status='cancelled')"
    )


def ready_batch_ids(db_path=DB_PATH):
    with connection(db_path) as conn:
        _expire_pending(conn, int(time.time()))
        return [
            r[0]
            for r in conn.execute(
                "SELECT id FROM chat_summary_batch WHERE status IN ('pending','failed') AND retry_after<=? ORDER BY id LIMIT 2",
                (int(time.time()),),
            )
        ]


def cached_batches(group_id, start, end, db_path=DB_PATH):
    with connection(db_path) as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM chat_summary_batch WHERE group_id=? AND status='complete' AND start_ts>=? AND end_ts<? ORDER BY start_ts,id",
                (group_id, start, end),
            )
        ]


def recent_count_batches(group_id, count=500, db_path=DB_PATH):
    """从最新完整批次向前取足条数，以消息身份避开合并前的旧版与重叠批次。"""
    with connection(db_path) as conn:
        candidates = conn.execute(
            "SELECT * FROM chat_summary_batch WHERE group_id=? AND status='complete' "
            "ORDER BY end_ts DESC,id DESC",
            (group_id,),
        )
        chosen, covered, total = [], set(), 0
        for row in candidates:
            keys = set(conn.execute(
                "SELECT sent_at,message_id FROM chat_summary_member WHERE batch_id=?",
                (row['id'],),
            ).fetchall())
            if not keys or covered.intersection(keys):
                continue
            covered.update(keys)
            chosen.append(dict(row))
            total += int(row['message_count'])
            if total >= count:
                break
    return list(reversed(chosen))


def member_ids(batch_ids, db_path=DB_PATH):
    if not batch_ids:
        return set()
    with connection(db_path) as conn:
        return {
            r[0]
            for r in conn.execute(
                "SELECT message_id FROM chat_summary_member WHERE batch_id IN ("
                + ",".join("?" for _ in batch_ids)
                + ")",
                batch_ids,
            )
        }


def query_cache_get(key, db_path=DB_PATH):
    with connection(db_path) as conn:
        row = conn.execute(
            "SELECT result FROM chat_query_cache WHERE cache_key=? AND created_at>=?",
            (key, int(time.time()) - RAW_TTL),
        ).fetchone()
    return json.loads(row[0]) if row else None


def query_cache_save(key, result, db_path=DB_PATH):
    with connection(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO chat_query_cache VALUES(?,?,?)",
            (key, json.dumps(result, ensure_ascii=False), int(time.time())),
        )
        conn.execute(
            "DELETE FROM chat_query_cache WHERE created_at<?",
            (int(time.time()) - 86400,),
        )


def recent_batches(db_path=DB_PATH):
    with connection(db_path) as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT id,group_id,start_ts,end_ts,message_count,status,error,origin FROM chat_summary_batch ORDER BY id DESC LIMIT 30"
            )
        ]
