"""Replace a verified historical interaction range from an independent database.

The rollback database stores every old row and replay provenance before writes.
Short, atomic time windows replace all four interaction kinds together. A small
transactional receipt in production makes interruption recovery unambiguous;
source lineage and detailed backups stay outside the live database. No service
control, PID changes, database-file swaps, or online VACUUM are performed.
"""
import argparse
import collections
import hashlib
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from src.db import danmaku_sources as store
from src.db.event_search import (
    DEFAULT_SEARCH_DB_PATH, SEARCH_SCHEMA_VERSION, _document_tokens,
    _encode_rowid, _fts_query, index_pending_events,
)
from scripts.backfill_mit3uri_replay_danmaku import ROOM_ID
from scripts.apply_source_first_changes import durable_json

CUTOFF = store.EXTERNAL_CUTOFF_MS // 1000
KINDS = ('DANMU_MSG', 'SEND_GIFT', 'GUARD_BUY', 'SUPER_CHAT_MESSAGE')
FIELDS = ('id', 'room_id', 'cmd', 'uid', 'uname', 'content', 'gift_name',
          'gift_num', 'total_coin', 'title', 'timestamp')
COLUMNS = ','.join(FIELDS)
SCOPE = "room_id=? AND cmd IN ('DANMU_MSG','SEND_GIFT','GUARD_BUY','SUPER_CHAT_MESSAGE') AND timestamp>=? AND timestamp<?"
JOURNAL = """
CREATE TABLE IF NOT EXISTS historical_rebuild_batch (
    run_id TEXT NOT NULL, batch INTEGER NOT NULL,
    start INTEGER NOT NULL, end INTEGER NOT NULL,
    old_count INTEGER NOT NULL, new_count INTEGER NOT NULL,
    old_digest TEXT NOT NULL, new_digest TEXT NOT NULL,
    first_id INTEGER, last_id INTEGER,
    committed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(run_id,batch)
) WITHOUT ROWID;
"""


def ro(path):
    return sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.1)


def digest(rows):
    h = hashlib.sha256()
    for r in rows:
        h.update(json.dumps(list(r), ensure_ascii=False, separators=(',', ':')).encode())
        h.update(b'\n')
    return h.hexdigest()


def file_hash(path):
    wal=Path(str(path)+'-wal')
    if wal.exists() and wal.stat().st_size:
        raise ValueError('Candidate must be checkpointed before freezing')
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def put(conn, key, value):
    conn.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, json.dumps(value)))


def get(conn, key, default=None):
    row = conn.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


def window_rows(conn, table, start, end):
    idx='idx_event_room_time' if table=='event' else 'old_event_time'
    return conn.execute(f'SELECT {COLUMNS} FROM {table} INDEXED BY {idx} WHERE {SCOPE} ORDER BY id',
                        (ROOM_ID, start, end)).fetchall()


def prepare(candidate, work, main_path, target=800):
    audit = json.loads((candidate / 'audit.json').read_text())
    if not audit.get('ready_for_promotion'):
        raise ValueError('Candidate has not passed its coverage and source audit')
    path = candidate / 'rebuilt.sqlite3'
    sha = file_hash(path)
    work.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(work / 'rollback.sqlite3')) as backup, closing(ro(main_path)) as main, closing(ro(path)) as new:
        backup.execute('PRAGMA journal_mode=WAL')
        backup.execute('PRAGMA synchronous=FULL')
        backup.execute('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
        existing = get(backup, 'candidate_sha256')
        if existing and existing != sha:
            raise ValueError('Candidate changed after backup preparation')
        if not existing:
            backup.execute('ATTACH DATABASE ? AS live', (main_path.resolve().as_uri()+'?mode=ro',))
            backup.execute('CREATE TABLE old_event AS SELECT * FROM live.event WHERE 0')
            backup.execute('CREATE UNIQUE INDEX old_event_id ON old_event(id)')
            backup.execute('CREATE INDEX old_event_time ON old_event(timestamp,id)')
            backup.execute('CREATE TABLE old_provenance AS SELECT * FROM live.mit3uri_replay_danmaku_provenance WHERE 0')
            backup.execute('CREATE UNIQUE INDEX old_provenance_id ON old_provenance(event_id)')
            backup.execute('CREATE TABLE windows(batch INTEGER PRIMARY KEY,start INTEGER,end INTEGER,old_count INTEGER,new_count INTEGER,old_digest TEXT,new_digest TEXT)')
            backup.execute('CREATE TABLE event_map(candidate_id INTEGER PRIMARY KEY,event_id INTEGER UNIQUE NOT NULL,batch INTEGER NOT NULL)')
            backup.execute('CREATE INDEX event_map_batch ON event_map(batch,candidate_id)')
            put(backup, 'candidate_sha256', sha)
            put(backup, 'candidate_path', str(path.resolve()))
            put(backup, 'source_max_id', main.execute('SELECT MAX(id) FROM event').fetchone()[0])
            put(backup, 'old_counts', dict(main.execute(f'SELECT cmd,COUNT(*) FROM event INDEXED BY idx_event_room_time WHERE {SCOPE} GROUP BY cmd', (ROOM_ID,0,CUTOFF))))
            put(backup, 'protected_status', main.execute("SELECT id,cmd,timestamp,title FROM event INDEXED BY idx_event_room_time WHERE room_id=? AND timestamp<? AND cmd NOT IN ('DANMU_MSG','SEND_GIFT','GUARD_BUY','SUPER_CHAT_MESSAGE') ORDER BY id", (ROOM_ID,CUTOFF)).fetchall())
            backup.commit()
        start = get(backup, 'backup_until', 0)
        if start == 0:
            oldest = main.execute(f'SELECT MIN(timestamp) FROM event INDEXED BY idx_event_room_time WHERE {SCOPE}', (ROOM_ID,0,CUTOFF)).fetchone()[0]
            start = int(oldest or CUTOFF) // 86400 * 86400
        pfields = [r[1] for r in main.execute('PRAGMA table_info(mit3uri_replay_danmaku_provenance)')]
        while start < CUTOFF:
            end = min(CUTOFF, start + 86400)
            rows = window_rows(main, 'event', start, end)
            prov = main.execute(f"SELECT {','.join('p.'+s for s in pfields)} FROM event e INDEXED BY idx_event_room_time JOIN mit3uri_replay_danmaku_provenance p ON p.event_id=e.id WHERE e.room_id=? AND e.timestamp>=? AND e.timestamp<? AND e.cmd IN ('DANMU_MSG','SEND_GIFT','GUARD_BUY','SUPER_CHAT_MESSAGE')", (ROOM_ID,start,end)).fetchall()
            with backup:
                backup.executemany(f"INSERT INTO old_event VALUES({','.join('?' for _ in FIELDS)})", rows)
                backup.executemany(f"INSERT INTO old_provenance VALUES({','.join('?' for _ in pfields)})", prov)
                put(backup, 'backup_until', end)
            start = end
            if rows:print('backup',start,'rows',len(rows),flush=True)
        actual = dict(backup.execute('SELECT cmd,COUNT(*) FROM old_event GROUP BY cmd'))
        if actual != get(backup,'old_counts'):
            raise ValueError('Historical range changed during backup; no replacement performed')
        if not get(backup,'plan_complete'):
            counts = collections.Counter(dict(backup.execute('SELECT CAST(timestamp AS INTEGER),COUNT(*) FROM old_event GROUP BY 1')))
            counts.update(dict(new.execute('SELECT CAST(timestamp AS INTEGER),COUNT(*) FROM event GROUP BY 1')))
            spans=[];start=0;n=0
            for ts,count in sorted(counts.items()):
                n+=count
                if n>=target:
                    spans.append((start,ts+1));start=ts+1;n=0
            if start<CUTOFF:spans.append((start,CUTOFF))
            with backup:
                backup.execute('DELETE FROM windows')
                for i,(start,end) in enumerate(spans,1):
                    old = window_rows(backup,'old_event',start,end)
                    fresh = window_rows(new,'event',start,end)
                    backup.execute('INSERT INTO windows VALUES(?,?,?,?,?,?,?)',(i,start,end,len(old),len(fresh),digest(old),digest(fresh)))
                put(backup,'plan_complete',True)
        result = dict(run_id=sha,prepared=True,production_modified=False,old_counts=actual,
                      candidate_counts=dict(new.execute('SELECT cmd,COUNT(*) FROM event GROUP BY cmd')),
                      windows=backup.execute('SELECT COUNT(*) FROM windows').fetchone()[0],
                      backup_path=str((work/'rollback.sqlite3').resolve()),cutoff=CUTOFF)
        durable_json(work/'prepared.json',result)
        print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)


def install_scope_guards(conn):
    """Connection-local triggers prohibit mutations outside the exact scope."""
    condition = "{ref}.room_id!={room} OR {ref}.timestamp IS NULL OR {ref}.timestamp<0 OR {ref}.timestamp>={cutoff} OR {ref}.cmd NOT IN {kinds}"
    for action,refs in [('DELETE',['OLD']),('INSERT',['NEW']),('UPDATE',['OLD','NEW'])]:
        where=' OR '.join(condition.format(ref=ref,room=ROOM_ID,cutoff=CUTOFF,kinds=KINDS) for ref in refs)
        conn.execute(f"CREATE TEMP TRIGGER history_guard_{action.lower()} BEFORE {action} ON main.event WHEN {where} BEGIN SELECT RAISE(ABORT,'protected event range'); END")


def apply_window(main, old, fresh, window, run_id, old_provenance=None, new_provenance=None):
    batch,start,end,old_n,new_n,old_hash,new_hash=window
    if not 0<=start<end<=CUTOFF:raise ValueError('Invalid historical window')
    if len(old)!=old_n or len(fresh)!=new_n or digest(old)!=old_hash or digest(fresh)!=new_hash:
        raise ValueError('Backup or candidate window changed')
    current=window_rows(main,'event',start,end)
    if current!=old:raise ValueError(f'Production snapshot differs in window {batch}')
    if old_provenance is not None:
        actual=[]
        for r in old:actual.extend(main.execute('SELECT * FROM mit3uri_replay_danmaku_provenance WHERE event_id=?',(r[0],)).fetchall())
        if actual!=old_provenance:raise ValueError('Replay provenance changed since backup')
    main.executemany('DELETE FROM mit3uri_replay_danmaku_provenance WHERE event_id=?',[(r[0],) for r in old])
    main.executemany('DELETE FROM event WHERE id=?',[(r[0],) for r in old])
    assigned=[]
    for r in fresh:
        cur=main.execute(f"INSERT INTO event({','.join(FIELDS[1:])}) VALUES({','.join('?' for _ in FIELDS[1:])})",r[1:])
        assigned.append(cur.lastrowid)
        if new_provenance and r[0] in new_provenance:
            p=new_provenance[r[0]][:];p[-1]=cur.lastrowid
            main.execute('INSERT INTO mit3uri_replay_danmaku_provenance(source,cid,dm_id,session_id,bvid,progress_ms,session_progress_ms,mid_hash,ctime,mapped_timestamp_ms,alignment_method,match_kind,event_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',p)
    first,last=(assigned[0],assigned[-1]) if assigned else (None,None)
    if assigned and last-first+1!=len(assigned):raise ValueError('Non-contiguous assigned IDs')
    main.execute('INSERT INTO historical_rebuild_batch(run_id,batch,start,end,old_count,new_count,old_digest,new_digest,first_id,last_id) VALUES(?,?,?,?,?,?,?,?,?,?)',(run_id,batch,start,end,old_n,new_n,old_hash,new_hash,first,last))
    return first,last


def prune_search(search,old,run_id,batch):
    key='historical_rebuild:'+run_id
    with search:
        search.execute('BEGIN IMMEDIATE')
        version=search.execute("SELECT value FROM event_search_meta WHERE key='schema_version'").fetchone()
        if not version or int(version[0])!=SEARCH_SCHEMA_VERSION:raise ValueError('Search schema changed')
        done=search.execute('SELECT value FROM event_search_meta WHERE key=?',(key,)).fetchone()
        if done and int(done[0])>=batch:return
        indexed=int(search.execute("SELECT value FROM event_search_meta WHERE key='last_indexed_event_id'").fetchone()[0])
        if any(r[0]>indexed for r in old):raise ValueError('Old search high-water mark is incomplete')
        docs=[(_encode_rowid(r[10],r[0]),_document_tokens(r[1],r[5])) for r in old if r[3] is not None and r[5]]
        search.executemany("INSERT INTO event_fts(event_fts,rowid,tokens) VALUES('delete',?,?)",docs)
        search.execute("UPDATE event_search_meta SET value=CAST(value AS INTEGER)-? WHERE key='indexed_document_count'",(len(docs),))
        search.execute('INSERT OR REPLACE INTO event_search_meta VALUES(?,?)',(key,str(batch)))


def retry(fn,seconds=30):
    end=time.monotonic()+seconds
    while True:
        try:return fn()
        except sqlite3.OperationalError as e:
            if not any(s in str(e) for s in ('locked','interrupted')) or time.monotonic()>=end:raise
            time.sleep(.25)


def apply(candidate,work,main_path,search_path):
    with closing(sqlite3.connect(work/'rollback.sqlite3')) as backup, closing(ro(candidate/'rebuilt.sqlite3')) as new, closing(sqlite3.connect(main_path,timeout=.1)) as main, closing(sqlite3.connect(search_path,timeout=.1)) as search:
        run_id=get(backup,'candidate_sha256')
        if not get(backup,'plan_complete') or file_hash(candidate/'rebuilt.sqlite3')!=run_id:
            raise ValueError('Frozen prepared candidate required')
        backup.execute('PRAGMA synchronous=FULL')
        for c in (main,search):c.execute('PRAGMA busy_timeout=100')
        main.execute('PRAGMA cache_size=-65536')
        for c in (backup,new):c.execute('PRAGMA cache_size=-32768')
        search.execute('PRAGMA cache_size=-16384')
        # Every deleted historical ID must already have passed the normal indexer.
        watermark=int(search.execute("SELECT value FROM event_search_meta WHERE key='last_indexed_event_id'").fetchone()[0])
        if watermark<backup.execute('SELECT MAX(id) FROM old_event').fetchone()[0]:
            raise ValueError('Catch up the normal search index before replacement')
        retry(lambda:main.executescript(JOURNAL))
        install_scope_guards(main)
        completed=get(backup,'completed_batch',0)
        total=backup.execute('SELECT COUNT(*) FROM windows').fetchone()[0]
        last_print=time.monotonic();largest=0
        for window in backup.execute('SELECT * FROM windows WHERE batch>? ORDER BY batch',(completed,)).fetchall():
            batch,start,end,*_=window
            old=window_rows(backup,'old_event',start,end);fresh=window_rows(new,'event',start,end)
            old_provenance=[]
            for r in old:old_provenance.extend(backup.execute('SELECT * FROM old_provenance WHERE event_id=?',(r[0],)).fetchall())
            receipt=main.execute('SELECT first_id,last_id,old_digest,new_digest FROM historical_rebuild_batch WHERE run_id=? AND batch=?',(run_id,batch)).fetchone()
            if receipt:
                first,last,oh,nh=receipt
                if (oh,nh)!=(window[5],window[6]):raise ValueError('Committed receipt mismatch')
                current=window_rows(main,'event',start,end)
                expected=[(first+i,*r[1:]) for i,r in enumerate(fresh)]
                if current!=expected:raise ValueError('Committed window differs from candidate')
            else:
                def write():
                    nonlocal largest
                    began=time.monotonic()
                    main.set_progress_handler(lambda:int(time.monotonic()-began>.4),1000)
                    try:
                        with main:
                            main.execute('BEGIN IMMEDIATE')
                            ids=apply_window(main,old,fresh,window,run_id,old_provenance)
                        largest=max(largest,time.monotonic()-began)
                        return ids
                    finally:main.set_progress_handler(None,0)
                first,last=retry(write)
            retry(lambda:prune_search(search,old,run_id,batch))
            with backup:
                backup.executemany('INSERT OR IGNORE INTO event_map VALUES(?,?,?)',[(r[0],first+i,batch) for i,r in enumerate(fresh)])
                put(backup,'completed_batch',batch)
            if time.monotonic()-last_print>=20 or batch==total:
                report=dict(run_id=run_id,complete=False,completed_windows=batch,total_windows=total,largest_main_transaction_seconds=round(largest,3))
                durable_json(work/'execution.json',report);print(report,flush=True);last_print=time.monotonic()
            time.sleep(.025)
        # Old replay links can point across replacement windows. Populate new
        # links only after all old scoped events and their links are gone.
        position=get(backup,'provenance_until',0)
        while True:
            links=new.execute('SELECT event_id,payload FROM replay_provenance WHERE event_id>? ORDER BY event_id LIMIT 300',(position,)).fetchall()
            if not links:break
            rows=[]
            for cid,payload in links:
                row=json.loads(payload);row[-1]=backup.execute('SELECT event_id FROM event_map WHERE candidate_id=?',(cid,)).fetchone()[0];rows.append(row)
            def link():
                with main:
                    main.execute('BEGIN IMMEDIATE')
                    for row in rows:
                        current=main.execute('SELECT source,cid,dm_id,session_id,bvid,progress_ms,session_progress_ms,mid_hash,ctime,mapped_timestamp_ms,alignment_method,match_kind,event_id FROM mit3uri_replay_danmaku_provenance WHERE event_id=?',(row[-1],)).fetchone()
                        if current is not None:
                            if list(current)!=row:raise ValueError('Unexpected existing replay link')
                            continue
                        main.execute('INSERT INTO mit3uri_replay_danmaku_provenance(source,cid,dm_id,session_id,bvid,progress_ms,session_progress_ms,mid_hash,ctime,mapped_timestamp_ms,alignment_method,match_kind,event_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',row)
            retry(link)
            position=links[-1][0]
            with backup:put(backup,'provenance_until',position)
            time.sleep(.02)
        durable_json(work/'execution.json',dict(run_id=run_id,complete=True,completed_windows=total,total_windows=total,source_lineage_map=str((work/'rollback.sqlite3').resolve()),largest_main_transaction_seconds=round(largest,3)))


def verify(candidate,work,main_path,search_path):
    with closing(ro(work/'rollback.sqlite3')) as backup, closing(ro(candidate/'rebuilt.sqlite3')) as new, closing(ro(main_path)) as main:
        if get(backup,'completed_batch')!=backup.execute('SELECT MAX(batch) FROM windows').fetchone()[0]:raise ValueError('Replacement incomplete')
        checked=0;samples=[]
        total_windows=backup.execute('SELECT COUNT(*) FROM windows').fetchone()[0]
        for batch,start,end,old_n,new_n,oh,nh in backup.execute('SELECT * FROM windows ORDER BY batch'):
            fresh=window_rows(new,'event',start,end)
            mapping=dict(backup.execute('SELECT candidate_id,event_id FROM event_map WHERE batch=?',(batch,)))
            expected=[(mapping[r[0]],*r[1:]) for r in fresh]
            current=window_rows(main,'event',start,end)
            if current!=expected:raise ValueError(f'Full window comparison failed: {batch}')
            checked+=len(current)
            if total_windows<10 or batch%10==0:
                samples.extend(r for r in current[:2] if r[3] is not None and r[5])
            if batch%500==0:print('verified windows',batch,total_windows,'rows',checked,flush=True)
        status=main.execute("SELECT id,cmd,timestamp,title FROM event INDEXED BY idx_event_room_time WHERE room_id=? AND timestamp<? AND cmd NOT IN ('DANMU_MSG','SEND_GIFT','GUARD_BUY','SUPER_CHAT_MESSAGE') ORDER BY id",(ROOM_ID,CUTOFF)).fetchall()
        if [list(r) for r in status]!=get(backup,'protected_status'):raise ValueError('Historical status events changed')
        lineage_checked=0
        for cid,payload in new.execute('SELECT event_id,payload FROM replay_provenance ORDER BY event_id'):
            expected=json.loads(payload)
            expected[-1]=backup.execute('SELECT event_id FROM event_map WHERE candidate_id=?',(cid,)).fetchone()[0]
            actual=main.execute('SELECT source,cid,dm_id,session_id,bvid,progress_ms,session_progress_ms,mid_hash,ctime,mapped_timestamp_ms,alignment_method,match_kind,event_id FROM mit3uri_replay_danmaku_provenance WHERE event_id=?',(expected[-1],)).fetchone()
            if actual is None or list(actual)!=expected:raise ValueError('Rebuilt replay lineage mismatch')
            lineage_checked+=1
            if lineage_checked%50000==0:print('verified replay lineage',lineage_checked,flush=True)
        highest=backup.execute('SELECT MAX(event_id) FROM event_map').fetchone()[0]
        while True:
            progress=index_pending_events(main_path,search_path,batch_size=1000,max_batches=20)
            print('index',progress,flush=True)
            if progress['last_indexed_event_id']>=highest:break
            time.sleep(.05)
        with closing(ro(search_path)) as search:
            for r in samples:
                if not search.execute('SELECT rowid FROM event_fts WHERE rowid=? AND event_fts MATCH ?',(_encode_rowid(r[10],r[0]),_fts_query(r[1],r[5][:2]))).fetchone():
                    raise ValueError(f'New search row missing: {r[0]}')
            for row in backup.execute('SELECT id,room_id,content,timestamp FROM old_event WHERE uid IS NOT NULL AND content IS NOT NULL AND content!="" AND (id%1000=0 OR id IN (SELECT id FROM old_event ORDER BY id LIMIT 20))'):
                eid,room,text,ts=row
                if search.execute('SELECT rowid FROM event_fts WHERE rowid=? AND event_fts MATCH ?',(_encode_rowid(ts,eid),_fts_query(room,text[:2]))).fetchone():
                    raise ValueError(f'Old search row retained: {eid}')
        result=dict(verified=True,run_id=get(backup,'candidate_sha256'),all_rebuilt_rows_compared=checked,
                    counts=dict(new.execute('SELECT cmd,COUNT(*) FROM event GROUP BY cmd')),
                    historical_status_preserved=len(status),protected_mutations_prohibited_by_connection_triggers=True,
                    search_samples_verified=len(samples),replay_lineage_verified=lineage_checked,cutoff=CUTOFF,
                    recovery_database=str((work/'rollback.sqlite3').resolve()))
        durable_json(work/'verification.json',result);print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['prepare','apply','verify'])
    p.add_argument('--candidate',type=Path,required=True)
    p.add_argument('--work',type=Path,required=True)
    p.add_argument('--main',type=Path,default=store.PRODUCTION_DB)
    p.add_argument('--search',type=Path,default=DEFAULT_SEARCH_DB_PATH)
    p.add_argument('--confirm-cutoff',type=int)
    args=p.parse_args()
    if args.action=='prepare':prepare(args.candidate,args.work,args.main)
    elif args.action=='apply':
        if args.confirm_cutoff!=CUTOFF:p.error(f'--confirm-cutoff {CUTOFF} required')
        apply(args.candidate,args.work,args.main,args.search)
    else:verify(args.candidate,args.work,args.main,args.search)


if __name__=='__main__':main()
