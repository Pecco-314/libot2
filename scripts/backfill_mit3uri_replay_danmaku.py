#!/usr/bin/env python3
"""Fetch and audit danmaku from Mit3uri's official Bilibili replays.

The script is deliberately split into metadata, fetch and audit stages. None
of these stages modifies the production ``event`` table.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
import re
import shutil
import sqlite3
import statistics
import subprocess
import sys
import time
import unicodedata
import zlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import httpx


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.common.bilibili_auth import build_bilibili_cookies  # noqa: E402


DEFAULT_DB = ROOT / "data" / "libot.db"
DEFAULT_WORK_DIR = ROOT / "data" / "mit3uri_replay_danmaku"
DEFAULT_BACKUP_DIR = ROOT / "data" / "backups"
DEFAULT_CUTOFF = "2026-04-20"
SOURCE_UID = 2030198123
ROOM_ID = 1967216004
TIMEZONE = ZoneInfo("Asia/Shanghai")
VIEW_API = "https://api.bilibili.com/x/web-interface/view"
DM_VIEW_API = "https://api.bilibili.com/x/v2/dm/web/view"
DM_SEGMENT_API = "https://api.bilibili.com/x/v2/dm/web/seg.so"
PLAYURL_API = "https://api.bilibili.com/x/player/playurl"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
}


@dataclass(frozen=True, slots=True)
class DmRow:
    session_id: str
    bvid: str
    cid: int
    dm_id: str
    progress_ms: int
    session_progress_ms: int
    mid_hash: str
    content: str
    ctime: int
    uid: int | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def common_network(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--db", type=Path, default=DEFAULT_DB)
        subparser.add_argument(
            "--work-dir",
            type=Path,
            default=DEFAULT_WORK_DIR,
        )
        subparser.add_argument("--cutoff", default=DEFAULT_CUTOFF)
        subparser.add_argument("--concurrency", type=int, default=4)
        subparser.add_argument("--request-interval", type=float, default=0.08)
        subparser.add_argument("--refresh", action="store_true")

    metadata = subparsers.add_parser(
        "metadata",
        help="validate official owners and cache CID/segment metadata",
    )
    common_network(metadata)

    fetch = subparsers.add_parser(
        "fetch",
        help="download every protobuf segment listed by metadata",
    )
    fetch.add_argument("--work-dir", type=Path, default=DEFAULT_WORK_DIR)
    fetch.add_argument("--concurrency", type=int, default=6)
    fetch.add_argument("--request-interval", type=float, default=0.04)
    fetch.add_argument("--refresh", action="store_true")
    fetch.add_argument(
        "--limit",
        type=int,
        help="only process the first N segments (for a safe network probe)",
    )

    audit = subparsers.add_parser(
        "audit",
        help="align replay progress, resolve known UIDs and stage candidates",
    )
    audit.add_argument("--db", type=Path, default=DEFAULT_DB)
    audit.add_argument("--work-dir", type=Path, default=DEFAULT_WORK_DIR)
    audit.add_argument(
        "--duplicate-tolerance-ms",
        type=int,
        default=5000,
    )
    audit.add_argument(
        "--resume",
        action="store_true",
        help="resume a previously interrupted audit.db",
    )

    lock_alignments = subparsers.add_parser(
        "lock-alignments",
        help="audit production and freeze every resolved session alignment",
    )
    lock_alignments.add_argument("--db", type=Path, default=DEFAULT_DB)
    lock_alignments.add_argument(
        "--work-dir",
        type=Path,
        default=DEFAULT_WORK_DIR,
    )
    lock_alignments.add_argument(
        "--yes",
        action="store_true",
        help="required confirmation for replacing alignment_locks.json",
    )

    frames = subparsers.add_parser(
        "clock-frames",
        help="extract replay frames for clock OCR without downloading videos",
    )
    frames.add_argument("--work-dir", type=Path, default=DEFAULT_WORK_DIR)
    frames.add_argument("--concurrency", type=int, default=2)
    frames.add_argument(
        "--offsets",
        default="183,257,331,405,479",
        help="comma-separated offsets in the first replay page",
    )
    frames.add_argument(
        "--session",
        action="append",
        help="only extract this session (repeatable)",
    )
    frames.add_argument(
        "--unresolved-only",
        action="store_true",
        help="only extract sessions unresolved by the latest clock OCR",
    )
    frames.add_argument(
        "--all-pages",
        action="store_true",
        help="sample every replay page and preserve its session offset",
    )
    frames.add_argument("--limit", type=int)
    frames.add_argument("--refresh", action="store_true")

    ocr = subparsers.add_parser(
        "clock-ocr",
        help="OCR extracted frames and generate exact alignment overrides",
    )
    ocr.add_argument("--work-dir", type=Path, default=DEFAULT_WORK_DIR)
    ocr.add_argument("--min-confidence", type=float, default=0.75)
    ocr.add_argument(
        "--session",
        action="append",
        help="only OCR this session (repeatable)",
    )
    ocr.add_argument(
        "--unresolved-only",
        action="store_true",
        help="only retry sessions unresolved by the latest clock OCR",
    )
    ocr.add_argument("--refresh", action="store_true")

    audio = subparsers.add_parser(
        "audio-align",
        help="align official replays to timestamped mirror recordings",
    )
    audio.add_argument("--db", type=Path, default=DEFAULT_DB)
    audio.add_argument("--work-dir", type=Path, default=DEFAULT_WORK_DIR)
    audio.add_argument(
        "--session",
        action="append",
        help="only align this session (repeatable)",
    )
    audio.add_argument(
        "--unresolved-only",
        action="store_true",
        help="only process sessions unresolved by the latest clock OCR",
    )
    audio.add_argument("--sample-seconds", type=int, default=900)
    audio.add_argument("--max-lag-seconds", type=int, default=600)
    audio.add_argument(
        "--accept",
        action="store_true",
        help="write high-confidence alignments to alignment_overrides.json",
    )
    audio.add_argument("--refresh", action="store_true")

    apply = subparsers.add_parser(
        "apply",
        help="re-audit, back up and import safe staged rows",
    )
    apply.add_argument("--db", type=Path, default=DEFAULT_DB)
    apply.add_argument("--work-dir", type=Path, default=DEFAULT_WORK_DIR)
    apply.add_argument(
        "--backup-dir",
        type=Path,
        default=DEFAULT_BACKUP_DIR,
    )
    apply.add_argument("--batch-size", type=int, default=2500)
    apply.add_argument(
        "--no-backup",
        action="store_true",
        help="only for disposable rehearsal databases",
    )
    apply.add_argument(
        "--yes",
        action="store_true",
        help="required confirmation for modifying the target database",
    )
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def write_bytes_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def read_varint(payload: bytes, position: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        if position >= len(payload):
            raise ValueError("truncated protobuf varint")
        byte = payload[position]
        position += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, position
        shift += 7
        if shift > 70:
            raise ValueError("protobuf varint is too long")


def decode_proto_fields(payload: bytes) -> dict[int, list[int | bytes]]:
    fields: dict[int, list[int | bytes]] = {}
    position = 0
    while position < len(payload):
        key, position = read_varint(payload, position)
        number = key >> 3
        wire_type = key & 0x07
        if number <= 0:
            raise ValueError("invalid protobuf field number")
        if wire_type == 0:
            value, position = read_varint(payload, position)
        elif wire_type == 1:
            end = position + 8
            value = payload[position:end]
            position = end
        elif wire_type == 2:
            size, position = read_varint(payload, position)
            end = position + size
            value = payload[position:end]
            position = end
        elif wire_type == 5:
            end = position + 4
            value = payload[position:end]
            position = end
        else:
            raise ValueError(f"unsupported protobuf wire type: {wire_type}")
        if position > len(payload):
            raise ValueError("truncated protobuf field")
        fields.setdefault(number, []).append(value)
    return fields


def proto_int(
    fields: dict[int, list[int | bytes]],
    number: int,
    default: int = 0,
) -> int:
    for value in reversed(fields.get(number, [])):
        if isinstance(value, int):
            return value
    return default


def proto_text(
    fields: dict[int, list[int | bytes]],
    number: int,
) -> str:
    for value in reversed(fields.get(number, [])):
        if isinstance(value, bytes):
            return value.decode("utf-8")
    return ""


def dm_segment_config(payload: bytes) -> tuple[int, int]:
    root = decode_proto_fields(payload)
    encoded = next(
        (
            value
            for value in root.get(4, [])
            if isinstance(value, bytes)
        ),
        None,
    )
    if encoded is None:
        return 360_000, 0
    fields = decode_proto_fields(encoded)
    return proto_int(fields, 1, 360_000), proto_int(fields, 2, 0)


def parse_dm_segment(
    payload: bytes,
    *,
    session_id: str,
    bvid: str,
    cid: int,
    session_offset_ms: int,
    uid_by_hash: dict[str, int],
) -> list[DmRow]:
    root = decode_proto_fields(payload)
    result: list[DmRow] = []
    for encoded in root.get(1, []):
        if not isinstance(encoded, bytes):
            continue
        fields = decode_proto_fields(encoded)
        progress_ms = proto_int(fields, 2)
        mid_hash = normalize_hash(proto_text(fields, 6))
        content = proto_text(fields, 7)
        dm_id = proto_text(fields, 12) or str(proto_int(fields, 1))
        if progress_ms < 0 or not dm_id or not content:
            continue
        result.append(
            DmRow(
                session_id=session_id,
                bvid=bvid,
                cid=cid,
                dm_id=dm_id,
                progress_ms=progress_ms,
                session_progress_ms=session_offset_ms + progress_ms,
                mid_hash=mid_hash,
                content=content,
                ctime=proto_int(fields, 8),
                uid=uid_by_hash.get(mid_hash),
            )
        )
    return result


def normalize_hash(value: str) -> str:
    return value.strip().lower().lstrip("0") or "0"


def normalize_content(value: str) -> str:
    return unicodedata.normalize("NFC", value).strip()


def cutoff_timestamp(cutoff: str) -> int:
    last_day = date.fromisoformat(cutoff)
    next_day = last_day.toordinal() + 1
    end = datetime.combine(
        date.fromordinal(next_day),
        datetime.min.time(),
        tzinfo=TIMEZONE,
    )
    return int(end.timestamp())


def replay_title_hour_timestamp(title: str) -> int | None:
    match = re.search(
        r"(\d{4})年(\d{2})月(\d{2})日(\d{2})点场",
        title,
    )
    if match is None:
        return None
    try:
        value = datetime(
            *(int(part) for part in match.groups()),
            tzinfo=TIMEZONE,
        )
    except ValueError:
        return None
    return int(value.timestamp())


def load_official_recordings(
    db_path: Path,
    cutoff: str,
) -> list[dict[str, Any]]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = list(
            conn.execute(
                """
                SELECT
                    s.session_id, s.live_date, s.start_time,
                    s.start_timestamp, s.start_time_precision,
                    s.duration_seconds AS session_duration_seconds,
                    s.preferred_bvid_mode, s.preferred_bvids_json,
                    r.bvid, r.title, r.duration_seconds,
                    r.source_mid, r.source_up_name
                FROM mit3uri_replay_recording r
                JOIN mit3uri_replay_session s USING(session_id)
                WHERE s.included_in_total = 1
                  AND s.live_date <= ?
                  AND r.source_mid = ?
                ORDER BY s.live_date, s.start_timestamp, r.bvid
                """,
                (cutoff, SOURCE_UID),
            )
        )
    result: list[dict[str, Any]] = []
    for row in rows:
        preferred = json.loads(str(row["preferred_bvids_json"]))
        bvid = str(row["bvid"])
        if bvid not in preferred:
            continue
        result.append(
            {
                **dict(row),
                "preferred_bvids": preferred,
                "recording_index": preferred.index(bvid),
            }
        )
    return sorted(
        result,
        key=lambda value: (
            value["live_date"],
            value["start_timestamp"] or 0,
            value["recording_index"],
        ),
    )


async def fetch_response(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, Any],
    headers: dict[str, str] | None = None,
    validator: Callable[[bytes], None] | None = None,
) -> bytes:
    last_error: Exception | None = None
    for attempt in range(5):
        try:
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            payload = response.content
            if payload.startswith(b"{"):
                error = json.loads(payload)
                if int(error.get("code") or 0) != 0:
                    raise RuntimeError(
                        f"Bilibili code={error.get('code')}: "
                        f"{error.get('message')}"
                    )
            if validator is not None:
                validator(payload)
            return payload
        except (
            httpx.HTTPError,
            json.JSONDecodeError,
            RuntimeError,
            ValueError,
        ) as exc:
            last_error = exc
            await asyncio.sleep(min(8.0, 0.8 * 2**attempt))
    raise RuntimeError(f"request failed: {url} {params}: {last_error}")


def validate_json_api(payload: bytes) -> None:
    value = json.loads(payload)
    if int(value.get("code") or 0) != 0:
        raise RuntimeError(
            f"Bilibili code={value.get('code')}: {value.get('message')}"
        )


async def command_metadata_async(args: argparse.Namespace) -> int:
    db_path = args.db.resolve()
    work_dir = args.work_dir.resolve()
    recordings = load_official_recordings(db_path, args.cutoff)
    views_dir = work_dir / "views"
    dm_views_dir = work_dir / "dm_views"
    semaphore = asyncio.Semaphore(max(1, args.concurrency))
    completed = 0

    async with httpx.AsyncClient(
        headers=HEADERS,
        timeout=30,
        follow_redirects=True,
        trust_env=False,
    ) as client:

        async def load_view(recording: dict[str, Any]) -> dict[str, Any]:
            nonlocal completed
            bvid = str(recording["bvid"])
            cache_path = views_dir / f"{bvid}.json"
            async with semaphore:
                if cache_path.is_file() and not args.refresh:
                    view = read_json(cache_path)
                else:
                    payload = await fetch_response(
                        client,
                        VIEW_API,
                        params={"bvid": bvid},
                        validator=validate_json_api,
                    )
                    view = json.loads(payload)["data"]
                    write_json_atomic(cache_path, view)
                    await asyncio.sleep(args.request_interval)
            owner = view.get("owner") or {}
            if int(owner.get("mid") or 0) != SOURCE_UID:
                raise RuntimeError(
                    f"{bvid} owner={owner.get('mid')} is not {SOURCE_UID}"
                )
            pages = view.get("pages") or []
            if not pages:
                raise RuntimeError(f"{bvid} has no pages")
            completed += 1
            if completed % 25 == 0 or completed == len(recordings):
                print(
                    f"metadata views={completed}/{len(recordings)}",
                    flush=True,
                )
            return {
                **recording,
                "aid": int(view["aid"]),
                "owner_mid": int(owner["mid"]),
                "owner_name": str(owner.get("name") or ""),
                "api_title": str(view.get("title") or ""),
                "api_duration_seconds": int(view.get("duration") or 0),
                "api_danmaku": int((view.get("stat") or {}).get("danmaku") or 0),
                "pubdate": int(view.get("pubdate") or 0),
                "pages": [
                    {
                        "page": int(page.get("page") or index),
                        "cid": int(page["cid"]),
                        "part": str(page.get("part") or ""),
                        "duration_seconds": int(page.get("duration") or 0),
                    }
                    for index, page in enumerate(pages, 1)
                ],
            }

        loaded = await asyncio.gather(
            *(load_view(recording) for recording in recordings)
        )

        page_jobs = [
            (recording, page)
            for recording in loaded
            if int(recording["api_danmaku"]) > 0
            for page in recording["pages"]
        ]
        completed_pages = 0

        async def load_dm_view(
            recording: dict[str, Any],
            page: dict[str, Any],
        ) -> None:
            nonlocal completed_pages
            cid = int(page["cid"])
            cache_path = dm_views_dir / f"{cid}.pb"
            async with semaphore:
                if cache_path.is_file() and not args.refresh:
                    payload = cache_path.read_bytes()
                else:
                    payload = await fetch_response(
                        client,
                        DM_VIEW_API,
                        params={
                            "type": 1,
                            "oid": cid,
                            "pid": int(recording["aid"]),
                        },
                        validator=lambda value: dm_segment_config(value),
                    )
                    write_bytes_atomic(cache_path, payload)
                    await asyncio.sleep(args.request_interval)
            segment_seconds, total_segments = dm_segment_config(payload)
            page["segment_seconds"] = segment_seconds // 1000
            page["total_segments"] = total_segments
            completed_pages += 1
            if completed_pages % 25 == 0 or completed_pages == len(page_jobs):
                print(
                    f"metadata dm_views={completed_pages}/{len(page_jobs)}",
                    flush=True,
                )

        await asyncio.gather(
            *(load_dm_view(recording, page) for recording, page in page_jobs)
        )

    grouped: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for recording in loaded:
        grouped[str(recording["session_id"])].append(recording)
    for session_recordings in grouped.values():
        for recording in session_recordings:
            recording["catalog_recording_index"] = int(
                recording["recording_index"]
            )
            recording["title_hour_timestamp"] = replay_title_hour_timestamp(
                str(recording["api_title"])
            )
        if (
            len(session_recordings) > 1
            and all(
                recording["title_hour_timestamp"] is not None
                for recording in session_recordings
            )
        ):
            session_recordings.sort(
                key=lambda value: (
                    int(value["title_hour_timestamp"]),
                    int(value["catalog_recording_index"]),
                )
            )
        else:
            session_recordings.sort(
                key=lambda value: value["catalog_recording_index"]
            )
        session_offset_ms = 0
        for recording_index, recording in enumerate(session_recordings):
            recording["recording_index"] = recording_index
            recording["session_offset_ms"] = session_offset_ms
            page_offset_ms = 0
            for page in recording["pages"]:
                page.setdefault("segment_seconds", 360)
                page.setdefault("total_segments", 0)
                page["recording_offset_ms"] = page_offset_ms
                page["session_offset_ms"] = session_offset_ms + page_offset_ms
                page_offset_ms += int(page["duration_seconds"]) * 1000
            session_offset_ms += page_offset_ms

    loaded.sort(
        key=lambda value: (
            value["live_date"],
            value["start_timestamp"] or 0,
            value["recording_index"],
        )
    )
    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(TIMEZONE).isoformat(),
        "source_uid": SOURCE_UID,
        "room_id": ROOM_ID,
        "cutoff_live_date_inclusive": args.cutoff,
        "cutoff_timestamp_exclusive": cutoff_timestamp(args.cutoff),
        "recordings": loaded,
    }
    write_json_atomic(work_dir / "metadata.json", manifest)
    summary = {
        "recordings": len(loaded),
        "sessions": len(grouped),
        "pages": sum(len(value["pages"]) for value in loaded),
        "recordings_with_danmaku": sum(
            int(value["api_danmaku"]) > 0 for value in loaded
        ),
        "api_danmaku_total": sum(
            int(value["api_danmaku"]) for value in loaded
        ),
        "segments": sum(
            int(page["total_segments"])
            for value in loaded
            for page in value["pages"]
        ),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def command_metadata(args: argparse.Namespace) -> int:
    return asyncio.run(command_metadata_async(args))


async def command_fetch_async(args: argparse.Namespace) -> int:
    work_dir = args.work_dir.resolve()
    manifest = read_json(work_dir / "metadata.json")
    jobs = [
        {
            "bvid": recording["bvid"],
            "cid": int(page["cid"]),
            "segment_index": segment_index,
        }
        for recording in manifest["recordings"]
        for page in recording["pages"]
        for segment_index in range(1, int(page["total_segments"]) + 1)
    ]
    if args.limit is not None:
        if args.limit < 1:
            raise ValueError("--limit must be positive")
        jobs = jobs[: args.limit]
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    for job in jobs:
        queue.put_nowait(job)
    completed = 0
    downloaded = 0
    failures: list[str] = []

    async with httpx.AsyncClient(
        headers=HEADERS,
        cookies=build_bilibili_cookies(),
        timeout=30,
        follow_redirects=True,
        trust_env=False,
    ) as client:

        async def worker() -> None:
            nonlocal completed, downloaded
            while True:
                try:
                    job = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                cid = int(job["cid"])
                segment_index = int(job["segment_index"])
                path = (
                    work_dir
                    / "segments"
                    / str(cid)
                    / f"{segment_index:04d}.pb"
                )
                try:
                    if path.is_file() and not args.refresh:
                        decode_proto_fields(path.read_bytes())
                    else:
                        payload = await fetch_response(
                            client,
                            DM_SEGMENT_API,
                            params={
                                "type": 1,
                                "oid": cid,
                                "segment_index": segment_index,
                            },
                            headers={
                                "Referer": (
                                    "https://www.bilibili.com/video/"
                                    f"{job['bvid']}"
                                )
                            },
                            validator=decode_proto_fields,
                        )
                        write_bytes_atomic(path, payload)
                        downloaded += 1
                        await asyncio.sleep(args.request_interval)
                except Exception as exc:
                    failures.append(
                        f"cid={cid} segment={segment_index}: {exc}"
                    )
                finally:
                    completed += 1
                    queue.task_done()
                    if completed % 200 == 0 or completed == len(jobs):
                        print(
                            f"segments={completed}/{len(jobs)} "
                            f"downloaded={downloaded} failures={len(failures)}",
                            flush=True,
                        )

        await asyncio.gather(
            *(worker() for _ in range(max(1, args.concurrency)))
        )
    if failures:
        write_json_atomic(
            work_dir / "fetch_failures.json",
            {"failures": failures},
        )
        raise RuntimeError(f"{len(failures)} segment downloads failed")
    print(
        json.dumps(
            {
                "segments": len(jobs),
                "downloaded": downloaded,
                "cached": len(jobs) - downloaded,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def command_fetch(args: argparse.Namespace) -> int:
    return asyncio.run(command_fetch_async(args))


def parse_frame_offsets(value: str) -> list[int]:
    try:
        result = sorted({int(part.strip()) for part in value.split(",")})
    except ValueError as exc:
        raise ValueError("--offsets must be comma-separated integers") from exc
    if not result or result[0] < 0:
        raise ValueError("--offsets must contain non-negative seconds")
    return result


def load_clock_targets(
    work_dir: Path,
    selected_sessions: list[str] | None,
    limit: int | None,
) -> list[dict[str, Any]]:
    manifest = read_json(work_dir / "metadata.json")
    audit = read_json(work_dir / "audit.json")
    fallback = {
        str(report["session_id"])
        for report in audit["session_reports"]
        if report["method"] == "catalog_hour"
        and int(report["source_rows"]) > 0
    }
    if selected_sessions:
        unknown = set(selected_sessions) - fallback
        if unknown:
            raise ValueError(
                "sessions are not unresolved catalog_hour targets: "
                + ", ".join(sorted(unknown))
            )
        fallback &= set(selected_sessions)
    grouped: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for recording in manifest["recordings"]:
        session_id = str(recording["session_id"])
        if session_id in fallback:
            grouped[session_id].append(recording)
    result: list[dict[str, Any]] = []
    for session_id, recordings in grouped.items():
        recordings.sort(key=lambda value: int(value["recording_index"]))
        recording = recordings[0]
        page = recording["pages"][0]
        pages = [
            {
                "bvid": str(value["bvid"]),
                "recording_index": int(value["recording_index"]),
                "page": int(page_value["page"]),
                "cid": int(page_value["cid"]),
                "duration_seconds": int(
                    page_value["duration_seconds"]
                ),
                "session_offset_ms": int(
                    page_value["session_offset_ms"]
                ),
            }
            for value in recordings
            for page_value in value["pages"]
        ]
        result.append(
            {
                "session_id": session_id,
                "live_date": recording["live_date"],
                "expected_base_ms": int(recording["start_timestamp"]) * 1000,
                "bvid": str(recording["bvid"]),
                "aid": int(recording["aid"]),
                "cid": int(page["cid"]),
                "page_duration_seconds": int(page["duration_seconds"]),
                "title": str(recording["api_title"]),
                "pages": pages,
            }
        )
    result.sort(key=lambda value: (value["live_date"], value["session_id"]))
    if limit is not None:
        if limit < 1:
            raise ValueError("--limit must be positive")
        result = result[:limit]
    return result


def sanitized_subprocess_env() -> dict[str, str]:
    result = dict(os.environ)
    for key in list(result):
        if key.lower().endswith("_proxy"):
            result.pop(key, None)
    return result


async def extract_remote_frame(
    urls: list[str],
    *,
    bvid: str,
    offset_seconds: int,
    output_path: Path,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.stem}.tmp.jpg")
    temporary.unlink(missing_ok=True)
    request_headers = (
        f"Referer: https://www.bilibili.com/video/{bvid}\r\n"
        f"User-Agent: {HEADERS['User-Agent']}\r\n"
    )
    errors: list[str] = []
    for url_index, url in enumerate(urls):
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-rw_timeout",
            "30000000",
            "-ss",
            str(offset_seconds),
            "-headers",
            request_headers,
            "-i",
            url,
            "-map",
            "0:v:0",
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(temporary),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=sanitized_subprocess_env(),
        )
        _stdout, stderr = await process.communicate()
        if process.returncode == 0 and temporary.is_file():
            os.replace(temporary, output_path)
            return
        errors.append(
            f"url#{url_index}: "
            f"{stderr.decode('utf-8', errors='replace')[-300:]}"
        )
        temporary.unlink(missing_ok=True)
    raise RuntimeError("ffmpeg frame extraction failed: " + " | ".join(errors))


async def command_clock_frames_async(args: argparse.Namespace) -> int:
    work_dir = args.work_dir.resolve()
    offsets = parse_frame_offsets(args.offsets)
    selected_sessions = args.session
    if args.unresolved_only:
        if selected_sessions:
            raise ValueError(
                "--unresolved-only cannot be combined with --session"
            )
        ocr_summary = read_json(work_dir / "clock_ocr.json")
        selected_sessions = [
            str(value) for value in ocr_summary["unresolved"]
        ]
    targets = load_clock_targets(
        work_dir,
        selected_sessions,
        args.limit,
    )
    semaphore = asyncio.Semaphore(max(1, args.concurrency))
    cookies = build_bilibili_cookies()
    completed = 0
    failures: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []

    async with httpx.AsyncClient(
        headers=HEADERS,
        cookies=cookies,
        timeout=30,
        follow_redirects=True,
        trust_env=False,
    ) as client:

        async def process_target(target: dict[str, Any]) -> None:
            nonlocal completed
            session_id = str(target["session_id"])
            frame_dir = work_dir / "clock_frames" / session_id
            pages = list(target["pages"])
            if not args.all_pages:
                pages = pages[:1]
            result = {
                **target,
                "requested_offsets": [],
                "frames": [],
            }
            try:
                async with semaphore:
                    for page in pages:
                        duration_seconds = int(page["duration_seconds"])
                        local_requested = [
                            offset
                            for offset in offsets
                            if offset < duration_seconds - 1
                        ]
                        if not local_requested:
                            local_requested = [
                                max(0, duration_seconds // 2)
                            ]
                        response = await fetch_response(
                            client,
                            PLAYURL_API,
                            params={
                                "bvid": page["bvid"],
                                "cid": page["cid"],
                                "qn": 80,
                                "fnval": 16,
                                "fourk": 0,
                            },
                            headers={
                                "Referer": (
                                    "https://www.bilibili.com/video/"
                                    f"{page['bvid']}"
                                )
                            },
                            validator=validate_json_api,
                        )
                        data = json.loads(response)["data"]
                        videos = (data.get("dash") or {}).get("video") or []
                        if not videos:
                            raise RuntimeError(
                                "playurl response has no DASH video"
                            )
                        videos.sort(
                            key=lambda value: (
                                int(value.get("height") or 0),
                                int(value.get("bandwidth") or 0),
                            ),
                            reverse=True,
                        )
                        video = videos[0]
                        urls = [
                            str(
                                video.get("baseUrl")
                                or video.get("base_url")
                                or ""
                            )
                        ]
                        urls.extend(
                            str(value)
                            for value in (
                                video.get("backupUrl")
                                or video.get("backup_url")
                                or []
                            )
                        )
                        urls = [value for value in urls if value]
                        session_offset_ms = int(
                            page["session_offset_ms"]
                        )
                        for offset in local_requested:
                            session_progress_ms = (
                                session_offset_ms + offset * 1000
                            )
                            session_offset_seconds = (
                                session_progress_ms // 1000
                            )
                            if session_offset_ms == 0:
                                output_path = (
                                    frame_dir / f"{offset:04d}.jpg"
                                )
                            else:
                                output_path = (
                                    frame_dir
                                    / str(page["cid"])
                                    / f"{offset:04d}.jpg"
                                )
                            if (
                                output_path.is_file()
                                and not args.refresh
                            ):
                                status = "cached"
                            else:
                                await extract_remote_frame(
                                    urls,
                                    bvid=str(page["bvid"]),
                                    offset_seconds=offset,
                                    output_path=output_path,
                                )
                                status = "downloaded"
                            result["requested_offsets"].append(
                                session_offset_seconds
                            )
                            result["frames"].append(
                                {
                                    "offset_seconds": (
                                        session_offset_seconds
                                    ),
                                    "page_offset_seconds": offset,
                                    "session_offset_ms": (
                                        session_offset_ms
                                    ),
                                    "bvid": str(page["bvid"]),
                                    "cid": int(page["cid"]),
                                    "page": int(page["page"]),
                                    "path": str(output_path),
                                    "status": status,
                                    "width": int(
                                        video.get("width") or 0
                                    ),
                                    "height": int(
                                        video.get("height") or 0
                                    ),
                                }
                            )
                    result["requested_offsets"] = sorted(
                        set(result["requested_offsets"])
                    )
            except Exception as exc:
                result["error"] = str(exc)
                failures.append(
                    {"session_id": session_id, "error": str(exc)}
                )
            results.append(result)
            completed += 1
            print(
                f"clock_frames={completed}/{len(targets)} "
                f"failures={len(failures)} session={session_id}",
                flush=True,
            )

        await asyncio.gather(*(process_target(target) for target in targets))

    manifest_path = work_dir / "clock_frames.json"
    merged_by_session: dict[str, dict[str, Any]] = {}
    if manifest_path.is_file():
        existing_manifest = read_json(manifest_path)
        merged_by_session = {
            str(session["session_id"]): session
            for session in existing_manifest["sessions"]
        }
    for result in results:
        session_id = str(result["session_id"])
        existing = merged_by_session.get(session_id)
        if existing is None:
            merged_by_session[session_id] = result
            continue
        frames_by_offset = {
            int(frame["offset_seconds"]): frame
            for frame in existing.get("frames", [])
        }
        frames_by_offset.update(
            {
                int(frame["offset_seconds"]): frame
                for frame in result.get("frames", [])
            }
        )
        merged = {
            **existing,
            **result,
            "requested_offsets": sorted(
                {
                    *existing.get("requested_offsets", []),
                    *result.get("requested_offsets", []),
                }
            ),
            "frames": [
                frames_by_offset[offset]
                for offset in sorted(frames_by_offset)
            ],
        }
        if "error" not in result:
            merged.pop("error", None)
        merged_by_session[session_id] = merged
    merged_results = sorted(
        merged_by_session.values(),
        key=lambda value: (value["live_date"], value["session_id"]),
    )
    output = {
        "schema_version": 1,
        "created_at": datetime.now(TIMEZONE).isoformat(),
        "offsets": offsets,
        "targets": len(targets),
        "failures": failures,
        "sessions": merged_results,
    }
    write_json_atomic(manifest_path, output)
    print(
        json.dumps(
            {
                "targets": len(targets),
                "frames": sum(
                    len(result["frames"]) for result in results
                ),
                "failures": len(failures),
                "output": str(manifest_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if not failures else 1


def command_clock_frames(args: argparse.Namespace) -> int:
    return asyncio.run(command_clock_frames_async(args))


CLOCK_TIME_PATTERN = re.compile(
    r"(?<!\d)([01]?\d|2[0-3])\s*[:：.·]\s*([0-5]\d)"
    r"(?:\s*[:：.·]\s*([0-5]\d))?(?!\d)"
)
CLOCK_TIME_COMPACT_PATTERN = re.compile(
    r"(?<!\d)([01]\d|2[0-3])([0-5]\d)[.:：·]([0-5]\d)(?!\d)"
)
START_MARKER_TEXT_PATTERN = re.compile(
    r"(?:开\s*[播情](?:了|时间)?|播\s*了|直播\s*(?:开始|已开始))"
)
START_MARKER_TIME_PATTERN = re.compile(
    r"(?:开\s*[播情]|播)\s*[了7]?\s*"
    r"([01]?\d|2[0-3])\s*[:：.·]\s*([0-5]\d)"
    r"\s*[:：.·]\s*([0-5]\d)(?!\d)"
)
OCR_SCHEMA_VERSION = 7


def ocr_time_tokens(
    records: list[Any] | None,
    *,
    x_offset: int,
    y_offset: int,
    image_width: int,
    image_height: int,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for record in records or []:
        if not isinstance(record, (list, tuple)) or len(record) < 3:
            continue
        box, raw_text, raw_score = record[:3]
        if not isinstance(box, (list, tuple)):
            continue
        try:
            score = float(raw_score)
            points = [
                [float(point[0]) + x_offset, float(point[1]) + y_offset]
                for point in box
            ]
        except (TypeError, ValueError, IndexError):
            continue
        text = str(raw_text).strip()
        if START_MARKER_TEXT_PATTERN.search(text):
            continue
        matches = list(CLOCK_TIME_PATTERN.finditer(text))
        matches.extend(CLOCK_TIME_COMPACT_PATTERN.finditer(text))
        for match in matches:
            hour = int(match.group(1))
            minute = int(match.group(2))
            second = int(match.group(3)) if match.group(3) is not None else None
            center_x = sum(point[0] for point in points) / len(points)
            center_y = sum(point[1] for point in points) / len(points)
            result.append(
                {
                    "text": text,
                    "score": score,
                    "hour": hour,
                    "minute": minute,
                    "second": second,
                    "box": points,
                    "center_x": center_x / image_width,
                    "center_y": center_y / image_height,
                }
            )
    return result


def _ocr_text_records(
    records: list[Any] | None,
    *,
    x_offset: int,
    y_offset: int,
    image_width: int,
    image_height: int,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for record in records or []:
        if not isinstance(record, (list, tuple)) or len(record) < 3:
            continue
        box, raw_text, raw_score = record[:3]
        if not isinstance(box, (list, tuple)):
            continue
        try:
            score = float(raw_score)
            points = [
                [float(point[0]) + x_offset, float(point[1]) + y_offset]
                for point in box
            ]
        except (TypeError, ValueError, IndexError):
            continue
        if not points:
            continue
        text = str(raw_text).strip()
        if not text:
            continue
        result.append(
            {
                "text": text,
                "score": score,
                "box": points,
                "left": min(point[0] for point in points),
                "right": max(point[0] for point in points),
                "top": min(point[1] for point in points),
                "bottom": max(point[1] for point in points),
                "center_x": (
                    sum(point[0] for point in points)
                    / len(points)
                    / image_width
                ),
                "center_y": (
                    sum(point[1] for point in points)
                    / len(points)
                    / image_height
                ),
            }
        )
    return result


def _ocr_text_lines(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lines: list[list[dict[str, Any]]] = []
    for record in sorted(
        records,
        key=lambda value: (
            (float(value["top"]) + float(value["bottom"])) / 2,
            float(value["left"]),
        ),
    ):
        center_y = (float(record["top"]) + float(record["bottom"])) / 2
        height = max(1.0, float(record["bottom"]) - float(record["top"]))
        best_index: int | None = None
        best_distance = float("inf")
        for index, line in enumerate(lines):
            line_center = statistics.mean(
                (
                    float(value["top"]) + float(value["bottom"])
                )
                / 2
                for value in line
            )
            line_height = statistics.median(
                max(
                    1.0,
                    float(value["bottom"]) - float(value["top"]),
                )
                for value in line
            )
            distance = abs(center_y - line_center)
            if distance <= max(height, line_height) * 0.65:
                if distance < best_distance:
                    best_index = index
                    best_distance = distance
        if best_index is None:
            lines.append([record])
        else:
            lines[best_index].append(record)

    result: list[dict[str, Any]] = []
    for line in lines:
        ordered = sorted(line, key=lambda value: float(value["left"]))
        text = "".join(str(value["text"]) for value in ordered)
        result.append(
            {
                "text": text,
                "score": statistics.mean(
                    float(value["score"]) for value in ordered
                ),
                "box": [
                    [min(float(value["left"]) for value in ordered),
                     min(float(value["top"]) for value in ordered)],
                    [max(float(value["right"]) for value in ordered),
                     min(float(value["top"]) for value in ordered)],
                    [max(float(value["right"]) for value in ordered),
                     max(float(value["bottom"]) for value in ordered)],
                    [min(float(value["left"]) for value in ordered),
                     max(float(value["bottom"]) for value in ordered)],
                ],
                "center_x": statistics.mean(
                    float(value["center_x"]) for value in ordered
                ),
                "center_y": statistics.mean(
                    float(value["center_y"]) for value in ordered
                ),
            }
        )
    return result


def start_marker_candidates(
    records: list[Any] | None,
    *,
    x_offset: int,
    y_offset: int,
    image_width: int,
    image_height: int,
    live_date: str,
    expected_base_ms: int,
    offset_seconds: int,
    frame_path: str,
    min_confidence: float,
) -> list[dict[str, Any]]:
    text_records = _ocr_text_records(
        records,
        x_offset=x_offset,
        y_offset=y_offset,
        image_width=image_width,
        image_height=image_height,
    )
    values = [*text_records, *_ocr_text_lines(text_records)]
    live_day = date.fromisoformat(live_date)
    lower_limit = expected_base_ms - 3 * 60 * 60 * 1000
    upper_limit = expected_base_ms + 3 * 60 * 60 * 1000
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for value in values:
        text = str(value["text"])
        label_detected = bool(START_MARKER_TEXT_PATTERN.search(text))
        confidence_floor = (
            max(0.50, min_confidence - 0.20)
            if label_detected
            else min_confidence
        )
        if float(value["score"]) < confidence_floor:
            continue
        if (
            not label_detected
            and (
                float(value["center_x"]) > 0.32
                or float(value["center_y"]) > 0.42
            )
        ):
            continue
        matches: list[tuple[re.Match[str], bool]] = [
            (match, True)
            for match in START_MARKER_TIME_PATTERN.finditer(text)
        ]
        if not matches:
            matches = [
                (match, False)
                for match in CLOCK_TIME_PATTERN.finditer(text)
                if match.group(3) is not None
            ]
        for match, pattern_labeled in matches:
            hour = int(match.group(1))
            minute = int(match.group(2))
            second = int(match.group(3))
            seconds_of_day = hour * 3600 + minute * 60 + second
            for day_shift in (-1, 0, 1):
                day_value = live_day + timedelta(days=day_shift)
                midnight = datetime.combine(
                    day_value,
                    datetime.min.time(),
                    tzinfo=TIMEZONE,
                )
                base_ms = (
                    int(midnight.timestamp()) + seconds_of_day
                ) * 1000
                if base_ms < lower_limit or base_ms > upper_limit:
                    continue
                key = (frame_path, base_ms)
                if key in seen:
                    continue
                seen.add(key)
                result.append(
                    {
                        **value,
                        "hour": hour,
                        "minute": minute,
                        "second": second,
                        "frame_path": frame_path,
                        "offset_seconds": offset_seconds,
                        "base_ms": base_ms,
                        "marker_label_detected": (
                            label_detected or pattern_labeled
                        ),
                    }
                )
    return result


def choose_start_marker_alignment(
    candidates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    by_base: dict[int, dict[str, dict[str, Any]]] = collections.defaultdict(
        dict
    )
    for candidate in candidates:
        base_ms = int(candidate["base_ms"])
        frame_path = str(candidate["frame_path"])
        previous = by_base[base_ms].get(frame_path)
        if previous is None or float(candidate["score"]) > float(
            previous["score"]
        ):
            by_base[base_ms][frame_path] = candidate
    eligible: list[tuple[int, list[dict[str, Any]], bool]] = []
    for base_ms, by_frame in by_base.items():
        evidence = list(by_frame.values())
        labeled_frames = sum(
            bool(value.get("marker_label_detected"))
            for value in evidence
        )
        minimum_support = 2 if labeled_frames >= 2 else 3
        if len(evidence) < minimum_support:
            continue
        if (
            max(float(value["center_x"]) for value in evidence)
            - min(float(value["center_x"]) for value in evidence)
            > 0.05
            or max(float(value["center_y"]) for value in evidence)
            - min(float(value["center_y"]) for value in evidence)
            > 0.05
        ):
            continue
        offsets = [int(value["offset_seconds"]) for value in evidence]
        if max(offsets) - min(offsets) < 5:
            continue
        eligible.append((base_ms, evidence, labeled_frames >= 2))
    if not eligible:
        return None
    base_ms, evidence, labeled = max(
        eligible,
        key=lambda value: (
            1 if value[2] else 0,
            len(value[1]),
            statistics.mean(float(item["score"]) for item in value[1]),
        ),
    )
    return {
        "base_ms": base_ms,
        "method": (
            "ocr_start_marker_second"
            if labeled
            else "ocr_static_start_time_second"
        ),
        "precision": "second",
        "support_frames": len(evidence),
        "mean_confidence": statistics.mean(
            float(value["score"]) for value in evidence
        ),
        "uncertainty_ms": 1000,
        "evidence": sorted(
            evidence,
            key=lambda value: (
                int(value["offset_seconds"]),
                str(value["frame_path"]),
            ),
        ),
    }


def time_token_candidates(
    tokens: list[dict[str, Any]],
    *,
    live_date: str,
    expected_base_ms: int,
    offset_seconds: int,
    frame_path: str,
    min_confidence: float,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    live_day = date.fromisoformat(live_date)
    lower_limit = expected_base_ms - 3 * 60 * 60 * 1000
    upper_limit = expected_base_ms + 3 * 60 * 60 * 1000
    for token in tokens:
        if float(token["score"]) < min_confidence:
            continue
        resolution_ms = 1000 if token["second"] is not None else 60_000
        seconds_of_day = (
            int(token["hour"]) * 3600
            + int(token["minute"]) * 60
            + int(token["second"] or 0)
        )
        for day_shift in (-1, 0, 1):
            day_value = live_day + timedelta(days=day_shift)
            midnight = datetime.combine(
                day_value,
                datetime.min.time(),
                tzinfo=TIMEZONE,
            )
            frame_time_ms = (
                int(midnight.timestamp()) + seconds_of_day
            ) * 1000
            base_lower_ms = frame_time_ms - offset_seconds * 1000
            base_upper_ms = base_lower_ms + resolution_ms
            if (
                base_upper_ms < lower_limit
                or base_lower_ms > upper_limit
            ):
                continue
            result.append(
                {
                    **token,
                    "frame_path": frame_path,
                    "offset_seconds": offset_seconds,
                    "precision": (
                        "second" if token["second"] is not None else "minute"
                    ),
                    "base_lower_ms": base_lower_ms,
                    "base_upper_ms": base_upper_ms,
                    "base_center_ms": (
                        base_lower_ms + base_upper_ms
                    ) // 2,
                }
            )
    return result


def choose_clock_alignment(
    candidates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    best: tuple[tuple[Any, ...], list[dict[str, Any]]] | None = None
    for precision, time_tolerance_ms, minimum_support in (
        ("second", 2500, 2),
        ("minute", 60_000, 3),
    ):
        values = [
            candidate
            for candidate in candidates
            if candidate["precision"] == precision
        ]
        for seed in values:
            by_frame: dict[str, dict[str, Any]] = {}
            for candidate in values:
                if (
                    abs(
                        int(candidate["base_center_ms"])
                        - int(seed["base_center_ms"])
                    )
                    > time_tolerance_ms
                ):
                    continue
                spatial_distance = (
                    (float(candidate["center_x"]) - float(seed["center_x"])) ** 2
                    + (float(candidate["center_y"]) - float(seed["center_y"])) ** 2
                ) ** 0.5
                if spatial_distance > 0.12:
                    continue
                frame_path = str(candidate["frame_path"])
                previous = by_frame.get(frame_path)
                if previous is None or float(candidate["score"]) > float(
                    previous["score"]
                ):
                    by_frame[frame_path] = candidate
            group = list(by_frame.values())
            if len(group) < minimum_support:
                continue
            offsets = [int(value["offset_seconds"]) for value in group]
            minimum_offset_span = 30 if precision == "second" else 120
            if max(offsets) - min(offsets) < minimum_offset_span:
                continue
            centers = [int(value["base_center_ms"]) for value in group]
            spatial_spread = max(
                (
                    (
                        (float(value["center_x"]) - float(seed["center_x"])) ** 2
                        + (
                            float(value["center_y"])
                            - float(seed["center_y"])
                        )
                        ** 2
                    )
                    ** 0.5
                )
                for value in group
            )
            score = (
                1 if precision == "second" else 0,
                len(group),
                -max(centers) + min(centers),
                statistics.mean(float(value["score"]) for value in group),
                -spatial_spread,
            )
            if best is None or score > best[0]:
                best = (score, group)
    if best is None:
        return None
    group = best[1]
    precision = str(group[0]["precision"])
    lowers = [int(value["base_lower_ms"]) for value in group]
    uppers = [int(value["base_upper_ms"]) for value in group]
    intersection_lower = max(lowers)
    intersection_upper = min(uppers)
    if intersection_lower < intersection_upper:
        base_ms = (intersection_lower + intersection_upper) // 2
        uncertainty_ms = intersection_upper - intersection_lower
    else:
        centers = [int(value["base_center_ms"]) for value in group]
        base_ms = round(statistics.median(centers))
        uncertainty_ms = max(centers) - min(centers)
    uncertainty_ms = max(1000, uncertainty_ms)
    return {
        "base_ms": base_ms,
        "method": f"ocr_clock_{precision}",
        "precision": precision,
        "support_frames": len(group),
        "mean_confidence": statistics.mean(
            float(value["score"]) for value in group
        ),
        "uncertainty_ms": uncertainty_ms,
        "evidence": sorted(
            group,
            key=lambda value: (
                int(value["offset_seconds"]),
                str(value["frame_path"]),
            ),
        ),
    }


def command_clock_ocr(args: argparse.Namespace) -> int:
    import cv2
    from rapidocr_onnxruntime import RapidOCR

    work_dir = args.work_dir.resolve()
    frames_manifest = read_json(work_dir / "clock_frames.json")
    sessions = list(frames_manifest["sessions"])
    if args.unresolved_only:
        if args.session:
            raise ValueError(
                "--unresolved-only cannot be combined with --session"
            )
        ocr_summary = read_json(work_dir / "clock_ocr.json")
        args.session = [
            str(value) for value in ocr_summary["unresolved"]
        ]
    if args.session:
        selected = set(args.session)
        sessions = [
            session
            for session in sessions
            if str(session["session_id"]) in selected
        ]
        missing = selected - {
            str(session["session_id"]) for session in sessions
        }
        if missing:
            raise ValueError(
                "sessions are missing from clock_frames.json: "
                + ", ".join(sorted(missing))
            )
    ocr = RapidOCR()
    output_dir = work_dir / "clock_ocr"
    results: list[dict[str, Any]] = []
    for index, session in enumerate(sessions, 1):
        session_id = str(session["session_id"])
        output_path = output_dir / f"{session_id}.json"
        frame_offsets = sorted(
            int(frame["offset_seconds"]) for frame in session["frames"]
        )
        cached_result = (
            read_json(output_path) if output_path.is_file() else None
        )
        if (
            cached_result is not None
            and not args.refresh
            and cached_result.get("schema_version") == OCR_SCHEMA_VERSION
            and cached_result.get("frame_offsets") == frame_offsets
        ):
            result = cached_result
        else:
            candidates: list[dict[str, Any]] = []
            marker_candidates: list[dict[str, Any]] = []
            frame_images: list[tuple[dict[str, Any], Any]] = []
            for frame in session["frames"]:
                frame_path = Path(str(frame["path"]))
                image = cv2.imread(str(frame_path))
                if image is None:
                    raise ValueError(f"failed to read frame: {frame_path}")
                frame_images.append((frame, image))
            for frame, image in frame_images:
                if int(frame["offset_seconds"]) > 90:
                    continue
                height, width = image.shape[:2]
                x_limit = round(width * 0.42)
                y_limit = round(height * 0.42)
                records, _elapsed = ocr(image[:y_limit, :x_limit])
                marker_candidates.extend(
                    start_marker_candidates(
                        records,
                        x_offset=0,
                        y_offset=0,
                        image_width=width,
                        image_height=height,
                        live_date=str(session["live_date"]),
                        expected_base_ms=int(session["expected_base_ms"]),
                        offset_seconds=int(frame["offset_seconds"]),
                        frame_path=str(frame["path"]),
                        min_confidence=args.min_confidence,
                    )
                )
            alignment = choose_start_marker_alignment(marker_candidates)
            reused_prior_clock_scan = (
                alignment is None
                and cached_result is not None
                and not args.refresh
                and int(cached_result.get("schema_version") or 0)
                < OCR_SCHEMA_VERSION
            )
            used_full_frame = False
            if reused_prior_clock_scan:
                candidates = [
                    value
                    for value in (cached_result.get("candidates") or [])
                    if not START_MARKER_TEXT_PATTERN.search(
                        str(value.get("text") or "")
                    )
                ]
                alignment = choose_clock_alignment(candidates)
                used_full_frame = bool(
                    cached_result.get("used_full_frame")
                )
                if alignment is None:
                    used_full_frame = True
                    for frame_index, (frame, image) in enumerate(
                        frame_images,
                        1,
                    ):
                        height, width = image.shape[:2]
                        records, _elapsed = ocr(image)
                        marker_candidates.extend(
                            start_marker_candidates(
                                records,
                                x_offset=0,
                                y_offset=0,
                                image_width=width,
                                image_height=height,
                                live_date=str(session["live_date"]),
                                expected_base_ms=int(
                                    session["expected_base_ms"]
                                ),
                                offset_seconds=int(
                                    frame["offset_seconds"]
                                ),
                                frame_path=str(frame["path"]),
                                min_confidence=args.min_confidence,
                            )
                        )
                        marker_alignment = (
                            choose_start_marker_alignment(
                                marker_candidates
                            )
                        )
                        if marker_alignment is not None:
                            alignment = marker_alignment
                            break
                        tokens = ocr_time_tokens(
                            records,
                            x_offset=0,
                            y_offset=0,
                            image_width=width,
                            image_height=height,
                        )
                        candidates.extend(
                            time_token_candidates(
                                tokens,
                                live_date=str(session["live_date"]),
                                expected_base_ms=int(
                                    session["expected_base_ms"]
                                ),
                                offset_seconds=int(
                                    frame["offset_seconds"]
                                ),
                                frame_path=str(frame["path"]),
                                min_confidence=args.min_confidence,
                            )
                        )
                        clock_alignment = choose_clock_alignment(
                            candidates
                        )
                        if clock_alignment is not None:
                            alignment = clock_alignment
                            break
                        if (
                            frame_index % 5 == 0
                            or frame_index == len(frame_images)
                        ):
                            print(
                                "clock_ocr_full_frame "
                                f"session={session_id} "
                                f"frames={frame_index}/"
                                f"{len(frame_images)}",
                                flush=True,
                            )
            if alignment is None:
                # The current stream overlay puts a small wall clock flush
                # against the lower-left edge.  Full-frame OCR commonly
                # misses it, while a tight crop detects it reliably.
                for frame, image in frame_images:
                    height, width = image.shape[:2]
                    y_offset = round(height * 0.72)
                    x_limit = round(width * 0.45)
                    records, _elapsed = ocr(
                        image[y_offset:, :x_limit]
                    )
                    tokens = ocr_time_tokens(
                        records,
                        x_offset=0,
                        y_offset=y_offset,
                        image_width=width,
                        image_height=height,
                    )
                    candidates.extend(
                        time_token_candidates(
                            tokens,
                            live_date=str(session["live_date"]),
                            expected_base_ms=int(
                                session["expected_base_ms"]
                            ),
                            offset_seconds=int(
                                frame["offset_seconds"]
                            ),
                            frame_path=str(frame["path"]),
                            min_confidence=args.min_confidence,
                        )
                    )
                alignment = choose_clock_alignment(candidates)
            if alignment is None and not reused_prior_clock_scan:
                for frame, image in frame_images:
                    height, width = image.shape[:2]
                    x_offset = round(width * 0.55)
                    y_limit = round(height * 0.35)
                    records, _elapsed = ocr(
                        image[:y_limit, x_offset:]
                    )
                    tokens = ocr_time_tokens(
                        records,
                        x_offset=x_offset,
                        y_offset=0,
                        image_width=width,
                        image_height=height,
                    )
                    candidates.extend(
                        time_token_candidates(
                            tokens,
                            live_date=str(session["live_date"]),
                            expected_base_ms=int(
                                session["expected_base_ms"]
                            ),
                            offset_seconds=int(
                                frame["offset_seconds"]
                            ),
                            frame_path=str(frame["path"]),
                            min_confidence=args.min_confidence,
                        )
                    )
                alignment = choose_clock_alignment(candidates)
            if alignment is None and not reused_prior_clock_scan:
                used_full_frame = True
                for frame, image in frame_images[:3]:
                    height, width = image.shape[:2]
                    records, _elapsed = ocr(image)
                    tokens = ocr_time_tokens(
                        records,
                        x_offset=0,
                        y_offset=0,
                        image_width=width,
                        image_height=height,
                    )
                    candidates.extend(
                        time_token_candidates(
                            tokens,
                            live_date=str(session["live_date"]),
                            expected_base_ms=int(session["expected_base_ms"]),
                            offset_seconds=int(frame["offset_seconds"]),
                            frame_path=str(frame["path"]),
                            min_confidence=args.min_confidence,
                        )
                    )
                alignment = choose_clock_alignment(candidates)
            result = {
                "schema_version": OCR_SCHEMA_VERSION,
                "session_id": session_id,
                "live_date": session["live_date"],
                "title": session["title"],
                "bvid": session["bvid"],
                "cid": session["cid"],
                "expected_base_ms": session["expected_base_ms"],
                "frame_offsets": frame_offsets,
                "used_full_frame": used_full_frame,
                "candidate_count": len(candidates),
                "start_marker_candidate_count": len(marker_candidates),
                "alignment": alignment,
                "candidates": candidates,
                "start_marker_candidates": marker_candidates,
            }
            write_json_atomic(output_path, result)
        results.append(result)
        alignment = result.get("alignment")
        print(
            f"clock_ocr={index}/{len(sessions)} session={session_id} "
            f"method={(alignment or {}).get('method', 'unresolved')} "
            f"support={(alignment or {}).get('support_frames', 0)}",
            flush=True,
        )

    all_result_paths = sorted(output_dir.glob("MIT3URI-*.json"))
    all_results = [read_json(path) for path in all_result_paths]
    overrides_path = work_dir / "alignment_overrides.json"
    if overrides_path.is_file():
        overrides = read_json(overrides_path)
    else:
        overrides = {"schema_version": 1, "sessions": {}}
    override_sessions = overrides.setdefault("sessions", {})
    if not isinstance(override_sessions, dict):
        raise ValueError("alignment_overrides.json sessions must be an object")
    for result in all_results:
        alignment = result.get("alignment")
        if not isinstance(alignment, dict):
            continue
        session_id = str(result["session_id"])
        override_sessions[session_id] = {
            key: alignment[key]
            for key in (
                "base_ms",
                "method",
                "precision",
                "support_frames",
                "mean_confidence",
                "uncertainty_ms",
                "evidence",
            )
        }
    overrides["updated_at"] = datetime.now(TIMEZONE).isoformat()
    write_json_atomic(overrides_path, overrides)
    summary = {
        "schema_version": 1,
        "created_at": datetime.now(TIMEZONE).isoformat(),
        "sessions": len(all_results),
        "resolved": sum(
            isinstance(result.get("alignment"), dict)
            for result in all_results
        ),
        "unresolved": [
            result["session_id"]
            for result in all_results
            if not isinstance(result.get("alignment"), dict)
        ],
        "methods": dict(
            collections.Counter(
                str(result["alignment"]["method"])
                for result in all_results
                if isinstance(result.get("alignment"), dict)
            )
        ),
        "overrides": str(overrides_path),
    }
    write_json_atomic(work_dir / "clock_ocr.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


EXACT_RECORDING_TIME_PATTERNS = (
    re.compile(
        r"(?P<year>20\d{2})-(?P<month>\d{2})-(?P<day>\d{2})"
        r"\s+(?P<hour>\d{2})_(?P<minute>\d{2})_(?P<second>\d{2})"
    ),
    re.compile(
        r"(?P<year>20\d{2})年(?P<month>\d{2})月(?P<day>\d{2})日"
        r"\s*(?P<hour>\d{2})点(?P<minute>\d{2})分"
        r"(?P<second>\d{2})秒"
    ),
)
AUDIO_TITLE_CALIBRATION_SOURCE_MID = 1702090167
AUDIO_TITLE_CALIBRATION_BIAS_MS = 1700
AUDIO_TITLE_CALIBRATION_UNCERTAINTY_MS = 5000
AUDIO_TITLE_CALIBRATION_SAMPLES = 9


def exact_recording_title_timestamp(title: str) -> int | None:
    for pattern in EXACT_RECORDING_TIME_PATTERNS:
        match = pattern.search(title)
        if match is None:
            continue
        try:
            value = datetime(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
                int(match.group("hour")),
                int(match.group("minute")),
                int(match.group("second")),
                tzinfo=TIMEZONE,
            )
        except ValueError:
            continue
        return int(value.timestamp())
    return None


def load_audio_alignment_targets(
    db_path: Path,
    work_dir: Path,
    selected_sessions: list[str] | None,
    unresolved_only: bool,
) -> list[dict[str, Any]]:
    manifest = read_json(work_dir / "metadata.json")
    official_by_session: dict[str, dict[str, Any]] = {}
    for recording in manifest["recordings"]:
        session_id = str(recording["session_id"])
        previous = official_by_session.get(session_id)
        if (
            previous is None
            or int(recording["recording_index"])
            < int(previous["recording_index"])
        ):
            official_by_session[session_id] = recording

    selected = set(selected_sessions or ())
    if unresolved_only:
        if selected:
            raise ValueError(
                "--unresolved-only cannot be combined with --session"
            )
        selected = set(
            str(value)
            for value in read_json(work_dir / "clock_ocr.json")[
                "unresolved"
            ]
        )
    elif not selected:
        selected = set(official_by_session)

    result: list[dict[str, Any]] = []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        for session_id in sorted(selected):
            official = official_by_session.get(session_id)
            if official is None:
                continue
            mirrors = list(
                conn.execute(
                    """
                    SELECT
                        bvid, title, duration_seconds, source_priority,
                        source_mid, source_up_name
                    FROM mit3uri_replay_recording
                    WHERE session_id = ? AND source_mid != ?
                    ORDER BY source_priority, bvid
                    """,
                    (session_id, SOURCE_UID),
                )
            )
            candidates = [
                {
                    **dict(row),
                    "title_timestamp": exact_recording_title_timestamp(
                        str(row["title"])
                    ),
                }
                for row in mirrors
            ]
            candidates = [
                value
                for value in candidates
                if value["title_timestamp"] is not None
            ]
            if not candidates:
                continue
            mirror = candidates[0]
            official_page = official["pages"][0]
            result.append(
                {
                    "session_id": session_id,
                    "live_date": official["live_date"],
                    "expected_base_ms": (
                        int(official["start_timestamp"]) * 1000
                    ),
                    "official": {
                        "bvid": str(official["bvid"]),
                        "cid": int(official_page["cid"]),
                        "duration_seconds": int(
                            official_page["duration_seconds"]
                        ),
                    },
                    "mirror": mirror,
                }
            )
    return result


async def load_playurl_audio_urls(
    client: httpx.AsyncClient,
    *,
    bvid: str,
    cid: int,
) -> list[str]:
    payload = await fetch_response(
        client,
        PLAYURL_API,
        params={
            "bvid": bvid,
            "cid": cid,
            "qn": 64,
            "fnval": 16,
            "fourk": 0,
        },
        headers={"Referer": f"https://www.bilibili.com/video/{bvid}"},
        validator=validate_json_api,
    )
    data = json.loads(payload)["data"]
    audios = (data.get("dash") or {}).get("audio") or []
    if not audios:
        raise RuntimeError(f"{bvid} playurl response has no DASH audio")
    audios.sort(
        key=lambda value: int(value.get("bandwidth") or 0),
        reverse=True,
    )
    audio = audios[0]
    urls = [str(audio.get("baseUrl") or audio.get("base_url") or "")]
    urls.extend(
        str(value)
        for value in (
            audio.get("backupUrl") or audio.get("backup_url") or []
        )
    )
    return [value for value in urls if value]


async def load_playurl_video_urls(
    client: httpx.AsyncClient,
    *,
    bvid: str,
    cid: int,
) -> list[str]:
    payload = await fetch_response(
        client,
        PLAYURL_API,
        params={
            "bvid": bvid,
            "cid": cid,
            "qn": 80,
            "fnval": 16,
            "fourk": 0,
        },
        headers={"Referer": f"https://www.bilibili.com/video/{bvid}"},
        validator=validate_json_api,
    )
    data = json.loads(payload)["data"]
    videos = (data.get("dash") or {}).get("video") or []
    if not videos:
        raise RuntimeError(f"{bvid} playurl response has no DASH video")
    videos.sort(
        key=lambda value: (
            int(value.get("height") or 0),
            int(value.get("bandwidth") or 0),
        ),
        reverse=True,
    )
    video = videos[0]
    urls = [str(video.get("baseUrl") or video.get("base_url") or "")]
    urls.extend(
        str(value)
        for value in (
            video.get("backupUrl") or video.get("backup_url") or []
        )
    )
    return [value for value in urls if value]


async def load_secondary_view(
    client: httpx.AsyncClient,
    *,
    bvid: str,
    cache_path: Path,
    refresh: bool,
) -> dict[str, Any]:
    if cache_path.is_file() and not refresh:
        return read_json(cache_path)
    payload = await fetch_response(
        client,
        VIEW_API,
        params={"bvid": bvid},
        validator=validate_json_api,
    )
    view = json.loads(payload)["data"]
    write_json_atomic(cache_path, view)
    return view


async def extract_remote_audio(
    urls: list[str],
    *,
    bvid: str,
    sample_seconds: int,
    output_path: Path,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.stem}.tmp.wav")
    temporary.unlink(missing_ok=True)
    request_headers = (
        f"Referer: https://www.bilibili.com/video/{bvid}\r\n"
        f"User-Agent: {HEADERS['User-Agent']}\r\n"
    )
    errors: list[str] = []
    for url_index, url in enumerate(urls):
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-rw_timeout",
            "30000000",
            "-headers",
            request_headers,
            "-i",
            url,
            "-t",
            str(sample_seconds),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "4000",
            "-c:a",
            "pcm_s16le",
            str(temporary),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=sanitized_subprocess_env(),
        )
        _stdout, stderr = await process.communicate()
        if process.returncode == 0 and temporary.is_file():
            os.replace(temporary, output_path)
            return
        errors.append(
            f"url#{url_index}: "
            f"{stderr.decode('utf-8', errors='replace')[-300:]}"
        )
        temporary.unlink(missing_ok=True)
    raise RuntimeError(
        "ffmpeg audio extraction failed: " + " | ".join(errors)
    )


async def mirror_start_marker_alignment(
    client: httpx.AsyncClient,
    ocr: Any,
    *,
    work_dir: Path,
    bvid: str,
    cid: int,
    live_date: str,
    expected_base_ms: int,
    refresh: bool,
) -> dict[str, Any] | None:
    import cv2

    offsets = (1, 3, 5, 8, 12, 18, 25, 35, 45, 55, 61)
    frame_dir = work_dir / "audio_alignment" / "mirror_frames" / bvid
    urls: list[str] | None = None
    candidates: list[dict[str, Any]] = []
    for offset in offsets:
        frame_path = frame_dir / f"{offset:04d}.jpg"
        if not frame_path.is_file() or refresh:
            if urls is None:
                urls = await load_playurl_video_urls(
                    client,
                    bvid=bvid,
                    cid=cid,
                )
            await extract_remote_frame(
                urls,
                bvid=bvid,
                offset_seconds=offset,
                output_path=frame_path,
            )
        image = cv2.imread(str(frame_path))
        if image is None:
            raise ValueError(f"failed to read frame: {frame_path}")
        height, width = image.shape[:2]
        x_limit = round(width * 0.42)
        y_limit = round(height * 0.42)
        records, _elapsed = ocr(image[:y_limit, :x_limit])
        candidates.extend(
            start_marker_candidates(
                records,
                x_offset=0,
                y_offset=0,
                image_width=width,
                image_height=height,
                live_date=live_date,
                expected_base_ms=expected_base_ms,
                offset_seconds=offset,
                frame_path=str(frame_path),
                min_confidence=0.75,
            )
        )
    alignment = choose_start_marker_alignment(candidates)
    if alignment is None:
        return None
    alignment["method"] = "mirror_ocr_start_marker_second"
    return alignment


def audio_feature_alignment(
    official_path: Path,
    mirror_path: Path,
    *,
    max_lag_seconds: int,
) -> dict[str, Any]:
    import numpy as np
    from scipy import signal
    from scipy.io import wavfile

    official_rate, official_raw = wavfile.read(official_path)
    mirror_rate, mirror_raw = wavfile.read(mirror_path)
    if official_rate != mirror_rate:
        raise ValueError("audio sample rates differ")
    if official_raw.ndim != 1 or mirror_raw.ndim != 1:
        raise ValueError("audio must be mono")
    sample_rate = int(official_rate)
    hop = max(1, sample_rate // 10)
    frame = max(hop * 4, sample_rate // 2)

    def features(raw: Any) -> Any:
        values = np.asarray(raw, dtype=np.float32) / 32768.0
        frequencies, _times, spectrum = signal.spectrogram(
            values,
            fs=sample_rate,
            window="hann",
            nperseg=frame,
            noverlap=frame - hop,
            nfft=frame,
            mode="magnitude",
        )
        bands: list[Any] = []
        for lower, upper in (
            (80, 180),
            (180, 320),
            (320, 560),
            (560, 900),
            (900, 1300),
            (1300, 1900),
        ):
            selected = (frequencies >= lower) & (frequencies < upper)
            band = np.log1p(spectrum[selected].mean(axis=0) * 1000)
            deviation = float(band.std())
            if deviation < 1e-5:
                band = np.zeros_like(band)
            else:
                band = (band - band.mean()) / deviation
            bands.append(band)
        return np.stack(bands)

    official = features(official_raw)
    mirror = features(mirror_raw)
    correlation = np.zeros(
        official.shape[1] + mirror.shape[1] - 1,
        dtype=np.float64,
    )
    for band in range(official.shape[0]):
        correlation += signal.correlate(
            mirror[band],
            official[band],
            mode="full",
            method="fft",
        )
    lags = signal.correlation_lags(
        mirror.shape[1],
        official.shape[1],
        mode="full",
    )
    overlap = np.minimum(
        np.minimum(mirror.shape[1], official.shape[1]),
        np.minimum(mirror.shape[1] - np.maximum(lags, 0),
                   official.shape[1] + np.minimum(lags, 0)),
    )
    valid = (
        (np.abs(lags) <= max_lag_seconds * 10)
        & (overlap >= min(1200, official.shape[1], mirror.shape[1]))
    )
    scores = np.full_like(correlation, -np.inf)
    scores[valid] = (
        correlation[valid]
        / overlap[valid]
        / official.shape[0]
    )
    peak_index = int(np.argmax(scores))
    peak_lag = int(lags[peak_index])
    peak_score = float(scores[peak_index])
    exclusion = np.abs(lags - peak_lag) <= 30
    alternative_scores = scores.copy()
    alternative_scores[exclusion] = -np.inf
    second_score = float(np.max(alternative_scores))
    lag_seconds = peak_lag / 10
    accepted = (
        peak_score >= 0.30
        and peak_score - second_score >= 0.025
    )
    return {
        "lag_feature_frames": peak_lag,
        "lag_seconds": lag_seconds,
        "peak_score": peak_score,
        "second_score": second_score,
        "score_margin": peak_score - second_score,
        "feature_hz": 10,
        "official_feature_frames": int(official.shape[1]),
        "mirror_feature_frames": int(mirror.shape[1]),
        "accepted": accepted,
    }


async def command_audio_align_async(args: argparse.Namespace) -> int:
    from rapidocr_onnxruntime import RapidOCR

    db_path = args.db.resolve()
    work_dir = args.work_dir.resolve()
    if args.sample_seconds < 180:
        raise ValueError("--sample-seconds must be at least 180")
    if args.max_lag_seconds < 1:
        raise ValueError("--max-lag-seconds must be positive")
    targets = load_audio_alignment_targets(
        db_path,
        work_dir,
        args.session,
        args.unresolved_only,
    )
    cookies = build_bilibili_cookies()
    results: list[dict[str, Any]] = []
    cache_root = work_dir / "audio_alignment"
    ocr = RapidOCR()
    async with httpx.AsyncClient(
        headers=HEADERS,
        cookies=cookies,
        timeout=30,
        follow_redirects=True,
        trust_env=False,
    ) as client:
        for index, target in enumerate(targets, 1):
            session_id = str(target["session_id"])
            official = target["official"]
            mirror = target["mirror"]
            mirror_bvid = str(mirror["bvid"])
            result = {**target}
            try:
                view = await load_secondary_view(
                    client,
                    bvid=mirror_bvid,
                    cache_path=cache_root / "views" / f"{mirror_bvid}.json",
                    refresh=args.refresh,
                )
                pages = view.get("pages") or []
                if not pages:
                    raise RuntimeError(f"{mirror_bvid} view has no pages")
                mirror_cid = int(pages[0]["cid"])
                marker_alignment = await mirror_start_marker_alignment(
                    client,
                    ocr,
                    work_dir=work_dir,
                    bvid=mirror_bvid,
                    cid=mirror_cid,
                    live_date=str(target["live_date"]),
                    expected_base_ms=int(target["expected_base_ms"]),
                    refresh=args.refresh,
                )
                if marker_alignment is not None:
                    result["alignment"] = {
                        **marker_alignment,
                        "accepted": True,
                    }
                else:
                    audio_paths: dict[str, Path] = {}
                    for role, bvid, cid in (
                        (
                            "official",
                            str(official["bvid"]),
                            int(official["cid"]),
                        ),
                        ("mirror", mirror_bvid, mirror_cid),
                    ):
                        output_path = (
                            cache_root
                            / "audio"
                            / f"{bvid}-{args.sample_seconds}s.wav"
                        )
                        if not output_path.is_file() or args.refresh:
                            urls = await load_playurl_audio_urls(
                                client,
                                bvid=bvid,
                                cid=cid,
                            )
                            await extract_remote_audio(
                                urls,
                                bvid=bvid,
                                sample_seconds=args.sample_seconds,
                                output_path=output_path,
                            )
                        audio_paths[role] = output_path
                    alignment = audio_feature_alignment(
                        audio_paths["official"],
                        audio_paths["mirror"],
                        max_lag_seconds=args.max_lag_seconds,
                    )
                    fingerprint_accepted = bool(alignment["accepted"])
                    uncalibrated_base_ms = round(
                        (
                            int(mirror["title_timestamp"])
                            + float(alignment["lag_seconds"])
                        )
                        * 1000
                    )
                    calibration_accepted = (
                        fingerprint_accepted
                        and int(mirror["source_mid"])
                        == AUDIO_TITLE_CALIBRATION_SOURCE_MID
                        and args.sample_seconds >= 600
                    )
                    base_ms = uncalibrated_base_ms
                    if calibration_accepted:
                        base_ms -= AUDIO_TITLE_CALIBRATION_BIAS_MS
                    result["alignment"] = {
                        **alignment,
                        "accepted": calibration_accepted,
                        "fingerprint_accepted": fingerprint_accepted,
                        "base_ms": base_ms,
                        "method": (
                            "mirror_title_audio_calibrated_second"
                            if calibration_accepted
                            else "mirror_title_audio_diagnostic"
                        ),
                        "precision": "second",
                        "support_frames": int(
                            min(
                                alignment["official_feature_frames"],
                                alignment["mirror_feature_frames"],
                            )
                        ),
                        "mean_confidence": float(
                            alignment["peak_score"]
                        ),
                        "uncertainty_ms": (
                            AUDIO_TITLE_CALIBRATION_UNCERTAINTY_MS
                            if calibration_accepted
                            else 15_000
                        ),
                        "evidence": [
                            {
                                "official_bvid": official["bvid"],
                                "mirror_bvid": mirror_bvid,
                                "mirror_title": mirror["title"],
                                "mirror_title_timestamp": mirror[
                                    "title_timestamp"
                                ],
                                "official_audio": str(
                                    audio_paths["official"]
                                ),
                                "mirror_audio": str(
                                    audio_paths["mirror"]
                                ),
                                "lag_seconds": alignment["lag_seconds"],
                                "uncalibrated_base_ms": (
                                    uncalibrated_base_ms
                                ),
                                "calibration_source_mid": (
                                    AUDIO_TITLE_CALIBRATION_SOURCE_MID
                                ),
                                "calibration_bias_ms": (
                                    AUDIO_TITLE_CALIBRATION_BIAS_MS
                                ),
                                "calibration_uncertainty_ms": (
                                    AUDIO_TITLE_CALIBRATION_UNCERTAINTY_MS
                                ),
                                "calibration_samples": (
                                    AUDIO_TITLE_CALIBRATION_SAMPLES
                                ),
                                "calibration_residual_range_ms": [
                                    -2700,
                                    3900,
                                ],
                                "peak_score": alignment["peak_score"],
                                "score_margin": alignment[
                                    "score_margin"
                                ],
                            }
                        ],
                    }
            except Exception as exc:
                result["error"] = str(exc)
            results.append(result)
            value = result.get("alignment") or {}
            print(
                f"audio_align={index}/{len(targets)} "
                f"session={session_id} "
                f"lag={value.get('lag_seconds', 'error')} "
                f"score={value.get('peak_score', 0):.3f} "
                f"accepted={value.get('accepted', False)}",
                flush=True,
            )

    output = {
        "schema_version": 1,
        "created_at": datetime.now(TIMEZONE).isoformat(),
        "sample_seconds": args.sample_seconds,
        "max_lag_seconds": args.max_lag_seconds,
        "sessions": results,
    }
    write_json_atomic(cache_root / "results.json", output)
    accepted_results = [
        value
        for value in results
        if isinstance(value.get("alignment"), dict)
        and bool(value["alignment"].get("accepted"))
    ]
    if args.accept:
        overrides_path = work_dir / "alignment_overrides.json"
        overrides = read_json(overrides_path)
        override_sessions = overrides.setdefault("sessions", {})
        for value in accepted_results:
            alignment = value["alignment"]
            override_sessions[str(value["session_id"])] = {
                key: alignment[key]
                for key in (
                    "base_ms",
                    "method",
                    "precision",
                    "support_frames",
                    "mean_confidence",
                    "uncertainty_ms",
                    "evidence",
                )
            }
        overrides["updated_at"] = datetime.now(TIMEZONE).isoformat()
        write_json_atomic(overrides_path, overrides)
    summary = {
        "targets": len(targets),
        "aligned": sum(
            isinstance(value.get("alignment"), dict) for value in results
        ),
        "accepted": len(accepted_results),
        "errors": sum("error" in value for value in results),
        "overrides_updated": bool(args.accept),
        "output": str(cache_root / "results.json"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not summary["errors"] else 1


def command_audio_align(args: argparse.Namespace) -> int:
    return asyncio.run(command_audio_align_async(args))


def build_uid_hash_map(conn: sqlite3.Connection) -> dict[str, int]:
    result: dict[str, list[int]] = collections.defaultdict(list)
    for (raw_uid,) in conn.execute(
        "SELECT DISTINCT uid FROM event WHERE uid IS NOT NULL AND uid > 0"
    ):
        uid = int(raw_uid)
        crc = zlib.crc32(str(uid).encode()) & 0xFFFFFFFF
        result[normalize_hash(format(crc, "x"))].append(uid)
    collisions = {key: value for key, value in result.items() if len(value) > 1}
    if collisions:
        raise RuntimeError(f"UID CRC32 collisions detected: {collisions}")
    return {key: value[0] for key, value in result.items()}


def load_session_dm(
    work_dir: Path,
    recordings: list[dict[str, Any]],
    uid_by_hash: dict[str, int],
) -> list[DmRow]:
    result: list[DmRow] = []
    seen: set[tuple[int, str]] = set()
    for recording in recordings:
        for page in recording["pages"]:
            cid = int(page["cid"])
            for segment_index in range(1, int(page["total_segments"]) + 1):
                path = (
                    work_dir
                    / "segments"
                    / str(cid)
                    / f"{segment_index:04d}.pb"
                )
                if not path.is_file():
                    raise FileNotFoundError(path)
                rows = parse_dm_segment(
                    path.read_bytes(),
                    session_id=str(recording["session_id"]),
                    bvid=str(recording["bvid"]),
                    cid=cid,
                    session_offset_ms=int(page["session_offset_ms"]),
                    uid_by_hash=uid_by_hash,
                )
                for row in rows:
                    key = (row.cid, row.dm_id)
                    if key in seen:
                        continue
                    seen.add(key)
                    result.append(row)
    return sorted(
        result,
        key=lambda row: (
            row.session_progress_ms,
            row.cid,
            row.dm_id,
        ),
    )


def load_existing_session_rows(
    conn: sqlite3.Connection,
    *,
    expected_base_ms: int,
    duration_ms: int,
) -> list[dict[str, Any]]:
    since = expected_base_ms // 1000 - 3 * 3600
    until = (expected_base_ms + duration_ms) // 1000 + 3 * 3600
    return [
        {
            "id": int(row[0]),
            "uid": int(row[1]),
            "content": normalize_content(str(row[2])),
            "timestamp_ms": int(row[3]) * 1000,
        }
        for row in conn.execute(
            """
            SELECT id, uid, content, timestamp
            FROM event
            WHERE room_id = ? AND cmd = 'DANMU_MSG'
              AND uid IS NOT NULL
              AND timestamp BETWEEN ? AND ?
            ORDER BY timestamp, id
            """,
            (ROOM_ID, since, until),
        )
    ]


def alignment_candidates(
    dm_rows: list[DmRow],
    existing_rows: list[dict[str, Any]],
    expected_base_ms: int,
) -> list[tuple[int, tuple[int, str]]]:
    official: dict[tuple[int, str], list[int]] = collections.defaultdict(list)
    for row in dm_rows:
        if row.uid is not None:
            official[(row.uid, normalize_content(row.content))].append(
                row.session_progress_ms
            )
    existing: dict[tuple[int, str], list[int]] = collections.defaultdict(list)
    for row in existing_rows:
        existing[(row["uid"], row["content"])].append(row["timestamp_ms"])
    result: list[tuple[int, tuple[int, str]]] = []
    for key, progresses in official.items():
        timestamps = existing.get(key, [])
        if not timestamps or len(progresses) * len(timestamps) > 25:
            continue
        for progress_ms in progresses:
            for timestamp_ms in timestamps:
                base_ms = timestamp_ms - progress_ms
                if abs(base_ms - expected_base_ms) <= 3 * 3600 * 1000:
                    result.append((base_ms, key))
    return result


def choose_alignment(
    candidates: list[tuple[int, tuple[int, str]]],
    *,
    expected_base_ms: int,
    precision: str,
) -> dict[str, Any]:
    if not candidates:
        return {
            "base_ms": expected_base_ms,
            "method": f"catalog_{precision}",
            "anchor_keys": 0,
            "candidate_pairs": 0,
            "delta_ms": 0,
        }
    bins: dict[int, set[tuple[int, str]]] = collections.defaultdict(set)
    bin_pair_counts: collections.Counter[int] = collections.Counter()
    for base_ms, key in candidates:
        second = round(base_ms / 1000)
        bins[second].add(key)
        bin_pair_counts[second] += 1
    scores: list[tuple[int, int, int]] = []
    for center in bins:
        keys: set[tuple[int, str]] = set()
        pair_count = 0
        for value in range(center - 4, center + 5):
            keys.update(bins.get(value, set()))
            pair_count += bin_pair_counts[value]
        scores.append((len(keys), pair_count, center))
    _key_count, _pair_count, center = max(
        scores,
        key=lambda value: (
            value[0],
            value[1],
            -abs(value[2] * 1000 - expected_base_ms),
        ),
    )
    per_key: dict[tuple[int, str], list[int]] = collections.defaultdict(list)
    for base_ms, key in candidates:
        if abs(base_ms - center * 1000) <= 4000:
            per_key[key].append(base_ms)
    key_centers = [
        round(statistics.median(values)) for values in per_key.values()
    ]
    if len(key_centers) < 3:
        return {
            "base_ms": expected_base_ms,
            "method": f"catalog_{precision}",
            "anchor_keys": len(key_centers),
            "candidate_pairs": len(candidates),
            "delta_ms": 0,
        }
    base_ms = round(statistics.median(key_centers))
    return {
        "base_ms": base_ms,
        "method": "matched_uid_content",
        "anchor_keys": len(key_centers),
        "candidate_pairs": len(candidates),
        "delta_ms": base_ms - expected_base_ms,
    }


def match_existing_rows(
    dm_rows: list[DmRow],
    existing_rows: list[dict[str, Any]],
    *,
    base_ms: int,
    tolerance_ms: int,
) -> dict[tuple[int, str], int]:
    official: dict[tuple[int, str], list[DmRow]] = collections.defaultdict(list)
    for row in dm_rows:
        if row.uid is not None:
            official[(row.uid, normalize_content(row.content))].append(row)
    existing: dict[
        tuple[int, str], list[dict[str, Any]]
    ] = collections.defaultdict(list)
    for row in existing_rows:
        existing[(row["uid"], row["content"])].append(row)

    matched: dict[tuple[int, str], int] = {}
    for key, official_rows in official.items():
        source_rows = sorted(
            official_rows,
            key=lambda row: base_ms + row.session_progress_ms,
        )
        database_rows = sorted(
            existing.get(key, []),
            key=lambda row: (row["timestamp_ms"], row["id"]),
        )
        source_index = 0
        database_index = 0
        while (
            source_index < len(source_rows)
            and database_index < len(database_rows)
        ):
            source = source_rows[source_index]
            database = database_rows[database_index]
            source_time = base_ms + source.session_progress_ms
            difference = database["timestamp_ms"] - source_time
            if abs(difference) <= tolerance_ms:
                matched[(source.cid, source.dm_id)] = database["id"]
                source_index += 1
                database_index += 1
            elif difference < -tolerance_ms:
                database_index += 1
            else:
                source_index += 1
    return matched


def resolve_uname(
    conn: sqlite3.Connection,
    cache: dict[int, str | None],
    uid: int,
    cutoff: int,
) -> str | None:
    if uid in cache:
        return cache[uid]
    row = conn.execute(
        """
        SELECT uname FROM name_history
        WHERE uid = ? AND first_seen < ?
        ORDER BY first_seen DESC
        LIMIT 1
        """,
        (uid, cutoff),
    ).fetchone()
    if row is None:
        row = conn.execute(
            """
            SELECT uname FROM event
            WHERE uid = ? AND uname IS NOT NULL AND uname != ''
            ORDER BY id DESC
            LIMIT 1
            """,
            (uid,),
        ).fetchone()
    value = str(row[0]) if row and row[0] else None
    cache[uid] = value
    return value


def create_audit_db(
    path: Path,
    *,
    resume: bool = False,
) -> sqlite3.Connection:
    if path.exists() and resume:
        conn = sqlite3.connect(path)
        required = {"source_row", "session_alignment"}
        present = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if not required.issubset(present):
            conn.close()
            raise RuntimeError("existing audit.db cannot be resumed")
        return conn
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.executescript(
        """
        CREATE TABLE source_row (
            id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            bvid TEXT NOT NULL,
            cid INTEGER NOT NULL,
            dm_id TEXT NOT NULL,
            progress_ms INTEGER NOT NULL,
            session_progress_ms INTEGER NOT NULL,
            mid_hash TEXT NOT NULL,
            content TEXT NOT NULL,
            ctime INTEGER NOT NULL,
            uid INTEGER,
            uname TEXT,
            mapped_timestamp_ms INTEGER NOT NULL,
            action TEXT NOT NULL,
            duplicate_event_id INTEGER,
            alignment_method TEXT NOT NULL,
            UNIQUE(cid, dm_id)
        );
        CREATE INDEX idx_source_action ON source_row(action);
        CREATE INDEX idx_source_time ON source_row(mapped_timestamp_ms);
        CREATE TABLE session_alignment (
            session_id TEXT PRIMARY KEY,
            live_date TEXT NOT NULL,
            expected_base_ms INTEGER NOT NULL,
            aligned_base_ms INTEGER NOT NULL,
            method TEXT NOT NULL,
            anchor_keys INTEGER NOT NULL,
            candidate_pairs INTEGER NOT NULL,
            delta_ms INTEGER NOT NULL,
            source_rows INTEGER NOT NULL,
            existing_matches INTEGER NOT NULL
        );
        """
    )
    return conn


TRUSTED_OVERRIDE_METHOD_PREFIXES = (
    "activity_",
    "ocr_",
    "mirror_ocr_",
    "mirror_title_audio_calibrated_",
)
LOCKED_ALIGNMENT_METHODS = {
    "activity_manual_second",
    "catalog_second",
    "matched_uid_content",
    "mirror_title_audio_calibrated_second",
    "ocr_clock_second",
    "ocr_start_marker_second",
}


def alignment_from_record(
    value: Any,
    *,
    expected_base_ms: int,
    allowed: Callable[[str], bool],
    dynamic_alignment: dict[str, Any],
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    method = str(value.get("method") or "")
    if not allowed(method):
        return None
    base_ms = int(value["base_ms"])
    return {
        "base_ms": base_ms,
        "method": method,
        "anchor_keys": int(
            value.get("anchor_keys", dynamic_alignment["anchor_keys"])
        ),
        "candidate_pairs": int(
            value.get(
                "candidate_pairs",
                dynamic_alignment["candidate_pairs"],
            )
        ),
        "delta_ms": base_ms - expected_base_ms,
    }


def command_audit(args: argparse.Namespace) -> int:
    db_path = args.db.resolve()
    work_dir = args.work_dir.resolve()
    manifest = read_json(work_dir / "metadata.json")
    overrides_path = work_dir / "alignment_overrides.json"
    overrides = (
        read_json(overrides_path).get("sessions", {})
        if overrides_path.is_file()
        else {}
    )
    if not isinstance(overrides, dict):
        raise ValueError("alignment_overrides.json sessions must be an object")
    locks_path = work_dir / "alignment_locks.json"
    locks = (
        read_json(locks_path).get("sessions", {})
        if (
            locks_path.is_file()
            and not bool(
                getattr(args, "ignore_alignment_locks", False)
            )
        )
        else {}
    )
    if not isinstance(locks, dict):
        raise ValueError("alignment_locks.json sessions must be an object")
    cutoff_timestamp = int(manifest["cutoff_timestamp_exclusive"])
    cutoff_ms = cutoff_timestamp * 1000
    recordings_by_session: dict[
        str, list[dict[str, Any]]
    ] = collections.defaultdict(list)
    for recording in manifest["recordings"]:
        recordings_by_session[str(recording["session_id"])].append(recording)

    production = sqlite3.connect(db_path)
    production.execute("PRAGMA query_only = ON")
    uid_by_hash = build_uid_hash_map(production)
    audit_path = work_dir / "audit.db"
    staging = create_audit_db(
        audit_path,
        resume=bool(getattr(args, "resume", False)),
    )
    completed_sessions = {
        str(row[0])
        for row in staging.execute(
            "SELECT session_id FROM session_alignment"
        )
    }
    if completed_sessions:
        print(
            f"resuming audit with {len(completed_sessions)} "
            "completed sessions",
            flush=True,
        )
    uname_cache: dict[int, str | None] = {}
    totals: collections.Counter[str] = collections.Counter()
    method_counts: collections.Counter[str] = collections.Counter()
    session_reports: list[dict[str, Any]] = []
    try:
        for session_index, (session_id, recordings) in enumerate(
            recordings_by_session.items(),
            1,
        ):
            if session_id in completed_sessions:
                continue
            recordings.sort(key=lambda value: value["recording_index"])
            first = recordings[0]
            dm_rows = load_session_dm(work_dir, recordings, uid_by_hash)
            expected_base_ms = int(first["start_timestamp"]) * 1000
            duration_ms = max(
                (
                    int(page["session_offset_ms"])
                    + int(page["duration_seconds"]) * 1000
                    for recording in recordings
                    for page in recording["pages"]
                ),
                default=int(first["session_duration_seconds"] or 0) * 1000,
            )
            existing_rows = load_existing_session_rows(
                production,
                expected_base_ms=expected_base_ms,
                duration_ms=duration_ms,
            )
            candidates = alignment_candidates(
                dm_rows,
                existing_rows,
                expected_base_ms,
            )
            alignment = choose_alignment(
                candidates,
                expected_base_ms=expected_base_ms,
                precision=str(first["start_time_precision"]),
            )
            override = overrides.get(session_id)
            locked = locks.get(session_id)
            fixed_alignment = alignment_from_record(
                override,
                expected_base_ms=expected_base_ms,
                allowed=lambda method: method.startswith(
                    TRUSTED_OVERRIDE_METHOD_PREFIXES
                ),
                dynamic_alignment=alignment,
            )
            if fixed_alignment is None:
                fixed_alignment = alignment_from_record(
                    locked,
                    expected_base_ms=expected_base_ms,
                    allowed=lambda method: (
                        method in LOCKED_ALIGNMENT_METHODS
                    ),
                    dynamic_alignment=alignment,
                )
            if fixed_alignment is not None:
                # Independently verified wall-clock anchors and alignment
                # locks are immutable. Letting imported source rows replace
                # them with a fresh content-derived alignment makes repeat
                # runs drift.
                alignment = fixed_alignment
            alignment_resolved = (
                alignment["method"] != "catalog_hour"
            )
            matched = match_existing_rows(
                dm_rows,
                existing_rows,
                base_ms=int(alignment["base_ms"]),
                tolerance_ms=args.duplicate_tolerance_ms,
            )
            rows_to_insert: list[tuple[Any, ...]] = []
            for row in dm_rows:
                mapped_ms = (
                    int(alignment["base_ms"]) + row.session_progress_ms
                )
                duplicate_event_id = matched.get((row.cid, row.dm_id))
                if mapped_ms >= cutoff_ms:
                    action = "outside_cutoff"
                elif duplicate_event_id is not None:
                    action = "existing"
                elif not alignment_resolved:
                    action = "unresolved_alignment"
                elif row.uid is None:
                    action = "unknown_uid"
                else:
                    action = "new"
                uname = (
                    resolve_uname(
                        production,
                        uname_cache,
                        row.uid,
                        mapped_ms // 1000,
                    )
                    if row.uid is not None
                    else None
                )
                rows_to_insert.append(
                    (
                        row.session_id,
                        row.bvid,
                        row.cid,
                        row.dm_id,
                        row.progress_ms,
                        row.session_progress_ms,
                        row.mid_hash,
                        row.content,
                        row.ctime,
                        row.uid,
                        uname,
                        mapped_ms,
                        action,
                        duplicate_event_id,
                        str(alignment["method"]),
                    )
                )
                totals[action] += 1
                totals["resolved_uid" if row.uid is not None else "unknown_uid"] += 1
                totals[
                    "resolved_uname" if uname is not None else "unknown_uname"
                ] += 1
            staging.executemany(
                """
                INSERT INTO source_row (
                    session_id, bvid, cid, dm_id, progress_ms,
                    session_progress_ms, mid_hash, content, ctime,
                    uid, uname, mapped_timestamp_ms, action,
                    duplicate_event_id, alignment_method
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows_to_insert,
            )
            staging.execute(
                """
                INSERT INTO session_alignment (
                    session_id, live_date, expected_base_ms,
                    aligned_base_ms, method, anchor_keys,
                    candidate_pairs, delta_ms, source_rows,
                    existing_matches
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    str(first["live_date"]),
                    expected_base_ms,
                    int(alignment["base_ms"]),
                    str(alignment["method"]),
                    int(alignment["anchor_keys"]),
                    int(alignment["candidate_pairs"]),
                    int(alignment["delta_ms"]),
                    len(dm_rows),
                    len(matched),
                ),
            )
            staging.commit()
            method_counts[str(alignment["method"])] += 1
            session_report = {
                "session_id": session_id,
                "live_date": first["live_date"],
                "source_rows": len(dm_rows),
                "existing_matches": len(matched),
                **alignment,
            }
            session_reports.append(session_report)
            if (
                session_index % 25 == 0
                or session_index == len(recordings_by_session)
            ):
                print(
                    f"audit sessions={session_index}/"
                    f"{len(recordings_by_session)} "
                    f"rows={totals['new'] + totals['existing'] + totals['outside_cutoff']}",
                    flush=True,
                )
        action_counts = collections.Counter(
            {
                str(row[0]): int(row[1])
                for row in staging.execute(
                    "SELECT action, COUNT(*) FROM source_row GROUP BY action"
                )
            }
        )
        identity_counts: collections.Counter[str] = collections.Counter()
        identity_counts["resolved_uid"] = int(
            staging.execute(
                "SELECT COUNT(*) FROM source_row WHERE uid IS NOT NULL"
            ).fetchone()[0]
        )
        identity_counts["unknown_uid"] = int(
            staging.execute(
                "SELECT COUNT(*) FROM source_row WHERE uid IS NULL"
            ).fetchone()[0]
        )
        identity_counts["resolved_uname"] = int(
            staging.execute(
                "SELECT COUNT(*) FROM source_row WHERE uname IS NOT NULL"
            ).fetchone()[0]
        )
        identity_counts["unknown_uname"] = int(
            staging.execute(
                "SELECT COUNT(*) FROM source_row WHERE uname IS NULL"
            ).fetchone()[0]
        )
        method_counts = collections.Counter(
            {
                str(row[0]): int(row[1])
                for row in staging.execute(
                    """
                    SELECT method, COUNT(*)
                    FROM session_alignment
                    GROUP BY method
                    """
                )
            }
        )
        session_reports = [
            {
                "session_id": str(row[0]),
                "live_date": str(row[1]),
                "expected_base_ms": int(row[2]),
                "base_ms": int(row[3]),
                "method": str(row[4]),
                "anchor_keys": int(row[5]),
                "candidate_pairs": int(row[6]),
                "delta_ms": int(row[7]),
                "source_rows": int(row[8]),
                "existing_matches": int(row[9]),
            }
            for row in staging.execute(
                """
                SELECT
                    session_id, live_date, expected_base_ms,
                    aligned_base_ms, method, anchor_keys,
                    candidate_pairs, delta_ms, source_rows,
                    existing_matches
                FROM session_alignment
                ORDER BY live_date, session_id
                """
            )
        ]
        quick_check = str(staging.execute("PRAGMA quick_check").fetchone()[0])
    finally:
        staging.close()
        production.close()

    source_rows = sum(
        int(report["source_rows"]) for report in session_reports
    )
    report = {
        "schema_version": 1,
        "created_at": datetime.now(TIMEZONE).isoformat(),
        "status": "ready" if quick_check == "ok" else "failed",
        "db_path": str(db_path),
        "metadata": str(work_dir / "metadata.json"),
        "audit_db": str(audit_path),
        "cutoff_timestamp_exclusive": cutoff_timestamp,
        "duplicate_tolerance_ms": args.duplicate_tolerance_ms,
        "sessions": len(session_reports),
        "source_rows": source_rows,
        "actions": {
            key: int(action_counts[key])
            for key in (
                "new",
                "existing",
                "outside_cutoff",
                "unresolved_alignment",
                "unknown_uid",
            )
        },
        "identity": {
            key: int(identity_counts[key])
            for key in (
                "resolved_uid",
                "unknown_uid",
                "resolved_uname",
                "unknown_uname",
            )
        },
        "alignment_methods": dict(sorted(method_counts.items())),
        "quick_check": quick_check,
        "session_reports": session_reports,
    }
    write_json_atomic(work_dir / "audit.json", report)
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "status",
                    "sessions",
                    "source_rows",
                    "actions",
                    "identity",
                    "alignment_methods",
                    "quick_check",
                )
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["status"] == "ready" else 1


def write_alignment_locks(
    report: dict[str, Any],
    *,
    work_dir: Path,
) -> dict[str, Any]:
    sessions: dict[str, dict[str, Any]] = {}
    for value in report["session_reports"]:
        method = str(value["method"])
        if method not in LOCKED_ALIGNMENT_METHODS:
            continue
        session_id = str(value["session_id"])
        if session_id in sessions:
            raise RuntimeError(f"duplicate alignment lock: {session_id}")
        sessions[session_id] = {
            "live_date": str(value["live_date"]),
            "base_ms": int(value["base_ms"]),
            "method": method,
            "anchor_keys": int(value["anchor_keys"]),
            "candidate_pairs": int(value["candidate_pairs"]),
        }
    expected = sum(
        int(count)
        for method, count in report["alignment_methods"].items()
        if method in LOCKED_ALIGNMENT_METHODS
    )
    if len(sessions) != expected:
        raise RuntimeError(
            "alignment lock count mismatch: "
            f"sessions={len(sessions)} expected={expected}"
        )
    payload = {
        "schema_version": 1,
        "created_at": datetime.now(TIMEZONE).isoformat(),
        "db_path": str(report["db_path"]),
        "cutoff_timestamp_exclusive": int(
            report["cutoff_timestamp_exclusive"]
        ),
        "duplicate_tolerance_ms": int(
            report["duplicate_tolerance_ms"]
        ),
        "sessions": sessions,
    }
    path = work_dir / "alignment_locks.json"
    write_json_atomic(path, payload)
    return {
        "path": str(path),
        "sessions": len(sessions),
        "methods": dict(
            sorted(
                collections.Counter(
                    value["method"] for value in sessions.values()
                ).items()
            )
        ),
    }


def command_lock_alignments(args: argparse.Namespace) -> int:
    if not args.yes:
        raise ValueError(
            "lock-alignments replaces alignment_locks.json; pass --yes"
        )
    db_path = args.db.resolve()
    work_dir = args.work_dir.resolve()
    audit_args = argparse.Namespace(
        db=db_path,
        work_dir=work_dir,
        duplicate_tolerance_ms=5000,
        ignore_alignment_locks=True,
    )
    if command_audit(audit_args) != 0:
        raise RuntimeError("fresh audit failed")
    report = read_json(work_dir / "audit.json")
    if report.get("status") != "ready" or report.get("quick_check") != "ok":
        raise RuntimeError("fresh audit is not ready")
    result = write_alignment_locks(report, work_dir=work_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


PROVENANCE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS mit3uri_replay_danmaku_provenance (
    source TEXT NOT NULL,
    cid INTEGER NOT NULL,
    dm_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    bvid TEXT NOT NULL,
    progress_ms INTEGER NOT NULL,
    session_progress_ms INTEGER NOT NULL,
    mid_hash TEXT NOT NULL,
    ctime INTEGER NOT NULL,
    mapped_timestamp_ms INTEGER NOT NULL,
    alignment_method TEXT NOT NULL,
    match_kind TEXT NOT NULL
        CHECK (match_kind IN ('existing', 'inserted')),
    event_id INTEGER NOT NULL,
    imported_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (source, cid, dm_id),
    UNIQUE (event_id),
    FOREIGN KEY (event_id) REFERENCES event(id)
);
CREATE INDEX IF NOT EXISTS idx_replay_dm_provenance_session
ON mit3uri_replay_danmaku_provenance(session_id, mapped_timestamp_ms);
"""


def backup_sqlite_database(source_path: Path, destination: Path) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(destination)
    source = sqlite3.connect(
        f"file:{source_path}?mode=ro",
        uri=True,
        timeout=60,
    )
    target = sqlite3.connect(destination)
    last_reported = -1

    def progress(_status: int, remaining: int, total: int) -> None:
        nonlocal last_reported
        completed = total - remaining
        percent = int(completed * 100 / total) if total else 100
        bucket = percent // 10
        if bucket != last_reported:
            last_reported = bucket
            print(
                f"backup={min(100, bucket * 10)}% "
                f"pages={completed}/{total}",
                flush=True,
            )

    try:
        source.backup(
            target,
            # A paged backup restarts whenever the live WAL database is
            # written, so a busy bot may never progress past the first
            # batches. Copy one consistent snapshot in a single step.
            pages=-1,
            progress=progress,
        )
        quick_check = str(target.execute("PRAGMA quick_check").fetchone()[0])
    finally:
        target.close()
        source.close()
    return quick_check


def command_apply(args: argparse.Namespace) -> int:
    if not args.yes:
        raise ValueError("apply modifies the target database; pass --yes")
    if args.batch_size < 100 or args.batch_size > 50_000:
        raise ValueError("--batch-size must be between 100 and 50000")

    db_path = args.db.resolve()
    work_dir = args.work_dir.resolve()
    backup_dir = args.backup_dir.resolve()
    audit_args = argparse.Namespace(
        db=db_path,
        work_dir=work_dir,
        duplicate_tolerance_ms=5000,
    )
    print("preflight: rebuilding the 5-second audit", flush=True)
    if command_audit(audit_args) != 0:
        raise RuntimeError("fresh audit failed")
    report = read_json(work_dir / "audit.json")
    if report.get("status") != "ready" or report.get("quick_check") != "ok":
        raise RuntimeError("fresh audit is not ready")
    if int(report.get("duplicate_tolerance_ms") or 0) != 5000:
        raise RuntimeError("fresh audit did not use the required 5s window")
    if Path(str(report["db_path"])).resolve() != db_path:
        raise RuntimeError("audit database path does not match apply target")

    audit_path = Path(str(report["audit_db"])).resolve()
    with sqlite3.connect(audit_path) as staging_check:
        staging_quick_check = str(
            staging_check.execute("PRAGMA quick_check").fetchone()[0]
        )
        staged_new = int(
            staging_check.execute(
                "SELECT COUNT(*) FROM source_row WHERE action = 'new'"
            ).fetchone()[0]
        )
        staged_existing = int(
            staging_check.execute(
                "SELECT COUNT(*) FROM source_row WHERE action = 'existing'"
            ).fetchone()[0]
        )
        duplicate_existing_event_ids = int(
            staging_check.execute(
                """
                SELECT COUNT(*)
                FROM (
                    SELECT duplicate_event_id
                    FROM source_row
                    WHERE action = 'existing'
                    GROUP BY duplicate_event_id
                    HAVING COUNT(*) > 1
                )
                """
            ).fetchone()[0]
        )
    if staging_quick_check != "ok":
        raise RuntimeError(
            f"staging database quick_check={staging_quick_check}"
        )
    if duplicate_existing_event_ids:
        raise RuntimeError(
            "multiple official source rows map to the same existing event: "
            f"{duplicate_existing_event_ids} duplicate event IDs"
        )

    alignment_locks = write_alignment_locks(
        report,
        work_dir=work_dir,
    )

    with sqlite3.connect(
        f"file:{db_path}?mode=ro",
        uri=True,
        timeout=60,
    ) as production_check:
        before_quick_check = str(
            production_check.execute("PRAGMA quick_check").fetchone()[0]
        )
    if before_quick_check != "ok":
        raise RuntimeError(
            f"production database quick_check={before_quick_check}"
        )

    backup_path: Path | None = None
    backup_quick_check: str | None = None
    if not args.no_backup:
        required_bytes = round(db_path.stat().st_size * 1.10)
        free_bytes = shutil.disk_usage(backup_dir.parent).free
        if free_bytes < required_bytes:
            raise RuntimeError(
                "insufficient free space for full backup: "
                f"required={required_bytes} free={free_bytes}"
            )
        timestamp = datetime.now(TIMEZONE).strftime("%Y%m%d-%H%M%S")
        backup_path = (
            backup_dir
            / f"libot-before-official-replay-danmaku-{timestamp}.db"
        )
        print(f"creating full backup: {backup_path}", flush=True)
        backup_quick_check = backup_sqlite_database(
            db_path,
            backup_path,
        )
        if backup_quick_check != "ok":
            raise RuntimeError(
                f"backup quick_check={backup_quick_check}"
            )

    staging = sqlite3.connect(
        f"file:{audit_path}?mode=ro",
        uri=True,
    )
    staging.row_factory = sqlite3.Row
    production = sqlite3.connect(db_path, timeout=120)
    production.execute("PRAGMA foreign_keys = ON")
    production.executescript(PROVENANCE_TABLE_SQL)
    provenance_columns = {
        str(row[1])
        for row in production.execute(
            "PRAGMA table_info(mit3uri_replay_danmaku_provenance)"
        )
    }
    if "match_kind" not in provenance_columns:
        production.execute(
            """
            ALTER TABLE mit3uri_replay_danmaku_provenance
            ADD COLUMN match_kind TEXT NOT NULL DEFAULT 'inserted'
                CHECK (match_kind IN ('existing', 'inserted'))
            """
        )
    production.commit()
    provenance_count_before = int(
        production.execute(
            "SELECT COUNT(*) FROM mit3uri_replay_danmaku_provenance"
        ).fetchone()[0]
    )
    existing_provenance: set[tuple[int, str]] = set()
    if provenance_count_before:
        existing_provenance = {
            (int(row[0]), str(row[1]))
            for row in production.execute(
                """
                SELECT cid, dm_id
                FROM mit3uri_replay_danmaku_provenance
                WHERE source = 'bilibili_official_replay'
                """
            )
        }

    linked_existing = 0
    skipped_existing_provenance = 0
    existing_cursor = staging.execute(
        """
        SELECT
            session_id, bvid, cid, dm_id, progress_ms,
            session_progress_ms, mid_hash, ctime,
            mapped_timestamp_ms, alignment_method,
            duplicate_event_id
        FROM source_row
        WHERE action = 'existing'
        ORDER BY id
        """
    )
    while True:
        rows = existing_cursor.fetchmany(args.batch_size)
        if not rows:
            break
        provenance_rows: list[tuple[Any, ...]] = []
        provenance_keys: list[tuple[int, str]] = []
        for row in rows:
            source_key = (int(row["cid"]), str(row["dm_id"]))
            if source_key in existing_provenance:
                skipped_existing_provenance += 1
                continue
            duplicate_event_id = row["duplicate_event_id"]
            if duplicate_event_id is None:
                raise RuntimeError(
                    "existing source row has no duplicate_event_id: "
                    f"cid={source_key[0]} dm_id={source_key[1]}"
                )
            provenance_rows.append(
                (
                    int(row["cid"]),
                    str(row["dm_id"]),
                    str(row["session_id"]),
                    str(row["bvid"]),
                    int(row["progress_ms"]),
                    int(row["session_progress_ms"]),
                    str(row["mid_hash"]),
                    int(row["ctime"]),
                    int(row["mapped_timestamp_ms"]),
                    str(row["alignment_method"]),
                    int(duplicate_event_id),
                )
            )
            provenance_keys.append(source_key)
        if provenance_rows:
            production.execute("BEGIN IMMEDIATE")
            try:
                production.executemany(
                    """
                    INSERT INTO mit3uri_replay_danmaku_provenance (
                        source, cid, dm_id, session_id, bvid,
                        progress_ms, session_progress_ms, mid_hash,
                        ctime, mapped_timestamp_ms, alignment_method,
                        match_kind, event_id
                    ) VALUES (
                        'bilibili_official_replay', ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, 'existing', ?
                    )
                    """,
                    provenance_rows,
                )
                production.commit()
            except Exception:
                production.rollback()
                raise
            existing_provenance.update(provenance_keys)
            linked_existing += len(provenance_rows)
        print(
            f"apply linked_existing={linked_existing}/"
            f"{staged_existing} "
            "skipped_existing_provenance="
            f"{skipped_existing_provenance}",
            flush=True,
        )

    inserted = 0
    skipped_provenance = 0
    source_cursor = staging.execute(
        """
        SELECT
            session_id, bvid, cid, dm_id, progress_ms,
            session_progress_ms, mid_hash, content, ctime,
            uid, uname, mapped_timestamp_ms, alignment_method
        FROM source_row
        WHERE action = 'new' AND uid IS NOT NULL
        ORDER BY id
        """
    )
    try:
        while True:
            rows = source_cursor.fetchmany(args.batch_size)
            if not rows:
                break
            production.execute("BEGIN IMMEDIATE")
            try:
                name_rows: set[tuple[int, str, int]] = set()
                for row in rows:
                    source_key = (int(row["cid"]), str(row["dm_id"]))
                    if source_key in existing_provenance:
                        skipped_provenance += 1
                        continue
                    timestamp = int(row["mapped_timestamp_ms"]) // 1000
                    event_id = int(
                        production.execute(
                            """
                            INSERT INTO event (
                                room_id, cmd, uid, uname, content,
                                gift_name, gift_num, total_coin,
                                title, timestamp
                            ) VALUES (?, 'DANMU_MSG', ?, ?, ?, NULL,
                                      NULL, NULL, NULL, ?)
                            RETURNING id
                            """,
                            (
                                ROOM_ID,
                                int(row["uid"]),
                                row["uname"],
                                str(row["content"]),
                                timestamp,
                            ),
                        ).fetchone()[0]
                    )
                    production.execute(
                        """
                        INSERT INTO mit3uri_replay_danmaku_provenance (
                            source, cid, dm_id, session_id, bvid,
                            progress_ms, session_progress_ms, mid_hash,
                            ctime, mapped_timestamp_ms,
                            alignment_method, match_kind, event_id
                        ) VALUES (
                            'bilibili_official_replay', ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, 'inserted', ?
                        )
                        """,
                        (
                            int(row["cid"]),
                            str(row["dm_id"]),
                            str(row["session_id"]),
                            str(row["bvid"]),
                            int(row["progress_ms"]),
                            int(row["session_progress_ms"]),
                            str(row["mid_hash"]),
                            int(row["ctime"]),
                            int(row["mapped_timestamp_ms"]),
                            str(row["alignment_method"]),
                            event_id,
                        ),
                    )
                    if row["uname"]:
                        name_rows.add(
                            (
                                int(row["uid"]),
                                str(row["uname"]),
                                timestamp,
                            )
                        )
                    existing_provenance.add(source_key)
                    inserted += 1
                production.executemany(
                    """
                    INSERT OR IGNORE INTO name_history (
                        uid, uname, first_seen
                    ) VALUES (?, ?, ?)
                    """,
                    name_rows,
                )
                production.commit()
            except Exception:
                production.rollback()
                raise
            print(
                f"apply inserted={inserted}/{staged_new} "
                f"skipped_provenance={skipped_provenance}",
                flush=True,
            )
        after_quick_check = str(
            production.execute("PRAGMA quick_check").fetchone()[0]
        )
        provenance_count_after = int(
            production.execute(
                "SELECT COUNT(*) "
                "FROM mit3uri_replay_danmaku_provenance"
            ).fetchone()[0]
        )
    finally:
        production.close()
        staging.close()

    if after_quick_check != "ok":
        raise RuntimeError(
            f"production database quick_check={after_quick_check}"
        )
    provenance_delta = provenance_count_after - provenance_count_before
    expected_provenance_delta = linked_existing + inserted
    if provenance_delta != expected_provenance_delta:
        raise RuntimeError(
            "provenance count mismatch: "
            f"delta={provenance_delta} "
            f"expected={expected_provenance_delta}"
        )
    apply_report = {
        "schema_version": 1,
        "created_at": datetime.now(TIMEZONE).isoformat(),
        "db_path": str(db_path),
        "audit": str(work_dir / "audit.json"),
        "audit_db": str(audit_path),
        "staged_new": staged_new,
        "staged_existing": staged_existing,
        "linked_existing": linked_existing,
        "skipped_existing_provenance": skipped_existing_provenance,
        "inserted": inserted,
        "skipped_provenance": skipped_provenance,
        "provenance_before": provenance_count_before,
        "provenance_after": provenance_count_after,
        "alignment_locks": alignment_locks,
        "backup": str(backup_path) if backup_path else None,
        "backup_quick_check": backup_quick_check,
        "production_quick_check_before": before_quick_check,
        "production_quick_check_after": after_quick_check,
    }
    write_json_atomic(work_dir / "apply.json", apply_report)
    print(json.dumps(apply_report, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    args = parse_args()
    started = time.monotonic()
    if args.command == "metadata":
        result = command_metadata(args)
    elif args.command == "fetch":
        result = command_fetch(args)
    elif args.command == "clock-frames":
        result = command_clock_frames(args)
    elif args.command == "clock-ocr":
        result = command_clock_ocr(args)
    elif args.command == "audio-align":
        result = command_audio_align(args)
    elif args.command == "lock-alignments":
        result = command_lock_alignments(args)
    elif args.command == "apply":
        result = command_apply(args)
    elif args.command == "audit":
        result = command_audit(args)
    else:
        raise ValueError(args.command)
    print(f"elapsed_seconds={time.monotonic() - started:.1f}")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
