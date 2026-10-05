"""Inventory historical gift/guard/SC evidence without changing event.

Counts are raw source records, not unique transactions or revenue. VTB pages
can be retained as compressed cold artifacts. Conflicting provider prices are
reported and never resolved by guessing.
"""
import argparse
import collections
import json
import sqlite3
import time
import zlib
from contextlib import closing
from datetime import datetime
from pathlib import Path

import msgpack

from src.db import danmaku_sources as store
from scripts.backfill_mit3uri_replay_danmaku import ROOM_ID
from scripts.audit_mit3uri_danmaku_integrity import save_json,iso

KINDS={1:'gift',2:'guard',3:'sc'}
COMMANDS={'gift':'SEND_GIFT','guard':'GUARD_BUY','sc':'SUPER_CHAT_MESSAGE'}


def packed_inventory(payload):
    frame=msgpack.unpackb(payload,raw=False,strict_map_key=False)
    if frame[0][2]!=ROOM_ID:raise ValueError('Wrong room')
    current,actors,emojis,records=frame[2]
    counts=collections.Counter();samples={}
    for record in records:
        current+=record[0]
        if not 0<current<store.EXTERNAL_CUTOFF_MS or record[1] not in KINDS:continue
        kind=KINDS[record[1]];counts[kind]+=1
        if kind not in samples:samples[kind]=dict(timestamp_ms=current,payload=record[5])
    return counts,samples


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--archive-vtb',action='store_true')
    args=p.parse_args()
    report=dict(cutoff_exclusive=iso(store.EXTERNAL_CUTOFF_MS/1000),event_mutated=False,counts_are_raw_not_deduplicated=True)
    totals=collections.Counter();sessions=collections.defaultdict(collections.Counter);examples={};formats=collections.Counter()
    with closing(sqlite3.connect(store.ARCHIVE_DB.as_uri()+'?mode=ro',uri=True)) as c:
        artifacts=c.execute("SELECT id,locator FROM danmaku_source_artifact WHERE source='danmakus' AND locator!='catalog'").fetchall()
        for n,(aid,locator) in enumerate(artifacts,1):
            compressed=c.execute('SELECT payload FROM danmaku_source_artifact WHERE id=?',(aid,)).fetchone()[0]
            payload=zlib.decompress(compressed)
            if locator.endswith('/full'):
                counts,samples=packed_inventory(payload);formats['full']+=1
            else:
                page=json.loads(payload);counts=collections.Counter();samples={};formats['paged']+=1
                for r in page['data']['frame']['records']:
                    if r.get('type') in KINDS and 0<r.get('ts',0)<store.EXTERNAL_CUTOFF_MS:
                        kind=KINDS[r['type']];counts[kind]+=1;samples.setdefault(kind,dict(timestamp_ms=r['ts'],payload=r.get('payload')))
            totals.update(counts);sessions[locator.split('/')[0]].update(counts)
            for k,v in samples.items():examples.setdefault(k,dict(artifact_id=aid,**v))
            if n%50==0:print('danmakus',n,len(artifacts),dict(totals),flush=True)
    report['danmakus']=dict(counts=dict(totals),formats=dict(formats),examples=examples,lives_with_events=sum(bool(sum(v.values())) for v in sessions.values()),streams=[dict(source_session=sid,counts=dict(counts)) for sid,counts in sorted(sessions.items())])
    totals=collections.Counter();pages=collections.defaultdict(set);expected={};price_conflicts=[];archived=0;vtb_sessions=collections.defaultdict(collections.Counter)
    root=store.ROOT/'data/vtb_cat/2030198123/lives'
    with closing(store.connect_archive()) as cold:
        for kind in COMMANDS:
            for path in sorted(root.glob(f'*/{kind}_*.json')):
                payload=path.read_bytes();page=json.loads(payload);records=[]
                for r in page.get('records',[]):
                    ts=int(datetime.fromisoformat(r['CreatedAt']).timestamp()*1000)
                    if int(r['LiveRoom'])!=ROOM_ID:raise ValueError('Wrong VTB room')
                    if 0<ts<store.EXTERNAL_CUTOFF_MS:records.append(r)
                if not records:continue
                key=(path.parent.name,kind);pages[key].add(int(page['page']));expected[key]=int(page['totalPages'])
                totals[kind]+=len(records)
                vtb_sessions[path.parent.name][kind]+=len(records)
                if args.archive_vtb:
                    with cold:store.artifact(cold,'vtbcat',f'{path.parent.name}/{path.name}',payload,dict(event_kind=kind,cutoff_ms=store.EXTERNAL_CUTOFF_MS,source_file=str(path)))
                    archived+=1
                for r in records:
                    if kind=='guard' and str(r.get('Extra','')).startswith('{'):
                        raw=json.loads(r['Extra']).get('data',{});display=r.get('GiftPrice',{}).get('Float64')
                        if display is not None and raw.get('price') and abs(display*1000-raw['price'])>.01 and len(price_conflicts)<10:
                            price_conflicts.append(dict(id=r['ID'],raw_price=raw['price'],provider_price_yuan=display,uid=r['FromId']))
                time.sleep(.002)
    report['vtbcat']=dict(counts=dict(totals),historical_pages=sum(len(v) for v in pages.values()),archived_pages=archived,
        incomplete_cached_pagination=[dict(live=k[0],kind=k[1],cached=len(v),expected=expected[k]) for k,v in pages.items() if v!=set(range(1,expected[k]+1))],price_conflict_examples=price_conflicts,streams=[dict(source_session=sid,counts=dict(counts)) for sid,counts in sorted(vtb_sessions.items())])
    counts={}
    with closing(sqlite3.connect(store.PRODUCTION_DB.as_uri()+'?mode=ro',uri=True)) as c:
        for kind,cmd in COMMANDS.items():
            deadline=time.monotonic()+20;c.set_progress_handler(lambda:int(time.monotonic()>deadline),1000)
            try:counts[kind]=c.execute('SELECT COUNT(*) FROM event INDEXED BY idx_event_cmd_time WHERE cmd=? AND timestamp<? AND room_id=?',(cmd,store.EXTERNAL_CUTOFF_MS//1000,ROOM_ID)).fetchone()[0]
            except sqlite3.OperationalError as exc:counts[kind]=str(exc)
            finally:c.set_progress_handler(None,0)
    report['main_before_cutoff']=counts
    save_json(args.output,report)
    print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':main()
