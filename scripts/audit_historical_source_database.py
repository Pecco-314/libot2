"""Audit independent reconstruction coverage before any production replacement."""
import argparse
import collections
import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from src.db import danmaku_sources as store
from scripts.backfill_mit3uri_replay_danmaku import ROOM_ID
from scripts.audit_mit3uri_danmaku_integrity import save_json,save_csv,iso
from scripts.import_qianqiu_sc import load_source,months_between
from scripts.normalize_historical_sc import clusters


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--candidate',type=Path,required=True);args=p.parse_args()
    summary=json.loads((args.candidate/'summary.json').read_text())
    with closing(sqlite3.connect((args.candidate/'rebuilt.sqlite3').resolve().as_uri()+'?mode=ro',uri=True)) as c,closing(sqlite3.connect(store.ARCHIVE_DB.as_uri()+'?mode=ro',uri=True)) as cold:
        count=c.execute('SELECT COUNT(*) FROM event').fetchone()[0]
        qqzy_count=sum(r['timestamp']<store.EXTERNAL_CUTOFF_MS/1000 for r in load_source(args.candidate.parent/'qqzy',ROOM_ID,list(months_between('2025-11','2026-04'))))
        checks=dict(event_origin_count=c.execute('SELECT COUNT(*) FROM origin').fetchone()[0]==count,no_foreign_or_protected_events=c.execute('SELECT COUNT(*) FROM event WHERE room_id!=? OR timestamp>=? OR timestamp<=0',(ROOM_ID,store.EXTERNAL_CUTOFF_MS//1000)).fetchone()[0]==0,no_main_event_fallback=c.execute("SELECT COUNT(*) FROM origin WHERE source NOT IN ('danmakus','vtbcat','replay','qqzy')").fetchone()[0]==0,qqzy_input_fully_represented=c.execute("SELECT COUNT(*) FROM financial_evidence WHERE source='qqzy'").fetchone()[0]==qqzy_count)
        checks['sc_unique_index_compatible']=c.execute("SELECT COUNT(*) FROM (SELECT 1 FROM event WHERE cmd='SUPER_CHAT_MESSAGE' GROUP BY room_id,uid,content,total_coin,timestamp HAVING COUNT(*)>1)").fetchone()[0]==0
        checks['gift_guard_one_to_one_per_stream']=c.execute("SELECT COUNT(*) FROM (SELECT f.event_id,f.source,json_extract(f.payload,'$.stream'),COUNT(*) n FROM financial_evidence f JOIN event e ON e.id=f.event_id WHERE e.cmd!='SUPER_CHAT_MESSAGE' GROUP BY 1,2,3 HAVING n>1)").fetchone()[0]==0
        checks['all_replay_lineage_preserved']=bool(c.execute("SELECT 1 FROM sqlite_master WHERE name='replay_provenance'").fetchone()) and c.execute('SELECT COUNT(*) FROM replay_provenance').fetchone()[0]==c.execute("SELECT COUNT(*) FROM origin WHERE source='replay'").fetchone()[0]
        checks['no_missing_replay_clock']=c.execute("SELECT COUNT(*) FROM review WHERE kind='missing_replay_clock'").fetchone()[0]==0
        sc_fields=[r[1] for r in c.execute('PRAGMA table_info(event)')]
        checks['no_identical_short_interval_sc']=not clusters([dict(zip(sc_fields,r)) for r in c.execute("SELECT * FROM event WHERE cmd='SUPER_CHAT_MESSAGE'")])
        checks['all_selected_segments_accounted']=c.execute('SELECT SUM(selected_count) FROM source_segment').fetchone()[0]==c.execute("SELECT COUNT(*) FROM event WHERE cmd='DANMU_MSG'").fetchone()[0]
        groups=[json.loads(r[0]) for r in c.execute('SELECT report FROM build_session')]
        windows=sorted((datetime.fromisoformat(r['start']).timestamp(),datetime.fromisoformat(r['end']).timestamp()) for r in groups)
        gaps=[];last=0
        for a,b in windows:
            if a>last:gaps.append((last,a))
            last=max(last,b)
        if last<store.EXTERNAL_CUTOFF_MS/1000:gaps.append((last,store.EXTERNAL_CUTOFF_MS/1000))
        uncovered=[]
        for a,b in gaps:
            for source,stream,n,lo,hi in cold.execute("SELECT source,source_session,COUNT(*),MIN(source_timestamp_ms),MAX(source_timestamp_ms) FROM danmaku_source_record INDEXED BY idx_dm_source_time WHERE room_id=? AND source_timestamp_ms>=? AND source_timestamp_ms<? AND source IN ('danmakus','vtbcat') AND eligibility='live' GROUP BY source,source_session",(ROOM_ID,a*1000,b*1000)):
                uncovered.append(dict(source=source,stream=stream,records=n,first=iso(lo/1000),last=iso(hi/1000),gap_start=a,gap_end=b))
        result=dict(candidate_only=True,production_modified=False,checks=checks,counts=summary['counts'],native_outside_windows=sum(r['records'] for r in uncovered),uncovered_native_groups=len(uncovered),financial_price_or_quantity_conflicts=c.execute("SELECT COUNT(*) FROM review WHERE kind='financial_field_conflict'").fetchone()[0],financial_conflict_policy='Primary provider values are retained; alternatives remain in financial_evidence and review. These are not additional transactions.',zero_source_session_groups=sum(r['selected']==0 for r in groups),ready_for_promotion=all(checks.values()) and not uncovered)
        save_json(args.candidate/'audit.json',result)
        if uncovered:save_csv(args.candidate/'uncovered_native.csv',uncovered)
        print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':main()
