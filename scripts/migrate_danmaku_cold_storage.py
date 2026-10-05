"""Copy and verify cold-source tables; never rewrite the live event table.

Source removal is an explicit step with a bounded SQLite statement deadline.
Freed pages remain reusable inside the live database until offline compaction.
"""
import argparse
import hashlib
import json
import pickle
import shutil
import sqlite3
import time
from contextlib import closing

from src.db import danmaku_sources as store

TABLES={
    'danmaku_source_artifact':('id',),
    'danmaku_source_record':('id',),
    'danmaku_source_link':('source_id','event_id'),
    'danmaku_source_checkpoint':('key',),
    'danmaku_merge_review':('run_id','extra_event_id'),
    'danmaku_removed_event':('event_id',),
}
REPORT=store.ROOT/'data/reports/cold_archive_migration.json'


def digest(conn,table,keys):
    sha=hashlib.sha256();count=0
    for row in conn.execute(f'SELECT * FROM {table} ORDER BY '+','.join(keys)):
        encoded=pickle.dumps(tuple(row),protocol=5)
        sha.update(len(encoded).to_bytes(8,'big'));sha.update(encoded);count+=1
    return dict(rows=count,sha256=sha.hexdigest())


def copy():
    store.ARCHIVE_DB.parent.mkdir(parents=True,exist_ok=True)
    report={'source':str(store.PRODUCTION_DB),'destination':str(store.ARCHIVE_DB),'tables':{}}
    with closing(sqlite3.connect(store.PRODUCTION_DB.as_uri()+'?mode=ro',uri=True)) as source, closing(sqlite3.connect(store.ARCHIVE_DB,timeout=10)) as dest:
        dest.execute('PRAGMA journal_mode=WAL')
        dest.execute('PRAGMA synchronous=NORMAL')
        dest.execute('PRAGMA cache_size=-32768')
        with dest:store.initialize(dest)
        for table,keys in TABLES.items():
            columns=[r[1] for r in source.execute(f'PRAGMA table_info({table})')]
            if not columns:raise RuntimeError(f'Missing source table: {table}')
            if columns!=[r[1] for r in dest.execute(f'PRAGMA table_info({table})')]:
                raise RuntimeError(f'Schema mismatch: {table}')
            order=','.join(keys)
            while True:
                last=dest.execute(f'SELECT {order} FROM {table} ORDER BY '+','.join(k+' DESC' for k in keys)+' LIMIT 1').fetchone()
                where=f' WHERE ({order})>({",".join("?" for _ in keys)})' if last else ''
                rows=source.execute(f'SELECT * FROM {table}'+where+f' ORDER BY {order} LIMIT 5000',last or ()).fetchall()
                if not rows:break
                with dest:
                    dest.executemany(f'INSERT INTO {table} VALUES({",".join("?" for _ in columns)})',rows)
            print('copied:',table,flush=True)
            left=digest(source,table,keys);right=digest(dest,table,keys)
            if left!=right:raise RuntimeError(f'Data mismatch: {table}')
            report['tables'][table]=left
            REPORT.write_text(json.dumps(report,indent=2)+'\n')
            print('verified:',table,left,flush=True)
    verify()


def verify():
    report=json.loads(REPORT.read_text())
    if set(report.get('tables',{}))!=set(TABLES):raise RuntimeError('All table digests required')
    with closing(sqlite3.connect(store.ARCHIVE_DB,timeout=10)) as conn:
        conn.execute('PRAGMA cache_size=-131072')
        for table in TABLES:
            print('quick_check:',table,flush=True)
            checks=conn.execute(f'PRAGMA quick_check({table})').fetchall()
            if checks!=[('ok',)]:raise RuntimeError(str(checks))
        print('foreign keys: artifact references',flush=True)
        artifact_ids={r[0] for r in conn.execute('SELECT id FROM danmaku_source_artifact')}
        for (aid,) in conn.execute('SELECT artifact_id FROM danmaku_source_record NOT INDEXED'):
            if aid is not None and aid not in artifact_ids:raise RuntimeError(f'Missing artifact {aid}')
        print('foreign keys: source references',flush=True)
        parent=conn.execute('SELECT id FROM danmaku_source_record NOT INDEXED ORDER BY id')
        current=next(parent,(None,))[0]
        for (sid,) in conn.execute('SELECT DISTINCT source_id FROM danmaku_source_link NOT INDEXED ORDER BY source_id'):
            while current is not None and current<sid:current=next(parent,(None,))[0]
            if current!=sid:raise RuntimeError(f'Missing source {sid}')
        parent.close()
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    report['copy_verified']=True
    REPORT.write_text(json.dumps(report,indent=2)+'\n')
    print('cold copy fully verified',flush=True)


def detach():
    report=json.loads(REPORT.read_text())
    if not report.get('copy_verified'):raise RuntimeError('Verified copy required')
    with closing(sqlite3.connect(store.PRODUCTION_DB,timeout=.5)) as conn, closing(sqlite3.connect(store.ARCHIVE_DB.as_uri()+'?mode=ro',uri=True)) as cold:
        conn.execute('PRAGMA busy_timeout=500')
        conn.execute('PRAGMA secure_delete=OFF')
        snapshot=conn.execute('SELECT MAX(id) FROM event').fetchone()[0]
        event_sql=conn.execute("SELECT sql FROM sqlite_master WHERE name='event'").fetchone()[0]
        sample_ids=sorted({1+round(i*(snapshot-1)/999) for i in range(1000)})
        def event_sample():
            sha=hashlib.sha256();count=0
            for start in range(0,len(sample_ids),200):
                ids=sample_ids[start:start+200]
                for row in conn.execute('SELECT * FROM event WHERE id IN ('+','.join('?' for _ in ids)+') ORDER BY id',ids):
                    sha.update(pickle.dumps(tuple(row),protocol=5));count+=1
            return dict(rows=count,sha256=sha.hexdigest())
        event_before=event_sample()
        # DROP TABLE can only touch this explicit cold-table allowlist.
        def authorize(action,arg1,arg2,db,trigger):
            if action in (sqlite3.SQLITE_INSERT,sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_DELETE) and arg1=='event':
                return sqlite3.SQLITE_DENY
            if action==sqlite3.SQLITE_DROP_TABLE and arg1 not in TABLES:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        conn.set_authorizer(authorize)
        order=['danmaku_source_link','danmaku_merge_review','danmaku_removed_event','danmaku_source_checkpoint','danmaku_source_record','danmaku_source_artifact']
        for table in order:
            if not conn.execute('SELECT 1 FROM sqlite_master WHERE type="table" AND name=?',(table,)).fetchone():continue
            # Resumption is safe because every remaining batch is compared to cold storage.
            actual=conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
            if actual>report['tables'][table]['rows']:raise RuntimeError(f'Source changed after copy: {table}')
            drain_table(conn,cold,table)
            deadline=time.monotonic()+2
            conn.set_progress_handler(lambda:int(time.monotonic()>deadline),1000)
            try:
                with conn:
                    conn.execute('BEGIN IMMEDIATE')
                    conn.execute(f'DROP TABLE {table}')
            finally:
                conn.set_progress_handler(None,0)
            print('detached:',table,flush=True)
            time.sleep(.1)
        if conn.execute("SELECT sql FROM sqlite_master WHERE name='event'").fetchone()[0]!=event_sql:
            raise RuntimeError('event schema changed')
        if event_sample()!=event_before:raise RuntimeError('event samples changed')
        report['event_sample']=event_before
        report['event_high_water_before']=snapshot
        report['event_high_water_after']=conn.execute('SELECT MAX(id) FROM event').fetchone()[0]
        report['main_free_bytes']=conn.execute('PRAGMA freelist_count').fetchone()[0]*conn.execute('PRAGMA page_size').fetchone()[0]
        report['detached']=True
        REPORT.write_text(json.dumps(report,indent=2)+'\n')


def drain_table(conn,cold,table):
    """Release verified archive rows in short transactions with writer breathing room."""
    if table not in TABLES:raise ValueError(table)
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name=?",(table,)).fetchone():
        raise RuntimeError(f'Unexpected archive trigger: {table}')
    keys=TABLES[table];order=','.join(keys)
    columns=[r[1] for r in conn.execute(f'PRAGMA table_info({table})')]
    offsets=[columns.index(k) for k in keys]
    batch=1000;removed=0;last_log=0
    while True:
        if shutil.disk_usage(store.PRODUCTION_DB.parent).free<5*1024**3:
            raise RuntimeError('Less than 5 GiB free; detach paused safely')
        wal=store.PRODUCTION_DB.with_name(store.PRODUCTION_DB.name+'-wal')
        if wal.exists() and wal.stat().st_size>512*1024**2:
            raise RuntimeError('Live WAL exceeds 512 MiB; detach paused safely')
        rows=conn.execute(f'SELECT * FROM {table} ORDER BY {order} LIMIT ?', (batch,)).fetchall()
        if not rows:return
        first=tuple(rows[0][i] for i in offsets);last=tuple(rows[-1][i] for i in offsets)
        key_expr='('+order+')' if len(keys)>1 else order
        param='('+','.join('?' for _ in keys)+')' if len(keys)>1 else '?'
        saved=cold.execute(f'SELECT * FROM {table} WHERE {key_expr}>={param} AND {key_expr}<={param} ORDER BY {order}',first+last).fetchall()
        if rows!=saved:raise RuntimeError(f'Cold batch mismatch: {table} {first}')
        started=time.monotonic()
        conn.set_progress_handler(lambda:int(time.monotonic()-started>.25),1000)
        try:
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                current=conn.execute(f'SELECT * FROM {table} ORDER BY {order} LIMIT ?', (len(rows),)).fetchall()
                if current!=rows:raise RuntimeError(f'Archive changed during detach: {table}')
                conn.executemany(f'DELETE FROM {table} WHERE '+ ' AND '.join(k+'=?' for k in keys),[tuple(row[i] for i in offsets) for row in rows])
        except sqlite3.OperationalError as exc:
            if not any(s in str(exc) for s in ('locked','interrupted')):raise
            batch=max(25,batch//2)
            time.sleep(.5)
            continue
        finally:
            conn.set_progress_handler(None,0)
        elapsed=time.monotonic()-started
        removed+=len(rows)
        if time.monotonic()-last_log>30:
            print('draining:',table,'removed',removed,'batch',len(rows),'seconds',round(elapsed,3),flush=True)
            last_log=time.monotonic()
        if elapsed<.04:batch=min(3000,batch+100)
        elif elapsed>.1:batch=max(25,batch//2)
        time.sleep(max(.03,min(.5,elapsed*2)))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['copy','verify','detach'])
    args=p.parse_args()
    {'copy':copy,'verify':verify,'detach':detach}[args.action]()


if __name__=='__main__':main()
