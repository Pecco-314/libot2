"""Review remaining June 6 candidates using independently checked video timing.

Local message clocks require independent-user validation. The mirror title and
audio alignment constrain the video clock; message appearance latency remains
a separate quantity. This script only emits evidence for the cleanup planner.
"""
import collections
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from src.db import danmaku_sources as store
from scripts.backfill_mit3uri_replay_danmaku import normalize_content
from scripts.review_mit3uri_danmaku_sessions import load_archived_replay, load_session, fit_model, predicted_offset, unambiguous_pairs
from scripts.audit_mit3uri_danmaku_integrity import iso, save_json


def main():
    output=Path('data/reports/title_clock_neighbors_20261004')
    output.mkdir(exist_ok=False)
    sid='MIT3URI-0151';cid=30360732428
    audio=json.loads(Path('data/reports/title_clock_trial_20261004/audio_clock_results.json').read_text())['results']
    if len(audio)!=2 or not all(r['accepted'] for r in audio) or abs(audio[0]['mirror_minus_official_seconds']-audio[1]['mirror_minus_official_seconds'])>.2:
        raise ValueError('Independent audio windows do not agree')
    archived=load_archived_replay(sid)
    times=[r['mapped_timestamp_ms'] for r in archived]
    live,baseline=load_session(store.PRODUCTION_DB,sid,min(times),max(times))
    alive={r['event_id'] for r in live}
    with closing(sqlite3.connect(store.ARCHIVE_DB.as_uri()+'?mode=ro',uri=True)) as c:
        removed,claimed=store.cleanup_state(c)
    replay=[r for r in archived if r['cid']==cid]
    left=collections.defaultdict(list);right=collections.defaultdict(list)
    for r in replay:left[(r['uid'],normalize_content(r['content']))].append(r)
    for r in baseline:right[(r['uid'],normalize_content(r['content']))].append(r)
    anchors=[]
    for key,rs in left.items():
        bs=right.get(key,[])
        if len(rs)==len(bs)==1 and len(key[1])>=6 and not key[1].startswith('['):
            r,b=rs[0],bs[0];delta=r['mapped_timestamp_ms']/1000-b['timestamp']
            if abs(delta)<=600:anchors.append((r['progress_ms']/1000,delta,key[0],r['event_id'],b['id']))
    reserved=claimed|{b['id'] for b in baseline if b['match_kind']=='existing'}
    options={};evidence={};counts=collections.Counter();details=[]
    for r in replay:
        if r['event_id'] not in alive or r['event_id'] in removed or r['match_kind']!='inserted':continue
        progress=r['progress_ms']/1000
        # Audio checks bracket this interval; no extrapolation to other parts.
        if not 1380<=progress<=1760:continue
        model=None
        for radius in (120,90,60,45):
            aa=[a for a in anchors if abs(a[0]-progress)<=radius and a[2]!=r['uid']]
            if sum(a[0]<progress for a in aa)<3 or sum(a[0]>progress for a in aa)<3:continue
            model=fit_model(aa,local=True,max_residual=4.5)
            if model:break
        offset=predicted_offset(model,progress)
        if offset is None:counts['no_local_clock']+=1;continue
        calibrated=r['mapped_timestamp_ms']/1000-offset
        near=[b for b in right.get((r['uid'],normalize_content(r['content'])),[]) if abs(b['timestamp']-calibrated)<=model['tolerance']]
        detail=dict(event_id=r['event_id'],content=r['content'],progress_seconds=progress,offset_seconds=offset,calibrated_time=iso(calibrated),model=model,candidate_baselines=[b['id'] for b in near])
        details.append(detail)
        if any(b['id'] in reserved for b in near):counts['reserved_occurrence']+=1;continue
        options[r['event_id']]=[b['id'] for b in near]
        evidence[r['event_id']]=(r,offset,model)
    accepted=unambiguous_pairs(options);by_id={b['id']:b for b in baseline}
    with (output/'verified_pairs.jsonl').open('w') as f:
        for eid,bid in accepted.items():
            r,offset,model=evidence[eid];b=by_id[bid]
            item=dict(session_id=sid,live_date='2025-06-06',extra_event_id=eid,baseline_event_id=bid,uid=r['uid'],content=r['content'],baseline_time=iso(b['timestamp']),replay_time=iso(r['timestamp']),offset_seconds=offset,tolerance_seconds=model['tolerance'],validation_anchors=model['validation'],validation_share=model['validation_share'],evidence='title_clock_trial_20261004 + independent_user_local_neighbors')
            f.write(json.dumps(item,ensure_ascii=False)+'\n')
    save_json(output/'details.json',details)
    counts['verified_pairs']=len(accepted)
    save_json(output/'summary.json',dict(event_mutated=False,scope='June 6 official P2, 1380–1760 seconds',counts=dict(counts)))
    print(dict(counts))


if __name__=='__main__':main()
