"""Summarize source-first dry-run changes and independent finance options.

Potential time relocations are descriptive only; they do not establish event
identity. No finance transactions or production events are changed.
"""
import argparse
import collections
import csv
import gzip
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from src.db import danmaku_sources as store
from scripts.backfill_mit3uri_replay_danmaku import normalize_content
from scripts.audit_mit3uri_danmaku_integrity import save_json,save_csv


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--plan',type=Path,required=True)
    args=p.parse_args()
    summary=json.loads((args.plan/'summary.json').read_text())
    sessions=list(csv.DictReader((args.plan/'sessions.csv').open(encoding='utf-8-sig')))
    segments=list(csv.DictReader((args.plan/'segments.csv').open(encoding='utf-8-sig')))
    bins=collections.defaultdict(lambda:collections.defaultdict(list));removed_ids=set();ops=collections.Counter()
    with gzip.open(args.plan/'draft_changes.jsonl.gz','rt') as f:
        for line in f:
            r=json.loads(line);operation=r['operation'];ops[operation]+=1
            if operation=='candidate_add_from_selected_source':
                uid,content,ts=r['uid'],r['content'],r['timestamp'];kind='add'
                if not 0<ts<store.EXTERNAL_CUTOFF_MS/1000:raise ValueError('Out-of-scope insertion draft')
            else:
                event=r['event_snapshot'];uid,content,ts=event['uid'],event['content'],event['timestamp'];kind='remove'
                if event['cmd']!='DANMU_MSG' or event['timestamp']>=store.EXTERNAL_CUTOFF_MS/1000:raise ValueError('Out-of-scope deletion draft')
                if event['id'] in removed_ids:raise ValueError('Overlapping session deletion draft')
                removed_ids.add(event['id'])
            bins[(r['session_id'],r['segment'],uid,normalize_content(content))][kind].append(ts)
    retimes=collections.Counter()
    for (sid,_,_,_),v in bins.items():
        if len(v['add'])==len(v['remove'])==1 and 0<abs(v['add'][0]-v['remove'][0])<=60:retimes[sid]+=1
    classification=collections.Counter();by_session=collections.defaultdict(list)
    for r in segments:by_session[r['session_id']].append(r)
    for s in sessions:
        if not s['primary']:label='no_native_source_keep_current'
        elif int(s.get('held_current') or 0)==0:label='whole_source_preview'
        elif int(s.get('add') or 0)+int(s.get('remove') or 0)>0:label='partial_intervals_preview'
        else:label='hold_or_already_matches'
        s['plan_class']=label;s['possible_single_pair_time_relocations']=retimes[s['session_id']];classification[label]+=1
    save_csv(args.plan/'session_decisions.csv',sessions)
    inventory=json.loads((store.ROOT/'data/reports/financial_source_inventory_firstlive_20261004.json').read_text())
    native={source:{r['source_session']:r['counts'] for r in inventory[source]['streams']} for source in ('danmakus','vtbcat')}
    catalog=json.loads((store.ROOT/'data/mit3uri_replay_catalog.json').read_text())
    with closing(sqlite3.connect(store.PRODUCTION_DB.as_uri()+'?mode=ro',uri=True)) as live:
        mapping=dict(live.execute('SELECT bvid,session_id FROM mit3uri_replay_recording'))
    upstream=collections.defaultdict(set)
    for s in catalog['sessions']:
        target={mapping[r['bvid']] for r in s['recordings'] if r['bvid'] in mapping}
        if len(target)!=1:continue
        sid=next(iter(target))
        for h in s.get('historical_sources',[]):
            if h['source'] in native:upstream[sid].add((h['source'],str(h['native_id'])))
    financial=[]
    for r in csv.DictReader((args.plan/'financial_plan.csv').open(encoding='utf-8-sig')):
        for kind in ('gift','guard','sc'):
            candidates={source+':'+key:native[source][key].get(kind,0) for source,key in upstream[r['session_id']] if key in native[source]}
            financial.append(dict(session_id=r['session_id'],date=r['date'],kind=kind,current=int(r[kind]),source_stream_raw_counts=json.dumps(candidates,ensure_ascii=False),proposed_action='keep_current_until_transaction_identity_audit',note='Raw stream counts are not deduplicated; do not sum overlapping sources or infer missing transactions from counts alone'))
    save_csv(args.plan/'financial_source_options.csv',financial)
    summary.update(session_classes=dict(classification),possible_single_pair_time_relocations=sum(retimes.values()),draft_operation_counts=dict(ops),draft_removed_ids_unique=True,finance_raw_counts={s:inventory[s]['counts'] for s in ('danmakus','vtbcat')},finance_current=inventory['main_before_cutoff'])
    save_json(args.plan/'overview.json',summary)
    print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
