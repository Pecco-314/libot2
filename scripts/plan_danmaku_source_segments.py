"""Read-only source selection by historical live and continuous coverage segment.

Coverage is relative to observed sources, not proof of absolute completeness.
All-source silence, uncertain clocks and partial deficits require review.
Only DANMU_MSG is in scope; financial events and source evidence are retained.
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from scripts.audit_mit3uri_danmaku_integrity import iso, save_csv, save_json
from scripts.backfill_mit3uri_replay_danmaku import ROOM_ID, normalize_content
from scripts.review_mit3uri_danmaku_sessions import fit_model, predicted_offset
from src.db import danmaku_sources as store

PRIORITY = ('monitor', 'danmakus', 'vtbcat', 'replay')
CELL = 120


def priority(candidate):
    return PRIORITY.index(candidate.split(':',1)[0])


def map_replay_sessions(metadata, recording_sessions):
    targets=collections.defaultdict(set)
    for rec in metadata:
        if rec['bvid'] in recording_sessions:targets[rec['session_id']].add(recording_sessions[rec['bvid']])
    return ({old:next(iter(values)) for old,values in targets.items() if len(values)==1},
            {old:sorted(values) for old,values in targets.items() if len(values)!=1})


def histogram(rows, start, end):
    result = collections.Counter()
    for r in rows:
        if start <= r['time'] < end:
            result[int((r['time'] - start) // CELL)] += 1
    return result


def choose_primary(counts):
    """Prefer near-best coverage, then source priority; raw volume alone is insufficient."""
    active = {i for values in counts.values() for i, n in values.items() if n >= 5}
    if not active:
        return next((s for s in sorted(counts,key=priority) if sum(counts[s].values())), None)
    scores = {s: sum(values.get(i, 0) >= .85 * max(v.get(i, 0) for v in counts.values())
                     for i in active) for s, values in counts.items()}
    best = max(scores.values())
    eligible=[s for s in scores if scores[s]>=.98*best]
    return min(eligible,key=lambda s:(priority(s),-scores[s],-sum(counts[s].values()),s))


def segments(counts, primary, start, end):
    cells = []
    for i in range(math.ceil((end-start)/CELL)):
        observed = {s: v.get(i, 0) for s, v in counts.items()}
        maximum = max(observed.values(), default=0)
        own = observed.get(primary, 0)
        selected = primary
        if maximum == 0:
            selected, status = None, 'all_sources_silent_unknown'
        elif maximum < 5:
            status = 'sparse_review'
        elif own >= .85*maximum:
            status = 'primary'
        elif own == 0:
            selected = min((s for s in observed if observed[s]>=.85*maximum),key=lambda s:(priority(s),-observed[s],s))
            status = 'gap_candidate'
        else:
            status = 'partial_deficit_review'
        cells.append(dict(cell=i, source=selected, status=status, counts=observed))
    # A single empty cell can be a clock/boundary effect. Require sustained absence.
    for i, cell in enumerate(cells):
        if cell['status'] != 'gap_candidate':
            continue
        supported = any(0 <= j < len(cells) and cells[j]['status'] in ('gap_candidate','gap_fill')
                        and cells[j]['source'] == cell['source'] for j in (i-1,i+1))
        cell['status'] = 'gap_fill' if supported else 'short_gap_review'
    out = []
    for c in cells:
        if not out or (out[-1]['source'],out[-1]['status']) != (c['source'],c['status']):
            out.append(dict(start=start+c['cell']*CELL, end=min(end,start+(c['cell']+1)*CELL),
                            source=c['source'],status=c['status'],counts=collections.Counter(c['counts'])))
        else:
            out[-1]['end'] = min(end,start+(c['cell']+1)*CELL)
            out[-1]['counts'].update(c['counts'])
    return out


def align(rows, primary, start):
    """Use independent users to validate cross-source time offsets; never rewrite raw times."""
    base = collections.defaultdict(list)
    streams = collections.defaultdict(list)
    for r in rows:
        key = (r['uid'],normalize_content(r['content']))
        r['key'] = key
        if r['candidate'] == primary:
            base[key].append(r)
        elif r['source'] in PRIORITY:
            streams[(r['source'],r['stream'])].append(r)
    reports = []
    for (source,stream), values in streams.items():
        grouped = collections.defaultdict(list)
        for r in values:grouped[r['key']].append(r)
        anchors=[]
        for key, items in grouped.items():
            other=base.get(key,[])
            if key[0] and len(key[1])>=6 and not key[1].startswith('[') and len(items)==len(other)==1:
                r,b=items[0],other[0]
                if abs(r['time']-b['time'])<=600:
                    anchors.append((r['time']-start,r['time']-b['time'],key[0],r['id'],b['id']))
        model=fit_model(anchors,max_residual=4.5)
        supported=0
        for r in values:
            offset=predicted_offset(model,r['time']-start)
            if offset is not None:
                r['time']-=offset
                supported+=1
        reports.append(dict(source=source,stream=stream,rows=len(values),anchors=len(anchors),
                            aligned_rows=supported,clock_validated=model is not None,
                            offset_at_start=model['intercept'] if model else None,
                            drift=model['slope'] if model else None,
                            validation_share=model['validation_share'] if model else None))
    return reports


def classify(counts,primary,plan,overlap):
    available=sum(sum(v.values())>0 for v in counts.values())
    if not available:return 'no_eligible_source'
    if overlap:return 'session_boundary_review'
    if available==1:return 'single_available_source'
    active={i for v in counts.values() for i,n in v.items() if n>=5}
    covered=sum(counts.get(primary,{}).get(i,0)>=.85*max(v.get(i,0) for v in counts.values()) for i in active)
    if active and covered/len(active)>=.95 and not any(p['status']=='gap_fill' for p in plan):
        return 'dominant_primary'
    if any(p['status']=='gap_fill' for p in plan):return 'needs_gap_splice'
    return 'partial_coverage_review'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--session')
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    locks=json.loads((store.ROOT/'data/mit3uri_replay_danmaku/alignment_locks.json').read_text())['sessions']
    metadata=json.loads((store.ROOT/'data/mit3uri_replay_danmaku/metadata.json').read_text())['recordings']
    with closing(sqlite3.connect(store.PRODUCTION_DB.as_uri()+'?mode=ro',uri=True)) as live:
        live.row_factory=sqlite3.Row
        sessions=[dict(r) for r in live.execute('SELECT * FROM mit3uri_replay_session WHERE included_in_total=1 AND start_timestamp<? ORDER BY start_timestamp', (store.EXTERNAL_CUTOFF_MS//1000,))]
        recording_sessions=dict(live.execute('SELECT bvid,session_id FROM mit3uri_replay_recording'))
    mapping,ambiguous=map_replay_sessions(metadata,recording_sessions)
    reverse=collections.defaultdict(list)
    for old,new in mapping.items():reverse[new].append(old)
    save_json(args.output/'session_mapping.json',dict(old_to_current=mapping,ambiguous=ambiguous))
    for s in sessions:
        s['start']=s['start_timestamp']
        # Sequential IDs are catalog-version-local. Lock lookup follows BVID identity.
        candidates=[locks[o]['base_ms']/1000 for o in reverse[s['session_id']] if o in locks and locks[o].get('live_date')==s['live_date']]
        if s['start_time_precision']!='second' and candidates and abs(min(candidates)-s['start'])<3600:
            s['start']=min(candidates)
        s['end']=min(store.EXTERNAL_CUTOFF_MS/1000,s['start']+(s['duration_seconds'] or 0))
    reports=[];intervals=[];clocks=[]
    with closing(sqlite3.connect(store.ARCHIVE_DB.as_uri()+'?mode=ro',uri=True)) as cold:
        cold.execute('PRAGMA cache_size=-32768')
        cold.row_factory=sqlite3.Row
        for n,s in enumerate(sessions,1):
            sid=s['session_id'];start=s['start'];end=s['end']
            if args.session and sid!=args.session:continue
            if end<=start:continue
            found=cold.execute("SELECT id,source,source_session,uid,content,source_timestamp_ms,mapped_timestamp_ms FROM danmaku_source_record WHERE room_id=? AND source_timestamp_ms>=? AND source_timestamp_ms<? AND source!='replay'", (ROOM_ID,(start-600)*1000,(end+600)*1000)).fetchall()
            for old_sid in reverse[sid]:
                found+=cold.execute("SELECT id,source,source_session,uid,content,source_timestamp_ms,mapped_timestamp_ms FROM danmaku_source_record WHERE source='replay' AND source_session=? AND eligibility='live'", (old_sid,)).fetchall()
            rows=[]
            for r in found:
                ts=r['mapped_timestamp_ms'] if r['source']=='replay' else r['source_timestamp_ms']
                if ts is None or ts>=store.EXTERNAL_CUTOFF_MS:continue
                rows.append(dict(id=r['id'],source=r['source'],stream=r['source_session'],candidate=r['source']+':'+r['source_session'],uid=r['uid'],content=r['content'],time=ts/1000))
            grouped=collections.defaultdict(list)
            for r in rows:
                if r['source'] in PRIORITY:grouped[r['candidate']].append(r)
            counts={source:histogram(rr,start,end) for source,rr in grouped.items() if rr}
            primary=choose_primary(counts)
            timing=align(rows,primary,start) if primary else []
            counts={source:histogram(rr,start,end) for source,rr in grouped.items() if rr}
            plan=segments(counts,primary,start,end)
            overlap=[o['session_id'] for o in sessions if o['session_id']!=sid and min(end,o['end'])-max(start,o['start'])>60]
            row=dict(session_id=sid,date=s['live_date'],start=iso(start),end=iso(end),primary=primary,
                     primary_rows=sum(counts.get(primary,{}).values()),
                     **{source:sum(sum(v.values()) for k,v in counts.items() if k.split(':',1)[0]==source) for source in PRIORITY},
                     legacy_unknown=sum(start<=r['time']<end for r in rows if r['source']=='legacy_unknown'),
                     gap_fill_segments=sum(p['status']=='gap_fill' for p in plan),
                     review_seconds=sum(p['end']-p['start'] for p in plan if p['status'] not in ('primary','gap_fill')),
                     unvalidated_streams=sum(not t['clock_validated'] for t in timing),
                     overlapping_sessions=';'.join(overlap),
                     decision=classify(counts,primary,plan,overlap))
            reports.append(row)
            for p in plan:
                intervals.append(dict(session_id=sid,start=iso(p['start']),end=iso(p['end']),source=p['source'],status=p['status'],candidate_counts=json.dumps(p['counts'],ensure_ascii=False)))
            clocks.extend(dict(session_id=sid,**t) for t in timing)
            if n%10==0 or args.session:
                print(n,len(sessions),sid,primary,row['decision'],flush=True)
            time.sleep(.02)
    save_csv(args.output/'sessions.csv',reports)
    save_csv(args.output/'segments.csv',intervals)
    save_csv(args.output/'clock_checks.csv',clocks)
    summary=dict(production_mutated=False,scope='Historical DANMU_MSG; gifts/guards/SC unchanged',cutoff_exclusive=iso(store.EXTERNAL_CUTOFF_MS/1000),
                 priority=PRIORITY,bin_seconds=CELL,completeness='relative observed coverage only',
                 sessions=len(reports),primary_counts=dict(collections.Counter(r['primary'].split(':',1)[0] if r['primary'] else None for r in reports)),
                 decisions=dict(collections.Counter(r['decision'] for r in reports)),
                 segment_statuses=dict(collections.Counter(r['status'] for r in intervals)),
                 affected_0327=[r for r in reports if r['date']=='2026-03-27'])
    save_json(args.output/'summary.json',summary)
    print(json.dumps(summary,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':main()
