"""预览歌单的歌词覆盖及时间连续性证据；分数是匹配指标，不是校准概率。"""
from src.common.lyrics_text import search_text

from rapidfuzz import fuzz


def normalize(text):
    return search_text(text)


def covered_length(spans):
    end = total = 0
    for left, right in sorted(spans):
        total += max(0, right - max(left, end))
        end = max(end, right)
    return total


def window_evidence(texts, matches, lyrics):
    """仅较长且匹配可靠的独立 ASR 片段贡献覆盖，重复句不重复计数。"""
    lyrics = normalize(lyrics or '')
    valid = {normalize(text) for text in texts if len(normalize(text)) >= 4}
    supported, spans, indices, scores = set(), [], [], []
    for index, text, score in matches:
        text = normalize(text)
        if len(text) < 6 or text in supported or score < .55 or not lyrics:
            continue
        alignment = fuzz.partial_ratio_alignment(text, lyrics)
        if alignment is None or alignment.score < 70:
            continue
        if alignment.src_end - alignment.src_start < .7 * len(text):
            continue
        supported.add(text)
        spans.append([alignment.dest_start, alignment.dest_end])
        indices.append(index)
        scores.append(score)
    coverage = len(supported) / len(valid) if valid else 0
    mean = sum(scores) / len(scores) if scores else 0
    return {
        'support_count': len(supported), 'asr_coverage': coverage,
        'lyric_spans': spans, 'lyrics_length': len(lyrics),
        'matched_indices': indices, 'matched_texts': sorted(supported),
        'preview_quality': .5 * coverage + .3 * min(len(supported) / 4, 1) + .2 * mean,
    }


def select_preview_entries(timeline, threshold):
    """强窗口确认歌曲，弱窗口延续；不确定窗口保留为空档。"""
    from src.capture.guesser import collapse_song_timeline

    selected = []
    continuation_floor = max(.55, threshold - .20)
    for point in timeline:
        candidates = sorted(point.get('results', []), key=lambda r: r.get('final_score', 0), reverse=True)
        winners = []
        if candidates:
            top = candidates[0]
            score = top.get('final_score', 0)
            runner_up = candidates[1].get('final_score', 0) if len(candidates) > 1 else 0
            margin = score - runner_up
            if score >= continuation_floor and margin >= .04:
                winners = [{**top, 'strong_window': score >= threshold and margin >= .10,
                            'candidate_margin': margin}]
        selected.append({**point, 'results': winners})
    entries = collapse_song_timeline(selected, min_score=continuation_floor, inclusive=True, max_gap_seconds=120)
    def enrich(entry):
        spans, words, times = [], set(), []
        run = longest = 0
        previous_end = None
        coverage_sum = 0
        sample_count = 0
        lyrics_length = 0
        strong_count = 0
        short_pass = False
        high_run = longest_high_run = 0
        direct_words = set()
        for point in selected:
            if not entry['first_ts'] <= point['timestamp'] <= entry['last_ts']:
                continue
            top = (point['results'] or [None])[0]
            if not top or (top['title'], top['singer']) != (entry['title'], entry['singer']):
                run = 0
                high_run = 0
                continue
            start = point.get('window_start', point['timestamp'])
            adjacent = previous_end is not None and start <= previous_end
            if top['final_score'] >= max(.95, threshold):
                high_run = high_run + 1 if adjacent else 1
            else:
                high_run = 0
            longest_high_run = max(longest_high_run, high_run)
            run = run + 1 if previous_end is not None and start <= previous_end else 1
            longest = max(longest, run)
            previous_end = point.get('window_end', point['timestamp'])
            spans.extend(top.get('lyric_spans', []))
            direct_words.update(top.get('matched_texts', []))
            words.update(top.get('observation_texts', top.get('matched_texts', [])))
            times.extend(top.get('observation_times', top.get('evidence_times', [])))
            lyrics_length = max(lyrics_length, top.get('lyrics_length', 0))
            coverage_sum += top.get('asr_coverage', 0)
            sample_count += 1
            strong_count += top['strong_window']
            short_pass |= (top['strong_window'] and top.get('support_count', 0) >= 3
                           and covered_length(top.get('lyric_spans', [])) / max(1, top.get('lyrics_length', 0)) >= .12)
        covered = covered_length(spans)
        lyric_coverage = covered / lyrics_length if lyrics_length else 0
        duration = max(times) - min(times) if times else 0
        # 多窗口一致领先可容忍逐句失真；单窗口短演唱仍需直接歌词证据。
        sustained = strong_count >= 1 and sample_count >= 2 and duration >= 30
        # 连续高分首位可共同确认歌曲；直接歌词证据约束分数饱和时的候选竞争。
        consistent = (longest_high_run >= 2 and len(direct_words) >= 3
                      and lyric_coverage >= .12 and duration >= 30)
        if len(words) < 3 or duration < 20 or not (sustained or short_pass or consistent):
            return None
        return {**entry, 'lyric_coverage': lyric_coverage, 'sample_count': sample_count,
                         'asr_coverage': coverage_sum / sample_count,
                         'distinct_evidence': len(words), 'longest_run': longest,
                         'evidence_seconds': duration, 'strong_windows': strong_count,
                         'consecutive_high_windows': longest_high_run}

    boundaries = {p['timestamp']: (p.get('window_start', p['timestamp']),
                                  p.get('window_end', p['timestamp'])) for p in selected}
    merged = []
    for entry in entries:
        # 尚未确认的短暂竞争候选不切断原曲；已确认的新歌曲建立边界。
        joined = False
        for previous in reversed(merged):
            if boundaries[entry['first_ts']][0] - boundaries[previous['last_ts']][1] > 120:
                break
            if (entry['title'], entry['singer']) == (previous['title'], previous['singer']):
                previous.update(last_ts=entry['last_ts'], max_score=max(previous['max_score'], entry['max_score']))
                joined = True
                break
            if enrich(previous) is not None:
                break
        if not joined:
            merged.append(entry)
    return [result for entry in merged if (result := enrich(entry)) is not None]
