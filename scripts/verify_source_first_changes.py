"""Verify applied event snapshots, provenance, cold receipts, and sampled FTS rows."""
import argparse
import collections
import hashlib
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from src.db import danmaku_sources as store
from src.db.event_search import DEFAULT_SEARCH_DB_PATH, _encode_rowid, _fts_query
from scripts.apply_source_first_changes import durable_json, validate


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--plan',type=Path,required=True);args=p.parse_args()
    path=args.plan/'changes.jsonl';sha=hashlib.sha256(path.read_bytes()).hexdigest()
    receipt=json.loads((args.plan/'execution_receipt.json').read_text())
    if not receipt['complete'] or receipt['proposal_sha256']!=sha:raise ValueError('Completed execution required')
    items=[json.loads(l) for l in path.open()];counts=collections.Counter();samples=collections.defaultdict(list)
    with closing(sqlite3.connect(store.PRODUCTION_DB.as_uri()+'?mode=ro',uri=True)) as c:
        c.row_factory=sqlite3.Row
        for x in items:
            validate(x);b=x['before'];row=c.execute('SELECT * FROM event WHERE id=?',(b['id'],)).fetchone()
            if (dict(row) if row else None)!=x.get('after'):raise ValueError(f'Wrong event {b["id"]}')
            prov=[dict(r) for r in c.execute('SELECT * FROM mit3uri_replay_danmaku_provenance WHERE event_id=?',(b['id'],))]
            expected=[] if x['operation']=='delete' else [{**r,'mapped_timestamp_ms':x['after']['timestamp']*1000,'alignment_method':'source_first:'+x['source']} for r in x['provenance']]
            if prov!=expected:raise ValueError(f'Wrong provenance {b["id"]}')
            counts[x['operation']]+=1;samples[x['operation']].append(x)
    checked=0
    with closing(sqlite3.connect(DEFAULT_SEARCH_DB_PATH.as_uri()+'?mode=ro',uri=True)) as c:
        pos=c.execute('SELECT value FROM event_search_meta WHERE key=?',('source_first:'+sha,)).fetchone()
        if not pos or int(pos[0])!=len(items):raise ValueError('Search checkpoint incomplete')
        for rows in samples.values():
            for x in rows[::max(1,len(rows)//300)]:
                b=x['before'];word=re.search(r'[\u4e00-\u9fff]{2}',b['content'])
                if not word:continue
                query=_fts_query(b['room_id'],word[0])
                def exists(ts):return c.execute('SELECT rowid FROM event_fts WHERE event_fts MATCH ? AND rowid=?',(query,_encode_rowid(ts,b['id']))).fetchone() is not None
                if exists(b['timestamp']):raise ValueError(f'Old search row retained {b["id"]}')
                if x['operation']=='retime' and not exists(x['after']['timestamp']):raise ValueError(f'New search row missing {b["id"]}')
                checked+=1
    with closing(sqlite3.connect(store.ARCHIVE_DB.as_uri()+'?mode=ro',uri=True)) as c:
        archived=dict(c.execute('SELECT operation,COUNT(*) FROM source_first_event_change WHERE proposal_sha256=? GROUP BY operation',(sha,)))
        if archived!=dict(counts):raise ValueError('Cold ledger incomplete')
    result=dict(verified=True,proposal_sha256=sha,counts=dict(counts),all_event_poststates_verified=True,all_provenance_poststates_verified=True,cold_ledger_verified=True,search_samples_verified=checked,protected_period_operations=0)
    durable_json(args.plan/'verification.json',result);print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
