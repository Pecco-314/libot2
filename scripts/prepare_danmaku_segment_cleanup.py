"""Intersect validated replay duplicate pairs with primary-source coverage.

Writes a cold deletion proposal with complete event/provenance snapshots.
Both databases are opened read-only. This tool never applies deletions.
"""
import argparse
import collections
import csv
import json
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path

from scripts.audit_mit3uri_danmaku_integrity import save_json
from scripts.backfill_mit3uri_replay_danmaku import ROOM_ID,normalize_content
from src.db import danmaku_sources as store


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--coverage',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--pairs',type=Path,default=store.ROOT/'data/reports/danmaku_sessions_review_archived_20261003/verified_pairs.jsonl')
    p.add_argument('--allow-evidenced-secondary',action='store_true')
    args=p.parse_args()
    if (args.coverage/'INVALIDATED.txt').exists():p.error('Coverage report was invalidated')
    args.output.mkdir(parents=True,exist_ok=False)
    mapping=json.loads((args.coverage/'session_mapping.json').read_text())['old_to_current']
    spans=collections.defaultdict(list)
    for row in csv.DictReader((args.coverage/'segments.csv').open(encoding='utf-8-sig')):
        if row['status']=='primary' and row['source'] and not row['source'].startswith('replay:'):
            spans[row['session_id']].append((datetime.fromisoformat(row['start']).timestamp(),datetime.fromisoformat(row['end']).timestamp(),row['source']))
    by_session=collections.defaultdict(list)
    path=args.pairs
    unmapped=0
    for line in path.open():
        row=json.loads(line)
        if row['session_id'] in mapping:by_session[mapping[row['session_id']]].append(row)
        else:unmapped+=1
    counts=collections.Counter(held_unmapped_session=unmapped);per_session=[];keeps=set();drops=set()
    with closing(sqlite3.connect(store.ARCHIVE_DB.as_uri()+'?mode=ro',uri=True)) as cold, closing(sqlite3.connect(store.PRODUCTION_DB.as_uri()+'?mode=ro',uri=True)) as live, (args.output/'proposals.jsonl').open('w') as out:
        live.row_factory=sqlite3.Row
        already_removed,claimed_baselines=store.cleanup_state(cold)
        for n,(sid,pairs) in enumerate(by_session.items(),1):
            counts['already_removed']+=sum(p['extra_event_id'] in already_removed for p in pairs)
            pairs=[p for p in pairs if p['extra_event_id'] not in already_removed]
            if not pairs:continue
            secondary={}
            if args.allow_evidenced_secondary:
                by_base={p['baseline_event_id']:p for p in pairs}
                base_ids=list(by_base)
                for start in range(0,len(base_ids),400):
                    batch=base_ids[start:start+400];marks=','.join('?' for _ in batch)
                    for eid,source,stream,uid,content,ts in cold.execute(f"SELECT l.event_id,r.source,r.source_session,r.uid,r.content,r.source_timestamp_ms FROM danmaku_source_link l JOIN danmaku_source_record r ON r.id=l.source_id WHERE l.event_id IN ({marks}) AND r.source IN ('monitor','danmakus','vtbcat') AND r.eligibility='live'",batch):
                        pair=by_base[eid];bt=datetime.fromisoformat(pair['baseline_time']).timestamp()
                        if ts is not None and ts<store.EXTERNAL_CUTOFF_MS and ts//1000==bt and uid==pair['uid'] and normalize_content(content)==normalize_content(pair['content']):
                            secondary.setdefault(eid,source+':'+stream)
            linked={}
            for _,_,candidate in spans[sid]:
                if candidate in linked:continue
                source,stream=candidate.split(':',1)
                linked[candidate]={eid for (eid,) in cold.execute('SELECT l.event_id FROM danmaku_source_record r JOIN danmaku_source_link l ON l.source_id=r.id WHERE r.source=? AND r.source_session=?',(source,stream))}
            chosen=[]
            for pair in pairs:
                if pair['baseline_event_id'] in claimed_baselines:
                    counts['held_previously_claimed_baseline']+=1;continue
                ts=datetime.fromisoformat(pair['baseline_time']).timestamp()
                candidate=next((c for a,b,c in spans[sid] if a<=ts<b and pair['baseline_event_id'] in linked[c]),None)
                mode='selected_primary'
                if candidate is None and pair['baseline_event_id'] in secondary:
                    candidate=secondary[pair['baseline_event_id']];mode='verified_secondary'
                if candidate is None:
                    counts['held_no_selected_primary_link']+=1;continue
                chosen.append((pair,candidate,mode))
            ids={p[k] for p,_,_ in chosen for k in ('baseline_event_id','extra_event_id')}
            current={};provenance={}
            ids=sorted(ids)
            for start in range(0,len(ids),500):
                batch=ids[start:start+500];marks=','.join('?' for _ in batch)
                current.update((r['id'],dict(r)) for r in live.execute(f'SELECT * FROM event WHERE id IN ({marks})',batch))
                for r in live.execute(f'SELECT * FROM mit3uri_replay_danmaku_provenance WHERE event_id IN ({marks})',batch):
                    provenance.setdefault(r['event_id'],[]).append(dict(r))
            accepted=0
            for pair,candidate,mode in chosen:
                bid=pair['baseline_event_id'];eid=pair['extra_event_id']
                base=current.get(bid);extra=current.get(eid);prov=provenance.get(eid,[])
                valid=base and extra and len(prov)==1 and prov[0]['match_kind']=='inserted' and prov[0]['session_id']==pair['session_id']
                valid=valid and all(r['cmd']=='DANMU_MSG' and r['room_id']==ROOM_ID and r['timestamp']<store.EXTERNAL_CUTOFF_MS//1000 and r['uid']==pair['uid'] and normalize_content(r['content'])==normalize_content(pair['content']) for r in (base,extra))
                valid=valid and base['timestamp']==datetime.fromisoformat(pair['baseline_time']).timestamp() and extra['timestamp']==datetime.fromisoformat(pair['replay_time']).timestamp()
                if not valid:counts['held_changed_or_invalid_event']+=1;continue
                if eid in drops or bid in keeps or eid in keeps or bid in drops:
                    counts['held_conflicting_pair']+=1;continue
                drops.add(eid);keeps.add(bid);accepted+=1
                counts[mode]+=1
                out.write(json.dumps(dict(selected_primary=candidate,selection_mode=mode,evidence=pair,keep_event=base,remove_event=extra,remove_provenance=prov),ensure_ascii=False)+'\n')
            counts['proposed_removals']+=accepted
            per_session.append(dict(session_id=sid,verified_pairs=len(pairs),proposed_removals=accepted))
            if n%20==0:print(n,len(by_session),dict(counts),flush=True)
            time.sleep(.03)
    summary=dict(production_mutated=False,scope='Validated replay duplicates with selected primary or explicitly allowed exact live-source evidence',allow_evidenced_secondary=args.allow_evidenced_secondary,cutoff_ms=store.EXTERNAL_CUTOFF_MS,pairs_file=str(path),counts=dict(counts),sessions=per_session)
    save_json(args.output/'summary.json',summary)
    print(json.dumps(counts,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
