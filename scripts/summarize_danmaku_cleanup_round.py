"""Verify a complete cleanup round and export compact per-session receipts."""
import argparse
import collections
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from src.db import danmaku_sources as store
from scripts.audit_mit3uri_danmaku_integrity import save_json,save_csv


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--plan',action='append',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();args.output.mkdir(exist_ok=False)
    drops={};keeps={};counts=collections.Counter();receipts=[]
    for plan in args.plan:
        applied=json.loads((plan/'apply_receipt.json').read_text())
        verified=json.loads((plan/'verification.json').read_text())
        search=json.loads((plan/'search_prune_receipt.json').read_text())
        content=(plan/'proposals.jsonl').read_bytes()
        if not applied['completed'] or not search['completed'] or hashlib.sha256(content).hexdigest()!=verified['sha256'] or verified['sha256']!=applied['proposal_sha256']:raise ValueError('Incomplete receipt')
        for line in content.splitlines():
            r=json.loads(line);eid=r['remove_event']['id'];bid=r['keep_event']['id']
            if eid in drops or bid in drops or eid in keeps:raise ValueError('Overlapping removal')
            drops[eid]=r['remove_event'];keeps[bid]=r['keep_event']
            counts[(r['evidence']['live_date'],r['evidence']['session_id'])]+=1
        receipts.append(dict(plan=str(plan),removed=applied['removed'],proposal_sha256=applied['proposal_sha256']))
    with closing(sqlite3.connect(store.PRODUCTION_DB.as_uri()+'?mode=ro',uri=True)) as c:
        c.row_factory=sqlite3.Row
        ids=sorted(drops.keys()|keeps.keys())
        for start in range(0,len(ids),400):
            batch=ids[start:start+400];marks=','.join('?' for _ in batch)
            rows={r['id']:dict(r) for r in c.execute(f'SELECT * FROM event WHERE id IN ({marks})',batch)}
            for eid in batch:
                if eid in drops and eid in rows:raise ValueError('Deleted row still present')
                if eid in keeps and rows.get(eid)!=keeps[eid]:raise ValueError('Original row changed')
    with closing(sqlite3.connect(store.ARCHIVE_DB.as_uri()+'?mode=ro',uri=True)) as c:
        tombstones,_=store.cleanup_state(c)
    if not drops.keys()<=tombstones:raise ValueError('Missing active cold tombstones')
    save_csv(args.output/'removed_by_session.csv',[dict(date=d,session_id=sid,removed=n) for (d,sid),n in sorted(counts.items())])
    result=dict(removed=len(drops),affected_sessions=len(counts),all_original_rows_unchanged=True,all_removed_absent=True,all_cold_tombstones_active=True,fts_prune_completed=True,protected_since='2026-04-20T17:30:37+08:00',plans=receipts)
    save_json(args.output/'summary.json',result)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
