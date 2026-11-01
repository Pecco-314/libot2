"""汇总逐日歌单对照，输出完整文本和 CSV 明细。"""
from collections import Counter, defaultdict
import csv
from datetime import datetime
import json
from pathlib import Path

ROOT=Path('data/maintenance/preview_benchmark_20261026')


def main():
    days=[json.loads(p.read_text()) for p in sorted(ROOT.glob('2026-*.json'))]
    relaxed={d['date']:d['variants'] for d in json.loads((ROOT/'relaxed.json').read_text())}
    lines=['预览歌单评测', '参照：song_record，按 record.id 排序；同日多场合并。',
           '按歌曲和次数对齐，不具备演唱音频时间标注，放原唱和真人演唱可能相互代替，因此这是记录层的召回，非音频级准确率。',
           '额外项：可能是放歌、哼唱、漏记、重复拆段或误识别，均保留待核对。',
           '数据以读取时为准；识别过程未使用当天正式歌单筛选候选。',
           '基线：逐窗口两段/40%，段内10%歌词覆盖和连续窗口；放宽对照：整段三段/20秒/至少20字符且4%歌词覆盖，允许120秒空缺，再合并短暂打断。',
           '参数在前六个已完成日期上选择（10-13、14、20、21、23、24），其余日期继续验证；这批结果包含调参样本，不等同独立测试集准确率。',
           '已确认数据错误：酸的。的数据库歌词实际属于眼镜的葬礼；业务库保持原样，未从召回分母扣除。',
           '核对来源：https://music-tw.line.me/track/7178902001 与 https://music-tw.line.me/track/7157042001', '']
    table=[]
    lang=defaultdict(Counter)
    for d in days:
        base=d['variants']['0.85']; new=relaxed[d['date']]['production']
        table.append([d['date'],len(d['sessions']),base['expected'],base['matched_count'],new['matched_count'],new['expected']-new['matched_count'],sum(new['extra'].values()),new['lcs']])
        missing=Counter(new['missing'])
        for title,singer,language,lyriclen in d['records']:
            lang[language]['expected']+=1
            if missing[title]:
                missing[title]-=1
                lang[language]['missing']+=1
            else: lang[language]['matched']+=1
        lines.extend([f"日期 {d['date']}，{len(d['sessions'])} 场，正式 {base['expected']} 次，基线命中 {base['matched_count']}，放宽命中 {new['matched_count']}，顺序LCS {new['lcs']}",
                      '基线漏项：'+json.dumps(base['missing'],ensure_ascii=False),
                      '放宽漏项：'+json.dumps(new['missing'],ensure_ascii=False),
                      '额外项：'+json.dumps(new['extra'],ensure_ascii=False),
                      '正式顺序：'+' → '.join(r[0] for r in d['records']),
                      '识别顺序：'+' → '.join(datetime.fromtimestamp(e['first_ts']).strftime('%m-%d %H:%M:%S')+' '+e['title'] for e in new['entries'])])
        for title in new['missing']:
            evidence=d['missing_evidence'].get(title,[])
            lines.append('剩余漏项证据 '+title+': '+json.dumps([{k:e.get(k) for k in ('timestamp','final_score','support_count','asr_coverage','matched_texts')} for e in evidence[:3]],ensure_ascii=False))
        lines.append('')
    totals=[sum(row[i] for row in table) for i in range(1,8)]
    lines.extend(['总计：'+str(totals),'语言：'+json.dumps(lang,ensure_ascii=False),
                  '匹配分门槛扫描（基线其余条件不变）：'])
    for threshold in ('.95','.85','.75','.65','.5','0'):
        key=str(float(threshold)) if threshold!='0' else '0'
        lines.append(f'{key}: 命中 {sum(d["variants"][key]["matched_count"] for d in days)}，额外 {sum(sum(d["variants"][key]["extra"].values()) for d in days)}')
    (ROOT/'report.txt').write_text('\n'.join(lines))
    with (ROOT/'summary.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.writer(f); writer.writerow(['日期','直播场次','正式演唱次数','基线命中','放宽命中','放宽漏项','额外项','顺序LCS']); writer.writerows(table)
    print('\n'.join(map(str,table))); print('TOTAL',totals); print('LANG',dict(lang))


if __name__=='__main__':
    main()
