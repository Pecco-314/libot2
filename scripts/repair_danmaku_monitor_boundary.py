"""Correct inferred monitor attribution before the first April 20 live.

Only normalized cold evidence is updated. Original rows are retained in an
append-only revision ledger; production event is attached read-only.
"""
import json
import sqlite3
import zlib
from collections import Counter
from contextlib import closing

from src.db import danmaku_sources as store
from scripts.rebuild_historical_danmaku import _decode_danmakus_rows
from scripts.backfill_mit3uri_replay_danmaku import ROOM_ID
from scripts.reconcile_danmaku_archive import link_exact

OLD_CUTOFF=1776614400000
REVISION='monitor_first_live_20260420_v3'


def repair(conn):
    conn.row_factory=sqlite3.Row
    with conn:
        conn.execute('CREATE TABLE IF NOT EXISTS danmaku_policy_revision(source_id INTEGER NOT NULL,revision TEXT NOT NULL,before_json TEXT NOT NULL,after_json TEXT NOT NULL,applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(source_id,revision))')
    candidates=conn.execute("SELECT * FROM danmaku_source_record WHERE source='monitor' AND source_timestamp_ms>=? AND source_timestamp_ms<?",(OLD_CUTOFF,store.EXTERNAL_CUTOFF_MS)).fetchall()
    result=Counter()
    for row in candidates:
        before=dict(row);evidence=json.loads(row['evidence'])
        if row['artifact_id'] is not None or evidence.get('attribution')!='user_confirmed_monitor_since_2026_04_20':
            result['direct_monitor_evidence_held']+=1;continue
        if conn.execute("SELECT 1 FROM danmaku_source_record WHERE source='legacy_unknown' AND source_key=?",(row['source_key'],)).fetchone():
            raise ValueError('Existing unknown snapshot requires explicit merge')
        evidence.update(attribution='unproven_legacy_origin',previous_attribution=evidence['attribution'],monitor_start_ms=store.EXTERNAL_CUTOFF_MS)
        after=dict(before,source='legacy_unknown',eligibility='unknown',evidence=store.encode(evidence))
        with conn:
            conn.execute('INSERT INTO danmaku_policy_revision(source_id,revision,before_json,after_json) VALUES(?,?,?,?)',(row['id'],REVISION,store.encode(before),store.encode(after)))
            conn.execute("UPDATE danmaku_source_record SET source='legacy_unknown',eligibility='unknown',evidence=? WHERE id=?",(after['evidence'],row['id']))
        result['monitor_to_unknown']+=1
    for source in ('danmakus','vtbcat','replay'):
        column='mapped_timestamp_ms' if source=='replay' else 'source_timestamp_ms'
        rows=conn.execute(f"SELECT * FROM danmaku_source_record WHERE source=? AND eligibility='outside_policy' AND {column}>=? AND {column}<?",(source,OLD_CUTOFF,store.EXTERNAL_CUTOFF_MS)).fetchall()
        for start in range(0,len(rows),200):
            with conn:
                for row in rows[start:start+200]:
                    before=dict(row);after=dict(before,eligibility='live')
                    conn.execute('INSERT INTO danmaku_policy_revision(source_id,revision,before_json,after_json) VALUES(?,?,?,?)',(row['id'],REVISION,store.encode(before),store.encode(after)))
                    conn.execute("UPDATE danmaku_source_record SET eligibility='live' WHERE id=?",(row['id'],))
        result[source+'_reeligible']=len(rows)
    conn.row_factory=None
    return dict(result)


def main():
    with closing(store.connect_archive()) as conn:
        result=repair(conn)
        emoji=json.loads((store.ROOT/'scripts/emoji.json').read_text())
        inserted=0;streams=[]
        for aid,locator in conn.execute("SELECT id,locator FROM danmaku_source_artifact WHERE source='danmakus' AND locator LIKE '%/full'").fetchall():
            compressed=conn.execute('SELECT payload FROM danmaku_source_artifact WHERE id=?',(aid,)).fetchone()[0]
            lid=locator.split('/')[0]
            rows,_=_decode_danmakus_rows(zlib.decompress(compressed),live_id=lid,cutoff_ms=store.EXTERNAL_CUTOFF_MS,emoji_map=emoji)
            boundary=[r for r in rows if r['timestamp_ms']>=OLD_CUTOFF]
            if not boundary:continue
            values=[store.record('danmakus',r['native_id'],lid,ROOM_ID,r['uid'],r['uname'],r['content'],r['timestamp_ms'],artifact_id=aid,evidence={'sequence':r['seq'],'format':'full','policy_revision':REVISION}) for r in boundary]
            with conn:inserted+=store.add_records(conn,values)
            streams.append(lid)
        result.update(danmakus_boundary_restored=inserted,affected_danmakus_streams=streams,event_mutated=False,monitor_start_ms=store.EXTERNAL_CUTOFF_MS)
        result['boundary_links']={}
        for source in ('danmakus','vtbcat'):
            sessions=[r[0] for r in conn.execute('SELECT DISTINCT source_session FROM danmaku_source_record WHERE room_id=? AND source_timestamp_ms>=? AND source_timestamp_ms<? AND source=?',(ROOM_ID,OLD_CUTOFF,store.EXTERNAL_CUTOFF_MS,source))]
            result['boundary_links'][source]=link_exact(conn,source,sessions=sessions,revision=REVISION)
        with conn:store.checkpoint(conn,'policy:external_cutoff_ms',store.EXTERNAL_CUTOFF_MS)
        (store.ROOT/'data/reports/monitor_boundary_repair_20261004.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':main()
