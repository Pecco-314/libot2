from bisect import bisect_left, bisect_right
import hashlib
import json
import time
from typing import Any

from src.db.transcript import get_recent_transcripts, list_transcripts_in_range
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


def _ensure_matcher() -> None:
    _matcher.ensure_fresh()


def _score_text(text: str) -> list[tuple[int, dict[str, Any], float]]:
    text = text.strip()
    if not text:
        return []

    effective_length = len(text.replace(" ", ""))
    if effective_length < 2:
        return []
    weight = (
        1.0
        if effective_length >= 10
        else (effective_length / 10.0) ** 1.5
    )
    return [
        (
            int(result["id"]),
            result,
            float(result["score"]) / 100.0 * weight,
        )
        for result in _matcher.search(text, limit=5)
    ]


def _guess_from_texts(
    texts: list[str],
    *,
    search_cache: dict[str, list[tuple[int, dict[str, Any], float]]]
    | None = None,
) -> list[dict[str, Any]]:
    _ensure_matcher()
    song_scores_map: dict[int, dict[str, Any]] = {}
    seen_texts: set[str] = set()

    for raw_text in texts:
        text = raw_text.strip()
        if not text or text in seen_texts:
            continue
        seen_texts.add(text)

        if search_cache is None:
            scored = _score_text(text)
        else:
            scored = search_cache.get(text)
            if scored is None:
                scored = _score_text(text)
                search_cache[text] = scored

        for song_id, info, evidence in scored:
            entry = song_scores_map.setdefault(
                song_id,
                {"info": info, "evidences": []},
            )
            entry["evidences"].append(evidence)

    final_results: list[dict[str, Any]] = []
    for data in song_scores_map.values():
        final_score = _calc_combined_probability(
            data["evidences"],
            top_n=3,
        )
        if final_score <= 0.4:
            continue
        final_results.append(
            {
                "title": data["info"]["title"],
                "title_trans": data["info"].get("title_trans") or "",
                "singer": data["info"]["original_singer"],
                "asr_score": final_score,
                "final_score": final_score,
            }
        )

    final_results.sort(
        key=lambda item: item["final_score"],
        reverse=True,
    )
    return final_results[:3]

def guess_song(room_id: int, target_ts: int | None = None, window: int = 60) -> list[dict[str, Any]]:
    if not target_ts:
        target_ts = int(time.time())

    asr_texts = get_recent_transcripts(room_id, target_ts, window_seconds=window, limit=15)
    return _guess_from_texts(asr_texts)


def guess_song_timeline(
    room_id: int,
    start_ts: int,
    end_ts: int,
    *,
    window: int = 60,
    step: int = 60,
    window_cache: dict | None = None,
) -> list[dict[str, Any]]:
    """按低重叠窗口批量运行与 ``guess_song`` 相同的评分逻辑。"""
    if end_ts < start_ts:
        raise ValueError("end_ts must not be earlier than start_ts")
    if window <= 0 or step <= 0:
        raise ValueError("window and step must be positive")

    rows = list_transcripts_in_range(room_id, start_ts, end_ts + 2)
    if not rows:
        return []
    timestamps = [int(row["timestamp"]) for row in rows]

    windows: list[tuple[int, int]] = []
    window_start = start_ts
    while window_start <= end_ts:
        window_end = min(window_start + window, end_ts)
        windows.append((window_start, window_end))
        if window_end >= end_ts:
            break
        window_start += step

    search_cache: dict[
        str,
        list[tuple[int, dict[str, Any], float]],
    ] = {}
    timeline: list[dict[str, Any]] = []
    for window_start, window_end in windows:
        left = bisect_left(timestamps, window_start)
        right = bisect_right(timestamps, window_end + 2)
        recent_rows = rows[max(left, right - 15):right]
        texts = [
            str(row["content"])
            for row in reversed(recent_rows)
        ]
        cache_key = f"{window_start}:{window_end}"
        signature = hashlib.sha256(json.dumps(texts, ensure_ascii=False).encode()).hexdigest()
        cached = window_cache.get(cache_key) if window_cache is not None else None
        if cached is not None and cached.get('signature') == signature:
            results = cached['results']
        else:
            results = _guess_from_texts(texts, search_cache=search_cache)
            if window_cache is not None:
                window_cache[cache_key] = {'signature': signature, 'results': results}
        timeline.append(
            {
                # 时间轴使用识别窗口中点，而不是窗口末端。
                "timestamp": window_start + (window_end - window_start) // 2,
                "window_start": window_start,
                "window_end": window_end,
                "results": results,
            }
        )
    return timeline


def collapse_song_timeline(
    timeline: list[dict[str, Any]],
    *,
    min_score: float = 0.0,
    inclusive: bool = False,
    max_gap_seconds: int = 0,
) -> list[dict[str, Any]]:
    """合并相同歌曲的相邻有效窗口，允许短空缺；其他达标歌曲会断开段落。"""
    if max_gap_seconds < 0:
        raise ValueError('max_gap_seconds must be non-negative')
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    gap_start: int | None = None
    last_window_end: int | None = None

    for point in timeline:
        timestamp = int(point["timestamp"])
        results = list(point.get("results") or [])
        if not results:
            if current is not None and gap_start is None:
                gap_start = last_window_end if last_window_end is not None else int(point.get('window_start', timestamp))
            continue

        top = results[0]
        title = str(top.get("title") or "未知歌曲")
        singer = str(top.get("singer") or "未知")
        key = (title.casefold(), singer.casefold())
        score = float(top.get("final_score") or 0.0)
        if score < min_score or (score == min_score and not inclusive):
            if current is not None and gap_start is None:
                gap_start = last_window_end if last_window_end is not None else int(point.get('window_start', timestamp))
            continue

        window_start = int(point.get('window_start', timestamp))
        # 使用窗口边界计量空缺，两个60秒空窗口恰好是120秒。
        boundary = gap_start if gap_start is not None else last_window_end
        gap = max(0, window_start - boundary) if boundary is not None else 0
        can_merge = gap <= max_gap_seconds and (gap_start is None or max_gap_seconds > 0)
        gap_start = None
        last_window_end = int(point['window_end']) if 'window_end' in point else None
        if current is not None and current["key"] == key and can_merge:
            current["last_ts"] = timestamp
            current["max_score"] = max(current["max_score"], score)
            current["sample_count"] += 1
            continue

        current = {
            "key": key,
            "title": title,
            "singer": singer,
            "first_ts": timestamp,
            "last_ts": timestamp,
            "max_score": score,
            "sample_count": 1,
        }
        entries.append(current)

    for entry in entries:
        entry.pop("key", None)
    return entries
