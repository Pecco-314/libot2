"""分层歌名匹配：原文优先，格式容错、部分匹配和保守拼写纠错依次降级。"""

import unicodedata
from functools import lru_cache

from opencc import OpenCC
from rapidfuzz import fuzz
from rapidfuzz.distance import DamerauLevenshtein

_SIMPLIFY = OpenCC('t2s')


@lru_cache(maxsize=8192)
def forms(value):
    raw = unicodedata.normalize('NFKC', value or '').strip().casefold()
    simple = _SIMPLIFY.convert(raw)
    compact = ''.join(c for c in simple if c.isalnum())
    return raw, simple, compact


def match_title(query, title, translation):
    q, qs, qc = forms(query)
    best = None
    for offset, value in enumerate((title, translation)):
        if not value:
            continue
        t, ts, tc = forms(value)
        if q == t:
            rank, score = offset, 100.0
        elif qs == ts or (qc and qc == tc):
            rank, score = 2 + offset, 100.0
        elif q in t or qs in ts or (qc and qc in tc):
            rank = 4 if t.startswith(q) or (qc and tc.startswith(qc)) else 5
            score = 100.0
        else:
            # 短词不猜字；只允许较长歌名少量增删、错字及相邻字母颠倒。
            if len(qc) < 5 or not tc:
                continue
            distance = DamerauLevenshtein.distance(qc, tc)
            allowed = 1 if min(len(qc), len(tc)) < 10 else 2
            score = fuzz.ratio(qc, tc)
            chinese = any('\u4e00' <= c <= '\u9fff' for c in qc)
            if distance > allowed or score < (80 if chinese else 85):
                continue
            rank = 6
        candidate = (rank, -score)
        if best is None or candidate < best:
            best = candidate
    return best


def rank_songs(query, songs):
    if not query.strip() or len(query) > 200 or not forms(query)[2]:
        return []
    matches = []
    for song in songs:
        match = match_title(query, song['title'], song.get('title_trans'))
        if match is not None:
            matches.append((match, song))
    if not matches:
        return []
    best_rank = min(m[0][0] for m in matches)
    # 有直接匹配时不混入拼写猜测，保留原有部分匹配候选。
    matches = [m for m in matches if best_rank == 6 or m[0][0] != 6]
    matches.sort(key=lambda m: (*m[0], -m[1]['count'], m[1]['id']))
    ambiguous = False
    if len(matches) > 1:
        first, second = matches[0][0], matches[1][0]
        ambiguous = (first[0] < 4 and first[0] == second[0]) or (
            first[0] == second[0] == 6 and second[1] - first[1] < 10)
    return [{**song, 'match_kind': 'typo' if rank == 6 else 'direct',
             'match_score': -negative_score,
             'needs_confirmation': ambiguous if index == 0 else False}
            for index, ((rank, negative_score), song) in enumerate(matches)]
