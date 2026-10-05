"""Audit compressed source clocks using independent-user occurrence anchors.

Only singleton UID/text occurrences within a session are used as anchors.
Compression requires several users and a wide reference-time span; identical
short messages and a uniform clock offset cannot establish a burst. All inputs
are read-only. Reports retain source record IDs for reproducible review.
"""
import argparse
import collections
import itertools
import json
import math
import sqlite3
import statistics
from contextlib import closing
from datetime import datetime
from pathlib import Path

from scripts.backfill_mit3uri_replay_danmaku import normalize_content
from scripts.plan_danmaku_source_segments import map_replay_sessions
from scripts.apply_source_first_changes import durable_json
from src.db import danmaku_sources as store


def singleton_index(rows):
    groups = collections.defaultdict(list)
    for r in rows:
        text = normalize_content(r['content'])
        if r['uid'] and len(text) >= 4 and not text.startswith('['):
            groups[(r['uid'], text)].append(r)
    return {k: v[0] for k, v in groups.items() if len(v) == 1}


def quantile(values, q):
    values = sorted(values)
    return values[int((len(values) - 1) * q)]


def find_bursts(left, right):
    anchors = [(a, right[k]) for k, a in left.items() if k in right
               and abs(a['time'] - right[k]['time']) <= 1800]
    if len(anchors) < 12:
        return []
    baseline = statistics.median(a['time'] - b['time'] for a, b in anchors)
    hits = []
    for phase in (0, 15):
        bins = collections.defaultdict(list)
        for a, b in anchors:
            bins[math.floor((a['time'] - phase) / 30)].append((a, b))
        for pairs in bins.values():
            if len(pairs) < 8 or len({a['uid'] for a, b in pairs}) < 5:
                continue
            at = [a['time'] for a, b in pairs]
            bt = [b['time'] for a, b in pairs]
            if len(set(bt)) < .75*len(bt) or len({int(t) for t in bt}) < 8:
                continue
            a_span = quantile(at, .9) - quantile(at, .1)
            b_span = quantile(bt, .9) - quantile(bt, .1)
            residual = statistics.median(a['time'] - b['time'] for a, b in pairs) - baseline
            if b_span < 45 or b_span < 3 * max(a_span, 1) or residual < 20:
                continue
            hits.append(pairs)
    groups = []
    for pairs in sorted(hits, key=lambda pp: min(a['time'] for a, b in pp)):
        lo = min(a['time'] for a, b in pairs)
        if groups and lo <= max(a['time'] for a, b in groups[-1].values()) + 30:
            groups[-1].update({a['id']: (a, b) for a, b in pairs})
        else:
            groups.append({a['id']: (a, b) for a, b in pairs})
    result = []
    for group in groups:
        pairs = sorted(group.values(), key=lambda p: p[0]['time'])
        result.append(dict(
            start=min(a['time'] for a, b in pairs), end=max(a['time'] for a, b in pairs),
            reference_start=min(b['time'] for a, b in pairs),
            reference_end=max(b['time'] for a, b in pairs),
            baseline_offset=baseline, anchors=len(pairs), users=len({a['uid'] for a, b in pairs}),
            evidence=[dict(source_id=a['id'], reference_id=b['id'], uid=a['uid'],
                           content=a['content'], time=a['time'], reference_time=b['time'])
                      for a, b in pairs]))
    return result


def load_streams(cold, start, end, old_sessions, locks):
    streams = collections.defaultdict(list)
    for row in cold.execute("SELECT id,source,source_session,uid,uname,content,source_timestamp_ms,evidence FROM danmaku_source_record INDEXED BY idx_dm_source_time WHERE room_id=? AND source_timestamp_ms>=? AND source_timestamp_ms<? AND source IN ('danmakus','vtbcat') AND eligibility='live'", (1967216004, (start-600)*1000, min(store.EXTERNAL_CUTOFF_MS, (end+600)*1000))):
        r = dict(row)
        e = json.loads(r.pop('evidence'))
        r['sequence_order'] = e.get('upstream_id', e.get('sequence', r['id']))
        r['provider_order_restored'] = 'upstream_id' in e
        r['time'] = r['source_timestamp_ms'] / 1000
        streams[r['source'] + ':' + r['source_session']].append(r)
    # Some native providers share a timestamped batch but return it in a
    # different order. The original provider message ID preserves chronology.
    original = collections.defaultdict(list)
    for key, rr in streams.items():
        if key.startswith('vtbcat:'):
            for r in rr:
                original[(r['source_timestamp_ms'],r['uid'],r['content'])].append(r['sequence_order'])
    for key, rr in streams.items():
        if not key.startswith('danmakus:'):
            continue
        groups=collections.defaultdict(list)
        for r in rr:
            groups[(r['source_timestamp_ms'],r['uid'],r['content'])].append(r)
        for ident, items in groups.items():
            order=sorted(set(original.get(ident,[])))
            if len(order)==len(items):
                for r, rank in zip(sorted(items,key=lambda r:r['sequence_order']),order):
                    r['sequence_order']=rank
                    r['provider_order_restored']=True
    for old in old_sessions:
        if old not in locks:
            continue
        for row in cold.execute("SELECT id,source,source_session,uid,uname,content,evidence FROM danmaku_source_record WHERE source='replay' AND source_session=? AND eligibility='live'", (old,)):
            r = dict(row)
            e = json.loads(r.pop('evidence'))
            if e.get('dm_from') != 3:
                continue
            r['time'] = (locks[old]['base_ms'] + e['session_progress_ms']) / 1000
            streams['replay:' + str(e['cid'])].append(r)
    return dict(streams)


def replay_mapping(main):
    meta = json.loads((store.ROOT / 'data/mit3uri_replay_danmaku/metadata.json').read_text())['recordings']
    locks = json.loads((store.ROOT / 'data/mit3uri_replay_danmaku/alignment_locks.json').read_text())['sessions']
    mapping, _ = map_replay_sessions(meta, dict(main.execute('SELECT bvid,session_id FROM mit3uri_replay_recording')))
    reverse = collections.defaultdict(list)
    for old, new in mapping.items():
        reverse[new].append(old)
    return reverse, locks


def ro(path):
    conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--candidate', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    with closing(ro(args.candidate / 'rebuilt.sqlite3')) as candidate, closing(ro(store.PRODUCTION_DB)) as main_db, closing(ro(store.ARCHIVE_DB)) as cold:
        reverse, locks = replay_mapping(main_db)
        sessions = [json.loads(r[0]) for r in candidate.execute('SELECT report FROM build_session')]
        summary = []
        with (args.output / 'bursts.jsonl').open('w') as out:
            for n, session in enumerate(sessions, 1):
                sid = session['session_id']
                start = datetime.fromisoformat(session['start']).timestamp()
                end = datetime.fromisoformat(session['end']).timestamp()
                old = list(itertools.chain.from_iterable(reverse[s] for s in session['members']))
                streams = load_streams(cold, start, end, old, locks)
                indexes = {k: singleton_index(v) for k, v in streams.items()}
                found = []
                for a, b in itertools.permutations(indexes, 2):
                    for hit in find_bursts(indexes[a], indexes[b]):
                        if not start <= hit['start'] < end:
                            continue
                        hit.update(session_id=sid, source=a, reference=b)
                        found.append(hit)
                        out.write(json.dumps(hit, ensure_ascii=False) + '\n')
                if found:
                    out.flush()
                    summary.append(dict(session_id=sid, date=session['start'][:10],
                                        findings=len(found), sources=sorted({h['source'] for h in found})))
                    print('found', summary[-1], flush=True)
                if n % 20 == 0:
                    print('scanned', n, '/', len(sessions), flush=True)
        durable_json(args.output / 'summary.json', dict(sessions_scanned=len(sessions),
                     affected_sessions=len(summary), findings=sum(s['findings'] for s in summary),
                     sessions=summary, production_modified=False))


if __name__ == '__main__':
    main()
