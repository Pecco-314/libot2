from __future__ import annotations

import json
import re
from typing import Any

from rapidfuzz import fuzz


def _has_hangul(text: str) -> bool:
    return bool(text and re.search(r"[\uAC00-\uD7A3]", text))


def _normalize_korean_spacing(text: str) -> str:
    return re.sub(r"\s+", "", text) if text else ""

from src.db.sqlite import connect_sqlite


def _score(keyword: str, text: str) -> float:
    if not keyword or not text:
        return 0.0
    if _has_hangul(keyword) or _has_hangul(text):
        keyword = _normalize_korean_spacing(keyword)
        text = _normalize_korean_spacing(text)
    if keyword in text:
        return 100.0
    return float(fuzz.partial_ratio(keyword, text))


class LyricsSearchIndex:
    def __init__(self, rows: list[tuple[Any, ...]]):
        entries: list[dict[str, Any]] = []
        for row in rows:
            lyrics_cleaned = row[6]
            if not lyrics_cleaned:
                continue
            entries.append(
                {
                    "row": row,
                    "text": str(lyrics_cleaned),
                }
            )
        self._entries = entries

    @classmethod
    def from_rows(cls, rows: list[tuple[Any, ...]]) -> "LyricsSearchIndex":
        return cls(rows)

    def search(
        self,
        keyword: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        keyword = (keyword or "").strip()
        if not keyword:
            return []

        ranked: list[tuple[float, tuple[Any, ...]]] = []
        for entry in self._entries:
            score = _score(keyword, entry["text"])
            ranked.append((score, entry["row"]))

        if not ranked:
            return []

        ranked.sort(key=lambda item: item[0], reverse=True)
        results = []
        for score, row in ranked[:limit]:
            try:
                records_list = json.loads(row[4]) if row[4] else []
            except Exception:
                records_list = []
            results.append(
                {
                    "id": row[0],
                    "title": row[1],
                    "title_trans": row[2],
                    "original_singer": row[3],
                    "records": records_list,
                    "count": row[5],
                    "score": score,
                }
            )
        return results


class LyricsMatcher:
    def __init__(self, index: LyricsSearchIndex | None = None):
        self._index = index

    def refresh(self) -> None:
        with connect_sqlite() as conn:
            rows = conn.execute(
                """
                SELECT id, title, title_trans, original_singer, records, count, lyrics_cleaned
                FROM song_list
                WHERE lyrics_cleaned IS NOT NULL AND lyrics_cleaned != ''
                """
            ).fetchall()
        self._index = LyricsSearchIndex.from_rows(rows)

    def search(
        self,
        keyword: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        if self._index is None:
            return []
        return self._index.search(keyword, limit=limit)

if __name__ == "__main__":
    matcher = LyricsMatcher()
    matcher.refresh()
    while True:
        query = input("请输入歌词片段（或 'exit' 退出）：").strip()
        if query.lower() == "exit":
            break
        results = matcher.search(query)
        print(f"找到 {len(results)} 条匹配结果：")
        for idx, result in enumerate(results, start=1):
            print(f"{idx}. {result['title']} - {result['original_singer']} (score: {result['score']:.2f})")