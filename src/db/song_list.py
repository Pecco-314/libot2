from __future__ import annotations

import json
import datetime
import csv
import re
from pathlib import Path
from typing import Any

from src.db.sqlite import execute_write, write_transaction, connect_sqlite

def init_song_list_db() -> None:
    with write_transaction() as conn:
        execute_write(
            conn,
            """
            CREATE TABLE IF NOT EXISTS song_info (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                title_trans TEXT,
                original_singer TEXT,
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
        execute_write(
            conn,
            """
            CREATE TABLE IF NOT EXISTS song_record (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                song_id INTEGER NOT NULL,
                record_date TEXT NOT NULL,
                FOREIGN KEY(song_id) REFERENCES song_info(id) ON DELETE CASCADE
            )
            """,
        )


def batch_upsert_songs(songs: list[dict[str, Any]]) -> None:
    sql_info = """
    INSERT INTO song_info (
        id, title, title_trans, original_singer, 
        notes, language, count, clips, tags, lyrics, lyrics_cleaned, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    ON CONFLICT(id) DO UPDATE SET
        title = COALESCE(excluded.title, title),
        title_trans = COALESCE(excluded.title_trans, title_trans),
        original_singer = COALESCE(excluded.original_singer, original_singer),
        notes = COALESCE(excluded.notes, notes),
        language = COALESCE(excluded.language, language),
        count = COALESCE(excluded.count, count),
        clips = COALESCE(excluded.clips, clips),
        tags = COALESCE(excluded.tags, tags),
        lyrics = COALESCE(excluded.lyrics, lyrics),
        lyrics_cleaned = COALESCE(excluded.lyrics_cleaned, lyrics_cleaned),
        updated_at = CURRENT_TIMESTAMP
    """
    
    sql_del_records = "DELETE FROM song_record WHERE song_id = ?"
    sql_ins_record = "INSERT INTO song_record (song_id, record_date) VALUES (?, ?)"
    
    with write_transaction() as conn:
        for song in songs:
            execute_write(
                conn,
                sql_info,
                (
                    song.get("id"),
                    song.get("title"),
                    song.get("title_trans"),
                    song.get("original_singer"),
                    song.get("notes"),
                    song.get("language"),
                    song.get("count"),
                    song.get("clips"),
                    song.get("tags"),
                    song.get("lyrics"),
                    song.get("lyrics_cleaned"),
                ),
            )
            
            song_id = song.get("id")
            # 重建记录，先删后插
            execute_write(conn, sql_del_records, (song_id,))
            
            records_val = song.get("records")
            if isinstance(records_val, str):
                try:
                    records_list = json.loads(records_val)
                except json.JSONDecodeError:
                    records_list = []
            elif isinstance(records_val, list):
                records_list = records_val
            else:
                records_list = []
                
            for record_date in records_list:
                execute_write(conn, sql_ins_record, (song_id, record_date))


def search_songs_by_title(keyword: str, limit: int = 5) -> list[dict[str, Any]]:
    clean_keyword = keyword.strip()
    like_query = f"%{clean_keyword}%"

    with connect_sqlite() as conn:
        query = """
            SELECT id, title, title_trans, original_singer, 
                   (SELECT json_group_array(record_date) FROM song_record WHERE song_id = song_info.id) AS records, 
                   count
            FROM song_info
            WHERE title LIKE ? OR title_trans LIKE ?
            ORDER BY 
                (CASE 
                    WHEN title = ? THEN 0
                    WHEN title_trans = ? THEN 1
                    WHEN title LIKE ? THEN 2
                    ELSE 3 
                END),
                count DESC
            LIMIT ?
        """
        
        params = (
            like_query, like_query,
            clean_keyword, clean_keyword,
            f"{clean_keyword}%",
            limit
        )
        
        rows = conn.execute(query, params).fetchall()
        
    results = []
    for row in rows:
        try:
            records_list = json.loads(row[4]) if row[4] and row[4] != '[]' else []
        except (json.JSONDecodeError, TypeError):
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
            SELECT id, title, title_trans, original_singer, 
                   (SELECT json_group_array(record_date) FROM song_record WHERE song_id = song_info.id) AS records, 
                   count
            FROM song_info
            WHERE count >= ?
            ORDER BY RANDOM()
            LIMIT 1
            """,
            (lowest_count,)
        ).fetchone()
    if not row:
        return None
    try:
        records_list = json.loads(row[4]) if row[4] and row[4] != '[]' else []
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
    normalized = singer.strip().casefold()
    if not normalized:
        return []
    with connect_sqlite() as conn:
        rows = conn.execute(
            """
            SELECT id, title, original_singer, count
            FROM song_info
            WHERE original_singer IS NOT NULL AND original_singer != ''
            ORDER BY count DESC, id ASC
            """,
        ).fetchall()

    results: list[dict[str, Any]] = []
    for row in rows:
        original_singer = row[2] or ""
        parts = [part.strip().casefold() for part in original_singer.split("/")]
        if normalized in [part for part in parts if part]:
            results.append(
                {
                    "id": row[0],
                    "title": row[1],
                    "original_singer": row[2],
                    "count": row[3],
                }
            )
    return results


def list_songs_without_lyrics(limit: int | None = None) -> list[dict[str, Any]]:
    sql = """
        SELECT id, title, original_singer
        FROM song_info
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
            UPDATE song_info
            SET lyrics = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (lyrics, song_id),
        )


def list_songs_without_cleaned_lyrics(limit: int | None = None) -> list[dict[str, Any]]:
    sql = """
        SELECT id, title, original_singer, lyrics
        FROM song_info
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
            UPDATE song_info
            SET lyrics_cleaned = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (lyrics_cleaned, song_id),
        )


def get_all_songs() -> list[dict[str, Any]]:
    with connect_sqlite() as conn:
        rows = conn.execute(
            """
            SELECT i.id, i.title, i.title_trans, i.original_singer, 
                   (SELECT json_group_array(record_date) FROM song_record WHERE song_id = i.id), 
                   i.notes, i.language, i.count, i.clips, i.tags, i.lyrics, i.lyrics_cleaned
            FROM song_info i
            ORDER BY i.id ASC
            """
        ).fetchall()
    return [
        {
            "id": row[0],
            "title": row[1],
            "title_trans": row[2],
            "original_singer": row[3],
            "records": row[4] if row[4] != '[]' else "[]",
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
            f"DELETE FROM song_record WHERE song_id NOT IN ({placeholders})",
            tuple(valid_ids)
        )
        execute_write(
            conn,
            f"DELETE FROM song_info WHERE id NOT IN ({placeholders})",
            tuple(valid_ids)
        )


def get_songs_of_date(date: datetime.date) -> list[dict[str, Any]]:
    date_str = date.strftime("%Y-%m-%d")
    with connect_sqlite() as conn:
        rows = conn.execute(
            """
            SELECT i.id, i.title, i.title_trans, i.original_singer, 
                   (SELECT json_group_array(record_date) FROM song_record WHERE song_id = i.id), 
                   i.notes, i.language, i.count, i.clips, i.tags, i.lyrics, i.lyrics_cleaned
            FROM song_info i
            JOIN song_record r ON i.id = r.song_id
            WHERE r.record_date = ?
            ORDER BY i.id ASC
            """,
            (date_str,)
        ).fetchall()
    return [
        {
            "id": row[0],
            "title": row[1],
            "title_trans": row[2],
            "original_singer": row[3],
            "records": row[4] if row[4] != '[]' else "[]",
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


def export_songs_to_csv(path: str | Path) -> None:
    """Export all songs to a CSV file with the required format.

    Header: 序号,歌名,歌名翻译,原唱,日期,备注,语言,次数,歌切,标签
    - list fields (`records`, `clips`) will be joined using Chinese comma '，'.
    - output is written with UTF-8-SIG encoding to improve Excel compatibility.
    """
    songs = get_all_songs()
    out_path = Path(path)
    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        header = [
            "序号",
            "歌名",
            "歌名翻译",
            "原唱",
            "日期",
            "备注",
            "语言",
            "次数",
            "歌切",
            "标签",
        ]
        writer.writerow(header)

        for idx, s in enumerate(songs, start=1):
            # records: stored as JSON array of YYYY-MM-DD strings in DB
            records_val = s.get("records") or ""
            if isinstance(records_val, list):
                records_list = [str(x) for x in records_val]
            else:
                records_list = json.loads(records_val) if records_val else []
                records_list = [str(x) for x in records_list]
            # convert YYYY-MM-DD -> YYYY/MM/DD
            records_list = [d.replace("-", "/") for d in records_list]
            date_field = "，".join(records_list) if records_list else ""

            # clips -> join with Chinese comma
            # clips: DB stores JSON array, often with a single string joined by Chinese commas
            clips_val = s.get("clips") or ""
            if isinstance(clips_val, list):
                clips_parsed = [str(x) for x in clips_val]
            else:
                clips_parsed = json.loads(clips_val) if clips_val else []
            # if parsed list contains a single string with Chinese commas, split it
            if len(clips_parsed) == 1 and "，" in clips_parsed[0]:
                clips_list = [p.strip() for p in clips_parsed[0].split("，") if p.strip()]
            else:
                clips_list = [str(x).strip() for x in clips_parsed if str(x).strip()]
            clips_field = "，".join(clips_list) if clips_list else ""

            # tags: try JSON list, else keep string
            tags_val = s.get("tags") or ""
            if isinstance(tags_val, list):
                tags_field = ",".join(str(x) for x in tags_val)
            else:
                # tags may be plain string or JSON array; handle deterministically
                if isinstance(tags_val, str) and tags_val.strip().startswith("["):
                    tags_parsed = json.loads(tags_val)
                    tags_field = ",".join(str(x) for x in tags_parsed)
                else:
                    tags_field = str(tags_val).strip()

            row = [
                idx,
                s.get("title") or "",
                s.get("title_trans") or "",
                s.get("original_singer") or "",
                date_field,
                s.get("notes") or "",
                s.get("language") or "",
                s.get("count") or 0,
                clips_field,
                tags_field,
            ]
            writer.writerow(row)


def add_new_song(title: str, singer: str = "", language: str = "", title_trans: str = "") -> int:
    """
    新建一首歌曲，返回它的新 ID。
    """
    with write_transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO song_info (title, original_singer, language, title_trans, count, clips, tags, lyrics, lyrics_cleaned) 
            VALUES (?, ?, ?, ?, 0, '[]', '', '', '')
            """,
            (title, singer, language, title_trans)
        )
        return cursor.lastrowid


def add_song_record(song_id: int, record_date: str) -> None:
    """
    为指定歌曲新增一条演唱记录，并同步更新计数字段
    """
    with write_transaction() as conn:
        conn.execute(
            "INSERT INTO song_record (song_id, record_date) VALUES (?, ?)", 
            (song_id, record_date)
        )
        conn.execute(
            """
            UPDATE song_info 
            SET count = count + 1, updated_at = CURRENT_TIMESTAMP 
            WHERE id = ?
            """, 
            (song_id,)
        )


if __name__ == "__main__":
    import sys

    def _print_usage_and_exit():
        print("usage: python -m src.db.song_list [init|export <output.csv>]")
        sys.exit(1)

    if len(sys.argv) == 1:
        init_song_list_db()
    else:
        cmd = sys.argv[1]
        if cmd == "init":
            init_song_list_db()
        elif cmd == "export":
            out = sys.argv[2] if len(sys.argv) > 2 else "song_list.csv"
            export_songs_to_csv(out)
            print(f"Exported songs to {out}")
        else:
            _print_usage_and_exit()