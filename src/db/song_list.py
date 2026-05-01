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
                lyrics TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
        )

def batch_upsert_songs(songs: list[dict[str, Any]]) -> None:
    sql = """
    INSERT INTO song_list (
        id, title, title_trans, original_singer, records, 
        notes, language, count, clips, tags, lyrics, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    ON CONFLICT(id) DO UPDATE SET
        title = COALESCE(excluded.title, title),
        title_trans = COALESCE(excluded.title_trans, title_trans),
        original_singer = COALESCE(excluded.original_singer, original_singer),
        records = COALESCE(excluded.records, records),
        notes = COALESCE(excluded.notes, notes),
        language = COALESCE(excluded.language, language),
        count = COALESCE(excluded.count, count),
        clips = COALESCE(excluded.clips, clips),
        tags = COALESCE(excluded.tags, tags),
        lyrics = COALESCE(excluded.lyrics, lyrics),
        updated_at = CURRENT_TIMESTAMP
    """
    
    with write_transaction() as conn:
        for song in songs:
            execute_write(
                conn,
                sql,
                (
                    song.get("id"),
                    song.get("title"),
                    song.get("title_trans"),
                    song.get("original_singer"),
                    song.get("records"),
                    song.get("notes"),
                    song.get("language"),
                    song.get("count"),
                    song.get("clips"),
                    song.get("tags"),
                    song.get("lyrics"),
                ),
            )

def search_songs_by_title(keyword: str, limit: int = 5) -> list[dict[str, Any]]:
    with connect_sqlite() as conn:
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

def random_song(lowest_count: int = 3) -> dict[str, Any] | None:
    with connect_sqlite() as conn:
        row = conn.execute(
            """
            SELECT id, title, title_trans, original_singer, records, count
            FROM song_list
            WHERE count >= ?
            ORDER BY RANDOM()
            LIMIT 1
            """,
            (lowest_count,)
        ).fetchone()
    if not row:
        return None
    try:
        records_list = json.loads(row[4]) if row[4] else []
    except Exception:
        records_list = []
    return {
        "id": row[0],
        "title": row[1],
        "title_trans": row[2],
        "original_singer": row[3],
        "records": records_list,
        "count": row[5]
    }