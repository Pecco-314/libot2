"""Restore later-round removals that reused an already consumed baseline.

Exact snapshots restore event IDs and provenance. Search restoration and cold
history use independent idempotent receipts; no online service is controlled.
"""
import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from src.db import danmaku_sources as store
from src.db.event_search import DEFAULT_SEARCH_DB_PATH,_document_tokens,_encode_rowid


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--earlier',type=Path,required=True);p.add_argument('--later',type=Path,required=True)
    args=p.parse_args()
    used={json.loads(line)['keep_event']['id'] for line in (args.earlier/'proposals.jsonl').open()}
    items=[r for line in (args.later/'proposals.jsonl').open() if (r:=json.loads(line))['keep_event']['id'] in used]
    with closing(sqlite3.connect(store.ARCHIVE_DB.as_uri()+'?mode=ro',uri=True)) as cold:
        if cold.execute("SELECT 1 FROM sqlite_master WHERE name='danmaku_cleanup_redelete_event'").fetchone():
            redeleted={r[0] for r in cold.execute('SELECT event_id FROM danmaku_cleanup_redelete_event')}
            if any(r['remove_event']['id'] in redeleted for r in items):
                raise ValueError('A newer validated cleanup supersedes this restoration; review its receipt first')
    with closing(sqlite3.connect(store.PRODUCTION_DB,timeout=.5)) as c:
        c.row_factory=sqlite3.Row
        with c:
            c.execute('BEGIN IMMEDIATE')
            for item in items:
                row=item['remove_event'];old=c.execute('SELECT * FROM event WHERE id=?',(row['id'],)).fetchone()
                if old is not None and dict(old)!=row:raise ValueError('Restored ID occupied')
                if old is None:c.execute('INSERT INTO event('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',list(row.values()))
                for prov in item['remove_provenance']:
                    old=c.execute('SELECT * FROM mit3uri_replay_danmaku_provenance WHERE event_id=?',(row['id'],)).fetchone()
                    if old is not None and dict(old)!=prov:raise ValueError('Provenance changed')
                    if old is None:c.execute('INSERT INTO mit3uri_replay_danmaku_provenance('+','.join(prov)+') VALUES('+','.join('?' for _ in prov)+')',list(prov.values()))
    key='restore_cross_round:'+str(args.later.resolve())
    with closing(sqlite3.connect(DEFAULT_SEARCH_DB_PATH,timeout=1)) as c:
        with c:
            c.execute('BEGIN IMMEDIATE')
            if not c.execute('SELECT 1 FROM event_search_meta WHERE key=?',(key,)).fetchone():
                for item in items:
                    r=item['remove_event'];c.execute('INSERT INTO event_fts(rowid,tokens) VALUES(?,?)',(_encode_rowid(r['timestamp'],r['id']),_document_tokens(r['room_id'],r['content'])))
                c.execute("UPDATE event_search_meta SET value=CAST(value AS INTEGER)+? WHERE key='indexed_document_count'",(len(items),))
                c.execute('INSERT INTO event_search_meta VALUES(?,?)',(key,str(len(items))))
    with closing(store.connect_archive()) as c:
        with c:
            c.execute('CREATE TABLE IF NOT EXISTS danmaku_cleanup_restored_event(event_id INTEGER PRIMARY KEY,reason TEXT NOT NULL,restored_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
            c.executemany('INSERT OR IGNORE INTO danmaku_cleanup_restored_event(event_id,reason) VALUES(?,?)',[(r['remove_event']['id'],'cross_round_baseline_reuse') for r in items])
    result=dict(restored=len(items),event_ids=[r['remove_event']['id'] for r in items],reason='cross_round_baseline_reuse',net_removed=json.loads((args.later/'apply_receipt.json').read_text())['removed']-len(items))
    (args.later/'restoration_receipt.json').write_text(json.dumps(result,indent=2)+'\n')
    print(result)


if __name__=='__main__':main()
