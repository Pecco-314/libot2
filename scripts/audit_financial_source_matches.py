"""Read-only financial provenance audit; nearby matches are evidence, not merge approval."""
import bisect
import collections
import json
import sqlite3
import zlib
from datetime import datetime
from pathlib import Path

import msgpack

from src.db.danmaku_sources import ARCHIVE_DB, PRODUCTION_DB, EXTERNAL_CUTOFF_MS, ROOT
from scripts.import_qianqiu_sc import clean_text, load_source

ROOM = 1967216004
KINDS = {1: 'gift', 2: 'guard', 3: 'sc'}
COMMANDS = {'gift': 'SEND_GIFT', 'guard': 'GUARD_BUY', 'sc': 'SUPER_CHAT_MESSAGE'}


def identity(r, price=True):
    return (r['kind'], r['uid'], r['text'], r['num']) + ((r['coin'],) if price else ())


def native_rows():
    with sqlite3.connect(ARCHIVE_DB.as_uri() + '?mode=ro', uri=True) as c:
        artifacts = c.execute("SELECT id,locator FROM danmaku_source_artifact WHERE source='danmakus' AND locator LIKE '%/full'").fetchall()
        for n, (aid, loc) in enumerate(artifacts):
            frame = msgpack.unpackb(zlib.decompress(c.execute('SELECT payload FROM danmaku_source_artifact WHERE id=?', (aid,)).fetchone()[0]), raw=False, strict_map_key=False)
            assert frame[0][2] == ROOM
            now, actors, _, records = frame[2]
            for seq, rec in enumerate(records):
                now += rec[0]
                if rec[1] not in KINDS or not 0 < now < EXTERNAL_CUTOFF_MS:
                    continue
                kind = KINDS[rec[1]]
                payload = rec[5]
                yield dict(provider='danmakus', key=f'{aid}:{seq}', stream=loc, kind=kind,
                           uid=int(actors[rec[2]-1][0]), uname=actors[rec[2]-1][1], ts=now/1000,
                           text=clean_text(payload[0]), num=1 if kind == 'sc' else int(payload[1]),
                           coin=payload[1]/1000 if kind == 'sc' else payload[2],
                           merged=bool(frame[1][7]))
            if n % 50 == 0:
                print('decoded', n, len(artifacts), flush=True)
    for kind in COMMANDS:
        for path in sorted((ROOT/'data/vtb_cat/2030198123/lives').glob(f'*/{kind}_*.json')):
            for r in json.loads(path.read_text()).get('records', []):
                ts = datetime.fromisoformat(r['CreatedAt']).timestamp()
                if not 0 < ts*1000 < EXTERNAL_CUTOFF_MS:
                    continue
                assert int(r['LiveRoom']) == ROOM
                amount = r.get('GiftAmount', {})
                num = int(amount['Int16']) if amount.get('Valid') else 1
                if kind == 'guard' and r.get('Extra'):
                    raw = json.loads(r['Extra'])
                    if raw.get('cmd') == 'GUARD_BUY' and int(raw['data']['uid']) == int(r['FromId']):
                        num = int(raw['data']['num'])
                price = r['GiftPrice']['Float64']
                yield dict(provider='vtbcat', key=str(r['ID']), stream=path.parent.name,
                           kind=kind, uid=int(r['FromId']), uname=r.get('FromName'), ts=ts,
                           text=clean_text(r['Extra'] if kind == 'sc' else r['GiftName']), num=num,
                           coin=price if kind == 'sc' else round(price*1000),raw_extra=r.get('Extra'))


def main():
    out = ROOT/'data/reports/financial_provenance_20261004'
    out.mkdir(parents=True, exist_ok=True)
    sources = list(native_rows())
    directory = ROOT/'data/imports/qianqiu_sc_20260924'
    qq = [r for r in load_source(directory, ROOM, ['2025-11','2025-12','2026-01','2026-02','2026-03','2026-04']) if r['timestamp']*1000 < EXTERNAL_CUTOFF_MS]
    for r in qq:
        sources.append(dict(provider='qqzy', key=r['source_key'], stream=r['month'], kind='sc',
                            uid=r['uid'], ts=r['timestamp'], text=r['content'], num=1, coin=r['price']))
    exact, loose = collections.defaultdict(list), collections.defaultdict(list)
    seen = set()
    unique = []
    for r in sources:
        k = r['provider'], r['key']
        if k in seen:
            continue
        seen.add(k)
        unique.append(r)
        exact[identity(r)].append(r)
        loose[identity(r, False)].append(r)
    for index in [exact, loose]:
        for key, rows in index.items():
            rows.sort(key=lambda r:r['ts'])
            index[key] = ([r['ts'] for r in rows], rows)

    def nearby(index, key, ts, window):
        times, rows = index.get(key, ([], []))
        return rows[bisect.bisect_left(times, ts-window):bisect.bisect_right(times, ts+window)]

    counts = collections.defaultdict(collections.Counter)
    unresolved = []
    matched_source = set()
    main_by_id = {}
    with sqlite3.connect(PRODUCTION_DB.as_uri()+'?mode=ro', uri=True) as c, (out/'matches.jsonl').open('w') as f:
        for kind, cmd in COMMANDS.items():
            for eid, uid, content, gift, num, coin, ts in c.execute('SELECT id,uid,content,gift_name,gift_num,total_coin,timestamp FROM event INDEXED BY idx_event_cmd_time WHERE cmd=? AND timestamp<? AND room_id=?', (cmd, EXTERNAL_CUTOFF_MS//1000, ROOM)):
                r = dict(id=eid, kind=kind, uid=uid, text=clean_text(content if kind=='sc' else gift), num=num, coin=coin, ts=ts)
                main_by_id[eid] = r
                hits = nearby(exact, identity(r), ts, 3)
                status = 'identity_within_3s'
                if not hits:
                    hits = nearby(exact, identity(r), ts, 300)
                    status = 'identity_within_300s_review'
                if not hits:
                    hits = nearby(loose, identity(r, False), ts, 3)
                    status = 'price_conflict_review'
                if not hits:
                    status = 'no_match_review'
                providers = '+'.join(sorted({h['provider'] for h in hits})) or 'none'
                counts[kind][status] += 1
                counts[kind]['providers:'+providers] += 1
                evidence = [dict(provider=h['provider'], key=h['key'], stream=h['stream'], delta_seconds=round(h['ts']-ts,3), coin=h['coin']) for h in hits]
                f.write(json.dumps(dict(event=r, status=status, evidence=evidence), ensure_ascii=False)+'\n')
                if status != 'identity_within_3s':
                    unresolved.append(dict(event=r, status=status, evidence=evidence))
                else:
                    matched_source.update((h['provider'],h['key']) for h in hits)
    receipt = json.loads((directory/'run_20260924_220632_074465/applied.json').read_text())
    inserted = [r for r in receipt['inserted'] if r['timestamp']*1000 < EXTERNAL_CUTOFF_MS]
    bad = []
    for r in inserted:
        old = main_by_id.get(r['event_id'])
        if not old or (old['kind'],old['uid'],old['text'],old['coin'],old['ts']) != ('sc',r['uid'],clean_text(r['content']),r['price'],r['timestamp']):
            bad.append(r)
    main_exact, main_loose = collections.defaultdict(list), collections.defaultdict(list)
    for r in main_by_id.values():
        main_exact[identity(r)].append(r)
        main_loose[identity(r, False)].append(r)
    for index in [main_exact, main_loose]:
        for key, rows in index.items():
            rows.sort(key=lambda r:r['ts'])
            index[key] = ([r['ts'] for r in rows], rows)
    reverse_counts = collections.defaultdict(collections.Counter)
    with (out/'source_unmatched.jsonl').open('w') as f:
        for r in unique:
            if (r['provider'],r['key']) in matched_source:
                continue
            hits = nearby(main_exact, identity(r), r['ts'], 300)
            status = 'same_identity_300s'
            if not hits:
                hits = nearby(main_loose, identity(r, False), r['ts'], 3)
                status = 'price_conflict_3s'
            if not hits:
                status = 'unresolved'
            reverse_counts[r['provider']+':'+r['kind']][status] += 1
            f.write(json.dumps(dict(source=r,status=status,main_candidates=hits),ensure_ascii=False)+'\n')
    report = dict(event_mutated=False, cutoff_ms=EXTERNAL_CUTOFF_MS,
                  matching_note='Candidate evidence only. No one-to-one consumption; raw duplicates and repeated transactions remain separate.',
                  source_raw_counts=dict(collections.Counter(r['provider']+':'+r['kind'] for r in sources)),
                  source_distinct_record_counts=dict(collections.Counter(r['provider']+':'+r['kind'] for r in unique)),
                  main_matches=dict(counts), reverse_review=dict(reverse_counts), qqzy_receipt=dict(historical_inserted=len(inserted), verified_unchanged=len(inserted)-len(bad), missing_or_changed=bad, by_month=dict(collections.Counter(r['month'] for r in inserted))),
                  source_without_3s_main_match=dict(collections.Counter(r['provider']+':'+r['kind'] for r in unique if (r['provider'],r['key']) not in matched_source)))
    (out/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    (out/'review.json').write_text(json.dumps(unresolved,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)


if __name__ == '__main__':
    main()
