"""Apply snapshot-checked historical retimes/deletions in small transactions.

Durable pending receipts precede commits. Search repair uses an independent
transactional checkpoint, allowing recovery after either database commits.
No service control, PID operations, financial edits, or broad database locks.
"""
import argparse
import hashlib
import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from src.db import danmaku_sources as store
from src.db.event_search import DEFAULT_SEARCH_DB_PATH, SEARCH_SCHEMA_VERSION, _document_tokens, _encode_rowid
from scripts.backfill_mit3uri_replay_danmaku import ROOM_ID, normalize_content


def durable_json(path, value):
    temp=path.with_suffix('.tmp')
    with temp.open('w') as f:
        json.dump(value,f,ensure_ascii=False,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
    temp.replace(path)
    fd=os.open(path.parent,os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


def validate(x):
    if x['operation'] not in ('delete','retime'):raise ValueError('Only retime/delete supported')
    b=x['before']
    if b['cmd']!='DANMU_MSG' or b['room_id']!=ROOM_ID or not 0<b['timestamp']<store.EXTERNAL_CUTOFF_MS//1000:raise ValueError('Protected event')
    if not x['source'].startswith(('danmakus:','vtbcat:')):raise ValueError('Native primary required')
    if not x['start']+5<=b['timestamp']<x['end']-5:raise ValueError('Unsafe boundary')
    if x['operation']=='retime':
        a=x['after']
        source=x.get('source_record')
        if source and (source['uid']!=b['uid'] or normalize_content(source['content'])!=normalize_content(b['content']) or int(source['time'])!=a['timestamp']):raise ValueError('Source occurrence mismatch')
        if {k:v for k,v in a.items() if k!='timestamp'}!={k:v for k,v in b.items() if k!='timestamp'}:raise ValueError('Retime changes payload')
        if not x['start']+5<=a['timestamp']<x['end']-5 or a['timestamp']>=store.EXTERNAL_CUTOFF_MS//1000 or abs(a['timestamp']-b['timestamp'])>60:raise ValueError('Unsafe retime')


def state(conn, x):
    r=conn.execute('SELECT * FROM event WHERE id=?',(x['before']['id'],)).fetchone()
    return dict(r) if r else None


def apply_main(conn, items):
    for x in items:
        validate(x)
        eid=x['before']['id']
        if state(conn,x)!=x['before']:raise RuntimeError(f'Changed event {eid}')
        prov=[dict(r) for r in conn.execute('SELECT * FROM mit3uri_replay_danmaku_provenance WHERE event_id=?',(eid,))]
        if prov!=x['provenance']:raise RuntimeError(f'Changed provenance {eid}')
    for x in items:
        eid=x['before']['id']
        if x['operation']=='delete':
            conn.execute('DELETE FROM mit3uri_replay_danmaku_provenance WHERE event_id=?',(eid,))
            conn.execute('DELETE FROM event WHERE id=?',(eid,))
        else:
            conn.execute('UPDATE event SET timestamp=? WHERE id=?',(x['after']['timestamp'],eid))
            conn.execute("UPDATE mit3uri_replay_danmaku_provenance SET mapped_timestamp_ms=?,alignment_method=? WHERE event_id=?",(x['after']['timestamp']*1000,'source_first:'+x['source'],eid))


def repair_search(conn, items, key, position):
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        v=conn.execute("SELECT value FROM event_search_meta WHERE key='schema_version'").fetchone()
        if not v or int(v[0])!=SEARCH_SCHEMA_VERSION:raise ValueError('Search schema mismatch')
        done=conn.execute('SELECT value FROM event_search_meta WHERE key=?',(key,)).fetchone()
        if done and int(done[0])>=position:return
        indexed=int(conn.execute("SELECT value FROM event_search_meta WHERE key='last_indexed_event_id'").fetchone()[0])
        removed=0
        for x in items:
            b=x['before']
            if b['id']>indexed or b['uid'] is None or not b['content']:continue
            tokens=_document_tokens(b['room_id'],b['content'])
            conn.execute("INSERT INTO event_fts(event_fts,rowid,tokens) VALUES('delete',?,?)",(_encode_rowid(b['timestamp'],b['id']),tokens))
            if x['operation']=='retime':
                a=x['after'];conn.execute('INSERT INTO event_fts(rowid,tokens) VALUES(?,?)',(_encode_rowid(a['timestamp'],a['id']),tokens))
            else:removed+=1
        conn.execute("UPDATE event_search_meta SET value=CAST(value AS INTEGER)-? WHERE key='indexed_document_count'",(removed,))
        conn.execute('INSERT INTO event_search_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,str(position)))


def archive_batch(conn, items, sha, position):
    with conn:
        conn.execute('CREATE TABLE IF NOT EXISTS source_first_event_change (proposal_sha256 TEXT NOT NULL,event_id INTEGER NOT NULL,operation TEXT NOT NULL,snapshot TEXT NOT NULL,PRIMARY KEY(proposal_sha256,event_id)) WITHOUT ROWID')
        for x in items:
            eid=x['before']['id']
            conn.execute('INSERT OR IGNORE INTO source_first_event_change VALUES(?,?,?,?)',(sha,eid,x['operation'],json.dumps(x,ensure_ascii=False)))
            if x['operation']=='delete':
                conn.execute('INSERT OR IGNORE INTO danmaku_removed_event(event_id,reason,event_json,provenance_json) VALUES(?,?,?,?)',(eid,'source_first_selected_stream:'+sha,json.dumps(x['before'],ensure_ascii=False),json.dumps(x['provenance'],ensure_ascii=False)))
            else:
                sid=x['source_record']['id']
                conn.execute('INSERT OR IGNORE INTO danmaku_source_link(source_id,event_id,method) VALUES(?,?,?)',(sid,eid,'source_first_validated_retime'))


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--plan',type=Path,required=True);p.add_argument('--apply',action='store_true');p.add_argument('--batch-size',type=int,default=40)
    args=p.parse_args()
    if not args.apply or not 1<=args.batch_size<=100:p.error('--apply and batch size 1..100 required')
    path=args.plan/'changes.jsonl';sha=hashlib.sha256(path.read_bytes()).hexdigest()
    summary=json.loads((args.plan/'summary.json').read_text())
    if sha!=summary['proposal_sha256']:raise ValueError('Proposal hash changed')
    items=[json.loads(l) for l in path.open()];ids=set()
    for x in items:
        validate(x)
        if x['before']['id'] in ids:raise ValueError('Repeated event')
        ids.add(x['before']['id'])
    receipt_path=args.plan/'execution_receipt.json'
    receipt=json.loads(receipt_path.read_text()) if receipt_path.exists() else dict(proposal_sha256=sha,done=0,complete=False)
    if receipt['proposal_sha256']!=sha:raise ValueError('Receipt mismatch')
    pending_path=args.plan/'pending_batch.json';key='source_first:'+sha
    with closing(sqlite3.connect(store.PRODUCTION_DB,timeout=.1)) as main, closing(sqlite3.connect(DEFAULT_SEARCH_DB_PATH,timeout=.1)) as search, closing(sqlite3.connect(store.ARCHIVE_DB,timeout=.1)) as cold:
        main.row_factory=sqlite3.Row
        for c in (main,search,cold):c.execute('PRAGMA busy_timeout=100')
        def finalize(batch,pos):
            for attempt in range(30):
                try:repair_search(search,batch,key,pos);archive_batch(cold,batch,sha,pos);break
                except sqlite3.OperationalError as e:
                    if 'locked' not in str(e):raise
                    time.sleep(.5)
            else:raise RuntimeError('Index/archive busy; pending receipt retained')
            receipt['done']=pos;durable_json(receipt_path,receipt)
        if pending_path.exists():
            pending=json.loads(pending_path.read_text());lo,hi=pending['lo'],pending['hi']
            if pending['proposal_sha256']!=sha:raise ValueError('Pending mismatch')
            if hi>receipt['done']:
                batch=items[lo:hi]
                before=[state(main,x)==x['before'] for x in batch]
                after=[state(main,x)==x.get('after') for x in batch]
                if all(after):finalize(batch,hi)
                elif not all(before):raise RuntimeError('Mixed pending state; manual recovery required')
        last=time.monotonic()
        for lo in range(receipt['done'],len(items),args.batch_size):
            hi=min(len(items),lo+args.batch_size);batch=items[lo:hi]
            durable_json(pending_path,dict(proposal_sha256=sha,lo=lo,hi=hi))
            for attempt in range(30):
                start=time.monotonic()
                main.set_progress_handler(lambda:int(time.monotonic()-start>.25),1000)
                try:
                    with main:
                        main.execute('BEGIN IMMEDIATE');apply_main(main,batch)
                    break
                except sqlite3.OperationalError as e:
                    if not any(k in str(e) for k in ('locked','interrupted')):raise
                    time.sleep(.5)
                finally:main.set_progress_handler(None,0)
            else:raise RuntimeError('Live DB busy; safely paused')
            finalize(batch,hi)
            if time.monotonic()-last>20:print(receipt,flush=True);last=time.monotonic()
            time.sleep(.05)
        for x in items:
            if state(main,x)!=x.get('after'):raise RuntimeError('Post-state verification failed')
        finance={cmd:main.execute('SELECT COUNT(*),SUM(COALESCE(total_coin,0)),SUM(id) FROM event INDEXED BY idx_event_cmd_time WHERE cmd=? AND room_id=? AND timestamp<?',(cmd,ROOM_ID,store.EXTERNAL_CUTOFF_MS//1000)).fetchone()[:] for cmd in summary['financial_baseline']}
        if {k:list(v) for k,v in finance.items()}!=summary['financial_baseline']:raise RuntimeError('Financial baseline changed')
        receipt.update(complete=True,financial_unchanged=True,counts=summary['counts']);durable_json(receipt_path,receipt)
        print(receipt,flush=True)


if __name__=='__main__':main()
