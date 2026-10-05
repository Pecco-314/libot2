"""Export actionable reasons and examples for the remaining clock review queue."""
import argparse
import collections
import csv
import json
import statistics
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.audit_mit3uri_danmaku_integrity import save_csv,save_json

TZ=ZoneInfo('Asia/Shanghai')
def stamp(t):return datetime.fromtimestamp(t,TZ).isoformat() if t is not None else ''


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--plan',type=Path,required=True);p.add_argument('--preview',type=Path,required=True);args=p.parse_args()
    records=json.loads((args.plan/'clock_diagnostics.json').read_text())
    sessions={r['session_id']:r for r in csv.DictReader((args.preview/'sessions.csv').open(encoding='utf-8-sig'))}
    detail=[];groups=collections.defaultdict(list)
    for r in records:
        c=r.get('clock',{});times=r.get('source_times',[]);ts=r.get('event_timestamp')
        delta=min((abs(t-ts) for t in times),default=None) if ts is not None else None
        row=dict(场次=r['session_id'],日期=sessions[r['session_id']]['date'],主源=r['source'],用户UID=r['uid'],弹幕=r['content'],主库ID=';'.join(map(str,r['event_ids'])),源记录ID=';'.join(map(str,r['source_ids'])),主库时间=stamp(ts),源时间=';'.join(stamp(t) for t in times),最近时间差秒=round(delta,3) if delta is not None else '',原因=c.get('reason','缺少对应记录'),全局模型成立=c.get('global_model'),附近训练锚点=c.get('nearby_train'),附近验证锚点=c.get('nearby_validation'),附近训练用户=c.get('train_users'),附近验证用户=c.get('validation_users'),最近锚点距离秒=c.get('nearest_anchor_seconds'))
        detail.append(row);groups[r['session_id']].append(row)
    overview=[]
    for sid,rows in groups.items():
        ds=[r['最近时间差秒'] for r in rows if isinstance(r['最近时间差秒'],(float,int))]
        overview.append(dict(场次=sid,日期=rows[0]['日期'],待核对组数=len(rows),时间差中位数秒=round(statistics.median(ds),3) if ds else '',模型存在但单条不匹配=sum('模型可预测' in r['原因'] for r in rows),模型范围受限=sum('全局模型成立' in r['原因'] for r in rows),附近锚点不足=sum('锚点不足' in r['原因'] for r in rows),例子=rows[0]['弹幕']))
    overview.sort(key=lambda r:-r['待核对组数'])
    save_csv(args.plan/'clock_review_details.csv',detail);save_csv(args.plan/'clock_review_sessions.csv',overview)
    save_json(args.plan/'clock_review_summary.json',dict(groups=len(records),sessions=len(groups),reasons=dict(collections.Counter(r['原因'] for r in detail)),note='模型存在但单条不匹配不等于缺少校时证据；不自动扩大容差。批量相同毫秒时间戳可能是采集抖动，仍需来源级序列核对。',top_sessions=overview[:10]))
    print(json.dumps(dict(groups=len(records),sessions=len(groups),reasons=dict(collections.Counter(r['原因'] for r in detail))),ensure_ascii=False))


if __name__=='__main__':main()
