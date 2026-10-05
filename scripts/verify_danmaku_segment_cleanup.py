"""Verify applied deletions and retain recoverable tombstones in the cold archive."""
import argparse
import hashlib
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from src.db import danmaku_sources as store


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--plan',type=Path,required=True)
    args=p.parse_args();receipt=json.loads((args.plan/'apply_receipt.json').read_text())
    if not receipt['completed']:raise ValueError('Apply is incomplete')
    path=args.plan/'proposals.jsonl';sha=hashlib.sha256();count=0
    with closing(sqlite3.connect(store.PRODUCTION_DB.as_uri()+'?mode=ro',uri=True)) as live, closing(store.connect_archive()) as cold, path.open('rb') as f:
        live.row_factory=sqlite3.Row
        restored={r[0] for r in cold.execute('SELECT event_id FROM danmaku_cleanup_restored_event')} if cold.execute("SELECT 1 FROM sqlite_master WHERE name='danmaku_cleanup_restored_event'").fetchone() else set()
        with cold:cold.execute('CREATE TABLE IF NOT EXISTS danmaku_cleanup_redelete_event(event_id INTEGER NOT NULL,proposal_sha256 TEXT NOT NULL,keep_event_id INTEGER NOT NULL,applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(event_id,proposal_sha256))')
        batch=[]
        def flush():
            ids=[row['keep_event']['id'] for row in batch]+[row['remove_event']['id'] for row in batch]
            marks=','.join('?' for _ in ids)
            current={r['id']:dict(r) for r in live.execute(f'SELECT * FROM event WHERE id IN ({marks})',ids)}
            for item in batch:
                if item['remove_event']['id'] in current:raise ValueError('Deleted event still exists')
                if current.get(item['keep_event']['id'])!=item['keep_event']:raise ValueError('Keep snapshot changed')
            dropped=[r['remove_event']['id'] for r in batch]
            if live.execute('SELECT 1 FROM mit3uri_replay_danmaku_provenance WHERE event_id IN ('+','.join('?' for _ in dropped)+') LIMIT 1',dropped).fetchone():
                raise ValueError('Removed provenance remains')
            with cold:
                cold.executemany('INSERT OR IGNORE INTO danmaku_removed_event(event_id,reason,event_json,provenance_json) VALUES(?,?,?,?)',
                    [(r['remove_event']['id'],'verified_duplicate:keep='+str(r['keep_event']['id']),store.encode(r['remove_event']),store.encode(r['remove_provenance'])) for r in batch])
                cold.executemany('INSERT OR IGNORE INTO danmaku_cleanup_redelete_event(event_id,proposal_sha256,keep_event_id) VALUES(?,?,?)',[(r['remove_event']['id'],receipt['proposal_sha256'],r['keep_event']['id']) for r in batch if r['remove_event']['id'] in restored])
            batch.clear();time.sleep(.01)
        for line in f:
            sha.update(line);batch.append(json.loads(line));count+=1
            if len(batch)>=250:flush()
            if count%50000==0:print('verified',count,flush=True)
        if batch:flush()
    if sha.hexdigest()!=receipt['proposal_sha256'] or count!=receipt['proposed']:raise ValueError('Proposal changed')
    result=dict(verified=count,all_removed_absent=True,all_kept_unchanged=True,provenance_removed=True,cold_tombstones_saved=True,sha256=sha.hexdigest())
    (args.plan/'verification.json').write_text(json.dumps(result,indent=2)+'\n')
    print(result,flush=True)


if __name__=='__main__':main()
