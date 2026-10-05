"""Inventory mirror per-part title clocks for remaining historical candidates.

Coverage locates a potential mirror clip, not a duplicate event. Independent
audio/visual alignment and message occurrence checks are still required.
"""
import asyncio
import collections
import csv
import json
import re
from datetime import datetime
from pathlib import Path

import httpx

from scripts.backfill_mit3uri_replay_danmaku import HEADERS, TIMEZONE, exact_recording_title_timestamp, load_secondary_view
from scripts.audit_mit3uri_danmaku_integrity import save_json,save_csv

ROOT=Path('data/reports/remaining_title_evidence_20261004_v2')
CACHE=Path('data/reports/remaining_title_evidence_20261004/views')


def part_clock(title, recording_title):
    exact=exact_recording_title_timestamp(title)
    if exact is not None:return exact,0
    year=re.search(r'(20\d{2})[年-]',recording_title)
    match=re.search(r'(\d{1,2})月(\d{1,2})日(\d{1,2})点(\d{1,2})分',title)
    if year and match:
        month,day,hour,minute=map(int,match.groups())
        return int(datetime(int(year.group(1)),month,day,hour,minute,tzinfo=TIMEZONE).timestamp()),60
    return None,None


async def main():
    ROOT.mkdir(exist_ok=True)
    rows=list(csv.DictReader(open('data/reports/remaining_danmaku_review_20261004_final/candidates.csv',encoding='utf-8-sig')))
    catalog=json.load(open('data/mit3uri_replay_catalog.json'))
    bybv={r['bvid']:s for s in catalog['sessions'] for r in s['recordings']}
    mirrors={};row_mirrors={}
    for r in rows:
        options=[v for v in bybv.get(r['bvid'],{}).get('recordings',[]) if v['source_mid']==1702090167]
        row_mirrors[r['extra_event_id']]=[v['bvid'] for v in options]
        mirrors.update((v['bvid'],v) for v in options)
    spans={};recordings=[]
    async with httpx.AsyncClient(headers=HEADERS,timeout=20,trust_env=False) as client:
        for n,(bv,rec) in enumerate(mirrors.items(),1):
            cache_path=CACHE/f'{bv}.json';cached=cache_path.exists()
            view=await load_secondary_view(client,bvid=bv,cache_path=cache_path,refresh=False)
            parts=[]
            for p in view['pages']:
                timestamp,uncertainty=part_clock(p['part'],rec['title'])
                parts.append(dict(page=p['page'],cid=p['cid'],title=p['part'],start_timestamp=timestamp,uncertainty_seconds=uncertainty,duration=p['duration']))
            spans[bv]=parts
            recordings.append(dict(bvid=bv,title=rec['title'],top_title_timestamp=exact_recording_title_timestamp(rec['title']),pages=parts))
            print(f'{n}/{len(mirrors)} {bv} exact_parts={sum(p["start_timestamp"] is not None for p in parts)}/{len(parts)}',flush=True)
            if not cached:await asyncio.sleep(.75)
    counts=collections.Counter();sessions=collections.defaultdict(collections.Counter);details=[]
    for r in rows:
        options=row_mirrors[r['extra_event_id']]
        known=any(p['start_timestamp'] is not None for bv in options for p in spans[bv])
        matches=[]
        ts=datetime.fromisoformat(r['baseline_time']).timestamp()
        for bv in options:
            for p in spans[bv]:
                t=p['start_timestamp']
                if t is not None and t<=ts<t+p['duration']+p['uncertainty_seconds']:
                    matches.append(dict(bvid=bv,page=p['page'],cid=p['cid'],progress_seconds_max=round(ts-t,3),progress_seconds_min=max(0,round(ts-t-p['uncertainty_seconds'],3)),precision='second' if p['uncertainty_seconds']==0 else 'minute',url=f'https://www.bilibili.com/video/{bv}?p={p["page"]}&t={max(0,int(ts-t-p["uncertainty_seconds"]))}'))
        status='no_mirror_in_catalog' if not options else 'no_parseable_part_title' if not known else 'second_title_covers_original_time' if any(m['precision']=='second' for m in matches) else 'minute_title_covers_original_time' if matches else 'original_time_outside_known_parts'
        counts[status]+=1;sessions[(r['date'],r['session_id'])][status]+=1
        details.append(dict(**r,title_evidence=status,mirror_bvids=','.join(options),mirror_locations=json.dumps(matches,ensure_ascii=False)))
    save_json(ROOT/'recordings.json',recordings)
    save_csv(ROOT/'candidates.csv',details)
    save_csv(ROOT/'sessions.csv',[dict(date=d,session_id=sid,total=sum(c.values()),**{k:c[k] for k in counts}) for (d,sid),c in sorted(sessions.items(),key=lambda x:-sum(x[1].values()))])
    result=dict(event_mutated=False,total=len(rows),sessions=len(sessions),mirror_recordings=len(mirrors),counts=dict(counts),minute_windows_assume_title_minutes_are_truncated=True,coverage_is_not_duplicate_confirmation=True,reasons_by_coverage={k:dict(collections.Counter(r['reason'] for r in details if r['title_evidence']==k)) for k in counts})
    save_json(ROOT/'summary.json',result)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':asyncio.run(main())
