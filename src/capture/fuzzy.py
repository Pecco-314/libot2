from __future__ import annotations

import json
import re
import threading
import time
from typing import Any

from rapidfuzz import fuzz
from src.db.sqlite import connect_sqlite


def _has_hangul(text: str) -> bool:
    return bool(text and re.search(r"[\uAC00-\uD7A3]", text))


def _normalize_korean_spacing(text: str) -> str:
    return re.sub(r"\s+", "", text) if text else ""


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
                # 兼容聚合查询返回的空数组字符串
                records_list = json.loads(row[4]) if row[4] and row[4] != '[]' else []
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
    """按需检查歌库内容，每分钟至多一次；外部进程的歌词更新同样生效。"""

    def __init__(
        self,
        index: LyricsSearchIndex | None = None,
        *,
        check_interval: float = 60.0,
    ):
        self._index = index
        self._auto_refresh = index is None
        self._check_interval = check_interval
        self._checked_at: float | None = None
        self._snapshot: list[tuple[Any, ...]] | None = None
        self._lock = threading.Lock()

    def ensure_fresh(self) -> None:
        if not self._auto_refresh:
            return
        with self._lock:
            now = time.monotonic()
            if (
                self._index is not None
                and self._checked_at is not None
                and now - self._checked_at < self._check_interval
            ):
                return
            self._refresh_if_changed()

    def refresh(self) -> None:
        with self._lock:
            self._refresh_if_changed(force=True)
            self._auto_refresh = True

    def _refresh_if_changed(self, *, force: bool = False) -> None:
        with connect_sqlite() as conn:
            # 内容比较覆盖直接 SQL 修改和同秒更新，不依赖 updated_at。
            # 只检查歌曲表，弹幕等高频写入不会触发索引重建。
            conn.execute("BEGIN")
            snapshot = conn.execute(
                """
                SELECT id, title, title_trans, original_singer, lyrics_cleaned
                FROM song_info
                WHERE lyrics_cleaned IS NOT NULL AND lyrics_cleaned != ''
                ORDER BY id
                """
            ).fetchall()
            if not force and self._index is not None and snapshot == self._snapshot:
                self._checked_at = time.monotonic()
                return
            rows = conn.execute(
                """
                SELECT 
                    id, 
                    title, 
                    title_trans, 
                    original_singer, 
                    (SELECT json_group_array(record_date) FROM song_record WHERE song_id = song_info.id) AS records, 
                    (SELECT COUNT(1) FROM song_record WHERE song_id = song_info.id) AS count, 
                    lyrics_cleaned
                FROM song_info
                WHERE lyrics_cleaned IS NOT NULL AND lyrics_cleaned != ''
                ORDER BY id
                """
            ).fetchall()
        new_index = LyricsSearchIndex.from_rows(rows)
        self._index = new_index
        self._snapshot = snapshot
        self._checked_at = time.monotonic()

    def search(
        self,
        keyword: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        self.ensure_fresh()
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
