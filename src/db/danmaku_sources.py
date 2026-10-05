"""Append-only source evidence, separate from the query-facing event table.

Raw artifacts are compressed in SQLite. Source times are never overwritten
by replay alignment. A link expresses attribution, not deletion permission.
Unattributed historical rows before the first 2026-04-20 live remain unknown. Later stock
rows follow the operator-confirmed monitor attribution, recorded explicitly
in evidence. Online monitor writes have no cold-archive dependency.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import zlib
from pathlib import Path
from typing import Any

EXTERNAL_CUTOFF_MS = 1776677437000  # 2026-04-20 17:30:37 Asia/Shanghai; first live start
ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_DB = ROOT / 'data/libot.db'
ARCHIVE_DB = ROOT / 'data/cold/danmaku_sources.sqlite3'


def connect_archive() -> sqlite3.Connection:
    """Writable cold archive with read-only access to the live event database."""
    ARCHIVE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(ARCHIVE_DB, timeout=10, uri=True)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('ATTACH DATABASE ? AS production', (PRODUCTION_DB.resolve().as_uri()+'?mode=ro',))
    return conn


def cleanup_state(conn: sqlite3.Connection) -> tuple[set[int], set[int]]:
    """Return active tombstones and baselines already consumed by past cleanups."""
    has_restorations=conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='danmaku_cleanup_restored_event'").fetchone()
    restored={r[0] for r in conn.execute('SELECT event_id FROM danmaku_cleanup_restored_event')} if has_restorations else set()
    restoration_times={}
    if has_restorations and any(r[1]=='restored_at' for r in conn.execute('PRAGMA table_info(danmaku_cleanup_restored_event)')):
        restoration_times=dict(conn.execute('SELECT event_id,restored_at FROM danmaku_cleanup_restored_event'))
    redeleted={}
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='danmaku_cleanup_redelete_event'").fetchone():
        for eid,bid,at in conn.execute('SELECT event_id,keep_event_id,applied_at FROM danmaku_cleanup_redelete_event ORDER BY applied_at,proposal_sha256'):
            if at>restoration_times.get(eid,''):redeleted[eid]=bid
    removed=set();claimed=set()
    for eid,reason in conn.execute('SELECT event_id,reason FROM danmaku_removed_event'):
        if eid in restored and eid not in redeleted:continue
        removed.add(eid)
        if eid in redeleted:
            claimed.add(redeleted[eid]);continue
        if reason.startswith('verified_duplicate:keep='):
            claimed.add(int(reason.split('=',1)[1]))
    return removed,claimed

SCHEMA = """
CREATE TABLE IF NOT EXISTS danmaku_source_artifact (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    locator TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    codec TEXT NOT NULL DEFAULT 'zlib',
    payload BLOB NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    archived_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source,locator,sha256)
);
CREATE TABLE IF NOT EXISTS danmaku_source_record (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL CHECK(source IN ('monitor','danmakus','vtbcat','replay','legacy_unknown')),
    source_key TEXT NOT NULL,
    source_session TEXT NOT NULL DEFAULT '',
    room_id INTEGER NOT NULL,
    uid INTEGER,
    uname TEXT,
    content TEXT NOT NULL,
    source_timestamp_ms INTEGER,
    mapped_timestamp_ms INTEGER,
    eligibility TEXT NOT NULL CHECK(eligibility IN ('live','video_comment','unknown','outside_policy')),
    artifact_id INTEGER REFERENCES danmaku_source_artifact(id),
    evidence TEXT NOT NULL DEFAULT '{}',
    archived_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source,source_key)
);
CREATE INDEX IF NOT EXISTS idx_dm_source_session ON danmaku_source_record(source,source_session,id);
CREATE INDEX IF NOT EXISTS idx_dm_source_time ON danmaku_source_record(room_id,source_timestamp_ms,id);
CREATE TABLE IF NOT EXISTS danmaku_source_link (
    source_id INTEGER NOT NULL REFERENCES danmaku_source_record(id),
    event_id INTEGER NOT NULL,
    method TEXT NOT NULL,
    PRIMARY KEY(source_id,event_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_dm_link_event ON danmaku_source_link(event_id,source_id);
CREATE TABLE IF NOT EXISTS danmaku_source_checkpoint (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS danmaku_merge_review (
    run_id TEXT NOT NULL,
    extra_event_id INTEGER NOT NULL,
    baseline_event_id INTEGER,
    decision TEXT NOT NULL,
    evidence TEXT NOT NULL,
    applied_at TEXT,
    PRIMARY KEY(run_id,extra_event_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS danmaku_removed_event (
    event_id INTEGER PRIMARY KEY,
    reason TEXT NOT NULL,
    event_json TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    removed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def initialize(conn: sqlite3.Connection) -> None:
    main_path = next((r[2] for r in conn.execute('PRAGMA database_list') if r[1]=='main'), '')
    if main_path and Path(main_path).resolve()==PRODUCTION_DB.resolve():
        raise ValueError('Cold source evidence must not be created in libot.db')
    # Individual statements keep the caller's transaction intact.
    for statement in SCHEMA.split(';'):
        if statement.strip():
            conn.execute(statement)


def encode(value: Any) -> str:
    return json.dumps(value,ensure_ascii=False,separators=(',',':'),sort_keys=True)


def artifact(conn, source: str, locator: str, payload: bytes, metadata=None) -> int:
    sha=hashlib.sha256(payload).hexdigest()
    old=conn.execute('SELECT id FROM danmaku_source_artifact WHERE source=? AND locator=? AND sha256=?',
                     (source,locator,sha)).fetchone()
    if old:return int(old[0])
    inserted=conn.execute(
        'INSERT INTO danmaku_source_artifact(source,locator,sha256,payload,metadata) VALUES(?,?,?,?,?) '
        'ON CONFLICT(source,locator,sha256) DO NOTHING RETURNING id',
        (source,locator,sha,zlib.compress(payload,6),encode(metadata or {}))).fetchone()
    if inserted:return int(inserted[0])
    return int(conn.execute('SELECT id FROM danmaku_source_artifact WHERE source=? AND locator=? AND sha256=?',
                            (source,locator,sha)).fetchone()[0])


def add_records(conn, records: list[dict]) -> int:
    """Idempotently store immutable records; retain first attribution on repeats."""
    before=conn.total_changes
    conn.executemany(
        'INSERT INTO danmaku_source_record(source,source_key,source_session,room_id,uid,uname,content,'
        'source_timestamp_ms,mapped_timestamp_ms,eligibility,artifact_id,evidence) '
        'VALUES(:source,:source_key,:source_session,:room_id,:uid,:uname,:content,:source_timestamp_ms,'
        ':mapped_timestamp_ms,:eligibility,:artifact_id,:evidence) ON CONFLICT(source,source_key) DO NOTHING', records)
    return conn.total_changes-before


def record(source, key, session, room, uid, uname, content, ts_ms, *, mapped=None,
           eligibility='live', artifact_id=None, evidence=None):
    return dict(source=source,source_key=str(key),source_session=str(session),room_id=room,uid=uid,uname=uname,
                content=content,source_timestamp_ms=ts_ms,mapped_timestamp_ms=mapped,eligibility=eligibility,
                artifact_id=artifact_id,evidence=encode(evidence or {}))


def checkpoint(conn, key, value):
    conn.execute('INSERT INTO danmaku_source_checkpoint(key,value) VALUES(?,?) '
                 'ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP',(key,str(value)))


class CapturedRow(tuple):
    """A normal event tuple carrying the websocket payload until batch commit."""
    def __new__(cls, values, raw_payload):
        obj=super().__new__(cls,values)
        obj.raw_payload=raw_payload
        return obj


def archive_monitor_batch(conn, first_id: int, last_id: int, rows) -> None:
    # Only DANMU_MSG rows have no uniqueness filter in event, so their order
    # within this writer transaction exactly matches the incoming row order.
    danmaku=[r for r in rows if r[1]=='DANMU_MSG']
    if not danmaku:return
    event_ids=[r[0] for r in conn.execute(
        "SELECT id FROM event NOT INDEXED WHERE id>? AND id<=? AND cmd='DANMU_MSG' ORDER BY id",(first_id,last_id))]
    if len(event_ids)!=len(danmaku):raise RuntimeError('monitor source/event batch mismatch')
    raw=[]
    for row in danmaku:
        payload=getattr(row,'raw_payload',None)
        try:
            raw.append(json.loads(payload) if payload else None)
        except (ValueError,TypeError):
            raw.append({'unparsed_payload':str(payload)})
    aid=artifact(conn,'monitor',f'events:{event_ids[0]}:{event_ids[-1]}',encode(raw).encode(),
                 {'capture':'monitor_writer','event_ids':event_ids})
    values=[]
    for slot,(eid,r) in enumerate(zip(event_ids,danmaku)):
        values.append(record('monitor',f'event:{eid}','',r[0],r[2],r[3],r[4],r[9]*1000,
            mapped=r[9]*1000,artifact_id=aid,evidence={'event_id':eid,'slot':slot,'capture':'monitor_writer'}))
    add_records(conn,values)
    conn.executemany("INSERT OR IGNORE INTO danmaku_source_link(source_id,event_id,method) "
        "SELECT id,?,'monitor_writer' FROM danmaku_source_record WHERE source='monitor' AND source_key=?",
        [(eid,f'event:{eid}') for eid in event_ids])
