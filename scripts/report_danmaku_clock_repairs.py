"""Verify reviewed reconstruction policies and report unresolved clock evidence."""
import argparse
import collections
import csv
import json
import math
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.audit_danmaku_clock_bursts import ro,replay_mapping,load_streams,singleton_index
from scripts.repair_danmaku_clock_bursts import reference_clock
from scripts.danmaku_clock_policy import load_policies,apply_policy,REGISTRY
from scripts.apply_source_first_changes import durable_json
from src.db import danmaku_sources as store


def remaining_reason(source,reference,source_id,start,end):
    source=sorted(source,key=lambda r:(r['time'],r.get('sequence_order',r['id'])))
    rank={r['id']:i for i,r in enumerate(source)}
    target=source[rank[source_id]]
    if not target.get('provider_order_restored'):return '缺少可靠的原始消息编号'
    ia,ib=singleton_index(source),singleton_index(reference)
    anchors=sorted((rank[a['id']],b['time'],a['uid']) for k,a in ia.items()
                   if a.get('provider_order_restored') and (b:=ib.get(k))
                   and start-600<=a['time']<=end+600 and abs(a['time']-b['time'])<=1800)
    left=[p for p in anchors if p[0]<rank[source_id] and p[2]!=target['uid']]
    right=[p for p in anchors if p[0]>rank[source_id] and p[2]!=target['uid']]
    if len(left)<2 or len(right)<2:return f'两侧独立锚点不足（左{min(len(left),2)}、右{min(len(right),2)}）'
    witnesses=left[-2:]+right[:2]
    users=len({p[2] for p in witnesses})
    if users<3:return f'附近锚点集中在{users}位其他用户，未达到3位'
    if any(y[1]<x[1]-2 for x,y in zip(witnesses,witnesses[1:])):return '相邻锚点的原始编号顺序与参考时间存在倒置'
    before,after=left[-1],right[0]
    if after[0]-before[0]>100:return '前后锚点之间超过100条源消息'
    if not 0<=after[1]-before[1]<=60:return f"参考锚点间隔{after[1]-before[1]:.1f}秒，超出安全范围"
    return '逐条匹配仍有歧义或参考记录不在独立锚点夹定的区间内'


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--candidate',type=Path,required=True)
    p.add_argument('--replacement',type=Path,required=True)
    p.add_argument('--audit',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    by=collections.defaultdict(list)
    for line in (args.audit/'bursts.jsonl').read_text().splitlines():
        h=json.loads(line);by[h['session_id']].append(h)
    policies=load_policies();remaining=[];genuine=[];reports=[];operations=collections.Counter()
    inserted={}
    for policy_path in json.loads(REGISTRY.read_text()):
        policy=json.loads(Path(policy_path).read_text());work=Path(policy['plan'])
        execution=json.loads((work/'execution.json').read_text())
        operations.update(execution['operations'])
        with closing(ro(work/'rollback.sqlite3')) as backup:
            inserted.update(dict(backup.execute('SELECT source_record_id,event_id FROM inserted_event')))
    with closing(ro(args.candidate/'rebuilt.sqlite3')) as c, closing(ro(args.replacement/'rollback.sqlite3')) as mapping, closing(ro(store.PRODUCTION_DB)) as main_db, closing(ro(store.ARCHIVE_DB)) as cold:
        reverse,locks=replay_mapping(main_db)
        for sid,hits in by.items():
            session=json.loads(c.execute('SELECT report FROM build_session WHERE session_id=?',(sid,)).fetchone()[0])
            lo,hi=(datetime.fromisoformat(session[k]).timestamp() for k in ('start','end'))
            streams=load_streams(cold,lo,hi,[o for s in session['members'] for o in reverse[s]],locks)
            clocks={r[0]:json.loads(r[1]) for r in c.execute('SELECT source,report FROM clock_report WHERE session_id=?',(sid,))}
            old=[];current={};available={r['id']:r for rr in streams.values() for r in rr}
            for row in c.execute("SELECT o.*,e.uid,e.uname,e.content,e.timestamp FROM event e INDEXED BY idx_event_room_time JOIN origin o ON o.event_id=e.id WHERE e.room_id=1967216004 AND e.cmd='DANMU_MSG' AND e.timestamp>=? AND e.timestamp<?",(lo,hi)):
                if row['session_id']!=sid:continue
                i=row['source_record_id']
                old.append(dict(id=i,uid=row['uid'],uname=row['uname'],content=row['content'],time=row['timestamp']))
                eid=mapping.execute('SELECT event_id FROM event_map WHERE candidate_id=?',(row['event_id'],)).fetchone()[0]
                now=main_db.execute('SELECT id,uid,uname,content,timestamp FROM event WHERE id=?',(eid,)).fetchone()
                if now:current[i]=dict(now)
            for source_id,eid in inserted.items():
                if source_id not in available:continue
                now=main_db.execute('SELECT id,uid,uname,content,timestamp FROM event WHERE id=?',(eid,)).fetchone()
                if now:current[source_id]=dict(now)
            rebuilt=apply_policy(old,streams,policies.get(sid,[]))
            expected={r['id']:(r['uid'],r['uname'],r['content'],int(r['time'])) for r in rebuilt}
            actual={i:(r['uid'],r['uname'],r['content'],r['timestamp']) for i,r in current.items()}
            if expected!=actual:
                raise ValueError(f'Reconstruction policy mismatch: {sid}')
            seen=set();n_remaining=0;n_genuine=0
            for h in hits:
                for e in h['evidence']:
                    a,b=current.get(e['source_id']),current.get(e['reference_id'])
                    if not a or not b or a['id']==b['id']:continue
                    pair=tuple(sorted((a['id'],b['id'])))
                    if pair in seen:continue
                    seen.add(pair)
                    proof=[]
                    for stream,rr in streams.items():
                        if not stream.startswith('replay:'):continue
                        candidates=[(r,reference_clock(r,stream,clocks)) for r in rr if r['uid']==e['uid'] and r['content']==e['content']]
                        left=[r for r,t in candidates if abs(t-a['timestamp'])<=7]
                        right=[r for r,t in candidates if abs(t-b['timestamp'])<=7]
                        if any(x['id']!=y['id'] for x in left for y in right):
                            proof=[r['id'] for r in left+right]
                    entry=dict(session_id=sid,date=session['start'][:10],uid=e['uid'],content=e['content'],
                               event_a=a['id'],event_b=b['id'],source_record_a=e['source_id'],source_record_b=e['reference_id'],
                               time_a=datetime.fromtimestamp(a['timestamp'],ZoneInfo('Asia/Shanghai')).isoformat(),
                               time_b=datetime.fromtimestamp(b['timestamp'],ZoneInfo('Asia/Shanghai')).isoformat(),
                               independent_replay_occurrences=proof)
                    if proof:genuine.append(entry);n_genuine+=1
                    else:
                        entry['reason']=remaining_reason(streams[h['source']],streams[h['reference']],e['source_id'],math.floor(h['start']/30)*30-15,math.ceil(h['end']/30)*30+15)
                        remaining.append(entry);n_remaining+=1
            reports.append(dict(session_id=sid,date=session['start'][:10],rows_checked=len(actual),
                                policy_decisions=len(policies.get(sid,[])),remaining_anchor_pairs=n_remaining,
                                genuine_repeated_pairs=n_genuine))
            print(reports[-1],flush=True)
        counts=dict(main_db.execute("SELECT cmd,COUNT(*) FROM event INDEXED BY idx_event_room_time WHERE room_id=1967216004 AND timestamp<? GROUP BY cmd",(store.EXTERNAL_CUTOFF_MS//1000,)))
    for filename,rows in [('remaining.csv',remaining),('genuine_repeats.csv',genuine),('sessions.csv',reports)]:
        with (args.output/filename).open('w',encoding='utf-8-sig',newline='') as f:
            if rows:
                writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    scanned=json.loads((args.audit/'summary.json').read_text())['sessions_scanned']
    result=dict(sessions_scanned=scanned,sessions_flagged=len(by),sessions_changed=sum(bool(s['policy_decisions']) for s in reports),
                operations=dict(operations),reconstruction_rows_verified=sum(r['rows_checked'] for r in reports),
                remaining_anchor_pairs=len(remaining),genuine_repeated_pairs=len(genuine),sessions=reports,
                historical_counts=counts,protected_cutoff=store.EXTERNAL_CUTOFF_MS//1000)
    durable_json(args.output/'summary.json',result)
    print(json.dumps({k:v for k,v in result.items() if k!='sessions'},ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
