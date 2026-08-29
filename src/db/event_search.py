from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable

from src.db.sqlite import DEFAULT_DB_PATH


DEFAULT_SEARCH_DB_PATH = DEFAULT_DB_PATH.with_name("libot_search.db")
SEARCH_SCHEMA_VERSION = 2
ROWID_MULTIPLIER = 1_000_000_000
DEFAULT_BATCH_SIZE = 5_000


class EventSearchUnavailable(RuntimeError):
    """Raised when the optional search index cannot safely answer a query."""


def _connect_search(
    search_db_path: Path = DEFAULT_SEARCH_DB_PATH,
) -> sqlite3.Connection:
    search_db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(search_db_path, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA cache_size=-32768")
    return conn


def _connect_source(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=5)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA query_only=ON")
    return conn


def init_event_search_db(
    search_db_path: Path = DEFAULT_SEARCH_DB_PATH,
) -> None:
    with _connect_search(search_db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS event_search_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            ) WITHOUT ROWID;

            CREATE VIRTUAL TABLE IF NOT EXISTS event_fts USING fts5(
                tokens,
                content='',
                detail=none,
                columnsize=0
            );
            """
        )
        conn.execute(
            """
            INSERT INTO event_search_meta(key, value)
            VALUES ('schema_version', ?)
            ON CONFLICT(key) DO NOTHING
            """,
            (str(SEARCH_SCHEMA_VERSION),),
        )
        conn.execute(
            """
            INSERT INTO event_search_meta(key, value)
            VALUES ('last_indexed_event_id', '0')
            ON CONFLICT(key) DO NOTHING
            """
        )
        conn.execute(
            """
            INSERT INTO event_search_meta(key, value)
            VALUES ('indexed_document_count', '0')
            ON CONFLICT(key) DO NOTHING
            """
        )


def reset_event_search_db(
    search_db_path: Path = DEFAULT_SEARCH_DB_PATH,
) -> None:
    """Discard the rebuildable search index without touching the main DB."""
    with _connect_search(search_db_path) as conn:
        conn.executescript(
            """
            DROP TABLE IF EXISTS event_fts;
            DROP TABLE IF EXISTS event_search_meta;
            """
        )
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    init_event_search_db(search_db_path)


def _hex_token(prefix: str, value: str) -> str:
    return f"{prefix}{value.encode('utf-8').hex()}"


def _content_tokens(content: str) -> list[str]:
    """Return collision-free character and adjacent-character tokens."""
    tokens = {_hex_token("u", character) for character in content}
    tokens.update(
        _hex_token("b", content[index : index + 2])
        for index in range(len(content) - 1)
    )
    return sorted(tokens)


def _document_tokens(room_id: int, content: str) -> str:
    return " ".join([f"r{int(room_id)}", *_content_tokens(content)])


def _query_tokens(room_id: int, keyword: str) -> list[str]:
    if len(keyword) == 1:
        content_tokens = [_hex_token("u", keyword)]
    else:
        content_tokens = sorted(
            {
                _hex_token("b", keyword[index : index + 2])
                for index in range(len(keyword) - 1)
            }
        )
    return [f"r{int(room_id)}", *content_tokens]


def _fts_query(room_id: int, keyword: str) -> str:
    # Every token is ASCII alphanumeric, but quoting also prevents FTS syntax
    # from acquiring meaning if the token format changes later.
    return " AND ".join(f'"{token}"' for token in _query_tokens(room_id, keyword))


def _encode_rowid(timestamp: int, event_id: int) -> int:
    if event_id < 0 or event_id >= ROWID_MULTIPLIER:
        raise ValueError(f"event id out of search rowid range: {event_id}")
    timestamp = max(0, int(timestamp))
    rowid = timestamp * ROWID_MULTIPLIER + event_id
    if rowid > 9_223_372_036_854_775_807:
        raise ValueError(f"event timestamp out of search rowid range: {timestamp}")
    return rowid


def _decode_event_id(search_rowid: int) -> int:
    return int(search_rowid) % ROWID_MULTIPLIER


def _read_meta(conn: sqlite3.Connection, key: str, default: int = 0) -> int:
    row = conn.execute(
        "SELECT value FROM event_search_meta WHERE key = ?",
        (key,),
    ).fetchone()
    if row is None:
        return default
    return int(row[0])


def _write_meta(conn: sqlite3.Connection, key: str, value: int) -> None:
    conn.execute(
        """
        INSERT INTO event_search_meta(key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, str(int(value))),
    )


def get_event_search_status(
    db_path: Path = DEFAULT_DB_PATH,
    search_db_path: Path = DEFAULT_SEARCH_DB_PATH,
) -> dict[str, int | bool | str]:
    if not search_db_path.exists():
        return {
            "available": False,
            "reason": "search database does not exist",
            "last_indexed_event_id": 0,
            "source_max_event_id": 0,
            "pending_events": 0,
            "indexed_document_count": 0,
        }
    try:
        with _connect_search(search_db_path) as search_conn:
            schema_version = _read_meta(search_conn, "schema_version", -1)
            last_indexed = _read_meta(
                search_conn,
                "last_indexed_event_id",
            )
            indexed_documents = _read_meta(
                search_conn,
                "indexed_document_count",
            )
        with _connect_source(db_path) as source_conn:
            row = source_conn.execute("SELECT COALESCE(MAX(id), 0) FROM event").fetchone()
            source_max = int(row[0] if row else 0)
    except (OSError, sqlite3.Error, ValueError) as exc:
        return {
            "available": False,
            "reason": str(exc),
            "last_indexed_event_id": 0,
            "source_max_event_id": 0,
            "pending_events": 0,
            "indexed_document_count": 0,
        }

    available = schema_version == SEARCH_SCHEMA_VERSION and last_indexed <= source_max
    reason = "" if available else "search schema or source database changed"
    return {
        "available": available,
        "reason": reason,
        "schema_version": schema_version,
        "last_indexed_event_id": last_indexed,
        "source_max_event_id": source_max,
        "pending_events": max(0, source_max - last_indexed),
        "indexed_document_count": indexed_documents,
    }


def index_pending_events(
    db_path: Path = DEFAULT_DB_PATH,
    search_db_path: Path = DEFAULT_SEARCH_DB_PATH,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_batches: int | None = 1,
) -> dict[str, int]:
    """Append new event rows to the independent search index in batches."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    init_event_search_db(search_db_path)

    scanned = 0
    indexed = 0
    batches = 0
    with _connect_source(db_path) as source_conn, _connect_search(
        search_db_path
    ) as search_conn:
        schema_version = _read_meta(search_conn, "schema_version", -1)
        if schema_version != SEARCH_SCHEMA_VERSION:
            raise EventSearchUnavailable("event search schema version changed")
        last_indexed = _read_meta(search_conn, "last_indexed_event_id")
        source_max_row = source_conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM event"
        ).fetchone()
        source_max = int(source_max_row[0] if source_max_row else 0)
        if last_indexed > source_max:
            raise EventSearchUnavailable(
                "source event ids moved backwards; rebuild the search index"
            )

        while max_batches is None or batches < max_batches:
            rows = source_conn.execute(
                """
                SELECT id, room_id, content, timestamp, uid
                FROM event
                WHERE id > ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (last_indexed, batch_size),
            ).fetchall()
            if not rows:
                break

            documents: list[tuple[int, str]] = []
            for event_id, room_id, content, timestamp, uid in rows:
                if uid is None or content is None or not str(content):
                    continue
                documents.append(
                    (
                        _encode_rowid(int(timestamp or 0), int(event_id)),
                        _document_tokens(int(room_id), str(content)),
                    )
                )

            search_conn.execute("BEGIN IMMEDIATE")
            try:
                current_last_indexed = _read_meta(
                    search_conn,
                    "last_indexed_event_id",
                )
                if current_last_indexed != last_indexed:
                    search_conn.rollback()
                    last_indexed = current_last_indexed
                    continue
                if documents:
                    search_conn.executemany(
                        "INSERT INTO event_fts(rowid, tokens) VALUES (?, ?)",
                        documents,
                    )
                last_indexed = int(rows[-1][0])
                indexed_documents = _read_meta(
                    search_conn,
                    "indexed_document_count",
                )
                _write_meta(
                    search_conn,
                    "indexed_document_count",
                    indexed_documents + len(documents),
                )
                _write_meta(
                    search_conn,
                    "last_indexed_event_id",
                    last_indexed,
                )
                search_conn.commit()
            except Exception:
                search_conn.rollback()
                raise

            batches += 1
            scanned += len(rows)
            indexed += len(documents)
            if len(rows) < batch_size:
                break

    return {
        "batches": batches,
        "scanned_events": scanned,
        "indexed_documents": indexed,
        "last_indexed_event_id": last_indexed,
        "source_max_event_id": source_max,
        "pending_events": max(0, source_max - last_indexed),
    }


def optimize_event_search_index(
    search_db_path: Path = DEFAULT_SEARCH_DB_PATH,
) -> None:
    with _connect_search(search_db_path) as conn:
        conn.execute("INSERT INTO event_fts(event_fts) VALUES ('optimize')")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        # FTS segment merges leave free pages behind. This command is used by
        # the explicit maintenance script, never by the frequent updater.
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _chunks(values: list[int], size: int = 900) -> Iterable[list[int]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _load_and_verify_candidates(
    source_conn: sqlite3.Connection,
    event_ids: list[int],
    *,
    room_id: int,
    keyword: str,
    excluded_range: tuple[int, int] | None,
    end_ts: int | None,
) -> dict[int, tuple[Any, ...]]:
    rows_by_id: dict[int, tuple[Any, ...]] = {}
    for chunk in _chunks(event_ids):
        placeholders = ",".join("?" for _ in chunk)
        rows = source_conn.execute(
            f"""
            SELECT id, room_id, uid, uname, cmd, content, gift_name, gift_num,
                   total_coin, title, timestamp
            FROM event
            WHERE id IN ({placeholders})
            """,
            chunk,
        ).fetchall()
        for row in rows:
            event_id = int(row[0])
            row_room_id = int(row[1])
            uid = row[2]
            content = row[5]
            timestamp = int(row[10] or 0)
            if uid is None or content is None or keyword not in str(content):
                continue
            if event_id < 0 or row_room_id != int(room_id):
                continue
            # Recheck every predicate against the source of truth. This also
            # makes stale index rows harmless after a main-database cleanup.
            if end_ts is not None and timestamp > end_ts:
                continue
            if (
                excluded_range is not None
                and excluded_range[0] <= timestamp < excluded_range[1]
            ):
                continue
            rows_by_id[event_id] = row
    return rows_by_id


def _row_to_event(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": int(row[0]),
        "uid": int(row[2]) if row[2] is not None else 0,
        "uname": str(row[3]) if row[3] is not None else "",
        "cmd": str(row[4]),
        "content": row[5],
        "gift_name": row[6],
        "gift_num": row[7],
        "total_coin": row[8],
        "title": row[9],
        "timestamp": int(row[10]) if row[10] is not None else 0,
    }


def _search_indexed_rows(
    source_conn: sqlite3.Connection,
    search_conn: sqlite3.Connection,
    *,
    room_id: int,
    keyword: str,
    limit: int,
    excluded_range: tuple[int, int] | None,
    end_ts: int | None,
) -> list[dict[str, Any]]:
    match_query = _fts_query(room_id, keyword)
    found: list[dict[str, Any]] = []
    candidate_batch_size = max(256, min(2_000, limit * 2))

    # FTS5 may otherwise inspect a common token's complete posting list just
    # to sort its newest rows. Search disjoint, exponentially widening time
    # windows instead: popular terms stop in the first week, while rare or
    # absent terms still cover all history in only a handful of probes.
    minimum_row = source_conn.execute(
        "SELECT MIN(timestamp) FROM event WHERE room_id = ?",
        (room_id,),
    ).fetchone()
    minimum_timestamp = max(0, int(minimum_row[0] or 0))
    if end_ts is not None:
        upper_timestamp = int(end_ts) + 1
    else:
        maximum_row = source_conn.execute(
            "SELECT MAX(timestamp) FROM event WHERE room_id = ?",
            (room_id,),
        ).fetchone()
        upper_timestamp = max(0, int(maximum_row[0] or 0)) + 1
    if upper_timestamp <= minimum_timestamp:
        return []
    window_seconds = 7 * 86_400
    while len(found) < limit and upper_timestamp > minimum_timestamp:
        lower_timestamp = max(
            minimum_timestamp,
            upper_timestamp - window_seconds,
        )
        lower_rowid = lower_timestamp * ROWID_MULTIPLIER
        cursor_rowid = upper_timestamp * ROWID_MULTIPLIER

        while len(found) < limit:
            conditions = [
                "event_fts MATCH ?",
                "rowid >= ?",
                "rowid < ?",
            ]
            params: list[Any] = [match_query, lower_rowid, cursor_rowid]
            if excluded_range is not None:
                conditions.append("NOT (rowid >= ? AND rowid < ?)")
                params.extend(
                    (
                        int(excluded_range[0]) * ROWID_MULTIPLIER,
                        int(excluded_range[1]) * ROWID_MULTIPLIER,
                    )
                )
            params.append(candidate_batch_size)
            rows = search_conn.execute(
                f"""
                SELECT rowid FROM event_fts
                WHERE {' AND '.join(conditions)}
                ORDER BY rowid DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
            if not rows:
                break

            search_rowids = [int(row[0]) for row in rows]
            cursor_rowid = search_rowids[-1]
            event_ids = [_decode_event_id(rowid) for rowid in search_rowids]
            verified = _load_and_verify_candidates(
                source_conn,
                event_ids,
                room_id=room_id,
                keyword=keyword,
                excluded_range=excluded_range,
                end_ts=end_ts,
            )
            for event_id in event_ids:
                row = verified.get(event_id)
                if row is not None:
                    found.append(_row_to_event(row))
                    if len(found) >= limit:
                        break
            if len(rows) < candidate_batch_size:
                break

        if lower_timestamp <= minimum_timestamp:
            break
        upper_timestamp = lower_timestamp
        window_seconds *= 2
    return found


def _search_unindexed_tail(
    source_conn: sqlite3.Connection,
    *,
    last_indexed_event_id: int,
    room_id: int,
    keyword: str,
    limit: int,
    excluded_range: tuple[int, int] | None,
    end_ts: int | None,
) -> list[dict[str, Any]]:
    conditions = [
        "id > ?",
        "room_id = ?",
        "uid IS NOT NULL",
        "content IS NOT NULL",
        "instr(content, ?) > 0",
    ]
    params: list[Any] = [last_indexed_event_id, room_id, keyword]
    if excluded_range is not None:
        conditions.append("NOT (timestamp >= ? AND timestamp < ?)")
        params.extend(excluded_range)
    if end_ts is not None:
        conditions.append("timestamp <= ?")
        params.append(end_ts)
    params.append(limit)
    rows = source_conn.execute(
        f"""
        SELECT id, room_id, uid, uname, cmd, content, gift_name, gift_num,
               total_coin, title, timestamp
        FROM event NOT INDEXED
        WHERE {' AND '.join(conditions)}
        ORDER BY timestamp DESC, id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [_row_to_event(row) for row in rows]


def list_recent_events_by_content_indexed(
    room_id: int,
    keyword: str,
    limit: int = 100,
    excluded_range: tuple[int, int] | None = None,
    end_ts: int | None = None,
    *,
    db_path: Path = DEFAULT_DB_PATH,
    search_db_path: Path = DEFAULT_SEARCH_DB_PATH,
) -> list[dict[str, Any]]:
    """Search indexed history, merge the small live tail, and verify exactly."""
    if not keyword:
        return []
    if limit < 1:
        return []
    if not search_db_path.exists():
        raise EventSearchUnavailable("event search database does not exist")

    try:
        with _connect_source(db_path) as source_conn, _connect_search(
            search_db_path
        ) as search_conn:
            schema_version = _read_meta(search_conn, "schema_version", -1)
            if schema_version != SEARCH_SCHEMA_VERSION:
                raise EventSearchUnavailable("event search schema version changed")
            last_indexed = _read_meta(search_conn, "last_indexed_event_id")
            source_max_row = source_conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM event"
            ).fetchone()
            source_max = int(source_max_row[0] if source_max_row else 0)
            if last_indexed > source_max:
                raise EventSearchUnavailable(
                    "source event ids moved backwards; rebuild required"
                )

            indexed = _search_indexed_rows(
                source_conn,
                search_conn,
                room_id=room_id,
                keyword=keyword,
                limit=limit,
                excluded_range=excluded_range,
                end_ts=end_ts,
            )
            tail = _search_unindexed_tail(
                source_conn,
                last_indexed_event_id=last_indexed,
                room_id=room_id,
                keyword=keyword,
                limit=limit,
                excluded_range=excluded_range,
                end_ts=end_ts,
            )
    except EventSearchUnavailable:
        raise
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise EventSearchUnavailable(str(exc)) from exc

    merged = {int(event["id"]): event for event in [*indexed, *tail]}
    newest = sorted(
        merged.values(),
        key=lambda event: (int(event["timestamp"]), int(event["id"])),
        reverse=True,
    )[:limit]
    return list(reversed(newest))
