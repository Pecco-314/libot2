import time
from typing import Any

from src.db.transcript import get_recent_transcripts
# 修正了导入路径
from src.capture.fuzzy import LyricsMatcher

_matcher = LyricsMatcher()

def refresh_now_playing_matcher():
    """在系统启动或歌单更新时刷新索引"""
    _matcher.refresh()

def _calc_combined_probability(evidences: list[float], top_n: int = 3) -> float:
    """独立证据累加：1 - (1-p1)*(1-p2)..."""
    if not evidences:
        return 0.0
    # 降序排列，只取前 N 个最强的证据，防止大量低分垃圾信息无限“水滴石穿”
    evidences.sort(reverse=True)
    p_fail = 1.0
    for e in evidences[:top_n]:
        p_fail *= (1.0 - e)
    return 1.0 - p_fail

def guess_song(room_id: int, target_ts: int | None = None, window: int = 60) -> list[dict[str, Any]]:
    if not target_ts:
        target_ts = int(time.time())
        
    if getattr(_matcher, '_index', None) is None:
        _matcher.refresh()

    asr_texts = get_recent_transcripts(room_id, target_ts, window_seconds=window, limit=15)

    # Each song accumulates independent ASR evidence from recent windows.
    song_scores_map = {}

    def _process_and_score(texts: list[str]):
        seen_texts: set[str] = set()
        for text in texts:
            text = text.strip()
            if not text or text in seen_texts:
                continue
            seen_texts.add(text)
            # 计算有效字符长度（剔除空格，因为英文单词间的空格会虚报长度）
            eff_len = len(text.replace(" ", ""))
            if eff_len < 2:
                continue
                
            # 非线性长度惩罚：10个字以上权重满分，短句权重呈指数级断崖下跌
            # 例如：长度为3，weight = 0.3^1.5 = 0.164
            weight = 1.0 if eff_len >= 10 else (eff_len / 10.0) ** 1.5
            
            results = _matcher.search(text, limit=5)
            for res in results:
                song_id = res['id']
                # 基础分数归一化
                base_score = res['score'] / 100.0 
                
                # 计算该片段提供的真实证据置信度
                evidence_score = base_score * weight
                
                if song_id not in song_scores_map:
                    song_scores_map[song_id] = {
                        'info': res,
                        'evidences': [],
                    }
                
                song_scores_map[song_id]['evidences'].append(evidence_score)

    _process_and_score(asr_texts)

    final_results = []
    for song_id, data in song_scores_map.items():
        final_score = _calc_combined_probability(data['evidences'], top_n=3)
        
        # 提高门槛：综合得分大于 0.4 才进入候选，彻底砍掉高频词噪音
        if final_score > 0.4:
            final_results.append({
                'title': data['info']['title'],
                'singer': data['info']['original_singer'],
                'asr_score': final_score,
                'final_score': final_score
            })

    # 按最终分数从高到低排序，返回 Top 3
    final_results.sort(key=lambda x: x['final_score'], reverse=True)
    return final_results[:3]