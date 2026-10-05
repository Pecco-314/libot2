"""Persist reviewed source-occurrence decisions for reproducible reconstruction.

Policies address immutable cold source IDs, not user/text suppression rules.
Only verified repairs may be registered. Raw source artifacts remain unchanged.
"""
import argparse
import hashlib
import json
from contextlib import closing
from pathlib import Path

from scripts.audit_danmaku_clock_bursts import ro
from scripts.apply_source_first_changes import durable_json
from src.db import danmaku_sources as store

REGISTRY = store.ROOT/'data/rebuild/clock_policies.json'


def export_policy(plan, candidate, replacement):
    execution=json.loads((plan/'execution.json').read_text())
    changes=plan/'changes.jsonl'
    if not execution.get('verified') or hashlib.sha256(changes.read_bytes()).hexdigest()!=execution['plan_sha256']:
        raise ValueError('Only a verified, unchanged plan can become a clock policy')
    items=[json.loads(l) for l in changes.read_text().splitlines()]
    retimes={x['before']['id']:x['after']['timestamp'] for x in items if x['operation']=='retime'}
    decisions=[]
    with closing(ro(candidate/'rebuilt.sqlite3')) as c, closing(ro(replacement/'rollback.sqlite3')) as m:
        def source_id(eid):
            cid=m.execute('SELECT candidate_id FROM event_map WHERE event_id=?',(eid,)).fetchone()[0]
            return c.execute('SELECT source_record_id FROM origin WHERE event_id=?',(cid,)).fetchone()[0]
        for x in items:
            d=dict(operation=x['operation'],session_id=x['session_id'])
            if x['operation']=='delete':
                d.update(source_id=source_id(x['before']['id']),canonical_id=source_id(x['keep']['id']),
                         timestamp=retimes.get(x['keep']['id'],x['keep']['timestamp']))
            elif x['operation']=='retime':
                d.update(source_id=source_id(x['before']['id']),timestamp=x['after']['timestamp'],
                         reference_id=x['reference_source_id'])
            else:
                d.update(source_id=x['source_record_id'],timestamp=x['after']['timestamp'])
            decisions.append(d)
    policy=plan/'clock_policy.json'
    durable_json(policy,dict(plan=str(plan.resolve()),plan_sha256=execution['plan_sha256'],decisions=decisions))
    paths=json.loads(REGISTRY.read_text()) if REGISTRY.exists() else []
    if str(policy.resolve()) not in paths:
        paths.append(str(policy.resolve()))
    durable_json(REGISTRY,paths)
    return policy


def load_policies(paths=None):
    if paths is None:
        paths=[Path(p) for p in json.loads(REGISTRY.read_text())] if REGISTRY.exists() else []
    result={}
    for path in paths:
        policy=json.loads(path.read_text())
        plan=Path(policy['plan'])
        execution=json.loads((plan/'execution.json').read_text())
        if not execution.get('verified') or hashlib.sha256((plan/'changes.jsonl').read_bytes()).hexdigest()!=policy['plan_sha256']:
            raise ValueError('Clock policy does not match its verified repair')
        for d in policy['decisions']:
            result.setdefault(d['session_id'],[]).append(d)
    return result


def apply_policy(selected, streams, decisions):
    if not decisions:
        return selected
    available={r['id']:r for rr in streams.values() for r in rr}
    wanted={r['id']:r for r in selected}
    for d in decisions:
        sid=d['source_id']; op=d['operation']
        if op=='delete':
            if sid not in wanted:
                continue
            wanted.pop(sid)
            target=d['canonical_id']
        elif op=='retime':
            if sid not in wanted:
                continue
            target=sid
        else:
            target=sid
        if target not in available:
            raise ValueError('Reviewed source occurrence missing from reconstruction inputs')
        r=dict(available[target]);r['time']=d['timestamp']
        r['clock_basis']='reviewed_burst_policy:'+str(d.get('reference_id',target))
        r['reviewed_clock_policy']=True
        wanted[target]=r
    return list(wanted.values())


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--plan',type=Path,required=True)
    p.add_argument('--candidate',type=Path,required=True)
    p.add_argument('--replacement',type=Path,required=True)
    args=p.parse_args()
    print(export_policy(args.plan,args.candidate,args.replacement))


if __name__=='__main__':
    main()
