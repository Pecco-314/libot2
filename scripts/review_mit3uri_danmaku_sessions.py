"""Read-only session/part review with held-out users and one-to-one matching.

Long messages unique within each source form time anchors. Users are split
deterministically between fitting and validation. Original event IDs already
claimed by a replay row are reserved, preserving real repeated messages.
The resulting pairs are review evidence; this module has no apply operation.
"""
from __future__ import annotations

import argparse
import bisect
import collections
import csv
import json
import math
import sqlite3
import statistics
import zlib
from contextlib import closing
from pathlib import Path

from scripts.audit_mit3uri_danmaku_integrity import ROOT, iso, save_csv, save_json
from scripts.backfill_mit3uri_replay_danmaku import ROOM_ID, normalize_content
from src.db import danmaku_sources as store

TOLERANCE = 2.5


def is_validation(uid):
    return zlib.crc32(str(uid).encode()) % 5 == 0


def quantile(values, q):
    values = sorted(values)
    return values[min(len(values)-1, math.ceil(q*len(values))-1)] if values else None


def fit_model(anchors, *, local=False, max_residual=TOLERANCE):
    train = [a for a in anchors if not is_validation(a[2])]
    test = [a for a in anchors if is_validation(a[2])]
    min_train, min_test = (8, 3) if local else (12, 5)
    if len(train) < min_train or len(test) < min_test:
        return None
    if len({a[2] for a in train}) < 4 or len({a[2] for a in test}) < 2:
        return None
    train.sort()
    # Bounded Theil-Sen sampling gives resistance to mismatched long messages.
    sample = [train[round(i*(len(train)-1)/min(59,len(train)-1))]
              for i in range(min(60,len(train)))]
    slopes = [(b[1]-a[1])/(b[0]-a[0]) for i,a in enumerate(sample)
              for b in sample[i+1:] if b[0]-a[0] >= 30]
    if not slopes:
        return None
    slope = statistics.median(slopes)
    if abs(slope) > .05:
        return None
    intercept = statistics.median(a[1]-slope*a[0] for a in train)
    residual = lambda a: abs(a[1]-(intercept+slope*a[0]))
    # Estimate timestamp jitter from fitting users only. Held-out users must
    # independently support that bound; no source gets a blanket wide window.
    tolerance=max(TOLERANCE,min(max_residual,quantile([residual(a) for a in train],.98)+.25))
    train_share = sum(residual(a)<=tolerance for a in train)/len(train)
    test_share = sum(residual(a)<=tolerance for a in test)/len(test)
    if train_share < .9 or test_share < .9:
        return None
    return dict(intercept=intercept,slope=slope,tolerance=tolerance,train=len(train),validation=len(test),
                validation_users=len({a[2] for a in test}),train_share=train_share,
                validation_share=test_share,validation_p95=quantile([residual(a) for a in test],.95),
                start=min(a[0] for a in anchors),end=max(a[0] for a in anchors),
                support=sorted(a[0] for a in anchors if residual(a)<=tolerance))


def predicted_offset(model, progress):
    if model is None or not model['start']-60 <= progress <= model['end']+60:
        return None
    points=model['support']; i=bisect.bisect_left(points,progress)
    if min((abs(points[j]-progress) for j in (i-1,i) if 0<=j<len(points)),default=1e9)>180:
        return None
    return model['intercept']+model['slope']*progress


def unambiguous_pairs(options):
    """Accept only singleton matches on both sides, without greedy reassignment."""
    degrees=collections.Counter(b for bs in options.values() for b in bs)
    return {a:bs[0] for a,bs in options.items() if len(bs)==1 and degrees[bs[0]]==1}


def load_session(db,sid,start,end):
    with closing(sqlite3.connect(f'file:{db}?mode=ro',uri=True)) as c:
        c.row_factory=sqlite3.Row
        replay=[dict(r) for r in c.execute(
            'SELECT p.*,e.uid,e.content,e.timestamp FROM mit3uri_replay_danmaku_provenance p '
            'JOIN event e ON e.id=p.event_id WHERE p.session_id=?',(sid,))]
        baseline=[dict(r) for r in c.execute(
            "SELECT e.id,e.uid,e.content,e.timestamp,p.match_kind FROM event e "
            "LEFT JOIN mit3uri_replay_danmaku_provenance p ON p.event_id=e.id "
            "WHERE e.room_id=? AND e.cmd='DANMU_MSG' AND e.timestamp BETWEEN ? AND ? "
            "AND (p.match_kind IS NULL OR p.match_kind='existing') ORDER BY e.timestamp,e.id",
            (ROOM_ID,start//1000-600,end//1000+600))]
    return replay,baseline


def review_session(replay,baseline,excluded,*,max_residual=TOLERANCE,candidate_ids=None,claimed_baselines=None):
    replay=[r for r in replay if r['event_id'] not in excluded]
    left=collections.defaultdict(list);right=collections.defaultdict(list)
    for r in replay:
        r['key']=(r['uid'],normalize_content(r['content']));left[r['key']].append(r)
    for r in baseline:
        right[(r['uid'],normalize_content(r['content']))].append(r)
    anchors=collections.defaultdict(list)
    for key, rs in left.items():
        other=right.get(key,[])
        if len(rs)!=1 or len(other)!=1 or len(key[1])<6 or key[1].startswith('['):continue
        r,b=rs[0],other[0]
        delta=r['mapped_timestamp_ms']/1000-b['timestamp']
        if abs(delta)<=600:
            anchors[r['cid']].append((r['progress_ms']/1000,delta,key[0],r['event_id'],b['id']))
    grouped=collections.defaultdict(list)
    for r in replay:grouped[r['cid']].append(r)
    # A linked original is already accounted for, even if another occurrence
    # from the same user is close after time calibration.
    reserved={b['id'] for b in baseline if b['match_kind']=='existing'}
    if claimed_baselines:reserved.update(b['id'] for b in baseline if b['id'] in claimed_baselines)
    options={};reasons={};details={};part_reports=[];model_reports=[]
    for cid,rs in grouped.items():
        aa=anchors[cid]
        global_model=fit_model(aa,max_residual=max_residual)
        models={}
        for cell in sorted({r['progress_ms']//300000 for r in rs}):
            center=cell*300+150
            model=global_model
            if model is None:
                for radius in (450,300,150):
                    model=fit_model([a for a in aa if abs(a[0]-center)<=radius],local=True,max_residual=max_residual)
                    if model is not None:break
            models[cell]=model
            if model:
                model_reports.append(dict(cid=cid,cell=cell,model='part' if global_model else 'local',
                    **{k:v for k,v in model.items() if k!='support'}))
        support=0;offsets=[];inserted_support=0;part_reasons=collections.Counter()
        for r in rs:
            model=models.get(r['progress_ms']//300000)
            offset=predicted_offset(model,r['progress_ms']/1000)
            if offset is not None:
                support+=1;offsets.append(offset)
            if r['match_kind']!='inserted' or (candidate_ids is not None and r['event_id'] not in candidate_ids):continue
            if offset is None:
                reasons[r['event_id']]='no_validated_time_model';part_reasons['no_model']+=1;continue
            inserted_support+=1
            calibrated=r['mapped_timestamp_ms']/1000-offset
            near=[b for b in right.get(r['key'],[]) if abs(b['timestamp']-calibrated)<=model['tolerance']]
            if any(b['id'] in reserved for b in near):
                reasons[r['event_id']]='original_already_claimed';part_reasons['reserved']+=1;continue
            options[r['event_id']]=[b['id'] for b in near]
            reasons[r['event_id']]='ambiguous_match' if near else 'no_same_uid_text_counterpart'
            details[r['event_id']]=(offset,model)
        part_reports.append(dict(cid=cid,bvid=rs[0]['bvid'],rows=len(rs),
            inserted_rows=sum(r['match_kind']=='inserted' for r in rs),
            anchors=len(aa),model_kind='part' if global_model else 'local' if any(models.values()) else 'unvalidated',
            supported_rows=support,supported_inserted=inserted_support,
            offset_min=round(min(offsets),3) if offsets else None,
            offset_max=round(max(offsets),3) if offsets else None,
            reserved_conflicts=part_reasons['reserved'],unmodelled_inserted=part_reasons['no_model']))
    accepted=unambiguous_pairs(options)
    for event_id in accepted:reasons[event_id]='verified_pair'
    by_cid=collections.Counter(r['cid'] for r in replay if r['event_id'] in accepted)
    for part in part_reports:part['verified_pairs']=by_cid[part['cid']]
    return accepted,reasons,details,part_reports,model_reports


def load_archived_replay(sid):
    """Retain deleted replay observations as clock anchors, never as new candidates."""
    result=[]
    with closing(sqlite3.connect(store.ARCHIVE_DB.as_uri()+'?mode=ro',uri=True)) as c:
        for eid,method,uid,content,mapped,evidence in c.execute("SELECT l.event_id,l.method,r.uid,r.content,r.mapped_timestamp_ms,r.evidence FROM danmaku_source_record r JOIN danmaku_source_link l ON l.source_id=r.id WHERE r.source='replay' AND r.source_session=? AND r.eligibility IN ('live','outside_policy') AND l.method IN ('replay_import_inserted','replay_import_existing')",(sid,)):
            if mapped is None:continue
            info=json.loads(evidence)
            result.append(dict(event_id=eid,match_kind='inserted' if method=='replay_import_inserted' else 'existing',uid=uid,content=content,mapped_timestamp_ms=mapped,
                               timestamp=mapped//1000,cid=info['cid'],bvid=info['bvid'],progress_ms=info['progress_ms']))
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,default=ROOT/'data/libot.db')
    parser.add_argument('--previous',type=Path,default=ROOT/'data/reports/danmaku_integrity_20261003')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--max-residual',type=float,default=TOLERANCE)
    parser.add_argument('--archived-anchors',action='store_true')
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    if not TOLERANCE<=args.max_residual<=4.5:parser.error('--max-residual must be between 2.5 and 4.5 seconds')
    metadata=json.loads((ROOT/'data/mit3uri_replay_danmaku/metadata.json').read_text())['recordings']
    meta={r['session_id']:r for r in metadata}
    page_meta={p['cid']:dict(page=p['page'],bvid=r['bvid'],recording_index=r['recording_index']) for r in metadata for p in r['pages']}
    excluded={int(r['event_id']) for r in csv.DictReader((args.previous/'imported_video_comments.csv').open(encoding='utf-8-sig'))}
    prior={int(r['extra_event_id']):int(r['baseline_event_id']) for r in csv.DictReader((args.previous/'duplicate_candidates.csv').open(encoding='utf-8-sig'))}
    with closing(sqlite3.connect(f'file:{args.db}?mode=ro',uri=True)) as c:
        sessions=c.execute('SELECT session_id,MIN(mapped_timestamp_ms),MAX(mapped_timestamp_ms) '
                           'FROM mit3uri_replay_danmaku_provenance GROUP BY session_id ORDER BY MIN(mapped_timestamp_ms)').fetchall()
    reports=[];parts=[];model_rows=[];decisions=collections.Counter();global_base=set();global_extra=set()
    with closing(sqlite3.connect(store.ARCHIVE_DB.as_uri()+'?mode=ro',uri=True)) as cold:
        _,claimed_baselines=store.cleanup_state(cold)
    with (args.output/'verified_pairs.jsonl').open('w') as pairs_file, (args.output/'previous_unconfirmed.jsonl').open('w') as rejected:
        for n,(sid,start,end) in enumerate(sessions,1):
            replay,baseline=load_session(args.db,sid,start,end)
            candidate_ids={r['event_id'] for r in replay}
            if args.archived_anchors:replay=load_archived_replay(sid)
            accepted,reasons,details,pp,mm=review_session(replay,baseline,excluded,max_residual=args.max_residual,candidate_ids=candidate_ids,claimed_baselines=claimed_baselines)
            by_id={r['event_id']:r for r in replay};original={r['id']:r for r in baseline}
            for eid,bid in list(accepted.items()):
                if bid in global_base or eid in global_extra:
                    reasons[eid]='cross_session_conflict';del accepted[eid];continue
                global_base.add(bid);global_extra.add(eid)
                r,b=by_id[eid],original[bid];offset,model=details[eid]
                item=dict(session_id=sid,live_date=meta[sid]['live_date'],cid=r['cid'],bvid=r['bvid'],
                          page=page_meta[r['cid']]['page'],extra_event_id=eid,baseline_event_id=bid,
                          uid=r['uid'],content=r['content'],replay_time=iso(r['timestamp']),baseline_time=iso(b['timestamp']),
                          offset_seconds=round(offset,3),residual_seconds=round(r['mapped_timestamp_ms']/1000-offset-b['timestamp'],3),
                          tolerance_seconds=model['tolerance'],
                          validation_anchors=model['validation'],validation_share=model['validation_share'],
                          previous_candidate=eid in prior,previous_same_target=prior.get(eid)==bid)
                pairs_file.write(json.dumps(item,ensure_ascii=False)+'\n')
            old={r['event_id'] for r in replay if r['event_id'] in prior and r['event_id'] in candidate_ids}
            for eid in old:
                status='verified_same_target' if accepted.get(eid)==prior[eid] else 'verified_other_target' if eid in accepted else reasons.get(eid,'excluded_video_comment')
                decisions[status]+=1
                if eid not in accepted:
                    r=by_id[eid]
                    rejected.write(json.dumps(dict(session_id=sid,live_date=meta[sid]['live_date'],extra_event_id=eid,
                        previous_baseline_event_id=prior[eid],reason=status,uid=r['uid'],content=r['content']),ensure_ascii=False)+'\n')
            report=dict(session_id=sid,live_date=meta[sid]['live_date'],start=iso(start/1000),
                        replay_rows=len(replay),inserted_rows=sum(r['match_kind']=='inserted' for r in replay),
                        previous_candidates=len(old),verified_pairs=len(accepted),
                        previous_verified=sum(e in accepted for e in old),new_pairs=sum(e not in prior for e in accepted),
                        parts=len(pp),affected_parts=sum(p['verified_pairs']>0 for p in pp),
                        supported_inserted=sum(p['supported_inserted'] for p in pp),
                        reserved_conflicts=sum(p['reserved_conflicts'] for p in pp),
                        excluded_video_comments=sum(r['event_id'] in excluded for r in replay))
            reports.append(report)
            parts.extend(dict(session_id=sid,live_date=meta[sid]['live_date'],page=page_meta[p['cid']]['page'],**p) for p in pp)
            model_rows.extend(dict(session_id=sid,live_date=meta[sid]['live_date'],**m) for m in mm)
            if n%25==0:print(f'review {n}/{len(sessions)} verified={len(global_extra)}',flush=True)
    save_csv(args.output/'sessions.csv',reports);save_csv(args.output/'parts.csv',parts);save_csv(args.output/'models.csv',model_rows)
    ranked=sorted(reports,key=lambda r:r['verified_pairs'],reverse=True)
    total=len(global_extra)
    summary=dict(production_mutated=False,archived_anchors=args.archived_anchors,max_residual=args.max_residual,session_count=len(reports),verified_pairs=total,
        affected_sessions=sum(r['verified_pairs']>0 for r in reports),previous_decisions=decisions,
        new_pairs=sum(r['new_pairs'] for r in reports),concentration={str(k):dict(pairs=sum(r['verified_pairs'] for r in ranked[:k]),
        share=sum(r['verified_pairs'] for r in ranked[:k])/total if total else 0) for k in (5,10,20,30,50,100)},
        top_sessions=ranked[:30],march27=[r for r in reports if r['live_date']=='2026-03-27'])
    save_json(args.output/'summary.json',summary)
    print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
