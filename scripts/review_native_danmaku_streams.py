"""Compare two native live-source streams with independent-user clock anchors.

Only pairs linked unambiguously to two different surviving event rows are
exported. This diagnostic does not mutate event or infer links from text alone.
"""
import argparse
import collections
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from src.db import danmaku_sources as store
from scripts.review_mit3uri_danmaku_sessions import review_session
from scripts.audit_mit3uri_danmaku_integrity import save_json


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--primary',required=True);p.add_argument('--secondary',required=True);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    streams=[];links=collections.defaultdict(set)
    with closing(sqlite3.connect(store.ARCHIVE_DB.as_uri()+'?mode=ro',uri=True)) as c:
        for spec in (args.primary,args.secondary):
            source,sid=spec.split(':',1)
            rows=c.execute("SELECT id,uid,content,source_timestamp_ms FROM danmaku_source_record WHERE source=? AND source_session=? AND eligibility='live' AND source_timestamp_ms<?",(source,sid,store.EXTERNAL_CUTOFF_MS)).fetchall()
            streams.append(rows)
            for rid,eid in c.execute('SELECT r.id,l.event_id FROM danmaku_source_record r JOIN danmaku_source_link l ON l.source_id=r.id WHERE r.source=? AND r.source_session=?',(source,sid)):links[rid].add(eid)
    alive={};ids=sorted({eid for v in links.values() for eid in v})
    with closing(sqlite3.connect(store.PRODUCTION_DB.as_uri()+'?mode=ro',uri=True)) as c:
        c.row_factory=sqlite3.Row
        for start in range(0,len(ids),500):
            batch=ids[start:start+500];marks=','.join('?' for _ in batch)
            alive.update((r['id'],dict(r)) for r in c.execute(f'SELECT * FROM event WHERE id IN ({marks})',batch))
    links={rid:events & alive.keys() for rid,events in links.items()}
    primary,secondary=streams
    primary_ids={r[0] for r in primary}
    primary_events={eid for rid,events in links.items() if rid in primary_ids for eid in events}
    shared={eid for r in secondary for eid in links.get(r[0],set()) if eid in primary_events}
    baseline=[dict(id=rid,uid=uid,content=text,timestamp=ts/1000,match_kind='existing' if links.get(rid,set()) & shared else None) for rid,uid,text,ts in primary]
    base=min(r[3] for r in secondary)
    replay=[dict(event_id=rid,uid=uid,content=text,cid=args.secondary,bvid='',progress_ms=ts-base,mapped_timestamp_ms=ts,match_kind='inserted') for rid,uid,text,ts in secondary]
    accepted,reasons,details,parts,models=review_session(replay,baseline,set(),max_residual=4.5)
    status=collections.Counter();pairs=[]
    for rid,bid in accepted.items():
        left=links.get(rid,set());right=links.get(bid,set())
        if len(left)!=1 or len(right)!=1:status['missing_or_ambiguous_event_link']+=1;continue
        drop=next(iter(left));keep=next(iter(right))
        if drop==keep:status['already_merged']+=1;continue
        a,b=alive[drop],alive[keep]
        if a['uid']!=b['uid'] or a['cmd']!='DANMU_MSG' or b['cmd']!='DANMU_MSG':raise ValueError('Invalid source link')
        pairs.append(dict(primary_source_id=bid,secondary_source_id=rid,keep_event=alive[keep],remove_event=alive[drop],offset=details[rid][0]))
    with (args.output/'pairs.jsonl').open('w') as f:
        for row in pairs:f.write(json.dumps(row,ensure_ascii=False)+'\n')
    report=dict(event_mutated=False,primary=args.primary,secondary=args.secondary,primary_rows=len(primary),secondary_rows=len(secondary),native_pairs=len(accepted),distinct_surviving_event_pairs=len(pairs),status=dict(status),reasons=dict(collections.Counter(reasons.values())),parts=parts,models=models)
    save_json(args.output/'summary.json',report)
    print(json.dumps({k:v for k,v in report.items() if k not in ('models','parts')},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
