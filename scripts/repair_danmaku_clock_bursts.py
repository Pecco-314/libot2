"""Evidence-constrained occurrence matching for compressed historical clocks.

Interpolation uses source sequence position, never a constant multi-minute
deduplication window. Held-out users validate the local mapping. Matching is
one-to-one and only between distinct source streams; unmatched occurrences are
retained. Production mutations require a separately frozen, backed-up plan.
"""
import bisect
import collections
import json
import argparse
import hashlib
import math
import sqlite3
import statistics
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path

from scripts.audit_danmaku_clock_bursts import singleton_index, quantile
from scripts.backfill_mit3uri_replay_danmaku import normalize_content
from scripts.audit_danmaku_clock_bursts import ro, replay_mapping, load_streams
from scripts.apply_source_first_changes import durable_json, repair_search
from scripts.replace_historical_source_database import retry
from src.db import danmaku_sources as store
from src.db.event_search import DEFAULT_SEARCH_DB_PATH, index_pending_events, _encode_rowid, _fts_query

ROOM = 1967216004
CUTOFF = store.EXTERNAL_CUTOFF_MS // 1000


def monotone_points(points):
    """Pool small sequence-order inversions before interpolating arrival times."""
    blocks = []
    for x, y in points:
        blocks.append(([x], y))
        while len(blocks) >= 2 and blocks[-2][1] > blocks[-1][1]:
            bx, by = blocks.pop()
            ax, ay = blocks.pop()
            blocks.append((ax+bx, (ay*len(ax)+by*len(bx))/(len(ax)+len(bx))))
    return [(x, y) for xs, y in blocks for x in xs]


def predict(points, rank):
    """Interpolate only inside a nearby pair of monotonically ordered anchors."""
    keys = [p[0] for p in points]
    k = bisect.bisect_left(keys, rank)
    if k < len(points) and points[k][0] == rank:
        return points[k][1]
    if k == 0 or k == len(points):
        return None
    (x0, y0), (x1, y1) = points[k-1:k+1]
    if x1-x0 > 100 or not 0 <= y1-y0 <= 60:
        return None
    return y0 + (y1-y0) * (rank-x0)/(x1-x0)


def sequence_model(source, reference, start, end):
    source = sorted(source, key=lambda r: (r['time'], r.get('sequence_order',r['id'])))
    left, right = singleton_index(source), singleton_index(reference)
    ranks = {r['id']: i for i, r in enumerate(source)}
    anchors = []
    for key, a in left.items():
        b = right.get(key)
        if b and start-30 <= a['time'] <= end+30 and abs(a['time']-b['time']) <= 1800:
            anchors.append((ranks[a['id']], b['time'], a['uid']))
    anchors.sort()
    # Isolated inconsistent anchors cannot bend the interpolation around them.
    # Temporal medians are used only as an outlier test, not as fitted times.
    clean = []
    for i, p in enumerate(anchors):
        near = anchors[max(0, i-3):i+4]
        if abs(p[1]-statistics.median(q[1] for q in near)) <= 15:
            clean.append(p)
    test = [(x, y, uid) for x, y, uid in anchors if start <= source[x]['time'] <= end]
    errors = []
    for x, y, uid in test:
        train = monotone_points([(xx, yy) for xx, yy, uu in clean if uu != uid])
        t = predict(train, x)
        if t is not None:
            errors.append(abs(t-y))
    validation = dict(anchors=len(anchors), clean_anchors=len(clean),
                      test=len(test), predicted_test=len(errors),
                      p95=quantile(errors, .95) if errors else None,
                      within7=sum(e <= 7 for e in errors)/len(errors) if errors else 0,
                      users=len({uid for x,y,uid in clean}))
    valid = (len(errors) >= 12 and len(errors) >= .7*len(test)
             and validation['users'] >= 8 and validation['within7'] >= .95
             and validation['p95'] <= 7)
    points = monotone_points([(x, y) for x, y, uid in clean]) if valid else []
    return source, ranks, points, validation, valid


def pair_occurrences(source, reference, start, end):
    source, ranks, points, validation, valid = sequence_model(source, reference, start, end)
    # Model support includes the surrounding anchors; only the requested burst
    # range is eligible for occurrence reconciliation.
    refs = collections.defaultdict(list)
    for r in reference:
        if r['uid']:
            refs[(r['uid'], normalize_content(r['content']))].append(r)
    targets = [r for r in source if start <= r['time'] <= end]
    edges = {}
    for a in targets:
        t = predict(points, ranks[a['id']])
        if t is None or not a['uid']:
            continue
        opts = [b for b in refs[(a['uid'], normalize_content(a['content']))] if abs(b['time']-t) <= 7]
        edges[a['id']] = opts
    # Disjoint UID/text components consume each reference occurrence at most
    # once. A source containing N repeats can never remove more than N repeats
    # from another source. Model support confines the edges to seven seconds.
    matched = {}
    by_id = {a['id']: a for a in targets}
    def claim(aid, seen):
        for b in edges.get(aid, []):
            if b['id'] in seen:
                continue
            seen.add(b['id'])
            if b['id'] not in matched or claim(matched[b['id']][0], seen):
                matched[b['id']] = (aid, b)
                return True
        return False
    for a in targets:
        claim(a['id'], set())
    pairs = [(by_id[aid], b) for aid, b in matched.values()]
    # Original provider IDs can bracket a message even when variable message
    # density prevents accurate interpolation. Four surrounding unique-text
    # anchors from at least three other users corroborate the local order.
    ia, ib = singleton_index(source), singleton_index(reference)
    anchors=sorted((ranks[a['id']],b['time'],a['uid'],a['id'])
                   for k,a in ia.items() if a.get('provider_order_restored')
                   and (b:=ib.get(k)) and start-600<=a['time']<=end+600
                   and abs(a['time']-b['time'])<=1800)
    already_a={a['id'] for a,b in pairs};already_b={b['id'] for a,b in pairs}
    bracket_edges={};bracket_proof={}
    for a in targets:
        if a['id'] in already_a or not a.get('provider_order_restored'):
            continue
        rank=ranks[a['id']]
        left=[p for p in anchors if p[0]<rank and p[2]!=a['uid']]
        right=[p for p in anchors if p[0]>rank and p[2]!=a['uid']]
        if len(left)<2 or len(right)<2:
            continue
        before,after=left[-1],right[0]
        witnesses=left[-2:]+right[:2]
        if len({p[2] for p in witnesses})<3 or any(y[1]<x[1]-2 for x,y in zip(witnesses,witnesses[1:])):
            continue
        if after[0]-before[0]>100 or not 0<=after[1]-before[1]<=60:
            continue
        opts=[b for b in refs[(a['uid'],normalize_content(a['content']))]
              if b['id'] not in already_b and before[1]-2<=b['time']<=after[1]+2]
        bracket_edges[a['id']]=opts
        bracket_proof[a['id']]=dict(before_source_id=before[3],after_source_id=after[3],
                                   reference_start=before[1],reference_end=after[1],
                                   witness_source_ids=[p[3] for p in witnesses],witness_users=[p[2] for p in witnesses])
    degree=collections.Counter(b['id'] for opts in bracket_edges.values() for b in opts)
    bracket_pairs=[(by_id[aid],opts[0]) for aid,opts in bracket_edges.items()
                   if len(opts)==1 and degree[opts[0]['id']]==1]
    pairs.extend(bracket_pairs)
    validation.update(sequence_validated=valid,bracket_pairs=len(bracket_pairs),
                      bracket_evidence={str(a['id']):bracket_proof[a['id']] for a,b in bracket_pairs})
    validation.update(targets=len(targets), matched=len(pairs),
                      unmatched=len(targets)-len(pairs))
    return pairs, validation


def reference_clock(row, stream, clocks):
    t = row['time']
    model = clocks.get(stream, {}).get('model')
    if model and model['start']-60 <= t <= model['end']+60:
        t -= model['intercept'] + model['slope']*t
    return t


def plan(candidate, replacement, audit, output):
    output.mkdir(parents=True, exist_ok=False)
    findings = collections.defaultdict(list)
    for line in (audit/'bursts.jsonl').open():
        h = json.loads(line)
        findings[h['session_id']].append(h)
    changes, reports, evidence = [], [], []
    with closing(ro(candidate/'rebuilt.sqlite3')) as c, closing(ro(replacement/'rollback.sqlite3')) as mapping, closing(ro(store.PRODUCTION_DB)) as main, closing(ro(store.ARCHIVE_DB)) as cold:
        reverse, locks = replay_mapping(main)
        for sid, hits in findings.items():
            session = json.loads(c.execute('SELECT report FROM build_session WHERE session_id=?', (sid,)).fetchone()[0])
            lo, hi = (datetime.fromisoformat(session[k]).timestamp() for k in ('start','end'))
            streams = load_streams(cold, lo, hi, [o for s in session['members'] for o in reverse[s]], locks)
            all_rows = {r['id']: r for rr in streams.values() for r in rr}
            stream_of = {r['id']: s for s, rr in streams.items() for r in rr}
            current = {}
            for row in c.execute("SELECT o.*,e.timestamp FROM event e INDEXED BY idx_event_room_time JOIN origin o ON o.event_id=e.id WHERE e.room_id=? AND e.timestamp>=? AND e.timestamp<? AND e.cmd='DANMU_MSG'", (ROOM,lo,hi)):
                if row['session_id'] != sid:
                    continue
                event_id = mapping.execute('SELECT event_id FROM event_map WHERE candidate_id=?', (row['event_id'],)).fetchone()[0]
                now=main.execute('SELECT timestamp FROM event WHERE id=?',(event_id,)).fetchone()
                if now is not None:
                    current[row['source_record_id']] = dict(event_id=event_id, timestamp=now['timestamp'])
            from scripts.danmaku_clock_policy import REGISTRY
            if REGISTRY.exists():
                for path in json.loads(REGISTRY.read_text()):
                    policy=json.loads(Path(path).read_text())
                    with closing(ro(Path(policy['plan'])/'rollback.sqlite3')) as applied:
                        for source_id,event_id in applied.execute('SELECT source_record_id,event_id FROM inserted_event'):
                            if source_id in all_rows:
                                now=main.execute('SELECT timestamp FROM event WHERE id=?',(event_id,)).fetchone()
                                if now is not None:
                                    current[source_id]=dict(event_id=event_id,timestamp=now['timestamp'])
            clocks = {r[0]: json.loads(r[1]) for r in c.execute('SELECT source,report FROM clock_report WHERE session_id=?', (sid,))}
            parent, members, scores, reasons = {}, {}, collections.Counter(), collections.defaultdict(list)
            def root(i):
                parent.setdefault(i, i)
                members.setdefault(i, {stream_of[i]: i})
                while parent[i] != i:
                    i = parent[i]
                return i
            def merge(a, b, reason):
                x, y = root(a['id']), root(b['id'])
                if x == y:
                    return
                if set(members[x]) & set(members[y]):
                    return
                parent[x] = y
                members[y].update(members[x])
                scores[b['id']] += 1
                reasons[b['id']].append(reason)
            local_reports, good_native_tails = [], []
            # Native references have first claim on occurrence identity.
            for h in sorted(hits, key=lambda h: (h['reference'].startswith('replay:'), -h['anchors'])):
                a, b = h['source'], h['reference']
                ref_times = [e['reference_time'] for e in h['evidence']]
                if len(set(ref_times)) < .75*len(ref_times) or len({int(t) for t in ref_times}) < 8:
                    local_reports.append(dict(source=a,reference=b,status='reference_clock_batched'))
                    continue
                begin = math.floor(h['start']/30)*30-15
                end = math.ceil(h['end']/30)*30+15
                pairs, validation = pair_occurrences(streams[a], streams[b], begin, end)
                method = 'sequence_cross_validation' if validation.get('sequence_validated') else 'provider_order_bracket'
                if not pairs:
                    # A shuffled batch has lost ordering. Only whole-session
                    # singleton long texts establish safe identity in that case.
                    at = [e['time'] for e in h['evidence']]
                    bspan = quantile(ref_times,.9)-quantile(ref_times,.1)
                    aspan = quantile(at,.9)-quantile(at,.1)
                    if h['anchors'] >= 12 and h['users'] >= 8 and bspan >= 80 and bspan >= 5*max(aspan,1):
                        ia, ib = singleton_index(streams[a]), singleton_index(streams[b])
                        pairs = [(x,ib[k]) for k,x in ia.items() if k in ib and len(k[1]) >= 6
                                 and begin <= x['time'] <= end and abs(x['time']-ib[k]['time']) <= 1800]
                        method = 'unordered_batch_unique_long_text'
                chosen_pairs = sum(x['id'] in current or y['id'] in current for x,y in pairs)
                local_reports.append(dict(source=a,reference=b,start=begin,end=end,
                                          method=method,validation=validation,pairs=len(pairs),selected_pairs=chosen_pairs))
                for x, y in pairs:
                    reason = dict(session_id=sid,source=a,reference=b,method=method,
                                  source_record_id=x['id'],reference_record_id=y['id'],
                                  source_time=x['time'],reference_time=y['time'])
                    bracket=validation.get('bracket_evidence',{}).get(str(x['id']))
                    if bracket:reason['provider_order_bracket']=bracket
                    merge(x, y, reason)
                    evidence.append(reason)
                if pairs and method == 'sequence_cross_validation' and not b.startswith('replay:'):
                    targets = [x for x in streams[a] if begin <= x['time'] <= end]
                    for run in c.execute('SELECT start,end,source FROM source_segment WHERE session_id=? AND start<=? AND end>?', (sid,h['start'],h['start'])):
                        if run['source'] == a:
                            good_native_tails.append((b, max(x['time'] for x in targets)+7, run['end']))
            groups = collections.defaultdict(list)
            for i in parent:
                groups[root(i)].append(i)
            n0 = len(changes)
            kept_refs, deleted_ids = set(), set()
            def snapshot(event_id):
                r = main.execute('SELECT * FROM event WHERE id=?', (event_id,)).fetchone()
                if r is None:
                    raise ValueError('Historical candidate no longer matches production')
                return dict(r)
            def provenance(event_id):
                return [dict(r) for r in main.execute('SELECT * FROM mit3uri_replay_danmaku_provenance WHERE event_id=?', (event_id,))]
            for ids in groups.values():
                selected = [i for i in ids if i in current]
                if not selected:
                    continue
                best = max(ids, key=lambda i:(scores[i],not stream_of[i].startswith('replay:'),i in current))
                keep = best if best in selected else min(selected, key=lambda i:abs(current[i]['timestamp']-reference_clock(all_rows[best],stream_of[best],clocks)))
                kept_refs.add(best)
                before_keep = snapshot(current[keep]['event_id'])
                proof = [e for i in ids for e in reasons[i]]
                for i in selected:
                    before = snapshot(current[i]['event_id'])
                    if before['timestamp'] != current[i]['timestamp']:
                        raise ValueError('Historical timestamp changed before planning')
                    if i != keep:
                        changes.append(dict(operation='delete',before=before,provenance=provenance(before['id']),
                                            keep=before_keep,session_id=sid,proof=proof))
                        deleted_ids.add(before['id'])
                    elif best != keep:
                        t = int(reference_clock(all_rows[best],stream_of[best],clocks))
                        if abs(t-before['timestamp']) > 7:
                            changes.append(dict(operation='retime',before=before,after={**before,'timestamp':t},
                                                provenance=provenance(before['id']),session_id=sid,
                                                reference_source_id=best,proof=proof))
            # Recover native messages in the quiet tail after a delayed batch.
            # Their true times are later than every raw timestamp in that batch,
            # so those delayed messages cannot represent these later arrivals.
            tail_inserted = set()
            for stream, begin, end in good_native_tails:
                for r in streams[stream]:
                    if not begin <= r['time'] < end or r['id'] in current or r['id'] in tail_inserted or r['id'] in kept_refs:
                        continue
                    t = int(reference_clock(r,stream,clocks))
                    if not r['uid']:
                        continue
                    nearby = main.execute("SELECT 1 FROM event INDEXED BY idx_event_uid_cmd WHERE uid=? AND cmd='DANMU_MSG' AND room_id=? AND timestamp BETWEEN ? AND ? AND content=? LIMIT 1", (r['uid'],ROOM,t-7,t+7,r['content'])).fetchone()
                    if nearby:
                        continue
                    after = dict(room_id=ROOM,cmd='DANMU_MSG',uid=r['uid'],uname=r['uname'],content=r['content'],gift_name=None,gift_num=None,total_coin=None,title=None,timestamp=t)
                    changes.append(dict(operation='insert',after=after,source_record_id=r['id'],session_id=sid,
                                        proof=dict(source=stream,quiet_tail_start=begin,quiet_tail_end=end)))
                    tail_inserted.add(r['id'])
            reports.append(dict(session_id=sid,date=session['start'][:10],checks=local_reports,
                                operations=dict(collections.Counter(x['operation'] for x in changes[n0:]))))
            print(sid,reports[-1]['operations'],flush=True)
        touched = [x['before']['id'] for x in changes if 'before' in x]
        if len(touched) != len(set(touched)):
            raise ValueError('An event occurs in more than one repair operation')
        for x in changes:
            validate(x)
        path = output/'changes.jsonl'
        with path.open('w') as f:
            for x in changes:
                f.write(json.dumps(x,ensure_ascii=False)+'\n')
        with (output/'evidence.jsonl').open('w') as f:
            for e in evidence:
                f.write(json.dumps(e,ensure_ascii=False)+'\n')
        durable_json(output/'report.json',dict(sessions=reports,operations=dict(collections.Counter(x['operation'] for x in changes)),
                     proposal_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),production_modified=False))


def validate(x):
    for key in ('before','after'):
        r=x.get(key)
        if r and (r['room_id'] != ROOM or r['cmd'] != 'DANMU_MSG' or not 0 < r['timestamp'] < CUTOFF):
            raise ValueError('Protected range or event kind')
    if x['operation'] == 'retime':
        if {k:v for k,v in x['before'].items() if k!='timestamp'} != {k:v for k,v in x['after'].items() if k!='timestamp'}:
            raise ValueError('Retime cannot change message identity')
        if abs(x['before']['timestamp']-x['after']['timestamp']) > 1800:
            raise ValueError('Clock correction exceeds evidence envelope')
    elif x['operation']=='delete':
        if x['before']['uid'] != x['keep']['uid'] or normalize_content(x['before']['content']) != normalize_content(x['keep']['content']):
            raise ValueError('Deletion lacks a matching retained occurrence')
    elif x['operation']!='insert':
        raise ValueError('Unknown operation')


def install_guards(conn):
    for action, refs in [('DELETE',['OLD']), ('INSERT',['NEW']), ('UPDATE',['OLD','NEW'])]:
        condition = ' OR '.join(f"{r}.room_id!={ROOM} OR {r}.cmd!='DANMU_MSG' OR {r}.timestamp IS NULL OR {r}.timestamp<=0 OR {r}.timestamp>={CUTOFF}" for r in refs)
        conn.execute(f"CREATE TEMP TRIGGER clock_guard_{action.lower()} BEFORE {action} ON main.event WHEN {condition} BEGIN SELECT RAISE(ABORT,'protected event'); END")


def apply_batch(main, items):
    inserted = []
    for x in items:
        validate(x)
        if 'before' in x:
            b=x['before']
            row=main.execute('SELECT * FROM event WHERE id=?',(b['id'],)).fetchone()
            if row is None or dict(row)!=b:
                raise ValueError('Snapshot changed before repair')
            prov=[dict(r) for r in main.execute('SELECT * FROM mit3uri_replay_danmaku_provenance WHERE event_id=?',(b['id'],))]
            if prov!=x['provenance']:
                raise ValueError('Replay provenance changed')
        if x['operation']=='delete':
            k=main.execute('SELECT uid,content FROM event WHERE id=?',(x['keep']['id'],)).fetchone()
            if not k or k['uid']!=b['uid'] or normalize_content(k['content'])!=normalize_content(b['content']):
                raise ValueError('Retained occurrence missing')
            main.execute('DELETE FROM mit3uri_replay_danmaku_provenance WHERE event_id=?',(b['id'],))
            main.execute('DELETE FROM event WHERE id=?',(b['id'],))
        elif x['operation']=='retime':
            main.execute('UPDATE event SET timestamp=? WHERE id=?',(x['after']['timestamp'],b['id']))
            main.execute("UPDATE mit3uri_replay_danmaku_provenance SET mapped_timestamp_ms=?,alignment_method='clock_burst_cross_source' WHERE event_id=?",(x['after']['timestamp']*1000,b['id']))
        else:
            r=x['after']; fields=list(r)
            cur=main.execute(f"INSERT INTO event({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",list(r.values()))
            inserted.append(dict(source_record_id=x['source_record_id'],event_id=cur.lastrowid))
    return inserted


def protected_digest(main):
    digest=hashlib.sha256(); count=0
    for row in main.execute("SELECT * FROM event INDEXED BY idx_event_room_time WHERE room_id=? AND timestamp<? AND cmd!='DANMU_MSG' ORDER BY timestamp,id",(ROOM,CUTOFF)):
        digest.update(json.dumps(list(row),ensure_ascii=False).encode());count+=1
    return dict(count=count,sha256=digest.hexdigest())


def apply_plan(output, main_path=store.PRODUCTION_DB, search_path=DEFAULT_SEARCH_DB_PATH):
    path=output/'changes.jsonl'; sha=hashlib.sha256(path.read_bytes()).hexdigest()
    report=json.loads((output/'report.json').read_text())
    if sha!=report['proposal_sha256']:
        raise ValueError('Repair plan hash changed')
    items=[json.loads(line) for line in path.read_text().splitlines()]
    for x in items:
        validate(x)
    removed={x['before']['id'] for x in items if x['operation']=='delete'}
    if any(x['keep']['id'] in removed for x in items if x['operation']=='delete'):
        raise ValueError('A retained occurrence is also scheduled for deletion')
    with closing(sqlite3.connect(main_path,timeout=.1)) as main, closing(sqlite3.connect(search_path,timeout=.1)) as search, closing(sqlite3.connect(output/'rollback.sqlite3')) as backup:
        main.row_factory=sqlite3.Row
        main.execute('PRAGMA busy_timeout=100'); search.execute('PRAGMA busy_timeout=100')
        backup.execute('PRAGMA synchronous=FULL')
        backup.execute('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT)')
        backup.execute('CREATE TABLE IF NOT EXISTS old_event(event_id INTEGER PRIMARY KEY,snapshot TEXT NOT NULL,provenance TEXT NOT NULL)')
        backup.execute('CREATE TABLE IF NOT EXISTS inserted_event(source_record_id INTEGER PRIMARY KEY,event_id INTEGER UNIQUE NOT NULL)')
        existing=backup.execute("SELECT value FROM meta WHERE key='plan_sha256'").fetchone()
        if existing and existing[0]!=sha:
            raise ValueError('Backup belongs to a different plan')
        if not existing:
            with backup:
                backup.execute("INSERT INTO meta VALUES('plan_sha256',?)",(sha,))
                backup.execute("INSERT INTO meta VALUES('protected_before',?)",(json.dumps(protected_digest(main)),))
                for x in items:
                    if 'before' in x:
                        backup.execute('INSERT INTO old_event VALUES(?,?,?)',(x['before']['id'],json.dumps(x['before'],ensure_ascii=False),json.dumps(x['provenance'],ensure_ascii=False)))
        watermark=int(search.execute("SELECT value FROM event_search_meta WHERE key='last_indexed_event_id'").fetchone()[0])
        if watermark<max((x['before']['id'] for x in items if 'before' in x),default=0):
            raise ValueError('Existing search index must catch up first')
        retry(lambda:main.execute('CREATE TABLE IF NOT EXISTS danmaku_clock_repair_batch(run_id TEXT NOT NULL,batch INTEGER NOT NULL,inserted_ids TEXT NOT NULL,PRIMARY KEY(run_id,batch)) WITHOUT ROWID'))
        install_guards(main)
        largest=0
        for pos in range(0,len(items),60):
            batch=items[pos:pos+60]; number=pos//60+1
            row=main.execute('SELECT inserted_ids FROM danmaku_clock_repair_batch WHERE run_id=? AND batch=?',(sha,number)).fetchone()
            if row:
                inserted=json.loads(row[0])
            else:
                def write():
                    nonlocal largest
                    started=time.monotonic()
                    main.set_progress_handler(lambda:int(time.monotonic()-started>.4),1000)
                    try:
                        with main:
                            main.execute('BEGIN IMMEDIATE')
                            ids=apply_batch(main,batch)
                            main.execute('INSERT INTO danmaku_clock_repair_batch VALUES(?,?,?)',(sha,number,json.dumps(ids)))
                        largest=max(largest,time.monotonic()-started)
                        return ids
                    finally:
                        main.set_progress_handler(None,0)
                inserted=retry(write)
            retry(lambda:repair_search(search,[x for x in batch if 'before' in x],'clock_burst:'+sha,pos+len(batch)))
            with backup:
                for row in inserted:
                    backup.execute('INSERT OR IGNORE INTO inserted_event VALUES(?,?)',(row['source_record_id'],row['event_id']))
            durable_json(output/'execution.json',dict(complete=False,done=pos+len(batch),total=len(items),plan_sha256=sha,largest_transaction_seconds=largest))
            if number%10==0:
                print('applied',pos+len(batch),'/',len(items),flush=True)
            time.sleep(.03)
        highest=backup.execute('SELECT MAX(event_id) FROM inserted_event').fetchone()[0] or 0
        while highest:
            progress=index_pending_events(main_path,search_path,batch_size=1000,max_batches=5)
            if progress['last_indexed_event_id']>=highest:
                break
            time.sleep(.05)
        protected_before=json.loads(backup.execute("SELECT value FROM meta WHERE key='protected_before'").fetchone()[0])
        if protected_digest(main)!=protected_before:
            raise ValueError('Historical non-danmaku events changed')
        checked_search=0
        for x in items:
            if x['operation']=='insert':
                eid=backup.execute('SELECT event_id FROM inserted_event WHERE source_record_id=?',(x['source_record_id'],)).fetchone()[0]
                expected={**x['after'],'id':eid}
            else:
                eid=x['before']['id'];expected=x.get('after')
            row=main.execute('SELECT * FROM event WHERE id=?',(eid,)).fetchone()
            if (dict(row) if row else None)!=expected:
                raise ValueError('Post-write event verification failed')
            if 'before' in x:
                b=x['before']
                if b['uid'] is not None and b['content']:
                    hit=search.execute('SELECT rowid FROM event_fts WHERE rowid=? AND event_fts MATCH ?',(_encode_rowid(b['timestamp'],eid),_fts_query(ROOM,b['content'][:2]))).fetchone()
                    if hit:
                        raise ValueError('Stale search document remains')
                    checked_search+=1
            if expected and expected['uid'] is not None and expected['content']:
                hit=search.execute('SELECT rowid FROM event_fts WHERE rowid=? AND event_fts MATCH ?',(_encode_rowid(expected['timestamp'],eid),_fts_query(ROOM,expected['content'][:2]))).fetchone()
                if not hit:
                    raise ValueError('Corrected search document missing')
                checked_search+=1
        result=dict(complete=True,verified=True,operations=report['operations'],plan_sha256=sha,
                    protected_non_danmaku=protected_before,protected_scope_enforced=True,
                    largest_transaction_seconds=largest,search_documents_checked=checked_search,
                    backup=str((output/'rollback.sqlite3').resolve()))
        durable_json(output/'execution.json',result)
        print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--candidate',type=Path)
    p.add_argument('--replacement',type=Path)
    p.add_argument('--audit',type=Path)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--apply',action='store_true')
    p.add_argument('--confirm-cutoff',type=int)
    args=p.parse_args()
    if args.apply:
        if args.confirm_cutoff!=CUTOFF:
            p.error(f'--confirm-cutoff {CUTOFF} required')
        apply_plan(args.output)
    else:
        plan(args.candidate,args.replacement,args.audit,args.output)


if __name__=='__main__':
    main()
