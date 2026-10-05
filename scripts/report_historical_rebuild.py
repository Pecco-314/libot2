"""Summarize verified reconstruction and the SC transaction normalization."""
import argparse
import json
from contextlib import closing
from pathlib import Path

from src.db import danmaku_sources as store
from src.db.event_search import get_event_search_status
from scripts.replace_historical_source_database import ro,durable_json,ROOM_ID,CUTOFF


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--work',type=Path,required=True);a=p.parse_args()
    full=json.loads((a.work/'verification.json').read_text())
    sc=json.loads((a.work/'sc_normalization_verification.json').read_text())
    before=json.loads((a.work/'prepared.json').read_text())['old_counts']
    if not full['verified'] or not sc['verified']:raise ValueError('Both verification stages must pass')
    expected={**full['counts'],'SUPER_CHAT_MESSAGE':sc['final_historical_sc_count']}
    with closing(ro(store.PRODUCTION_DB)) as db:
        counts=dict(db.execute("SELECT cmd,COUNT(*) FROM event INDEXED BY idx_event_room_time WHERE room_id=? AND timestamp<? AND cmd IN ('DANMU_MSG','SEND_GIFT','GUARD_BUY','SUPER_CHAT_MESSAGE') GROUP BY cmd",(ROOM_ID,CUTOFF)))
    if counts!=expected:raise ValueError('Final production counts differ from the verified result')
    report=dict(complete=True,scope=dict(room_id=ROOM_ID,cutoff_exclusive='2026-04-20T17:30:37+08:00',cutoff_timestamp=CUTOFF),
                before=before,after=counts,total_before=sum(before.values()),total_after=sum(counts.values()),net_change=sum(counts.values())-sum(before.values()),
                full_reconstruction_verified=full,sc_normalization_verified=sc,
                source_policy='One selected native stream per interval; other streams fill missing intervals; live-origin official replay fills remaining gaps. Fresh qqzy SC is included. Same-source danmaku multiplicity is retained; identical user/text/price SC signals within 60 seconds are coalesced.',
                financial_field_policy='Selected primary-provider amount is retained; raw GUARD_BUY quantities override flattened provider quantities. Alternative amounts and quantities remain in candidate financial_evidence/review and are not counted as additional transactions.',
                preserved=['Other rooms','All events from cutoff onwards','Historical LIVE/PREPARING/ROOM_CHANGE events'],
                search=get_event_search_status(),rollback_database=str((a.work/'rollback.sqlite3').resolve()),
                recoverability='All original scoped rows and replay links are retained in the rollback database. SC coalescing before-images and source observations are retained in sc_normalization_plan.json; source evidence is not deleted.')
    durable_json(a.work/'final_report.json',report)
    print(json.dumps({k:report[k] for k in ('complete','before','after','total_after','net_change','search')},ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':main()
