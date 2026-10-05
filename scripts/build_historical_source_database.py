"""Build an independent historical event database solely from source evidence.

Production is opened read-only for session/recording metadata, never as an event
source. The candidate database is not an apply plan: promotion requires a
separate coverage/identity/financial audit and a verified protected-range backup.
"""
import argparse
import bisect
import collections
import csv
import json
import math
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path

from src.db import danmaku_sources as store
from scripts.backfill_mit3uri_replay_danmaku import ROOM_ID, normalize_content
from scripts.plan_danmaku_source_segments import map_replay_sessions,choose_primary
from scripts.preview_danmaku_source_replacement import hist,primary_stream
from scripts.review_mit3uri_danmaku_sessions import fit_model
from scripts.continue_source_first_occurrences import clock_anchors
from scripts.audit_financial_source_matches import native_rows,COMMANDS
from scripts.import_qianqiu_sc import load_source,months_between
from scripts.audit_mit3uri_danmaku_integrity import save_json,save_csv,iso
from scripts.danmaku_clock_policy import load_policies,apply_policy

CELL=120
SCHEMA="""
CREATE TABLE event(id INTEGER PRIMARY KEY,room_id INTEGER NOT NULL,cmd TEXT NOT NULL,uid INTEGER,uname TEXT,content TEXT,gift_name TEXT,gift_num INTEGER,total_coin INTEGER,title TEXT,timestamp INTEGER NOT NULL);
CREATE TABLE origin(event_id INTEGER PRIMARY KEY,source TEXT NOT NULL,source_key TEXT NOT NULL,source_record_id INTEGER,source_stream TEXT NOT NULL,raw_timestamp REAL NOT NULL,selected_timestamp REAL NOT NULL,session_id TEXT NOT NULL,clock_basis TEXT NOT NULL,UNIQUE(source,source_key));
CREATE TABLE source_segment(session_id TEXT NOT NULL,start REAL NOT NULL,end REAL NOT NULL,source TEXT,reason TEXT NOT NULL,selected_count INTEGER NOT NULL);
CREATE TABLE clock_report(session_id TEXT NOT NULL,source TEXT NOT NULL,report TEXT NOT NULL);
CREATE TABLE build_session(session_id TEXT PRIMARY KEY,report TEXT NOT NULL);
CREATE TABLE financial_evidence(event_id INTEGER NOT NULL,source TEXT NOT NULL,source_key TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(source,source_key));
CREATE TABLE review(kind TEXT NOT NULL,scope TEXT NOT NULL,detail TEXT NOT NULL);
CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE INDEX idx_event_cmd_time ON event(cmd,timestamp);
CREATE INDEX idx_event_room_time ON event(room_id,timestamp);
"""


def source_clock(rows,base,kind):
    anchors=clock_anchors(rows,base);model=fit_model(anchors,max_residual=7)
    # Native providers carry UTC times. A missing relative fit does not erase them.
    # Replay clocks are uniform video bases, independently checked against native.
    for r in rows:
        ts=r['raw_time']
        if model and model['start']-60<=ts<=model['end']+60:
            r['time']=ts-(model['intercept']+model['slope']*ts)
            r['clock_basis']='cross_source_validated'
        else:r['clock_basis']='native_utc' if kind!='replay' else r['clock_basis']
    return dict(anchors=len(anchors),model={k:v for k,v in model.items() if k!='support'} if model else None,rows=len(rows))


def select_cells(streams,start,end):
    counts={s:hist(rr,start,end) for s,rr in streams.items()}
    primary=primary_stream(counts) or choose_primary(counts)
    if not primary:return None,[]
    chosen=[]
    for i in range(math.ceil((end-start)/CELL)):
        active={s:v.get(i,0) for s,v in counts.items() if v.get(i,0)>0}
        native={s:n for s,n in active.items() if not s.startswith('replay:')}
        own=active.get(primary,0)
        if not active:source,reason=None,'all_sources_silent'
        elif not native:
            source=primary if own else max(active,key=lambda s:(active[s],s));reason='replay_only_interval'
        elif own and not primary.startswith('replay:'):
            best=max(native,key=lambda s:native[s])
            # A sustained large deficit elects a complete alternative, not a union.
            sustained=all(counts[best].get(j,0)>=10 and counts[primary].get(j,0)<.5*counts[best].get(j,0) for j in (i,min(i+1,math.ceil((end-start)/CELL)-1)))
            source=best if sustained else primary;reason='native_deficit_switch' if source!=primary else 'native_primary'
        else:
            source=max(native,key=lambda s:(native[s],s.startswith('danmakus:')));reason='native_gap_fill'
        chosen.append(dict(start=start+i*CELL,end=min(end,start+(i+1)*CELL),source=source,reason=reason))
    runs=[]
    for c in chosen:
        if runs and (runs[-1]['source'],runs[-1]['reason'])==(c['source'],c['reason']):runs[-1]['end']=c['end']
        else:runs.append(c)
    return primary,runs


def insert_danmaku(out,r,session):
    if not 0<r['time']<store.EXTERNAL_CUTOFF_MS/1000:raise ValueError('Candidate outside historical boundary')
    if out.execute('SELECT 1 FROM origin WHERE source=? AND source_key=?',(r['source'],str(r['id']))).fetchone():return False
    cur=out.execute("INSERT INTO event(room_id,cmd,uid,uname,content,timestamp) VALUES(?,'DANMU_MSG',?,?,?,?)",(ROOM_ID,r['uid'],r.get('uname'),r['content'],int(r['time'])))
    out.execute('INSERT INTO origin VALUES(?,?,?,?,?,?,?,?,?)',(cur.lastrowid,r['source'],str(r['id']),r['id'],r['stream'],r['raw_time'],r['time'],session,r['clock_basis']))
    return True


def boundary_duplicates(rows):
    """One source occurrence can consume at most one occurrence in the next stream."""
    indexed=collections.defaultdict(list);removed=set();claims=set();ambiguous=0
    for r in sorted(rows,key=lambda r:(r['time'],r['id'])):
        key=(r['uid'],normalize_content(r['content']));stream=(r['source'],r['stream'])
        opts=[p for p in indexed[key] if r['uid'] and r['time']-p['time']<=7 and (p['source'],p['stream'])!=stream and abs(r['time']-r['run_start'])<=7 and abs(p['time']-p['run_end'])<=7 and (p['id'],stream) not in claims]
        if len(opts)==1:removed.add(r['id']);claims.add((opts[0]['id'],stream));continue
        if len(opts)>1:ambiguous+=1
        indexed[key].append(r)
    return removed,ambiguous


def sessions_from_metadata(main,start_evidence,cold=None):
    ss=[dict(r) for r in main.execute('SELECT * FROM mit3uri_replay_session WHERE included_in_total=1 AND start_timestamp<? ORDER BY start_timestamp',(store.EXTERNAL_CUTOFF_MS//1000,))]
    candidates={r['库内场次']:r for r in csv.DictReader(start_evidence.open(encoding='utf-8-sig')) if r['历史范围']=='True' and r['状态'] not in ('场次映射或时间冲突','未匹配或缺少库内起点')}
    locks=json.loads((store.ROOT/'data/mit3uri_replay_danmaku/alignment_locks.json').read_text())['sessions']
    metadata=json.loads((store.ROOT/'data/mit3uri_replay_danmaku/metadata.json').read_text())['recordings']
    if any(r['owner_mid']!=2030198123 for r in metadata):raise ValueError('Unofficial replay source')
    mapping,ambiguous=map_replay_sessions(metadata,dict(main.execute('SELECT bvid,session_id FROM mit3uri_replay_recording')))
    reverse=collections.defaultdict(list)
    for old,new in mapping.items():reverse[new].append(old)
    for s in ss:
        old_start=s['start_timestamp'];s['start']=old_start;s['start_basis']='catalog'
        if s['start_time_precision']!='second':
            evidence=candidates.get(s['session_id'])
            if evidence:s['start']=datetime.fromisoformat(evidence['表格时间']).timestamp();s['start_basis']='supplied_workbook:'+evidence['时间依据']
            else:
                bases=[locks[x]['base_ms']/1000 for x in reverse[s['session_id']] if x in locks]
                if bases and abs(min(bases)-old_start)<3600:s['start']=min(bases);s['start_basis']='video_clock_lock'
        s['end']=min(store.EXTERNAL_CUTOFF_MS/1000,s['start']+(s['duration_seconds'] or 0))
        # A recording may continue beyond an imprecise catalog duration. Its
        # independently locked live-origin comments extend the ownership window.
        if cold is not None:
            for old in reverse[s['session_id']]:
                if old not in locks:continue
                a,b=cold.execute("SELECT MIN(json_extract(evidence,'$.session_progress_ms')),MAX(json_extract(evidence,'$.session_progress_ms')) FROM danmaku_source_record WHERE source='replay' AND source_session=? AND eligibility='live' AND json_extract(evidence,'$.dm_from')=3",(old,)).fetchone()
                if a is not None:
                    s['start']=min(s['start'],(locks[old]['base_ms']+a)/1000)
                    s['end']=min(store.EXTERNAL_CUTOFF_MS/1000,max(s['end'],(locks[old]['base_ms']+b)/1000+1))
    # Connected overlapping catalog entries form one ownership window.
    groups=[]
    for s in sorted(ss,key=lambda s:s['start']):
        if s['end']<=s['start']:continue
        if groups and s['start']<groups[-1]['end']:
            groups[-1]['members'].append(s);groups[-1]['end']=max(groups[-1]['end'],s['end'])
        else:groups.append(dict(start=s['start'],end=s['end'],members=[s]))
    # Metadata marks live ownership, not a hard event cutoff. A bounded envelope
    # retains the opening/tail and is split at midpoints between close sessions.
    edges=[(a['end']+b['start'])/2 for a,b in zip(groups,groups[1:])]
    for i,g in enumerate(groups):
        g['catalog_start']=g['start'];g['catalog_end']=g['end']
        g['start']=max(g['start']-600,edges[i-1] if i else 0)
        g['end']=min(g['end']+600,edges[i] if i<len(edges) else store.EXTERNAL_CUTOFF_MS/1000)
    if cold is not None:
        gaps=[];last=0
        for g in groups:
            if last<g['start']:gaps.append((last,g['start']))
            last=g['end']
        if last<store.EXTERNAL_CUTOFF_MS/1000:gaps.append((last,store.EXTERNAL_CUTOFF_MS/1000))
        for a,b in gaps:
            lo,hi=cold.execute("SELECT MIN(source_timestamp_ms),MAX(source_timestamp_ms) FROM danmaku_source_record INDEXED BY idx_dm_source_time WHERE room_id=? AND source_timestamp_ms>=? AND source_timestamp_ms<? AND source IN ('danmakus','vtbcat') AND eligibility='live'",(ROOM_ID,a*1000,b*1000)).fetchone()
            if lo is None:continue
            lo=max(a,lo/1000-8);hi=min(b,hi/1000+8)
            sid=f'SOURCE-{int(lo)}'
            groups.append(dict(start=lo,end=hi,members=[dict(session_id=sid,start_basis='native_source_interval')]))
        groups.sort(key=lambda g:g['start'])
    return groups,reverse,locks,ambiguous


def build_danmaku(out,cold,groups,reverse,locks,clock_policies=None):
    if clock_policies is None:clock_policies=load_policies()
    for n,g in enumerate(groups,1):
        sid='+'.join(s['session_id'] for s in g['members'])
        if out.execute('SELECT 1 FROM build_session WHERE session_id=?',(sid,)).fetchone():continue
        streams=collections.defaultdict(list);lo=g['start'];hi=g['end'];filtered=collections.Counter()
        for r in cold.execute("SELECT id,source,source_session,uid,uname,content,source_timestamp_ms FROM danmaku_source_record INDEXED BY idx_dm_source_time WHERE room_id=? AND source_timestamp_ms>=? AND source_timestamp_ms<? AND source IN ('danmakus','vtbcat') AND eligibility='live'",(ROOM_ID,(lo-600)*1000,min(store.EXTERNAL_CUTOFF_MS,(hi+600)*1000))):
            d=dict(r);t=d['source_timestamp_ms']/1000;key=d['source']+':'+d['source_session']
            streams[key].append(dict(id=d['id'],source=d['source'],stream=d['source_session'],uid=d['uid'],uname=d['uname'],content=d['content'],time=t,raw_time=t,clock_basis='native_utc'))
        for s in g['members']:
            for old in reverse[s['session_id']]:
                lock=locks.get(old)
                for r in cold.execute("SELECT id,uid,uname,content,evidence,mapped_timestamp_ms,source_timestamp_ms FROM danmaku_source_record WHERE source='replay' AND source_session=? AND eligibility='live'",(old,)):
                    e=json.loads(r['evidence'])
                    if e.get('dm_from')!=3:filtered['not_live_origin']+=1;continue
                    if not lock:filtered['missing_video_clock']+=1;continue
                    t=(lock['base_ms']+e['session_progress_ms'])/1000
                    if not 0<t<store.EXTERNAL_CUTOFF_MS/1000:filtered['outside_boundary']+=1;continue
                    key='replay:'+str(e['cid'])
                    streams[key].append(dict(id=r['id'],source='replay',stream=str(e['cid']),uid=r['uid'],uname=r['uname'],content=r['content'],time=t,raw_time=t,clock_basis='video_lock:'+lock['method']))
        native=primary_stream({k:hist(v,lo,hi) for k,v in streams.items()})
        clocks=[]
        if native:
            for key,rr in streams.items():
                if key!=native:clocks.append((key,source_clock(rr,streams[native],key.split(':')[0])))
        primary,runs=select_cells(streams,lo,hi)
        all_selected=[];per_run=[]
        for run in runs:
            picked=[r for r in streams.get(run['source'],[]) if run['start']<=r['time']<run['end']]
            for r in picked:r['run_start']=run['start'];r['run_end']=run['end']
            all_selected.extend(picked);per_run.append((run,picked))
        # Boundary reconciliation only compares different streams, preserving
        # multiplicity inside a single stream even for identical millisecond times.
        boundary_removed,boundary_ambiguous=boundary_duplicates(all_selected)
        if clock_policies.get(sid):
            reviewed=apply_policy([r for r in all_selected if r['id'] not in boundary_removed],streams,clock_policies[sid])
            untouched={r['id'] for r in reviewed if not r.get('reviewed_clock_policy')}
            per_run=[(run,[r for r in rr if r['id'] in untouched]) for run,rr in per_run]
            repaired=collections.defaultdict(list)
            for r in reviewed:
                if r.get('reviewed_clock_policy'):
                    repaired[(int(r['time']//CELL)*CELL,r['source']+':'+r['stream'])].append(r)
            for (a,source),rr in sorted(repaired.items()):
                per_run.append((dict(start=a,end=min(a+CELL,store.EXTERNAL_CUTOFF_MS/1000),source=source,reason='reviewed_clock_repair'),rr))
            boundary_removed=set()
        with out:
            count=0
            for run,picked in per_run:
                selected_count=0
                for r in picked:
                    if r['id'] in boundary_removed:continue
                    selected_count+=insert_danmaku(out,r,sid)
                count+=selected_count
                out.execute('INSERT INTO source_segment VALUES(?,?,?,?,?,?)',(sid,run['start'],run['end'],run['source'],run['reason'],selected_count))
            for key,report in clocks:out.execute('INSERT INTO clock_report VALUES(?,?,?)',(sid,key,json.dumps(report)))
            report=dict(session_id=sid,members=[s['session_id'] for s in g['members']],start=iso(lo),end=iso(hi),primary=primary,selected=count,filtered=dict(filtered),boundary_duplicates=len(boundary_removed),boundary_ambiguous=boundary_ambiguous,raw_counts={k:len(v) for k,v in streams.items()},start_bases={s['session_id']:s['start_basis'] for s in g['members']})
            out.execute('INSERT INTO build_session VALUES(?,?)',(sid,json.dumps(report,ensure_ascii=False)))
            if filtered.get('missing_video_clock'):out.execute('INSERT INTO review VALUES(?,?,?)',('missing_replay_clock',sid,json.dumps(dict(filtered))))
        if n%10==0:print('sessions',n,len(groups),'last_rows',count,flush=True)


def build_finance(out,qqzy_dir):
    if out.execute("SELECT 1 FROM meta WHERE key='finance_complete'").fetchone():return
    streams=collections.defaultdict(list)
    for r in native_rows():
        r['stream']=r['stream'].removesuffix('/full');streams[(r['provider'],r['stream'])].append(r)
    for r in load_source(qqzy_dir,ROOM_ID,list(months_between('2025-11','2026-04'))):
        if r['timestamp']*1000>=store.EXTERNAL_CUTOFF_MS:continue
        streams[('qqzy',r['month'])].append(dict(provider='qqzy',stream=r['month'],key=f"{r['month']}:{r['source_index']}",kind='sc',uid=r['uid'],uname=r['uname'],ts=r['timestamp'],text=r['content'],num=1,coin=r['price']))
    selected=[];index=collections.defaultdict(list);conflicts=[];matches=[]
    for sk,rr in sorted(streams.items(),key=lambda item:({'qqzy':0,'danmakus':1,'vtbcat':2}[item[0][0]],not any(r.get('merged') for r in item[1]),-len(item[1]),item[0])):
        consumed=set()
        # Source multiplicity is retained; an existing transaction is consumed at
        # most once per stream. Providers with missing intervals can supplement it.
        for r in sorted(rr,key=lambda r:(r['ts'],r['key'])):
            key=r['kind'],r['uid'],r['text']
            candidates=[i for i in index[key] if i not in consumed and abs(selected[i]['ts']-r['ts'])<=7]
            if not candidates and r['kind']=='sc':
                possible=[i for i in index[key] if i not in consumed and selected[i]['coin']==r['coin'] and abs(selected[i]['ts']-r['ts'])<=300]
                if len(possible)==1 and sum(x['kind']=='sc' and x['uid']==r['uid'] and x['text']==r['text'] for x in rr)==1:candidates=possible
            if candidates:
                i=min(candidates,key=lambda i:((selected[i]['num'],selected[i]['coin'])!=(r['num'],r['coin']),selected[i]['num']!=r['num'],abs(selected[i]['ts']-r['ts'])));consumed.add(i);matches.append((i,r))
                if (selected[i]['coin'],selected[i]['num'])!=(r['coin'],r['num']):conflicts.append(dict(chosen=selected[i],alternative=r))
            else:
                i=len(selected);selected.append(r);index[key].append(i);consumed.add(i);matches.append((i,r))
    with out:
        event_ids=[]
        for r in selected:
            cur=out.execute('INSERT INTO event(room_id,cmd,uid,uname,content,gift_name,gift_num,total_coin,timestamp) VALUES(?,?,?,?,?,?,?,?,?)',(ROOM_ID,COMMANDS[r['kind']],r['uid'],r.get('uname'),r['text'] if r['kind']=='sc' else None,'醒目留言' if r['kind']=='sc' else r['text'],r['num'],r['coin'],r['ts'] if r['kind']=='sc' else int(r['ts'])))
            eid=cur.lastrowid;event_ids.append(eid)
            out.execute('INSERT INTO origin VALUES(?,?,?,?,?,?,?,?,?)',(eid,r['provider'],'financial:'+r['key'],None,r['stream'],r['ts'],r['ts'],'financial','provider_timestamp'))
        for i,r in matches:out.execute('INSERT INTO financial_evidence VALUES(?,?,?,?)',(event_ids[i],r['provider'],r['key'],json.dumps(r,ensure_ascii=False)))
        for r in conflicts:out.execute('INSERT INTO review VALUES(?,?,?)',('financial_field_conflict',str(r['chosen']['uid']),json.dumps(r,ensure_ascii=False)))
        out.execute("INSERT INTO meta VALUES('finance_complete',?)",(json.dumps(dict(selected=len(selected),source_rows=sum(map(len,streams.values())),conflicts=len(conflicts))),))
    print('finance',len(selected),'conflicts',len(conflicts),flush=True)


def finalize_evidence(out,cold):
    """Retain source precision and raw GUARD_BUY quantities without pricing guesses."""
    if out.execute("SELECT 1 FROM meta WHERE key='sc_normalization_complete'").fetchone():return
    out.execute('CREATE TABLE IF NOT EXISTS replay_provenance(event_id INTEGER PRIMARY KEY,payload TEXT NOT NULL)')
    out.execute('CREATE TABLE IF NOT EXISTS sc_signal_merge(removed_event_id INTEGER PRIMARY KEY,retained_event_id INTEGER NOT NULL,detail TEXT NOT NULL)')
    with out:
        out.execute("UPDATE event SET timestamp=(SELECT selected_timestamp FROM origin WHERE event_id=event.id) WHERE cmd='SUPER_CHAT_MESSAGE'")
        from scripts.normalize_historical_sc import clusters
        columns=[r[1] for r in out.execute('PRAGMA table_info(event)')]+['source']
        sc=[dict(zip(columns,r)) for r in out.execute("SELECT e.*,o.source FROM event e JOIN origin o ON o.event_id=e.id WHERE cmd='SUPER_CHAT_MESSAGE'")]
        for group in clusters(sc):
            keep=min(group,key=lambda r:({'qqzy':0,'danmakus':1,'vtbcat':2}[r['source']],r['timestamp'],r['id']))
            for removed in group:
                if removed['id']==keep['id']:continue
                origin=out.execute('SELECT * FROM origin WHERE event_id=?',(removed['id'],)).fetchone()
                detail=json.dumps(dict(event=removed,origin=list(origin),rule='same_uid_text_price_within_60_seconds'),ensure_ascii=False)
                out.execute('INSERT INTO sc_signal_merge VALUES(?,?,?)',(removed['id'],keep['id'],detail))
                out.execute('UPDATE financial_evidence SET event_id=? WHERE event_id=?',(keep['id'],removed['id']))
                out.execute('DELETE FROM origin WHERE event_id=?',(removed['id'],))
                out.execute('DELETE FROM event WHERE id=?',(removed['id'],))
                out.execute('INSERT INTO review VALUES(?,?,?)',('sc_duplicate_signal',str(keep['id']),detail))
        guards=collections.defaultdict(list)
        for eid,payload in out.execute("SELECT event_id,payload FROM financial_evidence WHERE source='vtbcat'"):
            r=json.loads(payload)
            if r['kind']!='guard' or not r.get('raw_extra'):continue
            raw=json.loads(r['raw_extra'])
            if raw.get('cmd')=='GUARD_BUY':guards[eid].append(int(raw['data']['num']))
        fixed=0
        for eid,nums in guards.items():
            if len(set(nums))!=1:raise ValueError('Conflicting raw guard quantities')
            old=out.execute('SELECT gift_num FROM event WHERE id=?',(eid,)).fetchone()[0]
            if old!=nums[0]:
                out.execute('UPDATE event SET gift_num=? WHERE id=?',(nums[0],eid))
                out.execute('INSERT INTO review VALUES(?,?,?)',('guard_quantity_raw_packet',str(eid),json.dumps(dict(before=old,after=nums[0],basis='vtbcat_raw_GUARD_BUY_num'))));fixed+=1
        for eid,srid,ts,basis in out.execute("SELECT event_id,source_record_id,selected_timestamp,clock_basis FROM origin WHERE source='replay'"):
            r=cold.execute('SELECT source_session,source_timestamp_ms,evidence FROM danmaku_source_record WHERE id=?',(srid,)).fetchone()
            e=json.loads(r[2])
            if e.get('dm_from')!=3:raise ValueError('Non-live replay origin')
            payload=['bilibili_official_replay',e['cid'],str(e['dm_id']),r[0],e['bvid'],e['progress_ms'],e['session_progress_ms'],e['mid_hash'],int(r[1]/1000),int(ts*1000),'source_rebuild:'+basis,'inserted',eid]
            out.execute('INSERT OR IGNORE INTO replay_provenance VALUES(?,?)',(eid,json.dumps(payload,ensure_ascii=False)))
        out.execute("INSERT OR REPLACE INTO meta VALUES('evidence_finalized',?)",(json.dumps(dict(raw_guard_quantity_fixes=fixed)),))
        out.execute("INSERT INTO meta VALUES('sc_normalization_complete','true')")


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--qqzy',type=Path,required=True);p.add_argument('--start-evidence',type=Path,required=True);args=p.parse_args()
    args.output.mkdir(parents=True,exist_ok=True);db=args.output/'rebuilt.sqlite3';fresh=not db.exists()
    if (args.output/'audit.json').exists():raise ValueError('Audited candidates are immutable; use a new output directory')
    if db.resolve() in (store.PRODUCTION_DB.resolve(),store.ARCHIVE_DB.resolve()):raise ValueError('Independent candidate database required')
    with closing(sqlite3.connect(store.PRODUCTION_DB.as_uri()+'?mode=ro',uri=True)) as main,closing(sqlite3.connect(store.ARCHIVE_DB.as_uri()+'?mode=ro',uri=True)) as cold,closing(sqlite3.connect(db)) as out:
        main.row_factory=cold.row_factory=sqlite3.Row
        out.execute('PRAGMA journal_mode=WAL');out.execute('PRAGMA synchronous=NORMAL')
        if fresh:out.executescript(SCHEMA)
        groups,reverse,locks,ambiguous=sessions_from_metadata(main,args.start_evidence,cold)
        build_danmaku(out,cold,groups,reverse,locks);build_finance(out,args.qqzy);finalize_evidence(out,cold)
        counts=dict(out.execute('SELECT cmd,COUNT(*) FROM event GROUP BY cmd'))
        report=dict(candidate_only=True,production_modified=False,production_events_used_as_input=0,cutoff=iso(store.EXTERNAL_CUTOFF_MS/1000),counts=counts,session_groups=out.execute('SELECT COUNT(*) FROM build_session').fetchone()[0],source_counts=dict(out.execute('SELECT source,COUNT(*) FROM origin GROUP BY source')),review_counts=dict(out.execute('SELECT kind,COUNT(*) FROM review GROUP BY kind')),ambiguous_session_mapping=ambiguous,ready_for_promotion=False)
        if out.execute('SELECT COUNT(*) FROM event WHERE room_id!=? OR timestamp>=? OR timestamp<=0',(ROOM_ID,store.EXTERNAL_CUTOFF_MS//1000)).fetchone()[0]:raise ValueError('Boundary check failed')
        if out.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('Candidate integrity failure')
        with out:out.execute("INSERT OR REPLACE INTO meta VALUES('build_summary',?)",(json.dumps(report),))
        save_json(args.output/'summary.json',report)
        save_csv(args.output/'sessions.csv',[json.loads(r[0]) for r in out.execute('SELECT report FROM build_session')])
        print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':main()
