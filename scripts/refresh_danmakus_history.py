"""Archive revised historical Danmakus snapshots without changing live events.

Positional IDs in the upstream dump may change when history is supplemented.
Snapshots therefore use content-addressed artifacts, with a resumable manifest,
and do not overwrite the immutable source records used by prior clock repairs.
"""

from __future__ import annotations

import argparse
import bisect
import collections
import csv
import json
import re
import sqlite3
import zlib
from contextlib import closing
from datetime import datetime
from pathlib import Path

from scripts.rebuild_historical_danmaku import (
    CHANNEL_UID, DANMAKUS_CATALOG_URL, DANMAKUS_FULL_URL, RateLimitedClient,
    TZ, _atomic_json, _decode_danmakus_rows, _fetch_danmakus_emoji_names,
    _load_emoji_map, _select_danmakus_lives,
)
from src.db import danmaku_sources as store


def canonical(text: str, aliases: dict[str, str]) -> str:
    value = text.strip()
    value = aliases.get(value, value)
    if value.startswith('[upower_[') and value.endswith(']]'):
        value = value[len('[upower_'): -1]
    elif value.startswith('upower_[') and value.endswith(']'):
        value = value[len('upower_'):]
    if re.fullmatch(r'(?:official_\d+|room_\d+_\d+)', value):
        value = '[' + value + ']'
    return value


def decode_snapshot(cold, info, lid):
    raw = zlib.decompress(cold.execute(
        'SELECT payload FROM danmaku_source_artifact WHERE id=?', (info['artifact_id'],)
    ).fetchone()[0])
    emojis = _load_emoji_map() | info.get('emoji_names', {})
    return _decode_danmakus_rows(raw, live_id=lid, cutoff_ms=store.EXTERNAL_CUTOFF_MS,
                                 emoji_map=emojis)[0]


def catalog_versions(catalog):
    """Include uploaded alternatives even when the primary count is unchanged."""
    versions = {}
    for live in _select_danmakus_lives(catalog, store.EXTERNAL_CUTOFF_MS):
        versions[live['liveId']] = live
        for version in live.get('versions', []):
            row = version.get('live', {})
            if row.get('liveId') and 0 < row.get('startDate', 0) < store.EXTERNAL_CUTOFF_MS:
                versions.setdefault(row['liveId'], row)
    return versions


def match_occurrences(left, right, tolerance_ms):
    """Nearest-first, one-to-one occurrence matching preserves multiplicity."""
    index = collections.defaultdict(list)
    for i, row in enumerate(right):
        index[row['uid'], row['norm']].append((row['timestamp_ms'], i))
    for entries in index.values():
        entries.sort()
    choices = []
    for i, row in enumerate(left):
        entries = index.get((row['uid'], row['norm']), [])
        pos = bisect.bisect_left(entries, (row['timestamp_ms'] - tolerance_ms, -1))
        for t, j in entries[pos:]:
            delta = abs(t - row['timestamp_ms'])
            if t > row['timestamp_ms'] + tolerance_ms:
                break
            choices.append((delta, i, j))
    pairs = {}; used = set()
    for delta, i, j in sorted(choices):
        if i not in pairs and j not in used:
            pairs[i] = j; used.add(j)
    return pairs


def match_resolved_emojis(left, right, pairs):
    """Pair renamed image placeholders only at identical UID/millisecond slots."""
    used = set(pairs.values())
    slots = collections.defaultdict(list)
    for j, row in enumerate(right):
        if j not in used and row['norm'].startswith('[/bfs/'):
            slots[row['uid'], row['timestamp_ms']].append(j)
    pending = collections.defaultdict(list)
    for i, row in enumerate(left):
        if i not in pairs and row['content'].startswith(('upower_[', 'room_', 'official_')):
            pending[row['uid'], row['timestamp_ms']].append(i)
    return {indices[0]: slots[key][0] for key, indices in pending.items()
            if len(indices) == 1 and len(slots.get(key, [])) == 1}


def audit(work: Path) -> None:
    manifest = json.loads((work / 'manifest.json').read_text())
    if manifest['failures'] or len(manifest['completed']) != len(manifest['selected']):
        raise ValueError('Finish all snapshot downloads before auditing')
    with closing(sqlite3.connect(store.ARCHIVE_DB.as_uri() + '?mode=ro', uri=True)) as cold, \
            closing(sqlite3.connect(store.PRODUCTION_DB.as_uri() + '?mode=ro', uri=True)) as main:
        votes = collections.defaultdict(collections.Counter)
        alias_users = collections.defaultdict(set)
        # Equal UID and millisecond timestamp supply independent evidence for
        # text-form room emoji identifiers introduced by the upstream API.
        for lid, info in manifest['completed'].items():
            snapshot = decode_snapshot(cold, info, lid)
            if not snapshot:
                continue
            old = cold.execute(
                "SELECT uid,content,source_timestamp_ms FROM danmaku_source_record INDEXED BY idx_dm_source_time "
                "WHERE room_id=1967216004 AND source='danmakus' AND source_timestamp_ms>=? AND source_timestamp_ms<=?",
                (min(r['timestamp_ms'] for r in snapshot), max(r['timestamp_ms'] for r in snapshot))).fetchall()
            by_time = collections.defaultdict(set)
            for uid, content, ts in old:
                by_time[uid, ts].add(canonical(content, {}))
            for row in snapshot:
                if re.fullmatch(r'room_\d+_\d+', row['content']):
                    names = by_time.get((row['uid'], row['timestamp_ms']), set())
                    if len(names) == 1:
                        name = next(iter(names)); votes[row['content']][name] += 1
                        alias_users[row['content'], name].add(row['uid'])
        aliases = {}
        for raw, counter in votes.items():
            name, count = counter.most_common(1)[0]
            if count >= 5 and count / sum(counter.values()) >= .99 and len(alias_users[raw, name]) >= 3:
                aliases[raw] = name
        _atomic_json(work / 'emoji_aliases.json', dict(aliases=aliases, votes=votes))
        reports = []; daily = collections.defaultdict(collections.Counter); resolved_details = []
        candidates = []
        for entry in manifest['selected']:
            live = entry['live']; lid = live['liveId']; info = manifest['completed'][lid]
            rows = decode_snapshot(cold, info, lid)
            if not rows:
                continue
            old = [dict(uid=u, content=t, timestamp_ms=s) for u, t, s in cold.execute(
                "SELECT uid,content,source_timestamp_ms FROM danmaku_source_record "
                "WHERE source='danmakus' AND source_session=?", (lid,))]
            lo = min(live['startDate'], min(r['timestamp_ms'] for r in rows)) // 1000 - 1800
            hi = min(store.EXTERNAL_CUTOFF_MS // 1000, max(r['timestamp_ms'] for r in rows) // 1000 + 1801)
            prod = [dict(id=e, uid=u, content=t, timestamp_ms=s*1000) for e, u, t, s in main.execute(
                "SELECT id,uid,content,timestamp FROM event INDEXED BY idx_event_room_time "
                "WHERE room_id=1967216004 AND timestamp>=? AND timestamp<? AND cmd='DANMU_MSG'", (lo, hi))]
            archived = [dict(id=e, uid=u, content=t, timestamp_ms=s) for e, u, t, s in cold.execute(
                "SELECT id,uid,content,source_timestamp_ms FROM danmaku_source_record INDEXED BY idx_dm_source_time "
                "WHERE room_id=1967216004 AND source='danmakus' AND source_timestamp_ms>=? AND source_timestamp_ms<?",
                (lo*1000, hi*1000))]
            for row in rows + old + prod + archived:
                row['norm'] = canonical(row['content'], aliases)
            old_pairs = match_occurrences(rows, old, 0)
            archive_pairs = match_occurrences(rows, archived, 0)
            resolved = match_resolved_emojis(rows, archived, archive_pairs)
            archive_pairs.update(resolved)
            for i, j in resolved.items():
                resolved_details.append(dict(live_id=lid, seq=rows[i]['seq'],
                    source_id=archived[j]['id'], timestamp_ms=rows[i]['timestamp_ms'],
                    uid=rows[i]['uid'], old_content=archived[j]['content'], new_content=rows[i]['content']))
            prod_pairs = match_occurrences(rows, prod, 8000)
            exact_prod = match_occurrences(rows, prod, 1000)
            prod_index = collections.defaultdict(list)
            for row in prod:
                prod_index[row['uid'], row['norm']].append(row['timestamp_ms'])
            new_index = collections.defaultdict(list)
            for i, row in enumerate(rows):
                new_index[row['uid'], row['norm']].append((i, row['timestamp_ms']))
            stats = collections.Counter(old=len(old), current=len(rows), old_retained=len(old_pairs),
                                         archive_matched=len(archive_pairs),
                                         production=len(prod), production_matched=len(prod_pairs),
                                         production_exact=len(exact_prod))
            for i, row in enumerate(rows):
                day = datetime.fromtimestamp(row['timestamp_ms']/1000, TZ).strftime('%Y-%m-%d')
                daily[day]['downloaded'] += 1
                if i in resolved:
                    daily[day]['emoji_names_resolved'] += 1
                if i not in old_pairs:
                    daily[day]['primary_stream_added'] += 1
                if i not in archive_pairs:
                    daily[day]['upstream_added'] += 1
                else:
                    daily[day]['archived_before'] += 1
                if i in prod_pairs:
                    daily[day]['already_present'] += 1
                    continue
                near = prod_index.get((row['uid'], row['norm']), [])
                other = [abs(row['timestamp_ms']-t) for j,t in new_index[row['uid'], row['norm']] if i != j]
                unresolved = bool(re.fullmatch(r'\[room_\d+_\d+\]', row['norm']))
                if i in archive_pairs:
                    stats['archived_unselected'] += 1
                    daily[day]['archived_unselected'] += 1
                    continue
                if unresolved:
                    status = 'unknown_emoji'
                elif near and min(abs(row['timestamp_ms']-t) for t in near) <= 1800000:
                    status = 'existing_text_review'
                elif other and min(other) <= 8000:
                    status = 'upstream_repeat_review'
                else:
                    status = 'candidate_absent'
                stats[status] += 1; daily[day][status] += 1
                candidates.append(dict(artifact_id=info['artifact_id'], date=day,
                                       status=status, **row))
            reports.append(dict(live_id=lid, date=datetime.fromtimestamp(live['startDate']/1000, TZ).strftime('%Y-%m-%d'),
                                title=live['title'], **stats))
            print(json.dumps(reports[-1], ensure_ascii=False), flush=True)
        _atomic_json(work / 'audit.json', dict(sessions=reports, daily=daily, resolved_emojis=resolved_details,
                                              totals=dict(sum(daily.values(), collections.Counter()))))
        _atomic_json(work / 'candidates.json', candidates)
        fields = sorted(set().union(*(set(v) for v in daily.values())))
        with (work / 'daily.csv').open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=['date', *fields]); writer.writeheader()
            writer.writerows(dict(date=k, **v) for k,v in sorted(daily.items()))
        with (work / '日期补充统计.csv').open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.writer(handle)
            writer.writerow(['北京时间日期', '本轮抓取弹幕数', '已有历史归档对应条数', '真正新增来源记录',
                             '表情名称解析变化', '主库实际新增'])
            for day, counts in sorted(daily.items()):
                writer.writerow([day, counts['downloaded'], counts['archived_before'], counts['upstream_added'],
                                 counts['emoji_names_resolved'], 0])
        with (work / '场次核对.csv').open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.writer(handle)
            writer.writerow(['开播日期', '场次ID', '标题', '原主条目弹幕数', '刷新主条目弹幕数',
                             '已有历史归档对应条数', '真正新增来源记录'])
            for row in reports:
                writer.writerow([row['date'], row['live_id'], row['title'], row['old'], row['current'],
                                 row['archive_matched'], row['current']-row['archive_matched']])


def fetch(work: Path, interval: float, all_lives: bool = False) -> None:
    work.mkdir(parents=True, exist_ok=True)
    manifest_path = work / 'manifest.json'
    client = RateLimitedClient(interval=interval)
    with closing(sqlite3.connect(store.ARCHIVE_DB, timeout=10)) as cold:
        cold.execute('PRAGMA journal_mode=WAL')
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
        else:
            old = cold.execute(
                "SELECT id,payload FROM danmaku_source_artifact "
                "WHERE source='danmakus' AND locator='catalog' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = json.loads(zlib.decompress(old[1]))
            old_lives = {r['liveId']: r for r in _select_danmakus_lives(previous, store.EXTERNAL_CUTOFF_MS)}
            response = client.get(DANMAKUS_CATALOG_URL, params={'uid': CHANNEL_UID})
            current = response.json()
            lives = _select_danmakus_lives(current, store.EXTERNAL_CUTOFF_MS)
            with cold:
                aid = store.artifact(cold, 'danmakus', 'catalog', response.content,
                                     {'purpose': 'history_refresh', 'cutoff_ms': store.EXTERNAL_CUTOFF_MS})
            old_versions = catalog_versions(previous); new_versions = catalog_versions(current)
            selected = [r for lid, r in new_versions.items() if all_lives or lid not in old_versions
                        or r.get('danmakusCount') != old_versions[lid].get('danmakusCount')]
            selected.sort(key=lambda r: (r['startDate'], r['liveId']))
            manifest = dict(previous_catalog_id=old[0], catalog_id=aid,
                            previous_lives=len(old_lives), current_lives=len(lives),
                            catalog_total=len(current['data']['lives']),
                            absent_live_ids=sorted(set(old_lives) - {r['liveId'] for r in lives}),
                            previous_versions=len(old_versions), current_versions=len(new_versions),
                            selected=[dict(live=r, previous=old_versions.get(r['liveId'])) for r in selected],
                            completed={}, failures={})
            _atomic_json(manifest_path, manifest)
        emojis = _load_emoji_map()
        try:
            for i, entry in enumerate(manifest['selected'], 1):
                live = entry['live']; lid = live['liveId']
                if lid in manifest['completed']:
                    continue
                try:
                    response = client.get(
                        DANMAKUS_FULL_URL.format(live_id=lid),
                        params={'include': 'extra', 'includeEnter': 'false'},
                        headers={'Accept': 'application/x-msgpack', 'Origin': 'https://danmakus.com',
                                 'Referer': 'https://danmakus.com/'})
                    rows, info = _decode_danmakus_rows(response.content, live_id=lid,
                                                     cutoff_ms=store.EXTERNAL_CUTOFF_MS, emoji_map=emojis)
                    learned = {}
                    if info['unknown_emojis']:
                        learned, _ = _fetch_danmakus_emoji_names(client, live_id=lid, resources=info['unknown_emojis'])
                        emojis.update(learned)
                        rows, info = _decode_danmakus_rows(response.content, live_id=lid,
                                                         cutoff_ms=store.EXTERNAL_CUTOFF_MS, emoji_map=emojis)
                    if not info['raw_records'] and live.get('danmakusCount'):
                        raise ValueError('Nonempty catalogue but empty full dump; requires paged verification')
                    with cold:
                        aid = store.artifact(cold, 'danmakus', lid + '/full', response.content,
                                             dict(purpose='history_refresh', live=live, decode=info,
                                                  emoji_names=learned))
                    manifest['completed'][lid] = dict(artifact_id=aid, **info, emoji_names=learned,
                                                     bytes=len(response.content))
                    manifest['failures'].pop(lid, None)
                    print(json.dumps(dict(index=i, total=len(manifest['selected']), live_id=lid,
                                          start=live['startDate'], **info), ensure_ascii=False), flush=True)
                except Exception as exc:
                    manifest['failures'][lid] = str(exc)
                    print(f'FAILED {lid}: {exc}', flush=True)
                _atomic_json(manifest_path, manifest)
        finally:
            client.close()
    print(json.dumps(dict(completed=len(manifest['completed']), failures=manifest['failures']), ensure_ascii=False), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--interval', type=float, default=0.85)
    parser.add_argument('--all', action='store_true', help='Fetch all currently listed historical sessions')
    parser.add_argument('--audit', action='store_true', help='Read-only comparison against the current event table')
    args = parser.parse_args()
    if args.interval < 0.5:
        parser.error('Use an interval of at least 0.5 seconds')
    if args.audit:
        audit(args.work)
    else:
        fetch(args.work, args.interval, args.all)


if __name__ == '__main__':
    main()
