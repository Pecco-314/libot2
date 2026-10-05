"""Compare the frozen rollback snapshot with a source-built candidate."""
import argparse
import csv
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from scripts.replace_historical_source_database import ro,get,durable_json


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--candidate',type=Path,required=True);p.add_argument('--work',type=Path,required=True)
    args=p.parse_args()
    with closing(ro(args.work/'rollback.sqlite3')) as old,closing(ro(args.candidate/'rebuilt.sqlite3')) as new:
        a={(day,cmd):n for day,cmd,n in old.execute("SELECT date(timestamp,'unixepoch','+8 hours'),cmd,COUNT(*) FROM old_event GROUP BY 1,2")}
        b={(day,cmd):n for day,cmd,n in new.execute("SELECT date(timestamp,'unixepoch','+8 hours'),cmd,COUNT(*) FROM event GROUP BY 1,2")}
        rows=[dict(date=day,kind=cmd,before=a.get((day,cmd),0),after=b.get((day,cmd),0),change=b.get((day,cmd),0)-a.get((day,cmd),0)) for day,cmd in sorted(a.keys()|b.keys())]
        report=dict(old_counts=dict(old.execute('SELECT cmd,COUNT(*) FROM old_event GROUP BY cmd')),new_counts=dict(new.execute('SELECT cmd,COUNT(*) FROM event GROUP BY cmd')),days_with_old_but_no_new=[r for r in rows if r['before'] and not r['after']],total_before=sum(a.values()),total_after=sum(b.values()),net_change=sum(b.values())-sum(a.values()))
        with (args.work/'daily_comparison.csv').open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=['date','kind','before','after','change']);writer.writeheader();writer.writerows(rows)
        durable_json(args.work/'comparison.json',report)
        print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':main()
