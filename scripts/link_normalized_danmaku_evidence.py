"""Link archived native observations to unique original events at the same second.

Only normalized whitespace/Unicode differences are accepted. Replay inserts
cannot serve as originals, and ambiguous original occurrences remain unlinked.
Production is read-only; optional writes add evidence links to the cold archive.
"""
import argparse
import collections
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from src.db import danmaku_sources as store
from scripts.backfill_mit3uri_replay_danmaku import ROOM_ID,normalize_content
from scripts.audit_mit3uri_danmaku_integrity import save_json


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pairs',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--apply',action='store_true')
    args=p.parse_args();args.output.mkdir(exist_ok=False)
    ids={json.loads(line)['baseline_event_id'] for line in args.pairs.open()}
    counts=collections.Counter();links=[]
    with closing(sqlite3.connect(store.PRODUCTION_DB.as_uri()+'?mode=ro',uri=True)) as live, closing(sqlite3.connect(store.ARCHIVE_DB.as_uri()+'?mode=ro',uri=True)) as cold:
        live.row_factory=sqlite3.Row
        for eid in ids:
            if cold.execute("SELECT 1 FROM danmaku_source_link l JOIN danmaku_source_record r ON r.id=l.source_id WHERE l.event_id=? AND r.source IN ('danmakus','vtbcat') LIMIT 1",(eid,)).fetchone():continue
            b=live.execute('SELECT * FROM event WHERE id=?',(eid,)).fetchone()
            if b is None or b['cmd']!='DANMU_MSG' or b['room_id']!=ROOM_ID or b['timestamp']>=store.EXTERNAL_CUTOFF_MS/1000:continue
            originals=[r['id'] for r in live.execute("SELECT e.id,e.content FROM event e LEFT JOIN mit3uri_replay_danmaku_provenance p ON p.event_id=e.id WHERE e.room_id=? AND e.uid=? AND e.timestamp=? AND e.cmd='DANMU_MSG' AND (p.match_kind IS NULL OR p.match_kind='existing')",(ROOM_ID,b['uid'],b['timestamp'])) if normalize_content(r['content'])==normalize_content(b['content'])]
            if originals!=[eid]:counts['ambiguous_original']+=1;continue
            rows=[r for r in cold.execute("SELECT id,content FROM danmaku_source_record INDEXED BY idx_dm_source_time WHERE room_id=? AND source_timestamp_ms>=? AND source_timestamp_ms<? AND uid=? AND source IN ('danmakus','vtbcat') AND eligibility='live'",(ROOM_ID,b['timestamp']*1000,(b['timestamp']+1)*1000,b['uid'])) if normalize_content(r[1])==normalize_content(b['content'])]
            if not rows:counts['no_exact_native_second']+=1;continue
            counts['linked_originals']+=1
            links.extend(dict(source_id=r[0],event_id=eid,method='exact_second_normalized_unique') for r in rows)
    save_json(args.output/'links.json',links)
    if args.apply:
        with closing(store.connect_archive()) as cold:
            for start in range(0,len(links),100):
                with cold:cold.executemany('INSERT OR IGNORE INTO danmaku_source_link(source_id,event_id,method) VALUES(:source_id,:event_id,:method)',links[start:start+100])
    save_json(args.output/'summary.json',dict(event_mutated=False,cold_links_applied=args.apply,links=len(links),counts=dict(counts)))
    print(dict(counts),len(links))


if __name__=='__main__':main()
