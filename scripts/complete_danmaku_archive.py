"""Resume historical source archiving and validate the saved session review.

This batch never controls online services. Duplicate decisions are retained
without applying deletions. A report records partial network failures.
"""
import hashlib
import json
import sqlite3
import time
import zlib
from contextlib import closing

from scripts.archive_danmaku_sources import archive_danmakus, archive_legacy
from scripts.reconcile_danmaku_archive import (
    ROOT, enforce_policy, link_exact, retain_review, remove_video_comments,
)
from src.db import danmaku_sources as store


def main():
    report = {}
    target = ROOT/'data/reports/source_archive_final.json'
    with closing(store.connect_archive()) as conn:
        conn.execute('PRAGMA foreign_keys=ON')
        with conn:
            store.initialize(conn)
        for attempt in range(3):
            report['danmakus_fetch'] = archive_danmakus(conn, .75)
            if not report['danmakus_fetch']['failures']:
                break
            if attempt<2:
                time.sleep(30)
        print('phase: enforce cutoff', flush=True)
        report['policy'] = enforce_policy(conn)
        print('phase: exact source associations', flush=True)
        report['danmakus_new_links'] = link_exact(conn, 'danmakus')
        print('phase: monitor stock watermark', flush=True)
        report['stock'] = archive_legacy(conn)
        print('phase: verify session proposals against archive', flush=True)
        report['review'] = retain_review(conn, ROOT/'data/reports/danmaku_sessions_review_archived_20261003/verified_pairs.jsonl')
        print('phase: verify removed video comments', flush=True)
        report['video_comments'] = remove_video_comments(conn, ROOT/'data/reports/danmaku_integrity_20261003/imported_video_comments.csv')
        conn.row_factory = None
        report['source_counts'] = dict(conn.execute('SELECT source,COUNT(*) FROM danmaku_source_record GROUP BY source'))
        report['artifact_checks'] = {}
        for source in ('monitor', 'vtbcat', 'replay', 'danmakus'):
            ids = [r[0] for r in conn.execute('SELECT id FROM danmaku_source_artifact WHERE source=? ORDER BY id', (source,))]
            chosen = sorted({ids[round(i*(len(ids)-1)/min(24,len(ids)-1))] for i in range(min(25,len(ids)))}) if len(ids)>1 else ids
            for aid in chosen:
                sha, payload = conn.execute('SELECT sha256,payload FROM danmaku_source_artifact WHERE id=?', (aid,)).fetchone()
                if hashlib.sha256(zlib.decompress(payload)).hexdigest()!=sha:
                    raise ValueError(f'Artifact checksum mismatch: {aid}')
            report['artifact_checks'][source] = dict(total=len(ids), verified_samples=chosen)
        report['complete'] = not report['danmakus_fetch']['failures'] and not report['review'].get('archive_check_failed')
        report['duplicates_deleted'] = 0
        with conn:
            store.checkpoint(conn, 'summary:final_archive', store.encode(report))
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__=='__main__':
    main()
