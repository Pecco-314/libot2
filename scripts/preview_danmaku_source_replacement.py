"""Read-only, source-first reconstruction preview for historical DANMU_MSG.

Native coverage selects the primary stream. Replay observations calibrate or
fill proven gaps only; unmatched replay messages never trigger supplementation.
Uncertain boundaries and deficient segments retain current production rows.
Financial events are outside the replacement diff and receive a separate plan.
This module has no apply operation.
"""
import argparse
import bisect
import collections
import gzip
import json
import math
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from src.db import danmaku_sources as store
from scripts.backfill_mit3uri_replay_danmaku import ROOM_ID,normalize_content
from scripts.plan_danmaku_source_segments import map_replay_sessions
from scripts.review_mit3uri_danmaku_sessions import fit_model,predicted_offset
from scripts.audit_mit3uri_danmaku_integrity import save_json,save_csv,iso

CELL=120
NATIVE=('monitor','danmakus','vtbcat')
PRIORITY=NATIVE+('replay',)


def rank(source):return PRIORITY.index(source.split(':',1)[0])


def hist(rows,start,end):
    c=collections.Counter()
    for r in rows:
        if start<=r['time']<end:c[int((r['time']-start)//CELL)]+=1
    return c


def primary_stream(counts):
    native={s:v for s,v in counts.items() if s.split(':',1)[0] in NATIVE and sum(v.values())}
    if not native:return None
    active={i for v in native.values() for i,n in v.items() if n>=5}
    scores={s:sum(v.get(i,0)>=.85*max(other.get(i,0) for other in native.values()) for i in active) for s,v in native.items()}
    best=max(scores.values(),default=0)
    eligible=[s for s in native if scores[s]>=.98*best]
    return min(eligible,key=lambda s:(rank(s),-scores[s],s))


def align_to_native(values,base,start):
    left=collections.defaultdict(list);right=collections.defaultdict(list)
    for r in values:left[(r['uid'],normalize_content(r['content']))].append(r)
    for r in base:right[(r['uid'],normalize_content(r['content']))].append(r)
    anchors=[]
    for key,rr in left.items():
        bb=right.get(key,[])
        if key[0] and len(key[1])>=6 and not key[1].startswith('[') and len(rr)==len(bb)==1:
            r,b=rr[0],bb[0];delta=r['raw_time']-b['raw_time']
            if abs(delta)<=600:anchors.append((r['raw_time']-start,delta,key[0],r['id'],b['id']))
    global_model=fit_model(anchors,max_residual=4.5);cache={};supported=[]
    for r in values:
        p=r['raw_time']-start;model=global_model
        if predicted_offset(model,p) is None:
            cell=int(p//120)
            if cell not in cache:
                center=cell*120+60;cache[cell]=None
                for radius in (450,300,150,90):
                    model=fit_model([a for a in anchors if abs(a[0]-center)<=radius],local=True,max_residual=4.5)
                    if model is not None:cache[cell]=model;break
            model=cache[cell]
        offset=predicted_offset(model,p)
        if offset is not None:
            r['time']=r['raw_time']-offset;r['aligned']=True
        else:r['aligned']=False
    for a in anchors:
        p,delta,uid,rid,bid=a;model=global_model
        if predicted_offset(model,p) is None:model=cache.get(int(p//120))
        off=predicted_offset(model,p)
        if off is not None and abs(delta-off)<=model['tolerance']:
            supported.append((start+p-delta,uid))
    return dict(anchors=len(anchors),supported_anchors=len(supported),aligned_rows=sum(r['aligned'] for r in values),rows=len(values),global_clock=global_model is not None,offset=global_model['intercept'] if global_model else None),supported


def boundary_supported(points,edge,before):
    users={uid for ts,uid in points if (edge-180<=ts<edge if before else edge<=ts<edge+180)}
    return len(users)>=3


def plan_cells(counts,primary,start,end,anchor_points):
    """Sustained absence is necessary, but not sufficient, for a source switch."""
    cells=[]
    for i in range(math.ceil((end-start)/CELL)):
        native={s:v.get(i,0) for s,v in counts.items() if s.split(':',1)[0] in NATIVE}
        own=native.get(primary,0);best=max(native.values(),default=0)
        if primary is None:status='replay_only_clock_review';selected=None
        elif own:
            status='partial_native_deficit_review' if best>=10 and own<.5*best else 'primary'
            selected=primary
        else:
            candidates=[s for s,v in counts.items() if s!=primary and v.get(i,0)>=5]
            selected=min(candidates,key=lambda s:(rank(s),-counts[s][i],s)) if candidates else None
            status='gap_candidate' if selected else 'all_source_silence_keep'
        cells.append(dict(start=start+i*CELL,end=min(end,start+(i+1)*CELL),source=selected,status=status))
    runs=[]
    for cell in cells:
        if runs and (runs[-1]['status'],runs[-1]['source'])==(cell['status'],cell['source']):runs[-1]['end']=cell['end']
        else:runs.append(dict(cell))
    result=[]
    for run in runs:
        if run['status']=='gap_candidate':
            points=anchor_points.get(run['source'],[])
            if run['end']-run['start']<2*CELL:run['status']='short_gap_review'
            elif not (boundary_supported(points,run['start'],True) and boundary_supported(points,run['end'],False)):
                run['status']='gap_boundary_clock_review'
            else:
                a,b=run['start'],run['end']
                result.append(dict(run,start=a,end=a+5,status='switch_boundary_keep'))
                run=dict(run,start=a+5,end=b-5,status='validated_gap_fill')
                result.append(run)
                result.append(dict(run,start=b-5,end=b,status='switch_boundary_keep'))
                continue
        result.append(run)
    return result


def diff_rows(selected,current):
    wanted=collections.defaultdict(list);existing=collections.defaultdict(list)
    for r in selected:wanted[(r['uid'],normalize_content(r['content']),int(r['time']))].append(r)
    for r in current:existing[(r['uid'],normalize_content(r['content']),r['timestamp'])].append(r)
    adds=[];drops=[];kept=0
    for key in wanted.keys()|existing.keys():
        a=sorted(wanted.get(key,[]),key=lambda r:r['id'])
        b=sorted(existing.get(key,[]),key=lambda r:(r.get('match_kind')=='inserted',r['id']))
        common=min(len(a),len(b));kept+=common
        adds.extend(a[common:]);drops.extend(b[common:])
    return adds,drops,kept


def coverage_conflict(counts,primary,start,a,b,selected_count,current):
    """Counterevidence can veto replacement without making replay the primary."""
    originals=sum(r.get('match_kind')!='inserted' for r in current)
    if originals>selected_count*1.25+5:return 'original_native_coverage_conflict_review'
    for cell in range(int((a-start)//CELL),math.ceil((b-start)/CELL)):
        own=counts.get(primary,{}).get(cell,0)
        observed=max((v.get(cell,0) for s,v in counts.items() if s.startswith('replay:')),default=0)
        if observed>=10 and observed>2*own:return 'replay_coverage_conflict_review'
    return None


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--session')
    args=p.parse_args();args.output.mkdir(exist_ok=False)
    metadata=json.loads((store.ROOT/'data/mit3uri_replay_danmaku/metadata.json').read_text())['recordings']
    locks=json.loads((store.ROOT/'data/mit3uri_replay_danmaku/alignment_locks.json').read_text())['sessions']
    with closing(sqlite3.connect(store.PRODUCTION_DB.as_uri()+'?mode=ro',uri=True)) as live:
        live.row_factory=sqlite3.Row
        sessions=[dict(r) for r in live.execute('SELECT * FROM mit3uri_replay_session WHERE included_in_total=1 AND start_timestamp<? ORDER BY start_timestamp',(store.EXTERNAL_CUTOFF_MS//1000,))]
        mapping,ambiguous=map_replay_sessions(metadata,dict(live.execute('SELECT bvid,session_id FROM mit3uri_replay_recording')))
    reverse=collections.defaultdict(list)
    for old,new in mapping.items():reverse[new].append(old)
    for s in sessions:
        s['start']=s['start_timestamp'];bases=[locks[o]['base_ms']/1000 for o in reverse[s['session_id']] if o in locks and locks[o].get('live_date')==s['live_date']]
        if s['start_time_precision']!='second' and bases and abs(min(bases)-s['start'])<3600:s['start']=min(bases)
        s['end']=min(store.EXTERNAL_CUTOFF_MS/1000,s['start']+(s['duration_seconds'] or 0))
    save_json(args.output/'session_mapping.json',dict(old_to_current=mapping,ambiguous=ambiguous))
    reports=[];intervals=[];clocks=[];finance=[];stats=collections.Counter()
    with closing(sqlite3.connect(store.ARCHIVE_DB.as_uri()+'?mode=ro',uri=True)) as cold,closing(sqlite3.connect(store.PRODUCTION_DB.as_uri()+'?mode=ro',uri=True)) as live,gzip.open(args.output/'draft_changes.jsonl.gz','wt') as changes:
        cold.row_factory=sqlite3.Row;live.row_factory=sqlite3.Row;cold.execute('PRAGMA cache_size=-32768')
        for n,s in enumerate(sessions,1):
            sid=s['session_id'];start=s['start'];end=s['end']
            if args.session and sid!=args.session:continue
            if end<=start:continue
            overlaps=[o['session_id'] for o in sessions if o['session_id']!=sid and min(end,o['end'])-max(start,o['start'])>0]
            streams=collections.defaultdict(list)
            found=cold.execute("SELECT id,source,source_session,uid,uname,content,source_timestamp_ms FROM danmaku_source_record INDEXED BY idx_dm_source_time WHERE room_id=? AND source_timestamp_ms>=? AND source_timestamp_ms<? AND source IN ('monitor','danmakus','vtbcat') AND eligibility='live'",(ROOM_ID,(start-600)*1000,min(store.EXTERNAL_CUTOFF_MS,(end+600)*1000))).fetchall()
            for r in found:
                key=r['source']+':'+r['source_session'];t=r['source_timestamp_ms']/1000
                streams[key].append(dict(id=r['id'],uid=r['uid'],uname=r['uname'],content=r['content'],time=t,raw_time=t,aligned=False))
            for old in reverse[sid]:
                for r in cold.execute("SELECT id,uid,uname,content,mapped_timestamp_ms,evidence FROM danmaku_source_record WHERE source='replay' AND source_session=? AND eligibility='live'",(old,)):
                    t=r['mapped_timestamp_ms']/1000
                    if t>=store.EXTERNAL_CUTOFF_MS/1000:continue
                    e=json.loads(r['evidence']);key='replay:'+str(e['cid'])
                    streams[key].append(dict(id=r['id'],uid=r['uid'],uname=r['uname'],content=r['content'],time=t,raw_time=t,aligned=False))
            counts={key:hist(rr,start,end) for key,rr in streams.items()}
            primary=primary_stream(counts);points={}
            if primary:
                for r in streams[primary]:r['aligned']=True
                for key,rr in streams.items():
                    if key==primary:continue
                    report,support=align_to_native(rr,streams[primary],start);points[key]=support
                    clocks.append(dict(session_id=sid,source=key,reference=primary,**report))
            counts={key:hist(rr,start,end) for key,rr in streams.items()}
            plan=plan_cells(counts,primary,start,end,points)
            current=[dict(r) for r in live.execute("SELECT e.*,p.match_kind FROM event e LEFT JOIN mit3uri_replay_danmaku_provenance p ON p.event_id=e.id WHERE e.room_id=? AND e.timestamp>=? AND e.timestamp<? AND e.cmd='DANMU_MSG' ORDER BY e.timestamp,e.id",(ROOM_ID,start,end))]
            totals=collections.Counter();primary_duplicates=0
            if primary:
                seen=collections.Counter((r['uid'],normalize_content(r['content']),r['raw_time']) for r in streams[primary] if start<=r['raw_time']<end)
                primary_duplicates=sum(v-1 for v in seen.values())
            for ix,run in enumerate(plan):
                a,b=run['start'],run['end'];key=run['source'];status=run['status']
                now=[r for r in current if a<=r['timestamp']<b]
                selected=[r for r in streams.get(key,[]) if a<=r['time']<b]
                if overlaps:status='session_overlap_review'
                elif s['start_time_precision']!='second':status='session_start_clock_review'
                elif primary_duplicates:status='primary_internal_multiplicity_review'
                elif status=='validated_gap_fill' and not all(r['aligned'] for r in selected):status='gap_interior_clock_review'
                elif status=='primary':status=coverage_conflict(counts,primary,start,a,b,len(selected),now) or status
                add=drop=[];kept=len(now)
                if status in ('primary','validated_gap_fill'):
                    add,drop,kept=diff_rows(selected,now)
                    for r in add:changes.write(json.dumps(dict(session_id=sid,segment=ix,operation='candidate_add_from_selected_source',source=key,source_record_id=r['id'],uid=r['uid'],content=r['content'],timestamp=int(r['time']),raw_timestamp=r['raw_time']),ensure_ascii=False)+'\n')
                    for r in drop:changes.write(json.dumps(dict(session_id=sid,segment=ix,operation='candidate_remove_not_in_selected_source',source=key,event_snapshot=r),ensure_ascii=False)+'\n')
                else:totals['held_current']+=len(now)
                totals.update(add=len(add),remove=len(drop),keep=kept)
                intervals.append(dict(session_id=sid,date=s['live_date'],start=iso(a),end=iso(b),source=key,status=status,current=len(now),selected=len(selected),candidate_add=len(add),candidate_remove=len(drop)))
            # Finance is independently preserved, even in preview replacement intervals.
            money=dict(live.execute("SELECT cmd,COUNT(*) FROM event WHERE room_id=? AND timestamp>=? AND timestamp<? AND cmd IN ('SEND_GIFT','GUARD_BUY','SUPER_CHAT_MESSAGE') GROUP BY cmd",(ROOM_ID,start,end)))
            finance.append(dict(session_id=sid,date=s['live_date'],gift=money.get('SEND_GIFT',0),guard=money.get('GUARD_BUY',0),sc=money.get('SUPER_CHAT_MESSAGE',0),action='retain_main_independent_transaction_audit',candidate_danmaku_primary=primary))
            row=dict(session_id=sid,date=s['live_date'],start=iso(start),end=iso(end),primary=primary,current=len(current),primary_rows=sum(counts.get(primary,{}).values()),primary_exact_timestamp_multiplicity=primary_duplicates,overlaps=';'.join(overlaps),**dict(totals))
            reports.append(row);stats.update(totals)
            print(f'{n}/{len(sessions)} {sid} {primary} {dict(totals)}',flush=True)
            time.sleep(.02)
    save_csv(args.output/'sessions.csv',reports);save_csv(args.output/'segments.csv',intervals);save_csv(args.output/'clocks.csv',clocks);save_csv(args.output/'financial_plan.csv',finance)
    save_json(args.output/'summary.json',dict(production_mutated=False,apply_supported=False,cutoff_exclusive=iso(store.EXTERNAL_CUTOFF_MS/1000),sessions=len(reports),counts=dict(stats),primary_sources=dict(collections.Counter(r['primary'].split(':',1)[0] if r['primary'] else 'no_native_source' for r in reports)),segment_statuses=dict(collections.Counter(r['status'] for r in intervals)),diff_semantics='Exact normalized UID/text/integer-second multiset diff; adds/removes may include timestamp relocations, not unique new/lost messages',financial_policy='Preserve all current gifts, guards and SC; independently audit raw transaction identities before any replacement',boundary_guard_seconds=5))


if __name__=='__main__':main()
