"""Reconcile ordered source occurrences across interval edges, preserving multiplicity.

Exact occurrences are reserved first. Clock-supported ordered pairs retain event
IDs. Only surplus replay insertions supported by an already reserved occurrence
are removable; native repeats and unmatched observations stay intact.
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
from scripts.backfill_mit3uri_replay_danmaku import ROOM_ID,normalize_content
from scripts.preview_danmaku_source_replacement import align_to_native
from scripts.review_mit3uri_danmaku_sessions import fit_model,is_validation,predicted_offset
from scripts.audit_mit3uri_danmaku_integrity import save_json,save_csv


def ident(r):return r['uid'],normalize_content(r['content'])


def reconcile_occurrences(wanted,current,tolerance=4.5):
    aa=sorted(wanted,key=lambda r:(r['time'],r['id']))
    bb=sorted(current,key=lambda r:(r['timestamp'],r.get('match_kind')=='inserted',r['id']))
    pairs=[];used_a=set();used_b=set()
    for a in aa:
        b=next((b for b in bb if b['id'] not in used_b and int(a['time'])==b['timestamp']),None)
        if b:pairs.append((a,b));used_a.add(a['id']);used_b.add(b['id'])
    # Equal cardinality and clock-supported monotone order preserve repetitions.
    ar=[a for a in aa if a['id'] not in used_a];br=[b for b in bb if b['id'] not in used_b]
    def compatible(a,b):return b.get('aligned') and abs(a['time']-b['time'])<=tolerance and abs(a['time']-b['timestamp'])<=60
    if len(ar)==len(br) and all(compatible(a,b) for a,b in zip(ar,br)):
        for a,b in zip(ar,br):pairs.append((a,b));used_a.add(a['id']);used_b.add(b['id'])
    else:
        # Resolve only mutually unique edges; no greedy nearest-neighbor merge.
        edges={a['id']:[b for b in br if compatible(a,b)] for a in ar}
        degree=collections.Counter(b['id'] for opts in edges.values() for b in opts)
        for a in ar:
            opts=edges[a['id']]
            if len(opts)==1 and degree[opts[0]['id']]==1:
                b=opts[0];pairs.append((a,b));used_a.add(a['id']);used_b.add(b['id'])
    drops=[]
    if len(used_a)==len(aa):
        for b in bb:
            if b['id'] in used_b or b.get('match_kind')!='inserted':continue
            candidates=[(a,keep) for a,keep in pairs if compatible(a,b) and keep.get('match_kind')!='inserted']
            if len(candidates)==1:drops.append((b,candidates[0][1]));used_b.add(b['id'])
    return pairs,drops,[a for a in aa if a['id'] not in used_a],[b for b in bb if b['id'] not in used_b]


def clock_anchors(values,base):
    left=collections.defaultdict(list);right=collections.defaultdict(list)
    for r in values:left[ident(r)].append(r)
    for r in base:right[ident(r)].append(r)
    anchors=[]
    for key,rr in left.items():
        ss=right.get(key,[])
        if key[0] and len(key[1])>=6 and not key[1].startswith('[') and len(rr)==len(ss)==1:
            delta=rr[0]['raw_time']-ss[0]['raw_time']
            if abs(delta)<=600:anchors.append((rr[0]['raw_time'],delta,key[0],rr[0]['id'],ss[0]['id']))
    return anchors


def clock_diagnosis(anchors,model,target):
    t=target['raw_time'];near=[a for a in anchors if abs(a[0]-t)<=450]
    train=[a for a in near if not is_validation(a[2])];test=[a for a in near if is_validation(a[2])]
    if target.get('aligned'):reason='模型可预测，但此条与源记录的残差或配对关系不满足要求'
    elif len(train)<8 or len(test)<3:reason='附近唯一长文本锚点不足（训练至少8条、独立验证至少3条）'
    elif len({a[2] for a in train})<4 or len({a[2] for a in test})<2:reason='附近锚点的独立用户不足（训练至少4人、验证至少2人）'
    elif model and predicted_offset(model,t) is None:reason='全局模型成立，但此位置超出有效范围或距支持锚点超过180秒'
    else:reason='锚点偏移不稳定，未通过漂移/残差/独立验证要求'
    return dict(reason=reason,global_model=bool(model),all_anchors=len(anchors),nearby_train=len(train),nearby_validation=len(test),train_users=len({a[2] for a in train}),validation_users=len({a[2] for a in test}),nearest_anchor_seconds=round(min((abs(a[0]-t) for a in anchors),default=-1),3),anchor_examples=[dict(timestamp=a[0],offset=round(a[1],3),uid=a[2]) for a in sorted(near,key=lambda a:abs(a[0]-t))[:8]])


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--previous',type=Path,required=True);p.add_argument('--preview',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--tolerance',type=float,default=4.5)
    p.add_argument('--clock-history',type=Path,action='append',default=[])
    args=p.parse_args()
    if not 0<args.tolerance<=7:p.error('Tolerance must be in (0,7] seconds')
    args.output.mkdir(exist_ok=False)
    previous=[json.loads(l) for l in (args.previous/'held.jsonl').open()]
    old_times={}
    for history in reversed([args.previous,*args.clock_history]):
        old_times.update({x['before']['id']:x['before']['timestamp'] for l in (history/'changes.jsonl').open() if (x:=json.loads(l))['operation']=='retime'})
    groups=collections.defaultdict(list)
    for s in csv.DictReader((args.preview/'segments.csv').open(encoding='utf-8-sig')):
        if s['status']=='primary':groups[(s['session_id'],s['source'])].append((datetime.fromisoformat(s['start']).timestamp(),datetime.fromisoformat(s['end']).timestamp()))
    held_by_session=collections.defaultdict(list)
    for h in previous:held_by_session[h['session_id']].append(h)
    total=collections.Counter();claimed_events=set();claimed_sources=set();reports=[];diagnoses=[]
    with closing(sqlite3.connect(store.PRODUCTION_DB.as_uri()+'?mode=ro',uri=True)) as main,closing(sqlite3.connect(store.ARCHIVE_DB.as_uri()+'?mode=ro',uri=True)) as cold,(args.output/'changes.jsonl').open('w') as output,(args.output/'remaining.jsonl').open('w') as remaining:
        main.row_factory=cold.row_factory=sqlite3.Row
        columns=[r[1] for r in main.execute('PRAGMA table_info(event)')]
        for n,((sid,source),spans) in enumerate(groups.items(),1):
            if not held_by_session[sid]:continue
            lo=min(a for a,b in spans);hi=max(b for a,b in spans);provider,stream=source.split(':',1)
            native=[dict(r) for r in cold.execute("SELECT id,uid,uname,content,source_timestamp_ms/1000.0 AS time,source_timestamp_ms/1000.0 AS raw_time FROM danmaku_source_record INDEXED BY idx_dm_source_session WHERE source=? AND source_session=? AND room_id=? AND eligibility='live' AND source_timestamp_ms>=? AND source_timestamp_ms<?",(provider,stream,ROOM_ID,(lo-600)*1000,min(store.EXTERNAL_CUTOFF_MS,(hi+600)*1000)))]
            current=[dict(r) for r in main.execute("SELECT e.*,p.match_kind FROM event e INDEXED BY idx_event_cmd_time LEFT JOIN mit3uri_replay_danmaku_provenance p ON p.event_id=e.id WHERE e.cmd='DANMU_MSG' AND e.room_id=? AND e.timestamp>=? AND e.timestamp<?",(ROOM_ID,lo-600,min(store.EXTERNAL_CUTOFF_MS/1000,hi+600)))]
            for r in current:r.update(raw_time=float(old_times.get(r['id'],r['timestamp'])),time=float(r['timestamp']))
            align_to_native(current,native,lo)
            anchors=clock_anchors(current,native);global_model=fit_model(anchors,max_residual=4.5)
            ni=collections.defaultdict(list);ci=collections.defaultdict(list)
            for r in native:ni[ident(r)].append(r)
            for r in current:ci[ident(r)].append(r)
            keys={ (h['uid'],normalize_content(h['content'])) for h in held_by_session[sid] }
            counts=collections.Counter(dict(retime=0,delete=0,resolved_groups=0,remaining_groups=0))
            for key in sorted(keys,key=str):
                aa=ni[key];bb=ci[key]
                pairs,drops,unmatched_a,unmatched_b=reconcile_occurrences(aa,bb,tolerance=args.tolerance)
                def eligible(a):return any(start<=a['time']<end for start,end in spans)
                planned=[]
                for a,b in pairs:
                    if not eligible(a) or b['timestamp']==int(a['time']):continue
                    if b['id'] in claimed_events or a['id'] in claimed_sources:continue
                    planned.append(dict(operation='retime',before=b,source_record=a,after={**{k:b[k] for k in columns},'timestamp':int(a['time'])},reason='ordered_source_occurrence_clock'))
                for b,keep in drops:
                    if b['id'] in claimed_events or not any(a+5<=b['timestamp']<z-5 for a,z in spans):continue
                    planned.append(dict(operation='delete',before=b,keep_event_id=keep['id'],reason='surplus_replay_with_reserved_native_occurrence'))
                for x in planned:
                    b=x['before'];x['before']={k:b[k] for k in columns};t=x.get('after',b)['timestamp']
                    x.update(session_id=sid,source=source,start=min(b['timestamp'],t)-6,end=max(b['timestamp'],t)+6)
                    x['provenance']=[dict(r) for r in main.execute('SELECT * FROM mit3uri_replay_danmaku_provenance WHERE event_id=?',(b['id'],))]
                    claimed_events.add(b['id'])
                    if 'source_record' in x:claimed_sources.add(x['source_record']['id'])
                    output.write(json.dumps(x,ensure_ascii=False)+'\n');counts[x['operation']]+=1
                ua=[a for a in unmatched_a if eligible(a)]
                ub=[b for b in unmatched_b if any(a<=b['timestamp']<z for a,z in spans)]
                if ua or ub:
                    counts['remaining_groups']+=1
                    detail=dict(session_id=sid,source=source,uid=key[0],content=key[1],source_ids=[a['id'] for a in ua],event_ids=[b['id'] for b in ub])
                    if ub:
                        detail['clock']=clock_diagnosis(anchors,global_model,ub[0]);detail['event_timestamp']=ub[0]['timestamp'];detail['source_times']=[a['time'] for a in ua[:5]]
                    remaining.write(json.dumps(detail,ensure_ascii=False)+'\n')
                    if any(h['reason']=='uncalibrated_identity' and h['uid']==key[0] and normalize_content(h['content'])==key[1] for h in held_by_session[sid]):diagnoses.append(detail)
                else:counts['resolved_groups']+=1
            total.update(counts);reports.append(dict(session_id=sid,source=source,**counts))
            if n%10==0:print(n,dict(total),flush=True)
        finance={cmd:main.execute('SELECT COUNT(*),SUM(COALESCE(total_coin,0)),SUM(id) FROM event INDEXED BY idx_event_cmd_time WHERE cmd=? AND room_id=? AND timestamp<?',(cmd,ROOM_ID,store.EXTERNAL_CUTOFF_MS//1000)).fetchone()[:] for cmd in ('SEND_GIFT','GUARD_BUY','SUPER_CHAT_MESSAGE')}
    sha=hashlib.sha256((args.output/'changes.jsonl').read_bytes()).hexdigest()
    save_csv(args.output/'sessions.csv',reports);save_json(args.output/'clock_diagnostics.json',diagnoses)
    save_json(args.output/'summary.json',dict(proposal_sha256=sha,counts=dict(total),financial_baseline=finance,production_modified=False,remaining_old_clock_groups=len(diagnoses),occurrence_tolerance_seconds=args.tolerance))
    print(dict(total),flush=True)


if __name__=='__main__':main()
