"""Apply a reviewed cold proposal in bounded transactions without controlling services.

Each deleted event and its provenance already have complete snapshots in the
proposal. Unchanged keep/remove rows are required inside each write transaction.
Search candidates are revalidated against event by the existing query layer.
"""
import argparse
import hashlib
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from scripts.backfill_mit3uri_replay_danmaku import ROOM_ID,normalize_content
from src.db import danmaku_sources as store


def validate(item):
    keep=item['keep_event'];drop=item['remove_event'];prov=item['remove_provenance']
    if keep['id']==drop['id'] or not item['selected_primary'].startswith(('monitor:','danmakus:','vtbcat:')):
        raise ValueError('Invalid primary/source pair')
    if any(r['cmd']!='DANMU_MSG' or r['room_id']!=ROOM_ID or r['timestamp']>=store.EXTERNAL_CUTOFF_MS//1000 for r in (keep,drop)):
        raise ValueError('Event outside historical danmaku scope')
    if keep['uid']!=drop['uid'] or normalize_content(keep['content'])!=normalize_content(drop['content']):
        raise ValueError('Content or UID mismatch')
    if len(prov)!=1 or prov[0]['event_id']!=drop['id'] or prov[0]['match_kind']!='inserted':
        raise ValueError('Removal requires inserted replay provenance')


def apply_batch(conn,items):
    removed=[];absent=[]
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        for item in items:
            validate(item)
            keep=item['keep_event'];drop=item['remove_event']
            base=conn.execute('SELECT * FROM event WHERE id=?',(keep['id'],)).fetchone()
            extra=conn.execute('SELECT * FROM event WHERE id=?',(drop['id'],)).fetchone()
            prov=[dict(r) for r in conn.execute('SELECT * FROM mit3uri_replay_danmaku_provenance WHERE event_id=?',(drop['id'],))]
            if base is None or dict(base)!=keep:raise RuntimeError(f'Keep event changed: {keep["id"]}')
            if extra is None and not prov:
                absent.append(drop['id']);continue
            if extra is None or dict(extra)!=drop or prov!=item['remove_provenance']:
                raise RuntimeError(f'Remove event changed: {drop["id"]}')
            conn.execute('DELETE FROM mit3uri_replay_danmaku_provenance WHERE event_id=?',(drop['id'],))
            conn.execute('DELETE FROM event WHERE id=?',(drop['id'],))
            removed.append(drop['id'])
    return removed,absent


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan',type=Path,required=True)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--batch-size',type=int,default=50)
    parser.add_argument('--pause',type=float,default=.05)
    args=parser.parse_args()
    if not args.apply:parser.error('Explicit --apply required after proposal review')
    if not 1<=args.batch_size<=200 or not .01<=args.pause<=1:parser.error('Batch size 1..200 and pause .01..1 required')
    if (args.plan/'INVALIDATED.txt').exists():parser.error('Proposal invalidated')
    with closing(sqlite3.connect(store.ARCHIVE_DB.as_uri()+'?mode=ro',uri=True)) as cold:
        removed_before,claimed_before=store.cleanup_state(cold)
    summary=json.loads((args.plan/'summary.json').read_text())
    path=args.plan/'proposals.jsonl';sha=hashlib.sha256();keeps=set();drops=set();count=0
    with path.open('rb') as f:
        for line in f:
            sha.update(line);item=json.loads(line);validate(item)
            keep=item['keep_event']['id'];drop=item['remove_event']['id']
            if keep in claimed_before and drop not in removed_before:raise ValueError('Baseline already consumed by an earlier cleanup')
            if keep in keeps or drop in drops or keep in drops or drop in keeps:raise ValueError('Non-disjoint proposal')
            keeps.add(keep);drops.add(drop);count+=1
    if count!=summary['counts']['proposed_removals']:raise ValueError('Proposal count changed')
    receipt=dict(proposal_sha256=sha.hexdigest(),proposed=count,removed=0,already_absent=0,completed=False)
    dest=args.plan/'apply_receipt.json'
    if dest.exists():
        previous=json.loads(dest.read_text())
        if previous['proposal_sha256']!=receipt['proposal_sha256']:raise ValueError('Resume proposal changed')
    dest.write_text(json.dumps(receipt,indent=2)+'\n')
    with closing(sqlite3.connect(store.PRODUCTION_DB,timeout=.1)) as conn, path.open() as f, (args.plan/'applied_batches.jsonl').open('a') as journal:
        conn.row_factory=sqlite3.Row
        conn.execute('PRAGMA busy_timeout=100')
        items=[];last_log=0
        def flush():
            nonlocal last_log
            for attempt in range(20):
                start=time.monotonic()
                conn.set_progress_handler(lambda:int(time.monotonic()-start>.25),1000)
                try:
                    removed,absent=apply_batch(conn,items)
                    break
                except sqlite3.OperationalError as exc:
                    if not any(t in str(exc) for t in ('locked','interrupted')):raise
                    time.sleep(.5)
                finally:conn.set_progress_handler(None,0)
            else:raise RuntimeError('Write contention persisted; safely paused after 20 attempts')
            journal.write(json.dumps(dict(removed=removed,already_absent=absent))+'\n');journal.flush()
            receipt['removed']+=len(removed);receipt['already_absent']+=len(absent)
            if time.monotonic()-last_log>30:
                dest.write_text(json.dumps(receipt,indent=2)+'\n');print(receipt,flush=True);last_log=time.monotonic()
            items.clear();time.sleep(args.pause)
        for line in f:
            items.append(json.loads(line))
            if len(items)>=args.batch_size:flush()
        if items:flush()
    receipt['completed']=True
    dest.write_text(json.dumps(receipt,indent=2)+'\n')
    print(receipt,flush=True)


if __name__=='__main__':main()
