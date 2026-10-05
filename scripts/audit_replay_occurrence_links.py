"""Audit sampled replay occurrence links against calibrated native timestamps.

This read-only diagnostic retains source identities and archived event links.
Saved clock summaries lack the full anchor-distance check, so these findings
are evidence for review and must not be used directly as deletion proposals.
"""
import argparse
import collections
import csv
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from src.db import danmaku_sources as store
from scripts.backfill_mit3uri_replay_danmaku import ROOM_ID, normalize_content
from scripts.audit_mit3uri_danmaku_integrity import save_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--review', type=Path, required=True)
    parser.add_argument('--models', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--all-candidates', action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    with (args.review / ('candidates.csv' if args.all_candidates else 'samples.csv')).open(encoding='utf-8-sig') as f:
        samples = list(csv.DictReader(f))
    with args.models.open(encoding='utf-8-sig') as f:
        models = {(int(r['cid']), int(r['cell'])): r for r in csv.DictReader(f)}
    ids = sorted({int(r[k]) for r in samples for k in ('extra_event_id', 'baseline_event_id')})
    events, provenance = {}, {}
    with closing(sqlite3.connect(store.PRODUCTION_DB.as_uri() + '?mode=ro', uri=True)) as main_db:
        main_db.row_factory = sqlite3.Row
        for start in range(0, len(ids), 400):
            batch = ids[start:start + 400]
            marks = ','.join('?' for _ in batch)
            events.update((r['id'], dict(r)) for r in main_db.execute(
                f'SELECT id,uid,content,timestamp FROM event WHERE id IN ({marks})', batch))
            provenance.update((r['event_id'], dict(r)) for r in main_db.execute(
                f'SELECT * FROM mit3uri_replay_danmaku_provenance WHERE event_id IN ({marks})', batch))
    counts = collections.Counter()
    with closing(sqlite3.connect(store.ARCHIVE_DB.as_uri() + '?mode=ro', uri=True)) as cold, (args.output / 'evidence.jsonl').open('w') as output:
        cold.row_factory = sqlite3.Row
        for n, sample in enumerate(samples, 1):
            observations = {}
            for side in ('extra', 'baseline'):
                eid = int(sample[side + '_event_id'])
                event, prov = events.get(eid), provenance.get(eid)
                if not event or not prov:
                    continue
                model = models.get((prov['cid'], prov['progress_ms'] // 300000))
                progress = prov['progress_ms'] / 1000
                if not model or not float(model['start']) <= progress <= float(model['end']):
                    continue
                calibrated = prov['mapped_timestamp_ms'] / 1000 - float(model['intercept']) - float(model['slope']) * progress
                tolerance = float(model['tolerance'])
                if (calibrated + tolerance) * 1000 >= store.EXTERNAL_CUTOFF_MS:
                    continue
                native = [dict(r) for r in cold.execute(
                    "SELECT id,source,source_session,source_key,source_timestamp_ms,content "
                    "FROM danmaku_source_record INDEXED BY idx_dm_source_time "
                    "WHERE room_id=? AND source_timestamp_ms BETWEEN ? AND ? AND uid=? "
                    "AND source IN ('danmakus','vtbcat') AND eligibility='live'",
                    (ROOM_ID, (calibrated - tolerance) * 1000, (calibrated + tolerance) * 1000, event['uid']))
                    if normalize_content(r['content']) == normalize_content(event['content'])]
                for row in native:
                    row['event_links'] = [dict(link) for link in cold.execute(
                        'SELECT event_id,method FROM danmaku_source_link WHERE source_id=?', (row['id'],))]
                observations[side] = dict(event=event, replay_dm_id=prov['dm_id'], progress_ms=prov['progress_ms'],
                    calibrated_timestamp=calibrated, tolerance=tolerance, native=native)
            extra, baseline = observations.get('extra', {}), observations.get('baseline', {})
            target = int(sample['baseline_event_id'])
            extra_ids = {link['event_id'] for r in extra.get('native', []) for link in r['event_links'] if link['method']=='exact_second_unique'}
            base_ids = {link['event_id'] for r in baseline.get('native', []) for link in r['event_links'] if link['method']=='exact_second_unique'}
            if target in extra_ids and base_ids and target not in base_ids:
                status = 'old_replay_link_shift_supported'
            elif target in extra_ids:
                status = 'extra_matches_native_target'
            else:
                status = 'unresolved'
            counts[status] += 1
            output.write(json.dumps(dict(sample=sample, status=status, observations=observations), ensure_ascii=False) + '\n')
            if n % 200 == 0:
                print(f'audit {n}/{len(samples)} {dict(counts)}', flush=True)
    report = dict(event_mutated=False, examined_candidates=len(samples), sampling='all' if args.all_candidates else 'representative', statuses=dict(counts), deletion_authorized=False)
    save_json(args.output / 'summary.json', report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
