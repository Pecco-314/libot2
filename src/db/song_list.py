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
                lyrics_cleaned TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
        )

def batch_upsert_songs(songs: list[dict[str, Any]]) -> None:
    sql = """
    INSERT INTO song_list (
        id, title, title_trans, original_singer, records, 
        notes, language, count, clips, tags, lyrics, lyrics_cleaned, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
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
        lyrics_cleaned = COALESCE(excluded.lyrics_cleaned, lyrics_cleaned),
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
                    song.get("lyrics_cleaned"),
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


def list_songs_by_singer(singer: str) -> list[dict[str, Any]]:
    with connect_sqlite() as conn:
        rows = conn.execute(
            """
            SELECT id, title, original_singer, count
            FROM song_list
            WHERE original_singer LIKE ?
            ORDER BY count DESC, id ASC
            """,
            (f"%{singer}%",),
        ).fetchall()
    return [
        {
            "id": row[0],
            "title": row[1],
            "original_singer": row[2],
            "count": row[3],
        }
        for row in rows
    ]


def list_songs_without_lyrics(limit: int | None = None) -> list[dict[str, Any]]:
    sql = """
        SELECT id, title, original_singer
        FROM song_list
        WHERE lyrics IS NULL OR lyrics = ''
    """
    params: tuple[Any, ...] = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)
    with connect_sqlite() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        {
            "id": row[0],
            "title": row[1],
            "original_singer": row[2],
        }
        for row in rows
    ]


def update_song_lyrics(song_id: int, lyrics: str) -> None:
    with write_transaction() as conn:
        execute_write(
            conn,
            """
            UPDATE song_list
            SET lyrics = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (lyrics, song_id),
        )


def list_songs_without_cleaned_lyrics(limit: int | None = None) -> list[dict[str, Any]]:
    sql = """
        SELECT id, title, original_singer, lyrics
        FROM song_list
        WHERE (lyrics_cleaned IS NULL OR lyrics_cleaned = '')
          AND lyrics IS NOT NULL AND lyrics != ''
    """
    params: tuple[Any, ...] = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)
    with connect_sqlite() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        {
            "id": row[0],
            "title": row[1],
            "original_singer": row[2],
            "lyrics": row[3],
        }
        for row in rows
    ]


def update_song_cleaned_lyrics(song_id: int, lyrics_cleaned: str) -> None:
    with write_transaction() as conn:
        execute_write(
            conn,
            """
            UPDATE song_list
            SET lyrics_cleaned = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (lyrics_cleaned, song_id),
        )


def get_all_songs() -> list[dict[str, Any]]:
    with connect_sqlite() as conn:
        rows = conn.execute(
            """
            SELECT id, title, title_trans, original_singer, records, 
                   notes, language, count, clips, tags, lyrics, lyrics_cleaned
            FROM song_list
            ORDER BY id ASC
            """
        ).fetchall()
    return [
        {
            "id": row[0],
            "title": row[1],
            "title_trans": row[2],
            "original_singer": row[3],
            "records": row[4],
            "notes": row[5],
            "language": row[6],
            "count": row[7],
            "clips": row[8],
            "tags": row[9],
            "lyrics": row[10],
            "lyrics_cleaned": row[11],
        }
        for row in rows
    ]


def delete_songs_not_in(valid_ids: list[int]) -> None:
    if not valid_ids:
        return
    placeholders = ",".join(["?"] * len(valid_ids))
    with write_transaction() as conn:
        execute_write(
            conn,
            f"DELETE FROM song_list WHERE id NOT IN ({placeholders})",
            tuple(valid_ids)
        )


if __name__ == "__main__":
    init_song_list_db()