"""Validate archived evidence, retain review decisions, and remove video comments.

Duplicate pairs remain proposals. Video-comment removals require matching raw
source attribution and retain the complete event and import provenance.
"""
import argparse
import csv
import json
import sqlite3
import time
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

from src.db import danmaku_sources as store
from scripts.backfill_mit3uri_replay_danmaku import ROOT, ROOM_ID, normalize_content


def link_exact(conn, source, *, sessions=None, revision='v1'):
    sessions = [(s,) for s in sessions] if sessions is not None else conn.execute('SELECT DISTINCT source_session FROM danmaku_source_record WHERE source=?', (source,)).fetchall()
    stats = Counter()
    for (session,) in sessions:
        if source=='danmakus' and not conn.execute('SELECT 1 FROM danmaku_source_checkpoint WHERE key=?', ('danmakus-live:'+session,)).fetchone():
            continue
        key = f'exact-links:{revision}:{source}:{session}'
        if conn.execute('SELECT 1 FROM danmaku_source_checkpoint WHERE key=?', (key,)).fetchone():
            continue
        rows = conn.execute("SELECT id,uid,content,source_timestamp_ms FROM danmaku_source_record WHERE source=? AND source_session=? AND eligibility='live'", (source, session)).fetchall()
        if not rows:
            continue
        low, high = min(r[3] for r in rows)//1000, max(r[3] for r in rows)//1000
        events = defaultdict(list)
        for eid, uid, text, ts in conn.execute("SELECT id,uid,content,timestamp FROM event WHERE room_id=? AND timestamp BETWEEN ? AND ? AND cmd='DANMU_MSG'", (ROOM_ID, low, high)):
            events[(uid, text, ts)].append(eid)
        groups = defaultdict(list)
        for sid, uid, text, ts in rows:
            groups[(uid, text, ts//1000)].append(sid)
        links = [(ids[0], events[k][0], 'exact_second_unique') for k, ids in groups.items() if len(ids)==1 and len(events[k])==1]
        for start in range(0, len(links), 500):
            with conn:
                conn.executemany('INSERT OR IGNORE INTO danmaku_source_link VALUES(?,?,?)', links[start:start+500])
            time.sleep(.02)
        stats['linked'] += len(links)
        stats['unresolved'] += len(rows)-len(links)
        with conn:
            store.checkpoint(conn, key, store.encode(dict(rows=len(rows), linked=len(links))))
    return dict(stats)


def retain_review(conn, path):
    stats = Counter()
    run_id = path.parent.name
    batch = []
    def flush():
        with conn:
            conn.executemany('INSERT OR IGNORE INTO danmaku_merge_review(run_id,extra_event_id,baseline_event_id,decision,evidence) VALUES(?,?,?,?,?)', batch)
        batch.clear()
        time.sleep(.02)
    with path.open() as handle:
        for line in handle:
            evidence = json.loads(line)
            extra, baseline = evidence['extra_event_id'], evidence['baseline_event_id']
            source = conn.execute("SELECT r.id,r.uid,r.content,r.eligibility FROM danmaku_source_record r JOIN danmaku_source_link l ON l.source_id=r.id JOIN event e ON e.id=l.event_id WHERE l.event_id=? AND r.source='replay'", (extra,)).fetchall()
            original = conn.execute("SELECT uid,content FROM event WHERE id=? AND cmd='DANMU_MSG'", (baseline,)).fetchone()
            valid = (len(source)==1 and source[0][3] in ('live','outside_policy') and original is not None
                     and source[0][1]==original[0]==evidence['uid']
                     and normalize_content(source[0][2])==normalize_content(original[1])==normalize_content(evidence['content']))
            # Text equality was normalized by the session reviewer. Keep both
            # raw strings in the review so normalization remains auditable.
            evidence['archived_source_id'] = source[0][0] if len(source)==1 else None
            evidence['baseline_content'] = original[1] if original else None
            evidence['source_content'] = source[0][2] if len(source)==1 else None
            evidence['source_eligibility'] = source[0][3] if len(source)==1 else None
            origins = [r[0] for r in conn.execute('SELECT DISTINCT r.source FROM danmaku_source_link l JOIN danmaku_source_record r ON r.id=l.source_id WHERE l.event_id=?', (baseline,))]
            evidence['baseline_sources'] = origins
            evidence['preferred_origin'] = 'monitor' if 'monitor' in origins else next((s for s in ('danmakus','vtbcat') if s in origins), 'unresolved')
            decision = 'verified_candidate' if valid else 'archive_check_failed'
            stats[decision] += 1
            batch.append((run_id, extra, baseline, decision, store.encode(evidence)))
            if len(batch)>=500:
                flush()
    if batch:
        flush()
    held = path.with_name('previous_unconfirmed.jsonl')
    if held.exists():
        with held.open() as handle:
            for line in handle:
                evidence = json.loads(line)
                batch.append((run_id, evidence['extra_event_id'], evidence.get('previous_baseline_event_id'), 'needs_review', store.encode(evidence)))
                stats['needs_review'] += 1
                if len(batch)>=500:
                    flush()
        if batch:
            flush()
    return dict(stats)


def remove_video_comments(conn, report, apply=False):
    conn.row_factory = sqlite3.Row
    with report.open(encoding='utf-8-sig') as handle:
        candidates = list(csv.DictReader(handle))
    stats = Counter()
    for item in candidates:
        eid = int(item['event_id'])
        with conn:
            event = conn.execute('SELECT * FROM event WHERE id=?', (eid,)).fetchone()
            if event is None:
                if not conn.execute('SELECT 1 FROM danmaku_removed_event WHERE event_id=?', (eid,)).fetchone():
                    raise ValueError(f'Event {eid} missing without backup')
                stats['already_removed'] += 1
                continue
            provenance = conn.execute('SELECT * FROM mit3uri_replay_danmaku_provenance WHERE event_id=?', (eid,)).fetchall()
            source = conn.execute("SELECT r.* FROM danmaku_source_record r JOIN danmaku_source_link l ON l.source_id=r.id WHERE l.event_id=? AND r.source='replay' AND r.source_key=?", (eid, f"{item['cid']}:{item['dm_id']}")).fetchone()
            if not (source and source['eligibility']=='video_comment' and source['artifact_id'] and
                    event['cmd']=='DANMU_MSG' and event['room_id']==ROOM_ID and
                    event['content']==source['content']==item['content'] and event['uid']==int(item['uid']) and
                    len(provenance)==1 and provenance[0]['match_kind']=='inserted'):
                raise ValueError(f'Unsafe video-comment removal: {eid}')
            if apply:
                conn.execute('INSERT INTO danmaku_removed_event(event_id,reason,event_json,provenance_json) VALUES(?,?,?,?)',
                             (eid, 'replay_viewer_comment', store.encode(dict(event)), store.encode([dict(p) for p in provenance])))
                conn.execute('DELETE FROM mit3uri_replay_danmaku_provenance WHERE event_id=?', (eid,))
                conn.execute('DELETE FROM event WHERE id=?', (eid,))
            stats['removed' if apply else 'validated'] += 1
    return dict(stats)


def enforce_policy(conn):
    changed = 0
    for source in ('vtbcat', 'danmakus', 'replay'):
        column = 'mapped_timestamp_ms' if source=='replay' else 'source_timestamp_ms'
        ids = conn.execute(f"SELECT id FROM danmaku_source_record WHERE source=? AND eligibility='live' AND {column}>=?", (source, store.EXTERNAL_CUTOFF_MS)).fetchall()
        for start in range(0, len(ids), 500):
            with conn:
                conn.executemany("UPDATE danmaku_source_record SET eligibility='outside_policy' WHERE id=?", ids[start:start+500])
            changed += len(ids[start:start+500])
            time.sleep(.02)
    with conn:
        store.checkpoint(conn, 'policy:external_cutoff_ms', store.EXTERNAL_CUTOFF_MS)
    return dict(outside_policy=changed, external_cutoff_ms=store.EXTERNAL_CUTOFF_MS)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['link', 'review', 'video-comments', 'policy'])
    parser.add_argument('--source', choices=['vtbcat', 'danmakus'])
    parser.add_argument('--review', type=Path, default=ROOT/'data/reports/danmaku_sessions_review_20261003_adaptive/verified_pairs.jsonl')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.apply:
        parser.error('Cold-archive reconciliation is read-only for event; use a separately reviewed live-data migration to apply deletions')
    with closing(store.connect_archive()) as conn:
        conn.execute('PRAGMA foreign_keys=ON')
        with conn:
            store.initialize(conn)
        if args.action=='policy':
            result = enforce_policy(conn)
        elif args.action=='link':
            if not args.source:
                parser.error('--source is required for link')
            result = link_exact(conn, args.source)
        elif args.action=='review':
            result = retain_review(conn, args.review)
        else:
            result = remove_video_comments(conn, ROOT/'data/reports/danmaku_integrity_20261003/imported_video_comments.csv', args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__=='__main__':
    main()
