"""Report comparable queue progress and explicit one-sided inventory corrections."""
import argparse
import collections
import json
from pathlib import Path

from scripts.audit_mit3uri_danmaku_integrity import save_json


def rows(path):
    with path.open() as f:
        for line in f:yield json.loads(line)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--plan',type=Path,required=True);p.add_argument('--previous',type=Path,required=True);p.add_argument('--history',type=Path,action='append',default=[]);args=p.parse_args()
    old={i for r in rows(args.previous/'remaining.jsonl') for i in r['event_ids']}
    pending=list(rows(args.plan/'remaining.jsonl'));new={i for r in pending for i in r['event_ids']}
    changed={r['before']['id'] for r in rows(args.plan/'changes.jsonl')}
    if new&changed:raise ValueError('Applied event still in current review queue')
    cumulative=collections.defaultdict(set)
    for directory in [*args.history,args.plan]:
        receipt=json.loads((directory/'execution_receipt.json').read_text())
        if not receipt['complete']:raise ValueError('Execution incomplete')
        for r in rows(directory/'changes.jsonl'):cumulative[r['operation']].add(r['before']['id'])
    kinds={}
    for key,predicate in [('both_sides',lambda r:bool(r['event_ids'] and r['source_ids'])),('main_only',lambda r:bool(r['event_ids'] and not r['source_ids'])),('source_only',lambda r:bool(r['source_ids'] and not r['event_ids']))]:
        subset=[r for r in pending if predicate(r)]
        kinds[key]=dict(groups=len(subset),main_events=len({i for r in subset for i in r['event_ids']}),source_records=len({i for r in subset for i in r['source_ids']}))
    result=dict(previous_listed_main_events=len(old),handled_from_previous_queue=len(old&changed),remaining_under_previous_scope=len(old&new),previously_unlisted_existing_events=len(new-old),complete_current_queue_main_events=len(new),remaining_groups=len(pending),breakdown=kinds,cumulative_distinct_changed_events={k:len(v) for k,v in cumulative.items()},scope='Only the fine-grained queue in accepted native-primary intervals; held whole intervals are outside this count. Queue entries are not confirmed duplicates.',inventory_correction='The previous occurrence pass skipped groups when either source or current rows were absent. Those unchanged existing records are explicitly retained in the current queue; no new events were inserted.')
    save_json(args.plan/'progress.json',result);print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
