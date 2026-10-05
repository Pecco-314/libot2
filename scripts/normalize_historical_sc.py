"""Coalesce identical historical SC signals under the approved 60-second rule.

Identity requires the same user, text and price; ordinary danmaku and different
SC text are never coalesced. The immutable candidate, before-images and source
evidence remain available after the canonical production occurrence is chosen.
"""
import argparse
import collections
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from src.db import danmaku_sources as store
from src.db.event_search import DEFAULT_SEARCH_DB_PATH,_document_tokens,_encode_rowid,_fts_query
from scripts.replace_historical_source_database import ro,get,durable_json,FIELDS,COLUMNS,ROOM_ID,CUTOFF,retry,install_scope_guards


def clusters(rows):
    grouped=collections.defaultdict(list)
    for r in rows:
        if r['cmd']!='SUPER_CHAT_MESSAGE' or r['room_id']!=ROOM_ID or not 0<r['timestamp']<CUTOFF:
            raise ValueError('Only historical SC in the target room is allowed')
        grouped[(r['uid'],r['content'],r['total_coin'])].append(r)
    result=[]
    for group in grouped.values():
        current=[]
        for r in sorted(group,key=lambda r:(r['timestamp'],r['id'])):
            if current and r['timestamp']-current[0]['timestamp']>60:
                if len(current)>1:result.append(current)
                current=[]
            current.append(r)
        if len(current)>1:result.append(current)
    return sorted(result,key=lambda group:group[0]['timestamp'])


def prepare(work):
    with closing(ro(work/'rollback.sqlite3')) as mapping:
        path=Path(get(mapping,'candidate_path'))
        mapping.execute('ATTACH DATABASE ? AS candidate',(path.resolve().as_uri()+'?mode=ro',))
        event_map=dict(mapping.execute("SELECT m.candidate_id,m.event_id FROM event_map m JOIN candidate.event e ON e.id=m.candidate_id WHERE e.cmd='SUPER_CHAT_MESSAGE'"))
    with closing(ro(path)) as candidate:
        candidate.row_factory=sqlite3.Row
        rows=[dict(r) for r in candidate.execute("SELECT e.*,o.source FROM event e JOIN origin o ON o.event_id=e.id WHERE cmd='SUPER_CHAT_MESSAGE'")]
        changes=[]
        for group in clusters(rows):
            keep=min(group,key=lambda r:({'qqzy':0,'danmakus':1,'vtbcat':2}[r['source']],r['timestamp'],r['id']))
            for remove in group:
                if remove['id']==keep['id']:continue
                if any(r['id'] not in event_map for r in (keep,remove)):
                    raise ValueError('SC windows must finish before preparing normalization')
                before={k:remove[k] for k in FIELDS};before['id']=event_map[remove['id']]
                retained={k:keep[k] for k in FIELDS};retained['id']=event_map[keep['id']]
                evidence={str(r['id']):[json.loads(x[0]) for x in candidate.execute('SELECT payload FROM financial_evidence WHERE event_id=?',(r['id'],))] for r in (keep,remove)}
                changes.append(dict(before=before,retained=retained,removed_candidate_id=remove['id'],retained_candidate_id=keep['id'],delta_seconds=abs(keep['timestamp']-remove['timestamp']),source_evidence=evidence))
        plan=dict(rule='same_uid_text_price_within_60_seconds',changes=changes,removed_count=len(changes),financial_value_removed_yuan=sum(r['before']['total_coin'] for r in changes))
        plan['sha256']=hashlib.sha256(json.dumps(changes,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        durable_json(work/'sc_normalization_plan.json',plan)
        print(json.dumps({k:v for k,v in plan.items() if k!='changes'},ensure_ascii=False,indent=2))


def current(conn,eid):
    row=conn.execute(f'SELECT {COLUMNS} FROM event WHERE id=?',(eid,)).fetchone()
    return dict(zip(FIELDS,row)) if row else None


def apply(work,main_path,search_path):
    verified=json.loads((work/'verification.json').read_text())
    if not verified.get('verified'):raise ValueError('Verified full reconstruction is required')
    plan=json.loads((work/'sc_normalization_plan.json').read_text());changes=plan['changes'];key='sc_normalization:'+plan['sha256']
    if hashlib.sha256(json.dumps(changes,ensure_ascii=False,sort_keys=True).encode()).hexdigest()!=plan['sha256']:raise ValueError('Plan hash mismatch')
    with closing(sqlite3.connect(main_path,timeout=.1)) as main,closing(sqlite3.connect(search_path,timeout=.1)) as search,closing(sqlite3.connect(work/'rollback.sqlite3')) as mapping:
        install_scope_guards(main)
        mapping.execute('CREATE TABLE IF NOT EXISTS sc_occurrence_merge(removed_candidate_id INTEGER PRIMARY KEY,retained_candidate_id INTEGER NOT NULL,removed_event_id INTEGER NOT NULL,retained_event_id INTEGER NOT NULL,rule TEXT NOT NULL)')
        mapping.commit()
        for i,x in enumerate(changes,1):
            before=x['before'];keep=x['retained']
            if len(clusters([before,keep]))!=1:raise ValueError('Invalid SC coalescing identity')
            def remove():
                with main:
                    main.execute('BEGIN IMMEDIATE')
                    if current(main,keep['id'])!=keep:raise ValueError('Retained SC changed')
                    actual=current(main,before['id'])
                    if actual is not None:
                        if actual!=before:raise ValueError('SC before-image changed')
                        main.execute('DELETE FROM event WHERE id=?',(before['id'],))
            retry(remove)
            def index():
                with search:
                    search.execute('BEGIN IMMEDIATE')
                    done=search.execute('SELECT value FROM event_search_meta WHERE key=?',(key,)).fetchone()
                    if done and int(done[0])>=i:return
                    high=int(search.execute("SELECT value FROM event_search_meta WHERE key='last_indexed_event_id'").fetchone()[0])
                    if before['id']>high:raise ValueError('SC is not fully indexed yet')
                    search.execute("INSERT INTO event_fts(event_fts,rowid,tokens) VALUES('delete',?,?)",(_encode_rowid(before['timestamp'],before['id']),_document_tokens(before['room_id'],before['content'])))
                    search.execute("UPDATE event_search_meta SET value=CAST(value AS INTEGER)-1 WHERE key='indexed_document_count'")
                    search.execute('INSERT OR REPLACE INTO event_search_meta VALUES(?,?)',(key,str(i)))
            retry(index)
            with mapping:mapping.execute('INSERT OR IGNORE INTO sc_occurrence_merge VALUES(?,?,?,?,?)',(x['removed_candidate_id'],x['retained_candidate_id'],before['id'],keep['id'],plan['rule']))
        for x in changes:
            b=x['before'];k=x['retained']
            if current(main,b['id']) is not None or current(main,k['id'])!=k:raise ValueError('SC final-state mismatch')
            for row,present in ((b,False),(k,True)):
                hit=search.execute('SELECT rowid FROM event_fts WHERE rowid=? AND event_fts MATCH ?',(_encode_rowid(row['timestamp'],row['id']),_fts_query(row['room_id'],row['content'][:2]))).fetchone()
                if bool(hit)!=present:raise ValueError('SC search verification failed')
        rows=[dict(zip(FIELDS,r)) for r in main.execute(f"SELECT {COLUMNS} FROM event INDEXED BY idx_event_cmd_time WHERE cmd='SUPER_CHAT_MESSAGE' AND room_id=? AND timestamp<?",(ROOM_ID,CUTOFF))]
        if clusters(rows):raise ValueError('Identical short-interval SC remains')
        result=dict(verified=True,sha256=plan['sha256'],removed_count=len(changes),financial_value_removed_yuan=plan['financial_value_removed_yuan'],remaining_identical_sc_clusters=0,final_historical_sc_count=len(rows),source_evidence_preserved=True)
        durable_json(work/'sc_normalization_verification.json',result)
        print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['prepare','apply']);p.add_argument('--work',type=Path,required=True)
    p.add_argument('--main',type=Path,default=store.PRODUCTION_DB);p.add_argument('--search',type=Path,default=DEFAULT_SEARCH_DB_PATH)
    args=p.parse_args()
    if args.action=='prepare':prepare(args.work)
    else:apply(args.work,args.main,args.search)


if __name__=='__main__':main()
