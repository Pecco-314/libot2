"""Resolve a rebuilt production event to its immutable source evidence."""
import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from src.db import danmaku_sources as store
from scripts.replace_historical_source_database import ro,get
from scripts.danmaku_clock_policy import REGISTRY


def clock_repair_history(event_id):
    result=[]
    if not REGISTRY.exists():return result
    for path in json.loads(REGISTRY.read_text()):
        policy=json.loads(Path(path).read_text());plan=Path(policy['plan'])
        with closing(ro(plan/'rollback.sqlite3')) as backup:
            inserted=backup.execute('SELECT source_record_id FROM inserted_event WHERE event_id=?',(event_id,)).fetchone()
        for line in (plan/'changes.jsonl').read_text().splitlines():
            x=json.loads(line)
            if x.get('before',{}).get('id')==event_id or (inserted and x.get('source_record_id')==inserted[0]):
                result.append(dict(plan=str(plan),change=x))
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('event_id',type=int)
    p.add_argument('--work',type=Path,required=True,help='Replacement directory containing rollback.sqlite3')
    args=p.parse_args()
    repairs=clock_repair_history(args.event_id)
    with closing(ro(args.work/'rollback.sqlite3')) as mapping:
        row=mapping.execute('SELECT candidate_id,batch FROM event_map WHERE event_id=?',(args.event_id,)).fetchone()
        if row is None:
            inserted=next((r['change'] for r in repairs if r['change']['operation']=='insert'),None)
            if not inserted:raise SystemExit('Event ID is not in this reconstruction')
            with closing(ro(store.PRODUCTION_DB)) as main,closing(ro(store.ARCHIVE_DB)) as cold:
                main.row_factory=cold.row_factory=sqlite3.Row
                current=main.execute('SELECT * FROM event WHERE id=?',(args.event_id,)).fetchone()
                raw=cold.execute('SELECT * FROM danmaku_source_record WHERE id=?',(inserted['source_record_id'],)).fetchone()
                print(json.dumps(dict(production_event_id=args.event_id,current_event=dict(current) if current else None,raw_record=dict(raw),clock_repairs=repairs),ensure_ascii=False,indent=2))
            return
        candidate=Path(get(mapping,'candidate_path'))
        merged=[];canonical=args.event_id
        if mapping.execute("SELECT 1 FROM sqlite_master WHERE name='sc_occurrence_merge'").fetchone():
            hit=mapping.execute('SELECT retained_candidate_id,retained_event_id FROM sc_occurrence_merge WHERE removed_event_id=?',(args.event_id,)).fetchone()
            if hit:row=(hit[0],row[1]);canonical=hit[1]
            merged=[r[0] for r in mapping.execute('SELECT removed_candidate_id FROM sc_occurrence_merge WHERE retained_candidate_id=?',(row[0],))]
    with closing(ro(candidate)) as source:
        source.row_factory=sqlite3.Row
        event=dict(source.execute('SELECT * FROM event WHERE id=?',(row[0],)).fetchone())
        origin=dict(source.execute('SELECT * FROM origin WHERE event_id=?',(row[0],)).fetchone())
        financial=[json.loads(r[0]) for cid in [row[0],*merged] for r in source.execute('SELECT payload FROM financial_evidence WHERE event_id=?',(cid,))]
    report=dict(production_event_id=args.event_id,canonical_event_id=canonical,candidate_event_id=row[0],replacement_batch=row[1],event=event,origin=origin,financial_sources=financial,merged_candidate_ids=merged)
    if repairs:
        report['clock_repairs']=repairs
        with closing(ro(store.PRODUCTION_DB)) as main:
            main.row_factory=sqlite3.Row
            current=main.execute('SELECT * FROM event WHERE id=?',(args.event_id,)).fetchone()
            report['current_event']=dict(current) if current else None
    if origin['source_record_id'] is not None:
        with closing(ro(store.ARCHIVE_DB)) as cold:
            cold.row_factory=sqlite3.Row
            raw=cold.execute('SELECT * FROM danmaku_source_record WHERE id=?',(origin['source_record_id'],)).fetchone()
            report['raw_record']=dict(raw)
            report['raw_record']['evidence']=json.loads(raw['evidence'])
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
