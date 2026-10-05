"""Compare a supplied live-start workbook with canonical sessions, read-only."""
import argparse
import collections
import re
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import openpyxl

from src.db import danmaku_sources as store
from scripts.audit_mit3uri_danmaku_integrity import save_csv,save_json


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--workbook',type=Path,required=True);p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    args.output.mkdir(exist_ok=True);tz=ZoneInfo('Asia/Shanghai')
    with closing(sqlite3.connect(store.PRODUCTION_DB.as_uri()+'?mode=ro',uri=True)) as c:
        c.row_factory=sqlite3.Row
        sessions={r['session_id']:dict(r) for r in c.execute('SELECT * FROM mit3uri_replay_session')}
        bvs=dict(c.execute('SELECT bvid,session_id FROM mit3uri_replay_recording'))
    w=openpyxl.load_workbook(args.workbook,read_only=True,data_only=True);rows=[]
    for sheet in w:
        for values in sheet.iter_rows(values_only=True):
            if len(values)<7 or not isinstance(values[0],datetime) or not hasattr(values[1],'hour'):continue
            day,t,title,precision,basis,sid,url=values[:7]
            ts=int(datetime.combine(day.date(),t,tz).timestamp());m=re.search(r'BV[0-9A-Za-z]+',str(url));bv=m[0] if m else None
            mapped=bvs.get(bv);d=sessions.get(mapped or sid);old=d['start_timestamp'] if d else None
            delta=ts-old if old is not None else None
            state='未匹配或缺少库内起点' if delta is None else '一致' if delta==0 else '有时间差'
            if delta is not None and abs(delta)>3600:state='场次映射或时间冲突'
            historical=ts<store.EXTERNAL_CUTOFF_MS//1000
            candidate=bool(historical and d and d['start_time_precision']!='second' and precision=='秒' and delta is not None and abs(delta)<=3600)
            rows.append(dict(表格场次=sid,库内场次=d['session_id'] if d else '',匹配依据='BV' if mapped else '场次编号待交叉核对',日期=day.date().isoformat(),表格时间=datetime.fromtimestamp(ts,tz).isoformat(),表格精度=precision,时间依据=basis,库内时间=datetime.fromtimestamp(old,tz).isoformat() if old is not None else '',库内精度=d['start_time_precision'] if d else '',时间差秒=delta,状态=state,历史范围=historical,可补充秒级起点=candidate,参考链接=url))
    save_csv(args.output/'comparison.csv',rows)
    save_csv(args.output/'precision_candidates.csv',[r for r in rows if r['可补充秒级起点']])
    summary=dict(production_modified=False,rows=len(rows),precisions=dict(collections.Counter(r['表格精度'] for r in rows)),evidence_types=dict(collections.Counter(r['时间依据'] for r in rows)),historical_rows=sum(r['历史范围'] for r in rows),historical_status=dict(collections.Counter(r['状态'] for r in rows if r['历史范围'])),second_precision_candidates=sum(r['可补充秒级起点'] for r in rows),note='秒精度表示记录细度；录播标题时刻可能对应录制开始，仍需核对是否等于实际开播。仅按场次编号匹配的条目也需要交叉核对。')
    save_json(args.output/'summary.json',summary);print(summary)


if __name__=='__main__':main()
