from __future__ import annotations

import json
import datetime
import csv
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from src.db.sqlite import (
    DEFAULT_DB_PATH,
    execute_write,
    write_transaction,
    connect_sqlite,
)

def init_song_list_db(db_path: Path = DEFAULT_DB_PATH) -> None:
    with write_transaction(db_path) as conn:
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
        execute_write(
            conn,
            """
            CREATE TABLE IF NOT EXISTS song_sync_source (
                source TEXT NOT NULL,
                external_id TEXT NOT NULL,
                song_id INTEGER NOT NULL,
                source_payload TEXT NOT NULL,
                synced_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (source, external_id),
                FOREIGN KEY(song_id) REFERENCES song_info(id) ON DELETE CASCADE
            )
            """,
        )
        execute_write(
            conn,
            """
            CREATE INDEX IF NOT EXISTS idx_song_sync_source_song
            ON song_sync_source(song_id, source)
            """,
        )


def _normalize_song_identity(value: Any) -> str:
    return re.sub(r"[\s\u3000]+", "", str(value or "")).casefold()


def _json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        values = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            values = [value]
        else:
            values = parsed if isinstance(parsed, list) else [value]
    else:
        values = []
    return [str(item).strip() for item in values if str(item).strip()]


def _tag_list(value: Any) -> list[str]:
    values = _json_list(value)
    result: list[str] = []
    for item in values:
        result.extend(
            part.strip()
            for part in re.split(r"[,，]", item)
            if part.strip()
        )
    return list(dict.fromkeys(result))


def _source_mapping_is_stable(
    previous: dict[str, Any],
    current: dict[str, Any],
) -> bool:
    if _normalize_song_identity(previous.get("title")) == _normalize_song_identity(
        current.get("title")
    ):
        return True
    if _normalize_song_identity(previous.get("original_singer")) != (
        _normalize_song_identity(current.get("original_singer"))
    ):
        return False
    previous_records = Counter(_json_list(previous.get("records")))
    current_records = Counter(_json_list(current.get("records")))
    if not previous_records or not current_records:
        return False
    overlap = sum((previous_records & current_records).values())
    return overlap / min(sum(previous_records.values()), sum(current_records.values())) >= 0.75


def merge_songs_from_source(
    source: str,
    songs: list[dict[str, Any]],
    *,
    db_path: Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Merge an external song list without treating it as a database mirror.

    Existing non-empty local metadata and lyrics win on the first import. On
    later imports a remote scalar edit is accepted only when the local value
    still equals the previous remote snapshot. Records are merged as a
    multiset (maximum occurrences per date), while clips and tags are unions.
    Local songs, records and metadata are never deleted by this operation.
    """

    source = source.strip()
    if not source:
        raise ValueError("song source must not be empty")
    external_ids = [str(song.get("external_id") or "").strip() for song in songs]
    if any(not value for value in external_ids):
        raise ValueError("every source song must have an external_id")
    if len(external_ids) != len(set(external_ids)):
        raise ValueError("source song external_id values must be unique")

    init_song_list_db(db_path)
    summary: dict[str, Any] = {
        "source": source,
        "source_rows": len(songs),
        "matched_songs": 0,
        "inserted_songs": 0,
        "metadata_updates": 0,
        "record_rows_added": 0,
        "clips_added": 0,
        "tags_added": 0,
        "conflicts": [],
        "ambiguous_rows": [],
    }
    with write_transaction(db_path) as conn:
        info_columns = (
            "id",
            "title",
            "title_trans",
            "original_singer",
            "notes",
            "language",
            "clips",
            "tags",
        )
        local: dict[int, dict[str, Any]] = {
            int(row[0]): dict(zip(info_columns, row))
            for row in conn.execute(
                """
                SELECT id, title, title_trans, original_singer,
                       notes, language, clips, tags
                FROM song_info
                ORDER BY id
                """
            )
        }
        record_counts: dict[int, Counter[str]] = defaultdict(Counter)
        for song_id, record_date in conn.execute(
            "SELECT song_id, record_date FROM song_record"
        ):
            record_counts[int(song_id)][str(record_date)] += 1

        source_mappings: dict[str, tuple[int, dict[str, Any]]] = {}
        for external_id, song_id, raw_payload in conn.execute(
            """
            SELECT external_id, song_id, source_payload
            FROM song_sync_source
            WHERE source = ?
            """,
            (source,),
        ):
            try:
                payload = json.loads(str(raw_payload))
            except (json.JSONDecodeError, TypeError):
                payload = {}
            source_mappings[str(external_id)] = (int(song_id), payload)

        claimed_song_ids: set[int] = set()
        scalar_fields = (
            "title",
            "title_trans",
            "original_singer",
            "notes",
            "language",
        )

        for raw_song in songs:
            payload = {
                "title": str(raw_song.get("title") or "").strip(),
                "title_trans": str(raw_song.get("title_trans") or "").strip(),
                "original_singer": str(
                    raw_song.get("original_singer") or ""
                ).strip(),
                "notes": str(raw_song.get("notes") or "").strip(),
                "language": str(raw_song.get("language") or "").strip(),
                "records": _json_list(raw_song.get("records")),
                "clips": _json_list(raw_song.get("clips")),
                "tags": _tag_list(raw_song.get("tags")),
            }
            external_id = str(raw_song["external_id"]).strip()
            if not payload["title"]:
                summary["ambiguous_rows"].append(
                    {"external_id": external_id, "reason": "empty_title"}
                )
                continue

            identity = _normalize_song_identity(payload["title"])
            exact_matches = {
                song_id
                for song_id, value in local.items()
                if identity
                in {
                    _normalize_song_identity(value.get("title")),
                    _normalize_song_identity(value.get("title_trans")),
                }
            }
            exact_candidates = exact_matches - claimed_song_ids
            song_id: int | None = None
            previous_payload: dict[str, Any] | None = None
            ambiguous_candidates: set[int] = set()
            if len(exact_candidates) == 1:
                song_id = next(iter(exact_candidates))
            elif len(exact_candidates) > 1 or exact_matches & claimed_song_ids:
                ambiguous_candidates.update(exact_matches)

            mapping = source_mappings.get(external_id)
            if song_id is None and mapping is not None:
                mapped_id, mapped_payload = mapping
                if (
                    mapped_id in local
                    and mapped_id not in claimed_song_ids
                    and _source_mapping_is_stable(mapped_payload, payload)
                ):
                    song_id = mapped_id
                    previous_payload = mapped_payload

            if song_id is None and payload["records"]:
                remote_records = Counter(payload["records"])
                fingerprint_candidates: list[int] = []
                for candidate_id, value in local.items():
                    if candidate_id in claimed_song_ids:
                        continue
                    if _normalize_song_identity(value.get("original_singer")) != (
                        _normalize_song_identity(payload["original_singer"])
                    ):
                        continue
                    overlap = sum(
                        (record_counts[candidate_id] & remote_records).values()
                    )
                    if overlap / sum(remote_records.values()) >= 0.75:
                        fingerprint_candidates.append(candidate_id)
                if len(fingerprint_candidates) == 1:
                    song_id = fingerprint_candidates[0]
                elif len(fingerprint_candidates) > 1:
                    ambiguous_candidates.update(fingerprint_candidates)

            if song_id is None and ambiguous_candidates:
                summary["ambiguous_rows"].append(
                    {
                        "external_id": external_id,
                        "title": payload["title"],
                        "reason": "multiple_or_already_claimed_local_matches",
                        "candidate_song_ids": sorted(ambiguous_candidates),
                    }
                )
                continue

            if song_id is None:
                cursor = conn.execute(
                    """
                    INSERT INTO song_info (
                        title, title_trans, original_singer, notes, language,
                        clips, tags, lyrics, lyrics_cleaned
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, '', '')
                    """,
                    (
                        payload["title"],
                        payload["title_trans"],
                        payload["original_singer"],
                        payload["notes"],
                        payload["language"],
                        json.dumps(payload["clips"], ensure_ascii=False),
                        ",".join(payload["tags"]),
                    ),
                )
                song_id = int(cursor.lastrowid)
                local[song_id] = {
                    "id": song_id,
                    **{field: payload[field] for field in scalar_fields},
                    "clips": json.dumps(payload["clips"], ensure_ascii=False),
                    "tags": ",".join(payload["tags"]),
                }
                summary["inserted_songs"] += 1
            else:
                summary["matched_songs"] += 1
                if previous_payload is None and mapping is not None and mapping[0] == song_id:
                    previous_payload = mapping[1]

                updates: dict[str, str] = {}
                for field in scalar_fields:
                    local_value = str(local[song_id].get(field) or "").strip()
                    remote_value = str(payload.get(field) or "").strip()
                    previous_value = (
                        str(previous_payload.get(field) or "").strip()
                        if previous_payload is not None
                        else None
                    )
                    if not remote_value or local_value == remote_value:
                        continue
                    if not local_value or (
                        previous_value is not None and local_value == previous_value
                    ):
                        updates[field] = remote_value
                    elif previous_value is not None and remote_value != previous_value:
                        summary["conflicts"].append(
                            {
                                "external_id": external_id,
                                "song_id": song_id,
                                "field": field,
                                "local": local_value,
                                "previous_remote": previous_value,
                                "current_remote": remote_value,
                            }
                        )

                local_clips = _json_list(local[song_id].get("clips"))
                merged_clips = list(dict.fromkeys([*local_clips, *payload["clips"]]))
                if merged_clips != local_clips:
                    updates["clips"] = json.dumps(
                        merged_clips,
                        ensure_ascii=False,
                    )
                    summary["clips_added"] += len(merged_clips) - len(local_clips)

                local_tags = _tag_list(local[song_id].get("tags"))
                merged_tags = list(dict.fromkeys([*local_tags, *payload["tags"]]))
                if merged_tags != local_tags:
                    updates["tags"] = ",".join(merged_tags)
                    summary["tags_added"] += len(merged_tags) - len(local_tags)

                if updates:
                    assignments = ", ".join(f"{field} = ?" for field in updates)
                    conn.execute(
                        f"UPDATE song_info SET {assignments}, "
                        "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (*updates.values(), song_id),
                    )
                    local[song_id].update(updates)
                    summary["metadata_updates"] += 1

            remote_record_counts = Counter(payload["records"])
            for record_date, remote_count in remote_record_counts.items():
                missing_count = max(
                    0,
                    remote_count - record_counts[song_id][record_date],
                )
                for _index in range(missing_count):
                    conn.execute(
                        "INSERT INTO song_record (song_id, record_date) VALUES (?, ?)",
                        (song_id, record_date),
                    )
                if missing_count:
                    record_counts[song_id][record_date] += missing_count
                    summary["record_rows_added"] += missing_count

            conn.execute(
                """
                INSERT INTO song_sync_source (
                    source, external_id, song_id, source_payload, synced_at
                ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(source, external_id) DO UPDATE SET
                    song_id = excluded.song_id,
                    source_payload = excluded.source_payload,
                    synced_at = CURRENT_TIMESTAMP
                """,
                (
                    source,
                    external_id,
                    song_id,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                ),
            )
            claimed_song_ids.add(song_id)

    summary["conflict_count"] = len(summary["conflicts"])
    summary["ambiguous_count"] = len(summary["ambiguous_rows"])
    return summary


def batch_upsert_songs(songs: list[dict[str, Any]]) -> None:
    sql_info = """
    INSERT INTO song_info (
        id, title, title_trans, original_singer, 
        notes, language, clips, tags, lyrics, lyrics_cleaned, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    ON CONFLICT(id) DO UPDATE SET
        title = COALESCE(excluded.title, title),
        title_trans = COALESCE(excluded.title_trans, title_trans),
        original_singer = COALESCE(excluded.original_singer, original_singer),
        notes = COALESCE(excluded.notes, notes),
        language = COALESCE(excluded.language, language),
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


def search_songs_by_title(keyword: str, limit: int = 5, singer: str | None = None) -> list[dict[str, Any]]:
    clean_keyword = keyword.strip()
    like_query = f"%{clean_keyword}%"

    with connect_sqlite() as conn:
        if singer:
            clean_singer = singer.strip()
            singer_query = f"%{clean_singer}%"
            query = """
                SELECT id, title, title_trans, original_singer, 
                       (SELECT json_group_array(record_date) FROM song_record WHERE song_id = song_info.id) AS records, 
                       (SELECT COUNT(1) FROM song_record WHERE song_id = song_info.id) AS count
                FROM song_info
                WHERE (title LIKE ? OR title_trans LIKE ?)
                  AND original_singer LIKE ?
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
                singer_query,
                clean_keyword, clean_keyword,
                f"{clean_keyword}%",
                limit
            )
        else:
            query = """
                SELECT id, title, title_trans, original_singer, 
                       (SELECT json_group_array(record_date) FROM song_record WHERE song_id = song_info.id) AS records, 
                       (SELECT COUNT(1) FROM song_record WHERE song_id = song_info.id) AS count
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
                   (SELECT COUNT(1) FROM song_record WHERE song_id = song_info.id) AS count
            FROM song_info
            WHERE (SELECT COUNT(1) FROM song_record WHERE song_id = song_info.id) >= ?
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
            SELECT id, title, original_singer, 
                   (SELECT COUNT(1) FROM song_record WHERE song_id = song_info.id) AS count
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
                   i.notes, i.language, 
                   (SELECT COUNT(1) FROM song_record WHERE song_id = i.id) AS count, 
                   i.clips, i.tags, i.lyrics, i.lyrics_cleaned
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
                   i.notes, i.language, 
                   (SELECT COUNT(1) FROM song_record WHERE song_id = i.id) AS count, 
                   i.clips, i.tags, i.lyrics, i.lyrics_cleaned
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
            INSERT INTO song_info (title, original_singer, language, title_trans, clips, tags, lyrics, lyrics_cleaned) 
            VALUES (?, ?, ?, ?, '[]', '', '', '')
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
