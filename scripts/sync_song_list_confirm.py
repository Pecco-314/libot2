#!/usr/bin/env python3
"""Preview a conservative website merge; apply an explicitly reviewed plan."""
import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.db.song_list import _normalize_song_identity, _json_list
from src.spider.jobs.song_list import CSV_RAW_URL, parse_song_csv

FIELDS = ('title','title_trans','original_singer','notes','language','clips','tags')


def snapshot(conn):
    return {table:[dict(r) for r in conn.execute(f'SELECT * FROM {table} ORDER BY id')]
            for table in ('song_info','song_record')}


def digest(data):
    return hashlib.sha256(json.dumps(data,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def preview(data, songs, mapping=None):
    counts = Counter((r['song_id'],r['record_date']) for r in data['song_record'])
    latest = max((d for s in songs for d in s['records']),default='')
    plan = {'version':1,'local_digest':digest(data),'remote_latest':latest,
            'local_latest':max((r['record_date'] for r in data['song_record']),default=''),
            'changes':[], 'unmatched':[], 'local_only':[]}
    claimed = set()
    mapping = mapping or {}
    for song in songs:
        for date in song['records']:
            if datetime.strptime(date,'%Y-%m-%d').strftime('%Y-%m-%d') != date:
                raise ValueError(f'invalid date: {date}')
        key = _normalize_song_identity(song['title'])
        matches = [s for s in data['song_info'] if key in
                   {_normalize_song_identity(s['title']),_normalize_song_identity(s['title_trans'])}]
        if len(matches)>1:
            matches = [s for s in matches if _normalize_song_identity(s['original_singer']) ==
                       _normalize_song_identity(song['original_singer'])]
        if song['external_id'] in mapping:
            matches = [s for s in data['song_info'] if s['id']==int(mapping[song['external_id']])]
            if len(matches)!=1: raise ValueError('映射的本地歌曲不存在')
        if len(matches)!=1 or matches[0]['id'] in claimed:
            plan['unmatched'].append(song)
            if not matches:
                plan['changes'].append(dict(kind='new_song',title=song['title'],before=None,
                    after=song,selected=False))
            continue
        local = matches[0]; sid = local['id']; claimed.add(sid)
        def change(kind, **kwargs):
            plan['changes'].append(dict(kind=kind,song_id=sid,title=local['title'],**kwargs))
        for field in FIELDS:
            if field=='clips':
                before = _json_list(local[field]); remote = _json_list(song[field])
                added = [x for x in remote if x not in before]
                if added:
                    change('field',field=field,before=local[field],after=json.dumps(before+added,ensure_ascii=False),selected=True)
            else:
                remote = str(song.get(field) or '').strip()
                if remote and remote!=str(local[field] or '').strip():
                    change('field',field=field,before=local[field],after=remote,selected=not bool(local[field]))
        remote_counts = Counter(song['records'])
        dates = sorted(set(remote_counts)|{d for i,d in counts if i==sid and d<=latest})
        for date in dates:
            before,after=counts[sid,date],remote_counts[date]
            if before!=after:
                change('count',date=date,before=before,after=after,selected=after>before)
    plan['local_only'] = [{'id':s['id'],'title':s['title']} for s in data['song_info'] if s['id'] not in claimed]
    return plan


def report(plan):
    lines=['# 歌单同步预览', '', f"网站最新：{plan['remote_latest']}；本地最新：{plan['local_latest']}",
           '', 'selected=true 的项目将在确认时执行。默认只补缺失次数、歌切及空字段；已有字段冲突、次数减少均不选中。',
           '网站最新日期之后的本地记录、歌词及现有歌单顺序保留。未匹配歌曲的新增建议默认不选中，需核对是否是别名。',
           '补入记录追加到当日已有记录之后，网站不提供可靠演唱顺序。', '',
           '|计划序号|选中|歌曲|修改|本地|网站/合并结果|','|---|---|---|---|---|---|']
    def cell(v): return str(v).replace('|','\\|').replace('\n',' ')
    for i,c in enumerate(plan['changes']):
        lines.append('|'+ '|'.join(map(cell,[i,c['selected'],c['title'],c.get('field',c.get('date')),c['before'],c['after']]))+'|')
    lines+=['','## 未匹配/重复映射，需单独核对','']
    lines += [f"- {s['external_id']} {s['title']} / {s['original_singer']}" for s in plan['unmatched']]
    lines+=['','## 仅本地/未匹配本地（保留）','']
    lines += [f"- {s['id']} {s['title']}" for s in plan['local_only']]
    return '\n'.join(lines)+'\n'


def apply(conn, plan, backup):
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('BEGIN IMMEDIATE')
    try:
        data=snapshot(conn)
        if digest(data)!=plan['local_digest']:
            raise ValueError('本地歌单已发生变化，请重新生成预览并确认')
        # Logical backup covers every table this script may modify, including lyrics.
        with backup.open('x',encoding='utf-8') as f:
            json.dump(data,f,ensure_ascii=False,indent=2)
        for c in plan['changes']:
            if c['selected'] is not True: continue
            if c['kind']=='new_song':
                song=c['after']
                sid=conn.execute('''INSERT INTO song_info(title,title_trans,original_singer,notes,language,clips,tags,lyrics,lyrics_cleaned)
                    VALUES(?,?,?,?,?,?,?,'','')''',
                    (song['title'],song['title_trans'],song['original_singer'],song['notes'],song['language'],
                     json.dumps(song['clips'],ensure_ascii=False),song['tags'])).lastrowid
                conn.executemany('INSERT INTO song_record(song_id,record_date) VALUES (?,?)',[(sid,d) for d in song['records']])
                continue
            sid=c['song_id']
            if c['kind']=='field' and c['field'] in FIELDS:
                conn.execute(f"UPDATE song_info SET {c['field']}=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(c['after'],sid))
            elif c['kind']=='count':
                before,after=c['before'],c['after']
                if not isinstance(after,int) or not 0<=after<=100:
                    raise ValueError('invalid occurrence count')
                if after>before:
                    conn.executemany('INSERT INTO song_record(song_id,record_date) VALUES (?,?)',[(sid,c['date'])]*(after-before))
                elif before>after:
                    conn.execute('DELETE FROM song_record WHERE id IN (SELECT id FROM song_record WHERE song_id=? AND record_date=? ORDER BY id DESC LIMIT ?)',(sid,c['date'],before-after))
            else: raise ValueError('invalid plan operation')
        conn.commit()
    except Exception:
        conn.rollback(); raise


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,default=ROOT/'data/libot.db')
    parser.add_argument('--csv',type=Path,help='local website CSV snapshot')
    parser.add_argument('--mapping',type=Path,help='reviewed external_id -> local song_id JSON mapping')
    parser.add_argument('--output',type=Path,default=ROOT/'data/song_sync_review')
    parser.add_argument('--confirm',type=Path,help='apply this reviewed plan JSON; no network fetch')
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    if args.confirm:
        plan=json.loads(args.confirm.read_text())
        if plan['version']!=1: raise ValueError('unsupported plan')
        backup=args.output/f'backup-{datetime.now():%Y%m%d-%H%M%S-%f}.json'
        with sqlite3.connect(args.db,timeout=5) as conn:
            conn.row_factory=sqlite3.Row
            apply(conn,plan,backup)
        print(f'已执行 {sum(c["selected"] is True for c in plan["changes"])} 项；备份：{backup}')
        return
    if args.csv:
        text=args.csv.read_text(encoding='utf-8-sig')
    else:
        import httpx
        response=httpx.get(CSV_RAW_URL,timeout=30,trust_env=False,follow_redirects=True)
        response.raise_for_status(); text=response.content.decode('utf-8-sig')
    songs=parse_song_csv(text)
    if not songs: raise ValueError('empty website data')
    with sqlite3.connect(f'file:{args.db.resolve()}?mode=ro',uri=True) as conn:
        conn.row_factory=sqlite3.Row
        conn.execute('BEGIN')
        data=snapshot(conn)
    plan=preview(data,songs,json.loads(args.mapping.read_text()) if args.mapping else None)
    name=f'review-{datetime.now():%Y%m%d-%H%M%S-%f}'
    path=args.output/f'{name}.json'
    path.write_text(json.dumps(plan,ensure_ascii=False,indent=2))
    path.with_suffix('.md').write_text(report(plan))
    path.with_suffix('.csv').write_text(text)
    print(json.dumps({'local_songs':len(data['song_info']),'remote_songs':len(songs),
        'local_latest':plan['local_latest'],'remote_latest':plan['remote_latest'],
        'selected':sum(c['selected'] for c in plan['changes']), 'differences':len(plan['changes']),
        'unmatched':len(plan['unmatched']), 'plan':str(path)},ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
