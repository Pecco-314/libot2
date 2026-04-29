from __future__ import annotations

import json
from typing import Any

from src.db.sqlite import execute_write, write_transaction, connect_sqlite

def init_song_list_db() -> None:
    with write_transaction() as conn:
        execute_write(
            conn,
            """
            CREATE TABLE IF NOT EXISTS song_list (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                title_trans TEXT,
                original_singer TEXT,
                records TEXT,
                notes TEXT,
                language TEXT,
                count INTEGER,
                clips TEXT,
                tags TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
        )

def batch_upsert_songs(songs: list[dict[str, Any]]) -> None:
    with write_transaction() as conn:
        for song in songs:
            execute_write(
                conn,
                """
                INSERT OR REPLACE INTO song_list (
                    id, title, title_trans, original_singer, records, 
                    notes, language, count, clips, tags, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    song.get("id"),
                    song.get("title", ""),
                    song.get("title_trans", ""),
                    song.get("original_singer", ""),
                    song.get("records", ""),
                    song.get("notes", ""),
                    song.get("language", ""),
                    song.get("count", ""),
                    song.get("clips", ""),
                    song.get("tags", ""),
                ),
            )


def search_songs_by_title(keyword: str, limit: int = 5) -> list[dict[str, Any]]:
    with connect_sqlite() as conn:
        # 假设 title 或 title_trans 匹配关键字
        rows = conn.execute(
            """
            SELECT id, title, title_trans, original_singer, records, count
            FROM song_list
            WHERE title LIKE ? OR title_trans LIKE ?
            LIMIT ?
            """,
            (f"%{keyword}%", f"%{keyword}%", limit)
        ).fetchall()
        
    results = []
    for row in rows:
        try:
            records_list = json.loads(row[4]) if row[4] else []
        except Exception:
            records_list = []
            
        results.append({
            "id": row[0],
            "title": row[1],
            "title_trans": row[2],
            "original_singer": row[3],
            "records": records_list,
            "count": row[5]
        })
    return results