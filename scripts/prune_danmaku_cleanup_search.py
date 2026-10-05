"""Prune verified removed events from the derived FTS index, with atomic progress."""
import argparse
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from src.db.event_search import DEFAULT_SEARCH_DB_PATH,SEARCH_SCHEMA_VERSION,_document_tokens,_encode_rowid


def prune_batch(conn, rows, key, position):
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        indexed=int(conn.execute("SELECT value FROM event_search_meta WHERE key='last_indexed_event_id'").fetchone()[0])
        docs=[(_encode_rowid(r['timestamp'],r['id']),_document_tokens(r['room_id'],r['content'])) for r in rows if r['id']<=indexed and r['uid'] is not None and r['content']]
        conn.executemany("INSERT INTO event_fts(event_fts,rowid,tokens) VALUES('delete',?,?)",docs)
        conn.execute("UPDATE event_search_meta SET value=CAST(value AS INTEGER)-? WHERE key='indexed_document_count'",(len(docs),))
        conn.execute('INSERT INTO event_search_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,str(position)))
    return len(docs)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--plan',type=Path,required=True)
    args=p.parse_args()
    receipt=json.loads((args.plan/'apply_receipt.json').read_text());verified=json.loads((args.plan/'verification.json').read_text())
    if not receipt['completed'] or verified['sha256']!=receipt['proposal_sha256']:raise ValueError('Verified cleanup required')
    key='cleanup:'+verified['sha256'];removed=0;batch=[]
    with closing(sqlite3.connect(DEFAULT_SEARCH_DB_PATH,timeout=1)) as conn, (args.plan/'proposals.jsonl').open() as f:
        version=conn.execute("SELECT value FROM event_search_meta WHERE key='schema_version'").fetchone()
        if not version or int(version[0])!=SEARCH_SCHEMA_VERSION:raise ValueError('Unsupported search schema')
        done=conn.execute('SELECT value FROM event_search_meta WHERE key=?',(key,)).fetchone();done=int(done[0]) if done else 0
        for n,line in enumerate(f,1):
            if n<=done:continue
            batch.append(json.loads(line)['remove_event'])
            if len(batch)>=200:
                removed+=prune_batch(conn,batch,key,n);batch.clear();time.sleep(.01)
            if n%50000==0:print('search pruned through',n,flush=True)
        if batch:removed+=prune_batch(conn,batch,key,n)
    result=dict(completed=True,newly_pruned=removed,proposal_rows=receipt['proposed'])
    (args.plan/'search_prune_receipt.json').write_text(json.dumps(result,indent=2)+'\n')
    print(result,flush=True)


if __name__=='__main__':main()
