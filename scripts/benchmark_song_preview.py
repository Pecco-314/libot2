"""只读对照正式歌单，独立保存窗口缓存和逐日评测结果。"""
import argparse
from collections import Counter
from datetime import datetime
import json
import logging
from pathlib import Path
import sqlite3
import time

from src.common.song_preview_store import PersistentPreviewRecognizer
from src.common.song_evidence import select_preview_entries
from src.db.event import list_live_sessions_by_date


def lcs(a, b):
    row = [0] * (len(b) + 1)
    for x in a:
        prev = row[:]
        for j, y in enumerate(b, 1):
            row[j] = prev[j-1] + 1 if x == y else max(prev[j], row[j-1])
    return row[-1]


def evaluate(expected, entries):
    a = Counter(expected)
    predicted = [x['title'] for x in entries]
    b = Counter(predicted)
    return {'expected': len(expected), 'detected': len(predicted),
            'matched_count': sum((a & b).values()), 'lcs': lcs(expected, predicted),
            'missing': dict(a-b), 'extra': dict(b-a),
            'unique_expected': len(a), 'unique_matched': len(a.keys() & b.keys())}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--days', type=int, default=12)
    parser.add_argument('--dates', nargs='+')
    parser.add_argument('--summary', default='results.json')
    parser.add_argument('--output', default='data/maintenance/preview_benchmark_20261026')
    args = parser.parse_args()
    root = Path(args.output)
    root.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    conn = sqlite3.connect('file:data/libot.db?mode=ro', uri=True)
    dates = args.dates or [r[0] for r in conn.execute('SELECT DISTINCT record_date FROM song_record ORDER BY record_date DESC LIMIT ?', (args.days,))]
    recognizer = PersistentPreviewRecognizer(root)
    results = []
    for date in dates:
        tick = time.monotonic()
        records = conn.execute('SELECT s.title,s.original_singer,s.language,length(s.lyrics_cleaned) FROM song_record r JOIN song_info s ON s.id=r.song_id WHERE r.record_date=? ORDER BY r.id', (date,)).fetchall()
        expected = [r[0] for r in records]
        timelines, sessions = [], []
        for session in list_live_sessions_by_date(1967216004, date):
            start, end = session['start_ts'], session['end_ts']
            if not end:
                continue
            recognizer.revision(1967216004, start, end)
            timeline = recognizer(1967216004, start, end)
            timelines.append(timeline)
            asr = conn.execute('SELECT count(*),min(timestamp),max(timestamp) FROM transcript WHERE room_id=1967216004 AND timestamp BETWEEN ? AND ?', (start,end)).fetchone()
            sessions.append({'start': start, 'end': end, 'asr': asr, 'windows': len(timeline)})
        variants = {}
        for threshold in (.95,.85,.75,.65,.5,0):
            entries = [entry for timeline in timelines for entry in select_preview_entries(timeline,threshold)]
            variants[str(threshold)] = {**evaluate(expected,entries), 'entries': entries}
        evidence = {}
        for title in variants['0.85']['missing']:
            candidates = [(p['timestamp'],r) for timeline in timelines for p in timeline for r in p['results'] if r['title']==title]
            candidates.sort(key=lambda x:x[1].get('preview_quality',0),reverse=True)
            evidence[title] = [{'timestamp': ts, **r} for ts,r in candidates[:8]]
        result = {'date':date, 'records': records, 'sessions':sessions, 'variants':variants, 'missing_evidence':evidence, 'elapsed':time.monotonic()-tick}
        (root/f'{date}.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
        results.append(result)
        (root/args.summary).write_text(json.dumps(results,ensure_ascii=False,indent=2))
        print('DAY_DONE',date,json.dumps({k:v for k,v in variants['0.85'].items() if k!='entries'},ensure_ascii=False),f'{result["elapsed"]:.1f}s',flush=True)
    print('COMPLETE',len(results),'days',sum(len(r['sessions']) for r in results),'sessions',flush=True)


if __name__ == '__main__':
    main()
