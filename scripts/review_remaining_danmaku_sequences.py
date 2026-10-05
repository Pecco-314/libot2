"""Review every remaining historical candidate with occurrence-level matching.

All archived replay occurrences, including removed and originally linked rows,
participate in cardinality checks. A stale import link may be superseded only
when its replay occurrence independently maps to another original occurrence.
Clock models use independent users; uncertain clock regions remain protected.
No database writes are performed.
"""
import argparse
import bisect
import collections
import csv
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from src.db import danmaku_sources as store
from scripts.backfill_mit3uri_replay_danmaku import normalize_content
from scripts.review_mit3uri_danmaku_sessions import load_archived_replay, load_session, fit_model, predicted_offset, unambiguous_pairs
from scripts.audit_mit3uri_danmaku_integrity import iso, save_json


def safe_occurrences(options, uncertain, replay, baseline):
    """Keep singleton matches with no nearby uncalibrated competing occurrence."""
    accepted=unambiguous_pairs(options)
    for eid,bid in list(accepted.items()):
        if any(abs(replay[other]['mapped_timestamp_ms']/1000-baseline[bid]['timestamp'])<=600 for other in uncertain):
            del accepted[eid]
    # Within each recording part, a mapping cannot reverse send order.
    by_part=collections.defaultdict(list)
    for eid,bid in accepted.items():by_part[replay[eid]['cid']].append((replay[eid]['progress_ms'],eid,baseline[bid]['timestamp']))
    bad=set()
    for rows in by_part.values():
        rows.sort()
        for a,b in zip(rows,rows[1:]):
            if a[2]>b[2]:bad.update((a[1],b[1]))
    return {eid:bid for eid,bid in accepted.items() if eid not in bad}


def ordered_bracket(anchors, progress, uid, baseline):
    """Bracket one occurrence with six independent, ordered nearby messages.

The candidate's own user is excluded. Tight brackets permit changing replay
latency without extrapolation or assuming a constant affine clock.
"""
    before=[a for a in anchors if progress-60<=a[0]<progress and a[2]!=uid][-3:]
    after=[a for a in anchors if progress<a[0]<=progress+60 and a[2]!=uid][:3]
    if len(before)!=3 or len(after)!=3:return None
    aa=before+after
    if len({a[2] for a in aa})<4:return None
    times=[baseline[a[4]]['timestamp'] for a in aa]
    if any(a>=b for a,b in zip(times,times[1:])):return None
    if after[0][0]-before[-1][0]>30 or not 0<times[3]-times[2]<=20:return None
    return times[2]-1,times[3]+1,[dict(replay_event_id=a[3],baseline_event_id=a[4],uid=a[2],progress=a[0],timestamp=t) for a,t in zip(aa,times)]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--all-inserted',action='store_true',help='Also audit historical replay inserts outside the original candidate queue')
    parser.add_argument('--ordered-brackets',action='store_true',help='Allow tight monotonic independent-user brackets in variable-latency regions')
    args=parser.parse_args();args.output.mkdir(exist_ok=False)
    review=store.ROOT/'data/reports/remaining_danmaku_review_20261004_v2/candidates.csv'
    groups=collections.defaultdict(list)
    for row in csv.DictReader(review.open(encoding='utf-8-sig')):groups[row['session_id']].append(row)
    if args.all_inserted:
        with closing(sqlite3.connect(store.PRODUCTION_DB.as_uri()+'?mode=ro',uri=True)) as c:
            groups={sid:[] for (sid,) in c.execute("SELECT DISTINCT session_id FROM mit3uri_replay_danmaku_provenance WHERE match_kind='inserted' AND mapped_timestamp_ms<?",(store.EXTERNAL_CUTOFF_MS,))}
    meta={r['session_id']:r['live_date'] for r in json.loads((store.ROOT/'data/mit3uri_replay_danmaku/metadata.json').read_text())['recordings']}
    with closing(sqlite3.connect(store.ARCHIVE_DB.as_uri()+'?mode=ro',uri=True)) as c:
        removed,claimed=store.cleanup_state(c)
    counts=collections.Counter();sessions=[];used=set()
    with (args.output/'verified_pairs.jsonl').open('w') as out, (args.output/'decisions.jsonl').open('w') as decisions:
        for n,(sid,candidates) in enumerate(groups.items(),1):
            candidates=[r for r in candidates if int(r['extra_event_id']) not in removed]
            if not candidates and not args.all_inserted:continue
            archived=load_archived_replay(sid);times=[r['mapped_timestamp_ms'] for r in archived]
            live,baseline=load_session(store.PRODUCTION_DB,sid,min(times),max(times))
            if args.all_inserted:
                candidates=[dict(extra_event_id=r['event_id'],date=meta[sid]) for r in live if r['match_kind']=='inserted' and r['timestamp']<store.EXTERNAL_CUTOFF_MS/1000 and r['event_id'] not in removed]
            alive={r['event_id'] for r in live};base={r['id']:r for r in baseline}
            left=collections.defaultdict(list);right=collections.defaultdict(list);byid={}
            for r in archived:
                left[(r['uid'],normalize_content(r['content']))].append(r);byid[r['event_id']]=r
            for b in baseline:right[(b['uid'],normalize_content(b['content']))].append(b)
            anchors=collections.defaultdict(list)
            for key,rs in left.items():
                bs=right.get(key,[])
                if len(rs)==len(bs)==1 and len(key[1])>=6 and not key[1].startswith('['):
                    r,b=rs[0],bs[0];delta=r['mapped_timestamp_ms']/1000-b['timestamp']
                    if abs(delta)<=600:anchors[r['cid']].append((r['progress_ms']/1000,delta,key[0],r['event_id'],b['id']))
            for aa in anchors.values():aa.sort()
            positions={cid:[a[0] for a in aa] for cid,aa in anchors.items()}
            cache={};fitted={};group_cache={};local_counts=collections.Counter()
            def clock(r):
                cid=r['cid'];uid=r['uid'];p=r['progress_ms']/1000
                gkey=(cid,uid,'global')
                if gkey not in cache:cache[gkey]=fit_model([a for a in anchors[cid] if a[2]!=uid],max_residual=4.5)
                model=cache[gkey]
                if predicted_offset(model,p) is not None:return model
                for radius in (450,300,150,120,90,60,45):
                    center=round(p/10)*10;key=(cid,uid,center,radius)
                    if key not in cache:
                        aa=anchors[cid];pp=positions.get(cid,[])
                        subset=[a for a in aa[bisect.bisect_left(pp,center-radius):bisect.bisect_right(pp,center+radius)] if a[2]!=uid]
                        cache[key]=fit_model(subset,local=True,max_residual=4.5) if sum(a[0]<p for a in subset)>=3 and sum(a[0]>p for a in subset)>=3 else None
                    model=cache[key]
                    if predicted_offset(model,p) is not None:return model
                return None
            for candidate in candidates:
                eid=int(candidate['extra_event_id']);r=byid.get(eid);status='unresolved'
                if not r or eid not in alive:status='absent'
                elif r['mapped_timestamp_ms']>=store.EXTERNAL_CUTOFF_MS:status='protected'
                else:
                    key=(r['uid'],normalize_content(r['content']))
                    if not any(abs(b['timestamp']-r['mapped_timestamp_ms']/1000)<=600 for b in right.get(key,[])):
                        status='no_near_original_occurrence'
                        local_counts[status]+=1
                        decisions.write(json.dumps(dict(event_id=eid,session_id=sid,status=status))+'\n')
                        continue
                    if key not in group_cache:
                        options={};uncertain=[]
                        for occurrence in left[key]:
                            oid=occurrence['event_id'];model=clock(occurrence);offset=predicted_offset(model,occurrence['progress_ms']/1000)
                            if offset is None:
                                bracket=ordered_bracket(anchors[occurrence['cid']],occurrence['progress_ms']/1000,occurrence['uid'],base) if args.ordered_brackets else None
                                if bracket is None:uncertain.append(oid);continue
                                low,high,proof=bracket
                                options[oid]=[b['id'] for b in right[key] if low<=b['timestamp']<=high]
                                fitted[oid]=(None,dict(tolerance=None,validation=6,validation_share=1.0,ordered_bracket=proof))
                                continue
                            calibrated=occurrence['mapped_timestamp_ms']/1000-offset
                            options[oid]=[b['id'] for b in right[key] if abs(b['timestamp']-calibrated)<=model['tolerance']]
                            fitted[oid]=(offset,model)
                        group_cache[key]=(safe_occurrences(options,uncertain,byid,base),options,uncertain)
                    accepted,options,uncertain=group_cache[key];bid=accepted.get(eid)
                    if bid is None:status='uncertain_clock_or_occurrence'
                    elif bid in claimed:status='prior_cleanup_claim'
                    elif bid in used:status='cross_session_claim'
                    elif base[bid]['timestamp']>=store.EXTERNAL_CUTOFF_MS/1000:status='protected'
                    elif base[bid]['match_kind']=='existing' and (bid not in accepted or accepted[bid]==bid):status='unproven_old_link_reassignment'
                    else:
                        status='verified';used.add(bid);offset,model=fitted[eid];b=base[bid]
                        item=dict(session_id=sid,live_date=candidate['date'],extra_event_id=eid,baseline_event_id=bid,uid=r['uid'],content=r['content'],baseline_time=iso(b['timestamp']),replay_time=iso(r['timestamp']),offset_seconds=offset,tolerance_seconds=model['tolerance'],validation_anchors=model['validation'],validation_share=model['validation_share'],occurrence_assignments=accepted,uncalibrated_occurrences=uncertain,old_link_reassigned=base[bid]['match_kind']=='existing',evidence_method='complete_archived_occurrences_independent_user_clocks',ordered_bracket=model.get('ordered_bracket'))
                        out.write(json.dumps(item,ensure_ascii=False)+'\n');out.flush()
                local_counts[status]+=1
                decisions.write(json.dumps(dict(event_id=eid,session_id=sid,status=status),ensure_ascii=False)+'\n')
            counts.update(local_counts);sessions.append(dict(session_id=sid,counts=dict(local_counts)))
            print(f'{n}/{len(groups)} {sid} {dict(local_counts)} total={dict(counts)}',flush=True)
            save_json(args.output/'progress.json',dict(completed=False,counts=dict(counts),sessions=sessions))
    save_json(args.output/'summary.json',dict(completed=True,event_mutated=False,counts=dict(counts),sessions=sessions))


if __name__=='__main__':main()
