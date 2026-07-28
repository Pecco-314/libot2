#!/usr/bin/env python3
"""Import the audited Mit3uri replay catalog into normalized SQLite tables."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = ROOT / "data" / "mit3uri_replay_catalog.json"
DEFAULT_DB = ROOT / "data" / "libot.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS mit3uri_replay_session (
    session_id TEXT PRIMARY KEY,
    channel_uid INTEGER NOT NULL,
    room_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    live_date TEXT NOT NULL,
    start_time TEXT,
    start_timestamp INTEGER,
    start_time_precision TEXT NOT NULL,
    duration_seconds INTEGER,
    preferred_source_priority INTEGER,
    preferred_bvid_mode TEXT,
    preferred_bvids_json TEXT NOT NULL,
    has_danmakus INTEGER NOT NULL,
    has_vtbcat INTEGER NOT NULL,
    has_database_session INTEGER NOT NULL,
    replay_only_session INTEGER NOT NULL,
    is_missing_recording INTEGER NOT NULL,
    included_in_total INTEGER NOT NULL,
    catalog_generated_at TEXT NOT NULL,
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_mit3uri_replay_session_date
ON mit3uri_replay_session(live_date, start_timestamp);

CREATE INDEX IF NOT EXISTS idx_mit3uri_replay_session_missing
ON mit3uri_replay_session(is_missing_recording, included_in_total);

CREATE TABLE IF NOT EXISTS mit3uri_replay_recording (
    bvid TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    title TEXT NOT NULL,
    duration_seconds INTEGER NOT NULL,
    source_priority INTEGER NOT NULL,
    source_up_name TEXT NOT NULL,
    source_mid INTEGER NOT NULL,
    source_url TEXT NOT NULL,
    match_method TEXT NOT NULL,
    match_confidence TEXT NOT NULL,
    FOREIGN KEY(session_id)
        REFERENCES mit3uri_replay_session(session_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_mit3uri_replay_recording_session
ON mit3uri_replay_recording(session_id, source_priority);

CREATE INDEX IF NOT EXISTS idx_mit3uri_replay_recording_source
ON mit3uri_replay_recording(source_mid, source_priority);

CREATE TABLE IF NOT EXISTS mit3uri_replay_evidence (
    evidence_key TEXT PRIMARY KEY,
    session_id TEXT,
    source TEXT NOT NULL,
    native_id TEXT NOT NULL,
    start_time TEXT NOT NULL,
    start_timestamp INTEGER NOT NULL,
    end_time TEXT,
    end_timestamp INTEGER,
    duration_seconds INTEGER,
    title TEXT NOT NULL,
    excluded_short INTEGER NOT NULL,
    FOREIGN KEY(session_id)
        REFERENCES mit3uri_replay_session(session_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_mit3uri_replay_evidence_session
ON mit3uri_replay_evidence(session_id, source);

CREATE INDEX IF NOT EXISTS idx_mit3uri_replay_evidence_time
ON mit3uri_replay_evidence(start_timestamp, source);
"""


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="解析并校验数据，但回滚数据库事务",
    )
    return parser.parse_args()


def _load_catalog(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "generated_at",
        "channel_uid",
        "room_id",
        "sessions",
        "excluded_short_source_records",
        "excluded_short_recording_sessions",
    }
    missing = required - payload.keys()
    if missing:
        raise ValueError(f"{path} 缺少字段: {', '.join(sorted(missing))}")
    return payload


def _timestamp(value: Any) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    return int(datetime.fromisoformat(value).timestamp())


def _session_rows(
    payload: dict[str, Any],
) -> tuple[list[tuple[Any, ...]], list[dict[str, Any]]]:
    included = payload["sessions"]
    excluded = payload["excluded_short_recording_sessions"]
    if not isinstance(included, list) or not isinstance(excluded, list):
        raise ValueError("sessions/excluded_short_recording_sessions 必须是列表")

    all_sessions: list[dict[str, Any]] = []
    rows: list[tuple[Any, ...]] = []
    for included_in_total, sessions in ((1, included), (0, excluded)):
        for session in sessions:
            recordings = session.get("recordings") or []
            rows.append(
                (
                    str(session["session_id"]),
                    int(payload["channel_uid"]),
                    int(payload["room_id"]),
                    str(session["title"]),
                    str(session["date"]),
                    session.get("start_time"),
                    _timestamp(session.get("start_time")),
                    str(session["start_time_precision"]),
                    session.get("duration_seconds"),
                    session.get("preferred_source_priority"),
                    session.get("preferred_bvid_mode"),
                    json.dumps(
                        session.get("preferred_bvids") or [],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    int(bool(session.get("has_danmakus"))),
                    int(bool(session.get("has_vtbcat"))),
                    int(bool(session.get("has_database_session"))),
                    int(bool(session.get("replay_only_session"))),
                    int(not recordings),
                    included_in_total,
                    str(payload["generated_at"]),
                )
            )
            all_sessions.append(session)
    return rows, all_sessions


def _recording_rows(sessions: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    seen: set[str] = set()
    for session in sessions:
        session_id = str(session["session_id"])
        for recording in session.get("recordings") or []:
            bvid = str(recording["bvid"])
            if bvid in seen:
                raise ValueError(f"重复 BV: {bvid}")
            seen.add(bvid)
            rows.append(
                (
                    bvid,
                    session_id,
                    str(recording["title"]),
                    int(recording["duration_seconds"]),
                    int(recording["source_priority"]),
                    str(recording["source_up_name"]),
                    int(recording["source_mid"]),
                    str(recording["source_url"]),
                    str(recording["match_method"]),
                    str(recording["match_confidence"]),
                )
            )
    return rows


def _evidence_rows(
    payload: dict[str, Any],
    sessions: list[dict[str, Any]],
) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    seen: set[str] = set()

    def append(
        evidence: dict[str, Any],
        *,
        session_id: str | None,
        excluded_short: int,
    ) -> None:
        source = str(evidence["source"])
        native_id = str(evidence["native_id"])
        key = f"{session_id or 'EXCLUDED'}:{source}:{native_id}"
        if key in seen:
            raise ValueError(f"重复证据: {key}")
        seen.add(key)
        start_time = str(evidence["start_time"])
        end_time = evidence.get("end_time")
        start_timestamp = _timestamp(start_time)
        if start_timestamp is None:
            raise ValueError(f"证据缺少开始时间: {key}")
        end_timestamp = _timestamp(end_time)
        duration = evidence.get("duration_seconds")
        if duration is None and end_timestamp is not None:
            duration = end_timestamp - start_timestamp
        rows.append(
            (
                key,
                session_id,
                source,
                native_id,
                start_time,
                start_timestamp,
                end_time,
                end_timestamp,
                duration,
                str(evidence.get("title") or ""),
                excluded_short,
            )
        )

    for session in sessions:
        session_id = str(session["session_id"])
        for evidence in session.get("historical_sources") or []:
            append(evidence, session_id=session_id, excluded_short=0)
    for evidence in payload["excluded_short_source_records"]:
        append(evidence, session_id=None, excluded_short=1)
    return rows


def _validate_counts(
    payload: dict[str, Any],
    session_rows: list[tuple[Any, ...]],
    recording_rows: list[tuple[Any, ...]],
) -> None:
    coverage = payload.get("coverage") or {}
    included = sum(int(row[17]) for row in session_rows)
    included_recorded = sum(int(row[17]) and not int(row[16]) for row in session_rows)
    included_missing = sum(int(row[17]) and int(row[16]) for row in session_rows)
    expected = {
        "sessions_total": included,
        "sessions_with_recording": included_recorded,
        "sessions_without_recording": included_missing,
        "unique_bvids": len(recording_rows),
    }
    mismatches = {
        key: (coverage.get(key), value)
        for key, value in expected.items()
        if coverage.get(key) != value
    }
    if mismatches:
        raise ValueError(f"目录统计不一致: {mismatches}")


def import_catalog(
    db_path: Path,
    payload: dict[str, Any],
    *,
    dry_run: bool,
) -> dict[str, int]:
    session_rows, sessions = _session_rows(payload)
    recording_rows = _recording_rows(sessions)
    evidence_rows = _evidence_rows(payload, sessions)
    _validate_counts(payload, session_rows, recording_rows)

    counts = {
        "sessions": len(session_rows),
        "included_sessions": sum(int(row[17]) for row in session_rows),
        "recordings": len(recording_rows),
        "evidence": len(evidence_rows),
    }
    if dry_run:
        return counts

    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.executescript(SCHEMA)
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM mit3uri_replay_evidence")
            conn.execute("DELETE FROM mit3uri_replay_recording")
            conn.execute("DELETE FROM mit3uri_replay_session")
            conn.executemany(
                """
                INSERT INTO mit3uri_replay_session (
                    session_id, channel_uid, room_id, title, live_date,
                    start_time, start_timestamp, start_time_precision,
                    duration_seconds, preferred_source_priority,
                    preferred_bvid_mode, preferred_bvids_json,
                    has_danmakus, has_vtbcat, has_database_session,
                    replay_only_session, is_missing_recording,
                    included_in_total, catalog_generated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                session_rows,
            )
            conn.executemany(
                """
                INSERT INTO mit3uri_replay_recording (
                    bvid, session_id, title, duration_seconds,
                    source_priority, source_up_name, source_mid, source_url,
                    match_method, match_confidence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                recording_rows,
            )
            conn.executemany(
                """
                INSERT INTO mit3uri_replay_evidence (
                    evidence_key, session_id, source, native_id,
                    start_time, start_timestamp, end_time, end_timestamp,
                    duration_seconds, title, excluded_short
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                evidence_rows,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return counts


def main() -> int:
    args = _parse_args()
    catalog_path = args.catalog.resolve()
    db_path = args.db.resolve()
    payload = _load_catalog(catalog_path)
    counts = import_catalog(db_path, payload, dry_run=args.dry_run)
    action = "validated (rolled back)" if args.dry_run else "imported"
    print(json.dumps({"action": action, **counts}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
