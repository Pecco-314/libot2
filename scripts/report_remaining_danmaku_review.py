"""Summarize surviving review candidates and export representative evidence.

Original reasons remain labeled as such. Protected live-period records are
separated from historical review, and source times and replay links are kept.
"""
import argparse
import collections
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from src.db import danmaku_sources as store
from scripts.audit_mit3uri_danmaku_integrity import save_json,save_csv,iso


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    root=store.ROOT/'data/reports'
    with closing(sqlite3.connect(store.ARCHIVE_DB.as_uri()+'?mode=ro',uri=True)) as cold:
        removed,claimed=store.cleanup_state(cold)
    rows=[]
    for name in ('verified_pairs','previous_unconfirmed'):
        for line in (root/'danmaku_sessions_review_archived_20261003'/f'{name}.jsonl').open():
            r=json.loads(line)
            if r['extra_event_id'] not in removed:rows.append(r)
    ids=sorted({r['extra_event_id'] for r in rows}|{r.get('baseline_event_id',r.get('previous_baseline_event_id')) for r in rows})
    events={};provenance={}
    with closing(sqlite3.connect(store.PRODUCTION_DB.as_uri()+'?mode=ro',uri=True)) as c:
        c.row_factory=sqlite3.Row
        for start in range(0,len(ids),500):
            batch=ids[start:start+500];marks=','.join('?' for _ in batch)
            events.update((r['id'],dict(r)) for r in c.execute(f'SELECT id,uid,content,timestamp FROM event WHERE id IN ({marks})',batch))
            provenance.update((r['event_id'],dict(r)) for r in c.execute(f'SELECT event_id,bvid,cid,progress_ms,dm_id,match_kind FROM mit3uri_replay_danmaku_provenance WHERE event_id IN ({marks})',batch))
    pages={p['cid']:p['page'] for r in json.loads((store.ROOT/'data/mit3uri_replay_danmaku/metadata.json').read_text())['recordings'] for p in r['pages']}
    groups=collections.defaultdict(list);counts=collections.Counter();multiplicity=collections.Counter();protected=0;missing=0
    for r in rows:
        extra=events.get(r['extra_event_id']);bid=r.get('baseline_event_id',r.get('previous_baseline_event_id'));base=events.get(bid)
        if not extra or not base:missing+=1;continue
        if max(extra['timestamp'],base['timestamp'])*1000>=store.EXTERNAL_CUTOFF_MS:protected+=1;continue
        reason=r.get('reason','previously_verified_pending_source')
        if bid in claimed:reason='baseline_used_by_other_occurrence'
        counts[reason]+=1
        prov=provenance.get(extra['id'],{});cid=prov.get('cid');bvid=prov.get('bvid','');progress=prov.get('progress_ms',0)//1000
        item=dict(date=r['live_date'],session_id=r['session_id'],reason=reason,extra_event_id=extra['id'],baseline_event_id=bid,uid=extra['uid'],content=extra['content'],extra_time=iso(extra['timestamp']),baseline_time=iso(base['timestamp']),bvid=bvid,cid=cid,replay_url=f'https://www.bilibili.com/video/{bvid}?p={pages.get(cid,1)}&t={progress}' if bvid else '')
        other=provenance.get(bid,{})
        # Separate replay observations are multiplicity evidence. Their presence
        # alone cannot establish whether the older event link was correct.
        relation='no_baseline_replay_link'
        gap=None
        if other:
            if other['cid']!=cid:relation='different_replay_parts'
            elif other['dm_id']==prov.get('dm_id'):relation='same_replay_native_id'
            else:
                relation='distinct_replay_observations_same_part'
                gap=round((prov['progress_ms']-other['progress_ms'])/1000,3)
        item.update(baseline_replay_dm_id=other.get('dm_id',''),extra_replay_dm_id=prov.get('dm_id',''),baseline_replay_cid=other.get('cid',''),replay_relation=relation,replay_gap_seconds=gap)
        multiplicity[relation]+=1
        groups[(r['live_date'],r['session_id'],reason,cid)].append(item)
    summary_rows=[];samples=[]
    for key,items in sorted(groups.items(),key=lambda pair:-len(pair[1])):
        date,sid,reason,cid=key
        summary_rows.append(dict(date=date,session_id=sid,reason=reason,cid=cid,rows=len(items),example_url=items[0]['replay_url']))
        positions=sorted({round(i*(len(items)-1)/4) for i in range(5)})
        samples.extend(items[i] for i in positions)
    save_csv(args.output/'groups.csv',summary_rows);save_csv(args.output/'samples.csv',samples)
    save_csv(args.output/'candidates.csv',[item for items in groups.values() for item in items])
    report=dict(event_mutated=False,remaining_candidates=len(rows),protected_since_first_live=protected,missing_event_pairs=missing,historical_candidates=sum(counts.values()),historical_reasons=dict(counts),replay_multiplicity=dict(multiplicity),groups=len(groups),sample_rows=len(samples),monitor_start=iso(store.EXTERNAL_CUTOFF_MS/1000))
    save_json(args.output/'summary.json',report)
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
