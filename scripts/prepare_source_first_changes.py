"""Prepare bounded source-selected changes with occurrence-preserving retimes.

Only accepted primary intervals from the read-only preview are eligible.
Repeated identities, uncalibrated retimes, and switch-edge candidates are held.
Financial events and the protected monitoring period are never selected.
"""
import argparse
import collections
import csv
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from src.db import danmaku_sources as store
from scripts.backfill_mit3uri_replay_danmaku import ROOM_ID, normalize_content
from scripts.preview_danmaku_source_replacement import align_to_native
from scripts.audit_mit3uri_danmaku_integrity import save_json, save_csv


def identity(r):
    return r['uid'], normalize_content(r['content'])


def reconcile(selected, current, source_halo, current_halo, start, end):
    """Return source multiset edits; ambiguous occurrence identities stay unchanged."""
    want, have = collections.defaultdict(list), collections.defaultdict(list)
    for r in selected: want[identity(r)].append(r)
    for r in current: have[identity(r)].append(r)
    source_outer, current_outer = collections.defaultdict(list), collections.defaultdict(list)
    for r in source_halo:
        if not start <= r['time'] < end: source_outer[identity(r)].append(r['time'])
    for r in current_halo:
        if not start <= r['timestamp'] < end: current_outer[identity(r)].append(r['timestamp'])
    changes, held = [], []
    kept = 0
    for key in sorted(want.keys() | have.keys(), key=str):
        aa = sorted(want[key], key=lambda r:(int(r['time']), r['id']))
        bb = sorted(have[key], key=lambda r:(r['timestamp'], r.get('match_kind')=='inserted',r['id']))
        used = set(); adds = []
        for a in aa:
            b = next((b for b in bb if b['id'] not in used and b['timestamp']==int(a['time'])),None)
            if b: used.add(b['id'])
            else: adds.append(a)
        drops = [b for b in bb if b['id'] not in used]
        kept += len(used)
        if not adds and not drops: continue
        reason = None
        if not key[0]: reason = 'unknown_uid'
        elif any(not start+5 <= r['time'] < end-5 for r in adds) or any(not start+5 <= r['timestamp'] < end-5 for r in drops): reason = 'edge_guard'
        elif any(abs(r['time']-t)<=600 for r in adds for t in current_outer[key]) or any(abs(r['timestamp']-t)<=600 for r in drops for t in source_outer[key]): reason = 'neighbor_occurrence'
        elif len(aa)>1 or len(bb)>1: reason = 'repeated_identity'
        elif adds and drops:
            a,b = adds[0],drops[0]
            if b.get('aligned') and abs(b['time']-a['time'])<=4.5 and abs(b['timestamp']-a['time'])<=60:
                changes.append(dict(operation='retime',before=b,source_record=a,reason='unique_identity_validated_clock'))
                continue
            reason = 'uncalibrated_identity'
        if reason:
            held.append(dict(uid=key[0],content=key[1],reason=reason,source_ids=[a['id'] for a in adds],event_ids=[b['id'] for b in drops]))
            continue
        for a in adds: changes.append(dict(operation='insert',source_record=a,reason='selected_source_only_identity'))
        for b in drops: changes.append(dict(operation='delete',before=b,reason='outside_selected_source_multiset'))
    return changes,held,kept


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--preview',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();args.output.mkdir(exist_ok=False)
    segments=list(csv.DictReader((args.preview/'segments.csv').open(encoding='utf-8-sig')))
    totals=collections.Counter();rows=[];ids=set();source_ids=set()
    with closing(sqlite3.connect(store.PRODUCTION_DB.as_uri()+'?mode=ro',uri=True)) as main, closing(sqlite3.connect(store.ARCHIVE_DB.as_uri()+'?mode=ro',uri=True)) as cold, (args.output/'changes.jsonl').open('w') as out, (args.output/'held.jsonl').open('w') as hold:
        main.row_factory=cold.row_factory=sqlite3.Row
        event_columns=[r[1] for r in main.execute('PRAGMA table_info(event)')]
        for n,seg in enumerate(segments):
            if seg['status']!='primary':continue
            start=datetime.fromisoformat(seg['start']).timestamp();end=datetime.fromisoformat(seg['end']).timestamp()
            provider,stream=seg['source'].split(':',1)
            if provider not in ('danmakus','vtbcat') or end>store.EXTERNAL_CUTOFF_MS/1000:raise ValueError('Unsafe source interval')
            selected=[dict(r) for r in cold.execute("SELECT id,uid,uname,content,source_timestamp_ms/1000.0 AS time,source_timestamp_ms/1000.0 AS raw_time FROM danmaku_source_record INDEXED BY idx_dm_source_session WHERE source=? AND source_session=? AND eligibility='live' AND room_id=? AND source_timestamp_ms>=? AND source_timestamp_ms<?",(provider,stream,ROOM_ID,(start-600)*1000,min(store.EXTERNAL_CUTOFF_MS,(end+600)*1000)))]
            current=[dict(r) for r in main.execute("SELECT e.*,p.match_kind FROM event e INDEXED BY idx_event_cmd_time LEFT JOIN mit3uri_replay_danmaku_provenance p ON p.event_id=e.id WHERE e.cmd='DANMU_MSG' AND e.timestamp>=? AND e.timestamp<? AND e.room_id=?",(start-600,min(end+600,store.EXTERNAL_CUTOFF_MS/1000),ROOM_ID))]
            for r in current:r.update(time=float(r['timestamp']),raw_time=float(r['timestamp']))
            clock,_=align_to_native(current,selected,start)
            chosen=[r for r in selected if start<=r['time']<end]
            now=[r for r in current if start<=r['timestamp']<end]
            changes,held,kept=reconcile(chosen,now,selected,current,start,end)
            counts=collections.Counter(dict(insert=0,delete=0,retime=0))
            counts.update(x['operation'] for x in changes)
            for x in changes:
                x.update(session_id=seg['session_id'],source=seg['source'],start=start,end=end)
                if 'before' in x:
                    eid=x['before']['id']
                    if eid in ids:raise ValueError('Event selected twice')
                    ids.add(eid);x['before']={k:x['before'][k] for k in event_columns}
                    x['provenance']=[dict(r) for r in main.execute('SELECT * FROM mit3uri_replay_danmaku_provenance WHERE event_id=?',(eid,))]
                if 'source_record' in x:
                    sr=x['source_record']
                    if sr['id'] in source_ids:raise ValueError('Source selected twice')
                    source_ids.add(sr['id'])
                    if x['operation']=='retime': x['after']={**x['before'],'timestamp':int(sr['time'])}
                    else: x['after']=dict(room_id=ROOM_ID,cmd='DANMU_MSG',uid=sr['uid'],uname=sr['uname'],content=sr['content'],gift_name=None,gift_num=None,total_coin=None,title=None,timestamp=int(sr['time']))
                out.write(json.dumps(x,ensure_ascii=False)+'\n')
            for h in held:hold.write(json.dumps(dict(session_id=seg['session_id'],start=start,end=end,**h),ensure_ascii=False)+'\n')
            totals.update(counts);totals['held_identity_groups']+=len(held);totals['exact_kept']+=kept
            rows.append(dict(session_id=seg['session_id'],start=seg['start'],end=seg['end'],source=seg['source'],current=len(now),selected=len(chosen),held=len(held),**counts,clock_anchors=clock['anchors'],aligned_current=clock['aligned_rows']))
            if len(rows)%10==0:print(len(rows),dict(totals),flush=True)
        finance={cmd:main.execute('SELECT COUNT(*),SUM(COALESCE(total_coin,0)),SUM(id) FROM event INDEXED BY idx_event_cmd_time WHERE cmd=? AND room_id=? AND timestamp<?',(cmd,ROOM_ID,store.EXTERNAL_CUTOFF_MS//1000)).fetchone()[:] for cmd in ('SEND_GIFT','GUARD_BUY','SUPER_CHAT_MESSAGE')}
    sha=hashlib.sha256((args.output/'changes.jsonl').read_bytes()).hexdigest()
    save_csv(args.output/'segments.csv',rows)
    save_json(args.output/'summary.json',dict(proposal_sha256=sha,counts=dict(totals),intervals=len(rows),financial_baseline=finance,cutoff_ms=store.EXTERNAL_CUTOFF_MS,production_modified=False))
    print(dict(totals),flush=True)


if __name__=='__main__':main()
