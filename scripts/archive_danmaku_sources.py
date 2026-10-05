"""Archive source evidence without merging or rewriting live-event results.

Network fetching is serial and resumable. Every page is retained compressed;
normalized rows are committed in small batches independently of live writes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import time
import zlib
from collections import Counter
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from src.db import danmaku_sources as store
from scripts.backfill_mit3uri_replay_danmaku import (
    ROOT, ROOM_ID, parse_dm_segment, normalize_hash, decode_proto_fields, proto_int,
)
from scripts.rebuild_historical_danmaku import (
    _decode_danmakus_rows, _decode_danmakus_legacy_page, _select_danmakus_lives,
    _select_vtb_lives,
)


def completed(conn,key):
    return conn.execute('SELECT value FROM danmaku_source_checkpoint WHERE key=?',(key,)).fetchone()


def archive_page(conn,source,locator,payload,metadata,rows,links=(),artifact_id=None):
    """Each source_key belongs to its upstream namespace, never UID/text alone."""
    key=f'page:{source}:{locator}:{hashlib.sha256(payload).hexdigest()}'
    if completed(conn,key):return 0
    with conn:
        aid=artifact_id if artifact_id is not None else store.artifact(conn,source,locator,payload,metadata)
    count=0
    for start in range(0,len(rows),500):
        values=rows[start:start+500]
        for r in values:r['artifact_id']=aid
        with conn:count+=store.add_records(conn,values)
        time.sleep(.02)
    for start in range(0,len(links),500):
        with conn:
            conn.executemany('INSERT OR IGNORE INTO danmaku_source_link(source_id,event_id,method) '
                'SELECT id,?,? FROM danmaku_source_record WHERE source=? AND source_key=?',
                [(eid,method,source,str(key)) for key,eid,method in links[start:start+500]])
        time.sleep(.02)
    with conn:store.checkpoint(conn,key,len(rows))
    return count


def archive_vtb(conn):
    root=ROOT/'data/vtb_cat/2030198123'
    catalog=json.loads((root/'space.json').read_text())
    lives=_select_vtb_lives(catalog,store.EXTERNAL_CUTOFF_MS)
    stats=Counter();missing=[]
    with conn:store.artifact(conn,'vtbcat','catalog', (root/'space.json').read_bytes(),{'cutoff_ms':store.EXTERNAL_CUTOFF_MS})
    for i,live in enumerate(lives,1):
        lid=live['ID'];pages=sorted((root/'lives'/str(lid)).glob('msg_*.json'),key=lambda p:int(p.stem.split('_')[1]))
        if not pages:missing.append(lid);continue
        for path in pages:
            payload=path.read_bytes();page=json.loads(payload);rows=[]
            for r in page['records']:
                if r.get('ActionName')!='msg' or int(r.get('ActionType',0))!=1:
                    stats['non_danmaku']+=1
                    continue
                ts=round(datetime.fromisoformat(r['CreatedAt']).timestamp()*1000)
                if int(r['LiveRoom'])!=ROOM_ID:raise ValueError('wrong VTB room')
                rows.append(store.record('vtbcat',r['ID'],lid,ROOM_ID,r['FromId'],r.get('FromName'),r.get('Extra') or r.get('EmotesContent') or '',ts,
                    eligibility='live' if ts<store.EXTERNAL_CUTOFF_MS else 'outside_policy',
                    evidence={'upstream_id':r['ID'],'created_at':r['CreatedAt'],'timestamp_kind':'provider_created_at'}))
            stats['inserted']+=archive_page(conn,'vtbcat',f'{lid}/{path.name}',payload,live,rows)
            stats['source_rows']+=len(rows)
        expected=int(page.get('totalPages') or len(pages))
        if len(pages)!=expected:missing.append(lid)
        if i%25==0:print('vtbcat',i,len(lives),dict(stats),flush=True)
    result=dict(stats,lives=len(lives),missing_or_partial_lives=missing)
    with conn:store.checkpoint(conn,'summary:vtbcat',store.encode(result))
    return result


def archive_replay(conn):
    root=ROOT/'data/mit3uri_replay_danmaku'
    metadata=json.loads((root/'metadata.json').read_text())
    locks=json.loads((root/'alignment_locks.json').read_text()).get('sessions',{})
    uidmap={};collisions=set()
    with closing(sqlite3.connect(f'file:{ROOT}/data/libot.db?mode=ro',uri=True)) as read:
        for (uid,) in read.execute('SELECT DISTINCT uid FROM event WHERE uid>0'):
            key=normalize_hash(format(zlib.crc32(str(uid).encode())&0xffffffff,'x'))
            if key in uidmap and uidmap[key]!=uid:collisions.add(key)
            uidmap[key]=uid
    for key in collisions:uidmap.pop(key,None)
    stats=Counter()
    for i,rec in enumerate(metadata['recordings'],1):
        if rec['owner_mid']!=2030198123:raise ValueError('nonofficial replay owner')
        for page in rec['pages']:
            cid=page['cid']
            with closing(sqlite3.connect(f'file:{ROOT}/data/libot.db?mode=ro',uri=True)) as read:
                read.row_factory=sqlite3.Row
                prov={r['dm_id']:dict(r) for r in read.execute(
                    'SELECT p.*,e.uid,e.uname FROM mit3uri_replay_danmaku_provenance p '
                    'JOIN event e ON e.id=p.event_id WHERE p.source=? AND p.cid=?',('bilibili_official_replay',cid))}
            for segment in range(1,page['total_segments']+1):
                path=root/'segments'/str(cid)/f'{segment:04d}.pb';payload=path.read_bytes()
                dm=parse_dm_segment(payload,session_id=rec['session_id'],bvid=rec['bvid'],cid=cid,
                    session_offset_ms=page['session_offset_ms'],uid_by_hash=uidmap)
                rows=[];links=[]
                for r in dm:
                    p=prov.get(r.dm_id);key=f'{cid}:{r.dm_id}';lock=locks.get(rec['session_id'],{})
                    mapped=p['mapped_timestamp_ms'] if p else int(lock['base_ms'])+r.session_progress_ms if 'base_ms' in lock else None
                    kind='live' if r.dm_from==3 else 'video_comment' if r.dm_from==1 else 'unknown'
                    if kind=='live' and mapped is not None and mapped>=store.EXTERNAL_CUTOFF_MS:kind='outside_policy'
                    rows.append(store.record('replay',key,rec['session_id'],ROOM_ID,p['uid'] if p else r.uid,p['uname'] if p else None,
                        r.content,r.ctime*1000,mapped=mapped,eligibility=kind,evidence=dict(bvid=r.bvid,cid=cid,dm_id=r.dm_id,
                        progress_ms=r.progress_ms,session_progress_ms=r.session_progress_ms,mid_hash=r.mid_hash,
                        pubdate=rec['pubdate'],dm_from=r.dm_from,attr=r.attr,timestamp_kind='video_comment_or_batch_creation')))
                    if p:links.append((key,p['event_id'],'replay_import_'+p['match_kind']))
                    stats[kind]+=1
                stats['inserted']+=archive_page(conn,'replay',f'{cid}/{segment:04d}.pb',payload,dict(recording=rec,page=page),rows,links)
        if i%25==0:print('replay',i,len(metadata['recordings']),dict(stats),flush=True)
    with conn:store.checkpoint(conn,'summary:replay',store.encode(stats))
    return dict(stats)


def archive_legacy(conn):
    checkpoint_key='legacy:v2:last_event_id'
    old=completed(conn,checkpoint_key);cursor=int(old[0]) if old else 0
    high=int(conn.execute('SELECT MAX(id) FROM event').fetchone()[0]);total=0
    while cursor<high:
        # event is resolved through the read-only production attachment; source
        # tables belong to this connection's cold main database.
        rows=conn.execute("SELECT id,room_id,uid,uname,content,timestamp FROM event NOT INDEXED "
            "WHERE id>? AND id<=? AND room_id=? AND cmd='DANMU_MSG' "
            "AND ((timestamp>=? AND NOT EXISTS(SELECT 1 FROM danmaku_source_record r "
            "WHERE r.source='monitor' AND r.source_key='event:'||event.id)) "
            "OR (timestamp<? AND NOT EXISTS(SELECT 1 FROM danmaku_source_link l WHERE l.event_id=event.id))) "
            "AND NOT EXISTS(SELECT 1 FROM mit3uri_replay_danmaku_provenance p WHERE p.event_id=event.id AND p.match_kind='inserted') "
            "ORDER BY id LIMIT 1000",(cursor,high,ROOM_ID,store.EXTERNAL_CUTOFF_MS//1000,store.EXTERNAL_CUTOFF_MS//1000)).fetchall()
        if not rows:break
        values=[]
        for r in rows:
            is_monitor=int(r[5])*1000>=store.EXTERNAL_CUTOFF_MS
            values.append(store.record('monitor' if is_monitor else 'legacy_unknown',f'event:{r[0]}','',r[1],r[2],r[3],r[4] or '',int(r[5])*1000,
                mapped=int(r[5])*1000,eligibility='live' if is_monitor else 'unknown',
                evidence={'event_id':r[0],'attribution':'user_confirmed_monitor_since_first_live_2026_04_20' if is_monitor else 'unproven_legacy_origin','monitor_start_ms':store.EXTERNAL_CUTOFF_MS}))
        with conn:
            total+=store.add_records(conn,values)
            conn.executemany("INSERT OR IGNORE INTO danmaku_source_link(source_id,event_id,method) "
                "SELECT id,?,'legacy_snapshot' FROM danmaku_source_record WHERE source=? AND source_key=?",
                [(r[0],v['source'],v['source_key']) for r,v in zip(rows,values)])
            cursor=rows[-1][0];store.checkpoint(conn,checkpoint_key,cursor)
        if total%100000==0:print('legacy',cursor,high,total,flush=True)
        time.sleep(.03)
    with conn:store.checkpoint(conn,checkpoint_key,high)
    return dict(inserted=total,high_water_event_id=high)


def archive_monitor_logs(conn):
    # Receipt time is merely a matching bound. A unique event with equal room,
    # UID and text is required; ambiguous log lines remain in the artifact.
    pattern=re.compile(r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ .*?房间 (\d+) 收到弹幕，uid=(\d+) uname=(.*?) content=(.*)$')
    stats=Counter()
    for path in sorted((ROOT/'logs').glob('monitor.log*')):
        payload=path.read_bytes();rows=[];links=[]
        if completed(conn,'monitor-log:'+hashlib.sha256(payload).hexdigest()):continue
        with conn:
            aid=store.artifact(conn,'monitor',path.name,payload,{'log':str(path)})
        with closing(sqlite3.connect(f'file:{ROOT}/data/libot.db?mode=ro',uri=True)) as read:
            for line_no,line in enumerate(payload.decode('utf8',errors='replace').splitlines(),1):
                m=pattern.match(line)
                if not m:continue
                ts=int(datetime.fromisoformat(m[1]).replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp());room=int(m[2]);uid=int(m[3]);text=m[5]
                if room!=ROOM_ID:continue
                found=read.execute("SELECT id,timestamp FROM event WHERE room_id=? AND uid=? AND timestamp BETWEEN ? AND ? "
                    "AND cmd='DANMU_MSG' AND content=?",(room,uid,ts-10,ts+2,text)).fetchall()
                if len(found)!=1:stats['ambiguous_or_missing']+=1;continue
                eid,event_ts=found[0];key=f'event:{eid}'
                rows.append(store.record('monitor',key,'',room,uid,m[4],text,event_ts*1000,mapped=event_ts*1000,
                    evidence={'capture':'monitor_log','line':line_no,'received_at':m[1],'event_id':eid}))
                links.append((key,eid,'monitor_log_exact_unique'))
                if len(rows)>=2000:
                    stats['inserted']+=archive_page(conn,'monitor',f'{path.name}/through-line:{line_no}',payload,{'log':str(path)},rows,links,artifact_id=aid)
                    rows=[];links=[]
            if rows:stats['inserted']+=archive_page(conn,'monitor',f'{path.name}/final',payload,{'log':str(path)},rows,links,artifact_id=aid)
        with conn:store.checkpoint(conn,'monitor-log:'+hashlib.sha256(payload).hexdigest(),len(payload))
        print('monitor',path.name,dict(stats),flush=True)
    return dict(stats)


def archive_danmakus(conn,interval):
    headers={'User-Agent':'Mozilla/5.0','Origin':'https://danmakus.com','Referer':'https://danmakus.com/'}
    emojis=json.loads((ROOT/'scripts/emoji.json').read_text())
    stats=Counter();failures=[]
    with httpx.Client(timeout=60,headers=headers) as client:
        def get(url,**kwargs):
            for attempt in range(3):
                time.sleep(interval if attempt==0 else 5*(attempt+1))
                try:
                    response=client.get(url,**kwargs);response.raise_for_status();return response
                except httpx.HTTPError:
                    if attempt==2:raise
        response=get('https://ukamnads.icu/api/v2/channel',params={'uid':2030198123})
        catalog=response.json();lives=_select_danmakus_lives(catalog,store.EXTERNAL_CUTOFF_MS)
        with conn:store.artifact(conn,'danmakus','catalog',response.content,{'cutoff_ms':store.EXTERNAL_CUTOFF_MS})
        for i,live in enumerate(lives,1):
            lid=live['liveId']
            if completed(conn,'danmakus-live:'+lid):stats['resumed_lives']+=1;continue
            try:
                response=get(f'https://ukamnads.icu/api/v3/lives/{lid}/full',params={'include':'extra','includeEnter':'false'},headers={'Accept':'application/x-msgpack'})
                decoded,info=_decode_danmakus_rows(response.content,live_id=lid,cutoff_ms=store.EXTERNAL_CUTOFF_MS,emoji_map=emojis)
                pages=[('full',response.content,decoded,info)]
                if not info['raw_records'] and live.get('danmakusCount',0):
                    pages=[];page=1;has_more=True
                    while has_more:
                        response=get(f'https://ukamnads.icu/api/v3/lives/{lid}/danmakus',params={'offset':(page-1)*2000,'limit':2000})
                        decoded,info=_decode_danmakus_legacy_page(response.json(),live_id=lid,page_number=page,cutoff_ms=store.EXTERNAL_CUTOFF_MS,emoji_map=emojis)
                        pages.append((f'page-{page}',response.content,decoded,info));page+=1;has_more=info['has_more']
                    if not sum(p[3]['raw_records'] for p in pages):raise ValueError('catalog has records but both APIs are empty')
                for name,payload,decoded,info in pages:
                    rows=[store.record('danmakus',r['native_id'],lid,ROOM_ID,r['uid'],r['uname'],r['content'],r['timestamp_ms'],
                                       evidence={'sequence':r['seq'],'format':name}) for r in decoded]
                    stats['inserted']+=archive_page(conn,'danmakus',f'{lid}/{name}',payload,dict(live=live,decode=info),rows)
                    stats['source_rows']+=len(rows)
                with conn:store.checkpoint(conn,'danmakus-live:'+lid,sum(len(p[2]) for p in pages))
            except (httpx.HTTPError,ValueError) as exc:
                failures.append(dict(live_id=lid,error=str(exc)));print('danmakus failure',lid,str(exc),flush=True)
            print('danmakus',i,len(lives),lid,dict(stats),flush=True)
    result=dict(stats,lives=len(lives),failures=failures)
    with conn:store.checkpoint(conn,'summary:danmakus',store.encode(result))
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('source',choices=['vtbcat','replay','danmakus','legacy','monitor'])
    p.add_argument('--interval',type=float,default=.75)
    args=p.parse_args()
    with closing(store.connect_archive()) as conn:
        conn.execute('PRAGMA busy_timeout=5000')
        with conn:store.initialize(conn)
        functions={'vtbcat':archive_vtb,'replay':archive_replay,'legacy':archive_legacy,'monitor':archive_monitor_logs}
        result=archive_danmakus(conn,args.interval) if args.source=='danmakus' else functions[args.source](conn)
    print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':main()
