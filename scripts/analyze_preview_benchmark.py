"""从已保存的窗口测试整段证据筛选；不重新运行 ASR 或写入业务库。"""
from collections import Counter
import json
from pathlib import Path
import sqlite3

from scripts.benchmark_song_preview import evaluate
from src.capture.guesser import collapse_song_timeline
from src.common.song_evidence import covered_length, select_preview_entries

ROOT = Path('data/maintenance/preview_benchmark_20261026')


def relaxed(timeline, min_words=3, min_coverage=.08, gap=120, merge=False):
    selected = []
    for point in timeline:
        candidates = [r for r in point['results'] if r.get('support_count',0)>=1 and r['final_score']>=.85]
        candidates.sort(key=lambda r:r['preview_quality'],reverse=True)
        selected.append({**point,'results':candidates[:1]})
    entries = collapse_song_timeline(selected,min_score=.85,inclusive=True,max_gap_seconds=gap)
    accepted = []
    for entry in entries:
        evidence = [p['results'][0] for p in selected if entry['first_ts']<=p['timestamp']<=entry['last_ts'] and p['results'] and (p['results'][0]['title'],p['results'][0]['singer'])==(entry['title'],entry['singer'])]
        words = set(w for e in evidence for w in e['matched_texts'])
        times = [t for e in evidence for t in e['evidence_times']]
        spans = [s for e in evidence for s in e['lyric_spans']]
        coverage = covered_length(spans)/max(1,max(e['lyrics_length'] for e in evidence))
        if len(words)>=min_words and times and max(times)-min(times)>=20 and coverage>=min_coverage:
            accepted.append({**entry,'distinct_evidence':len(words),'lyric_coverage':coverage,'evidence_seconds':max(times)-min(times)})
    if not merge:
        return accepted
    merged=[]
    for entry in accepted:
        if merged and (entry['title'],entry['singer']) == (merged[-1]['title'],merged[-1]['singer']) and entry['first_ts']-merged[-1]['last_ts']<=gap+60:
            prev=merged[-1]
            prev['last_ts']=entry['last_ts']
            prev['sample_count']+=entry['sample_count']
            prev['max_score']=max(prev['max_score'],entry['max_score'])
        else:
            merged.append(dict(entry))
    return merged


def main():
    conn=sqlite3.connect(f'file:{ROOT}/song_preview.sqlite3?mode=ro',uri=True)
    output=[]
    for path in sorted(ROOT.glob('2026-*.json')):
        day=json.loads(path.read_text())
        timelines=[]
        for s in day['sessions']:
            timeline=[]
            for a,b,r in conn.execute('SELECT window_start,window_end,results FROM preview_window WHERE session_start=? ORDER BY window_start',(s['start'],)):
                timeline.append({'timestamp':a+(b-a)//2,'window_start':a,'window_end':b,'results':json.loads(r)})
            timelines.append(timeline)
        variants={}
        for label,words,coverage,merge in [('segment3',3,.08,False),('segment2',2,.08,False),('segment3_lowcov',3,.04,False),('segment3_merge',3,.04,True)]:
            entries=[e for t in timelines for e in relaxed(t,words,coverage,merge=merge)]
            variants[label]={**evaluate([r[0] for r in day['records']],entries),'entries':entries}
        entries=[e for t in timelines for e in select_preview_entries(t,.85)]
        variants['production']={**evaluate([r[0] for r in day['records']],entries),'entries':entries}
        output.append({'date':day['date'],'variants':variants})
        print(day['date'],'baseline',day['variants']['0.85']['matched_count'],'/',len(day['records']),[(k,v['matched_count'],sum(v['extra'].values())) for k,v in variants.items()])
    (ROOT/'relaxed.json').write_text(json.dumps(output,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
