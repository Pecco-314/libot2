#!/usr/bin/env python3
"""Rebuild historical danmaku from Danmakus and VTB.cat.

The fetch phase writes normalized, resumable JSONL files.  The merge phase
uses exact user/text identity plus high-confidence sequence and local-batch
anchors; it never treats a wide time window as proof of duplication.
"""

from __future__ import annotations

import argparse
import bisect
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import statistics
import sys
import time
import unicodedata
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

import httpx
import msgpack


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "libot.db"
DEFAULT_BACKUP_DIR = PROJECT_ROOT / "data" / "backups"
EMOJI_PATH = PROJECT_ROOT / "scripts" / "emoji.json"

CHANNEL_UID = 2030198123
ROOM_ID = 1967216004
TZ = timezone(timedelta(hours=8))
DEFAULT_CUTOFF = "2026-04-21"

DANMAKUS_CATALOG_URL = "https://ukamnads.icu/api/v2/channel"
DANMAKUS_FULL_URL = "https://ukamnads.icu/api/v3/lives/{live_id}/full"
DANMAKUS_PAGED_URL = "https://ukamnads.icu/api/v3/lives/{live_id}/danmakus"
VTB_CATALOG_URL = "https://api.vtb.cat/liver/space"
VTB_LIVE_URL = "https://api.vtb.cat/live/{live_id}/"

TIGHT_MATCH_MS = 15_000
LOCAL_ALIGNMENT_TOLERANCE_MS = 15_000
INTRA_SOURCE_SHIFT_MS = 8 * 60 * 60 * 1000
INTRA_MIN_UNIQUE_ANCHORS = 5
INTRA_MIN_ANCHOR_UIDS = 3
INTRA_MIN_ALIGNMENT_SHARE = 0.80
CROSS_OFFSET_BIN_MS = 30_000
CROSS_MIN_UNIQUE_ANCHORS = 7
CROSS_MIN_ANCHOR_UIDS = 5
CROSS_MIN_ALIGNMENT_SHARE = 0.80
MAX_SEQUENCE_OFFSET_MS = 18 * 60 * 60 * 1000
UID_SEQUENCE_GAP_MS = 30 * 60 * 1000
GLOBAL_SEQUENCE_GAP_MS = 2 * 60 * 1000
CELL_MS = 2 * 60 * 1000
CELL_SHIFTS_MS = (0, CELL_MS // 2)
PAGE_LIMIT = 5000
DANMAKUS_PAGE_LIMIT = 2000


def _log(message: str) -> None:
    print(message, flush=True)


def _cutoff_ms(value: str) -> int:
    parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=TZ)
    return int(parsed.timestamp() * 1000)


def _parse_iso_ms(value: Any) -> int | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return int(parsed.timestamp() * 1000)


def _safe_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _clean_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return unicodedata.normalize("NFC", value.replace("\x00", "")).strip()


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number} 不是 JSON 对象")
            yield value


class RateLimitedClient:
    def __init__(self, *, interval: float = 0.55) -> None:
        self._interval = interval
        self._next_request = 0.0
        self._client = httpx.Client(
            trust_env=False,
            timeout=httpx.Timeout(120.0, connect=30.0),
            headers={"User-Agent": "libot2-history-rebuild/1.0"},
        )

    def close(self) -> None:
        self._client.close()

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(1, 7):
            delay = self._next_request - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._next_request = time.monotonic() + self._interval
            try:
                response = self._client.get(url, params=params, headers=headers)
                if response.status_code == 429:
                    retry_after = response.headers.get("retry-after")
                    wait = float(retry_after) if retry_after else min(30, attempt * 3)
                    _log(f"  API 限流，{wait:.1f}s 后重试")
                    time.sleep(wait)
                    continue
                response.raise_for_status()
                return response
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if attempt == 6:
                    break
                wait = min(30, 2**attempt)
                _log(f"  请求失败（第 {attempt}/6 次）：{exc}；{wait}s 后重试")
                time.sleep(wait)
        raise RuntimeError(f"请求最终失败: {url}") from last_error


def _load_emoji_map() -> dict[str, str]:
    value = json.loads(EMOJI_PATH.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{EMOJI_PATH} 不是 JSON 对象")
    return {
        str(resource): _clean_text(content)
        for resource, content in value.items()
        if _clean_text(content)
    }


def _select_danmakus_lives(
    catalog: dict[str, Any], cutoff_ms: int
) -> list[dict[str, Any]]:
    data = catalog.get("data")
    lives = data.get("lives") if isinstance(data, dict) else None
    if not isinstance(lives, list):
        raise ValueError("Danmakus 场次目录缺少 data.lives")
    result = [
        live
        for live in lives
        if isinstance(live, dict)
        and isinstance(live.get("liveId"), str)
        and (_safe_int(live.get("startDate")) or cutoff_ms) < cutoff_ms
    ]
    result.sort(key=lambda live: (_safe_int(live.get("startDate")) or 0, live["liveId"]))
    return result


def _vtb_live_reference_ms(live: dict[str, Any]) -> int | None:
    candidates = [
        value
        for value in (
            _parse_iso_ms(live.get("CreatedAt")),
            (_safe_int(live.get("EndAt")) or 0) * 1000,
            (_safe_int(live.get("StartAt")) or 0) * 1000,
        )
        if value and value > 0
    ]
    return min(candidates) if candidates else None


def _select_vtb_lives(
    catalog: dict[str, Any], cutoff_ms: int
) -> list[dict[str, Any]]:
    lives = catalog.get("Lives")
    if not isinstance(lives, list):
        raise ValueError("VTB.cat 场次目录缺少 Lives")
    result = [
        live
        for live in lives
        if isinstance(live, dict)
        and _safe_int(live.get("ID")) is not None
        and (_vtb_live_reference_ms(live) or cutoff_ms) < cutoff_ms
    ]
    result.sort(
        key=lambda live: (
            _vtb_live_reference_ms(live) or 0,
            _safe_int(live.get("ID")) or 0,
        )
    )
    return result


def _write_rows_atomic(path: Path, rows: Iterable[dict[str, Any]]) -> tuple[int, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    digest = hashlib.sha256()
    count = 0
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            encoded = (_json_dump(row) + "\n").encode("utf-8")
            handle.write(encoded.decode("utf-8"))
            digest.update(encoded)
            count += 1
    os.replace(temporary, path)
    return count, digest.hexdigest()


def _decode_danmakus_rows(
    packed: bytes,
    *,
    live_id: str,
    cutoff_ms: int,
    emoji_map: dict[str, str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    frame = msgpack.unpackb(packed, raw=False, strict_map_key=False)
    if not isinstance(frame, list) or len(frame) != 3:
        raise ValueError(f"Danmakus {live_id} full 响应不是三元组")
    channel, live_meta, danmakus = frame
    if (
        not isinstance(channel, list)
        or len(channel) < 3
        or _safe_int(channel[2]) != ROOM_ID
    ):
        raise ValueError(f"Danmakus {live_id} 返回了错误的房间")
    if not isinstance(danmakus, list) or len(danmakus) != 4:
        raise ValueError(f"Danmakus {live_id} 弹幕帧结构异常")
    base_ms, actors, room_emojis, records = danmakus
    if (
        not isinstance(base_ms, int)
        or not isinstance(actors, list)
        or not isinstance(room_emojis, list)
        or not isinstance(records, list)
    ):
        raise ValueError(f"Danmakus {live_id} 弹幕帧字段类型异常")
    catalog_count = (
        _safe_int(live_meta[8])
        if isinstance(live_meta, list) and len(live_meta) > 8
        else 0
    )
    rows: list[dict[str, Any]] = []
    current_ms = base_ms
    unknown_emojis: set[str] = set()
    type_counts: dict[str, int] = defaultdict(int)
    for sequence, record in enumerate(records):
        if not isinstance(record, list) or len(record) < 6:
            continue
        delta_ms, record_type, actor_id, _uploader, payload_kind, payload = record[:6]
        if not isinstance(delta_ms, int):
            continue
        current_ms += delta_ms
        type_counts[f"{record_type}:{payload_kind}"] += 1
        if current_ms <= 0 or current_ms >= cutoff_ms or record_type != 0:
            continue
        if not isinstance(actor_id, int) or actor_id <= 0 or actor_id > len(actors):
            continue
        actor = actors[actor_id - 1]
        if not isinstance(actor, list) or len(actor) < 2:
            continue
        uid = _safe_int(actor[0])
        uname = _clean_text(actor[1])
        if uid is None or uid <= 0:
            continue

        content = ""
        if payload_kind == 1 and isinstance(payload, list) and payload:
            content = _clean_text(payload[0])
        elif payload_kind == 2 and isinstance(payload, list) and payload:
            emoji_id = _safe_int(payload[0])
            if emoji_id and 0 < emoji_id <= len(room_emojis):
                emoji = room_emojis[emoji_id - 1]
                resource = (
                    _clean_text(emoji[0])
                    if isinstance(emoji, list) and emoji
                    else ""
                )
                if resource:
                    content = emoji_map.get(resource, f"[{resource}]")
                    if resource not in emoji_map:
                        unknown_emojis.add(resource)
        if not content:
            continue
        rows.append(
            {
                "source": "danmakus",
                "native_id": f"{live_id}:{sequence}",
                "live_id": live_id,
                "seq": sequence,
                "timestamp_ms": current_ms,
                "uid": uid,
                "uname": uname,
                "content": content,
            }
        )
    return rows, {
        "api_format": "msgpack-full",
        "raw_records": len(records),
        "messages": len(rows),
        "types": dict(sorted(type_counts.items())),
        "unknown_emojis": sorted(unknown_emojis),
    }


def _decode_danmakus_legacy_page(
    payload: Any,
    *,
    live_id: str,
    page_number: int,
    cutoff_ms: int,
    emoji_map: dict[str, str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    frame = data.get("frame") if isinstance(data, dict) else None
    if not isinstance(frame, dict):
        raise ValueError(f"Danmakus {live_id} 旧接口第 {page_number} 页缺少 frame")
    records = frame.get("records")
    actors = frame.get("actors")
    room_emojis = frame.get("roomEmojis")
    if not all(isinstance(value, list) for value in (records, actors, room_emojis)):
        raise ValueError(f"Danmakus {live_id} 旧接口第 {page_number} 页字段异常")

    rows: list[dict[str, Any]] = []
    unknown_emojis: set[str] = set()
    type_counts: dict[str, int] = defaultdict(int)
    for sequence_on_page, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        record_type = _safe_int(record.get("type"))
        payload_kind = _safe_int(record.get("payloadKind"))
        type_counts[f"{record_type}:{payload_kind}"] += 1
        timestamp_ms = _safe_int(record.get("ts"))
        actor_id = _safe_int(record.get("actorId"))
        if (
            timestamp_ms is None
            or timestamp_ms <= 0
            or timestamp_ms >= cutoff_ms
            or record_type != 0
            or actor_id is None
            or actor_id < 0
            or actor_id >= len(actors)
        ):
            continue
        actor = actors[actor_id]
        if not isinstance(actor, dict):
            continue
        uid = _safe_int(actor.get("uid"))
        if uid is None or uid <= 0:
            continue
        payload_data = record.get("payload")
        if not isinstance(payload_data, dict):
            payload_data = {}
        content = ""
        if payload_kind == 1:
            content = _clean_text(payload_data.get("rawText"))
        elif payload_kind == 2:
            emoji_id = _safe_int(payload_data.get("roomEmojiId"))
            if emoji_id is not None and 0 <= emoji_id < len(room_emojis):
                emoji = room_emojis[emoji_id]
                resource = _clean_text(emoji.get("resource")) if isinstance(emoji, dict) else ""
                if resource:
                    emoji_name = _clean_text(emoji.get("name"))
                    fallback = f"[{emoji_name}]" if emoji_name else f"[{resource}]"
                    content = emoji_map.get(resource, fallback)
                    if resource not in emoji_map:
                        unknown_emojis.add(resource)
        if not content:
            continue
        sequence = (page_number - 1) * DANMAKUS_PAGE_LIMIT + sequence_on_page
        rows.append(
            {
                "source": "danmakus",
                "native_id": f"{live_id}:legacy:{sequence}",
                "live_id": live_id,
                "seq": sequence,
                "timestamp_ms": timestamp_ms,
                "uid": uid,
                "uname": _clean_text(actor.get("name")),
                "content": content,
            }
        )
    return rows, {
        "raw_records": len(records),
        "messages": len(rows),
        "types": dict(type_counts),
        "unknown_emojis": sorted(unknown_emojis),
        "has_more": bool(data.get("hasMore")),
    }


def _fetch_danmakus_legacy(
    client: RateLimitedClient,
    *,
    live_id: str,
    cutoff_ms: int,
    emoji_map: dict[str, str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    offset = 0
    page_number = 1
    rows: list[dict[str, Any]] = []
    raw_records = 0
    type_counts: dict[str, int] = defaultdict(int)
    unknown_emojis: set[str] = set()
    while True:
        payload = client.get(
            DANMAKUS_PAGED_URL.format(live_id=live_id),
            params={"offset": offset, "limit": DANMAKUS_PAGE_LIMIT},
            headers={
                "Accept": "application/json",
                "Origin": "https://danmakus.com",
                "Referer": "https://danmakus.com/",
            },
        ).json()
        page_rows, details = _decode_danmakus_legacy_page(
            payload,
            live_id=live_id,
            page_number=page_number,
            cutoff_ms=cutoff_ms,
            emoji_map=emoji_map,
        )
        rows.extend(page_rows)
        raw_records += int(details["raw_records"])
        unknown_emojis.update(details["unknown_emojis"])
        for key, count in details["types"].items():
            type_counts[key] += int(count)
        if not details["has_more"] or not details["raw_records"]:
            break
        offset += DANMAKUS_PAGE_LIMIT
        page_number += 1
    rows.sort(key=lambda row: (row["timestamp_ms"], row["seq"]))
    return rows, {
        "api_format": "json-paged",
        "raw_records": raw_records,
        "messages": len(rows),
        "types": dict(sorted(type_counts.items())),
        "unknown_emojis": sorted(unknown_emojis),
        "api_pages": page_number,
    }


def _fetch_danmakus_live(
    client: RateLimitedClient,
    live: dict[str, Any],
    output_dir: Path,
    cutoff_ms: int,
    emoji_map: dict[str, str],
) -> dict[str, Any]:
    live_id = str(live["liveId"])
    output_path = output_dir / f"{live_id}.jsonl"
    done_path = output_dir / f"{live_id}.done.json"
    if output_path.exists() and done_path.exists():
        previous = json.loads(done_path.read_text(encoding="utf-8"))
        if int(previous.get("raw_records") or 0) > 0 or int(
            previous.get("catalog_danmakus") or 0
        ) == 0:
            return previous

    catalog_danmakus = _safe_int(live.get("danmakusCount")) or 0
    rows: list[dict[str, Any]] = []
    details: dict[str, Any] = {}
    for semantic_attempt in range(1, 4):
        response = client.get(
            DANMAKUS_FULL_URL.format(live_id=live_id),
            params={"include": "extra", "includeEnter": "false"},
            headers={
                "Accept": "application/x-msgpack",
                "Origin": "https://danmakus.com",
                "Referer": "https://danmakus.com/",
            },
        )
        rows, details = _decode_danmakus_rows(
            response.content,
            live_id=live_id,
            cutoff_ms=cutoff_ms,
            emoji_map=emoji_map,
        )
        if details["raw_records"] or not catalog_danmakus:
            break
        _log(f"  {live_id} 新版 full 为空，回退旧 JSON 分页接口")
        rows, details = _fetch_danmakus_legacy(
            client,
            live_id=live_id,
            cutoff_ms=cutoff_ms,
            emoji_map=emoji_map,
        )
        if details["raw_records"]:
            break
        if semantic_attempt < 3:
            _log(f"  {live_id} 新旧接口均为空，重新探测（{semantic_attempt}/3）")
            time.sleep(2)
    if not details["raw_records"] and catalog_danmakus:
        raise ValueError(
            f"Danmakus {live_id} 目录声称有 {catalog_danmakus} 条，"
            "但新旧接口都返回空数据"
        )
    count, sha256 = _write_rows_atomic(output_path, rows)
    result = {
        "live_id": live_id,
        "start_ms": _safe_int(live.get("startDate")),
        "stop_ms": _safe_int(live.get("stopDate")),
        "title": _clean_text(live.get("title")),
        "catalog_danmakus": catalog_danmakus,
        "jsonl_rows": count,
        "sha256": sha256,
        **details,
    }
    _atomic_json(done_path, result)
    return result


def _fetch_vtb_live(
    client: RateLimitedClient,
    live: dict[str, Any],
    output_dir: Path,
    cutoff_ms: int,
) -> dict[str, Any]:
    live_id = _safe_int(live.get("ID"))
    if live_id is None:
        raise ValueError("VTB.cat live ID 缺失")
    output_path = output_dir / f"{live_id}.jsonl"
    done_path = output_dir / f"{live_id}.done.json"
    if output_path.exists() and done_path.exists():
        return json.loads(done_path.read_text(encoding="utf-8"))

    rows: list[dict[str, Any]] = []
    page = 1
    total_pages = 1
    total_records = 0
    invalid_records = 0
    while page <= total_pages:
        payload = client.get(
            VTB_LIVE_URL.format(live_id=live_id),
            params={
                "page": page,
                "limit": PAGE_LIMIT,
                "order": "Time",
                "mid": 0,
                "type": "msg",
            },
        ).json()
        records = payload.get("records") if isinstance(payload, dict) else None
        if not isinstance(records, list):
            raise ValueError(f"VTB.cat {live_id} 第 {page} 页缺少 records")
        total_pages = _safe_int(payload.get("totalPages")) or 1
        total_records = _safe_int(payload.get("totalRecords")) or total_records
        for sequence_on_page, record in enumerate(records):
            if not isinstance(record, dict):
                invalid_records += 1
                continue
            if (
                record.get("ActionName") != "msg"
                or _safe_int(record.get("ActionType")) != 1
                or _safe_int(record.get("Live")) != live_id
                or _safe_int(record.get("LiveRoom")) != ROOM_ID
            ):
                invalid_records += 1
                continue
            uid = _safe_int(record.get("FromId"))
            timestamp_ms = _parse_iso_ms(record.get("CreatedAt"))
            content = _clean_text(record.get("Extra"))
            if not content:
                content = _clean_text(record.get("EmotesContent"))
            source_id = _safe_int(record.get("ID"))
            if (
                source_id is None
                or uid is None
                or uid <= 0
                or timestamp_ms is None
                or timestamp_ms >= cutoff_ms
                or not content
            ):
                if timestamp_ms is None or uid is None or not content:
                    invalid_records += 1
                continue
            rows.append(
                {
                    "source": "vtbcat",
                    "native_id": str(source_id),
                    "live_id": str(live_id),
                    "seq": (page - 1) * PAGE_LIMIT + sequence_on_page,
                    "timestamp_ms": timestamp_ms,
                    "uid": uid,
                    "uname": _clean_text(record.get("FromName")),
                    "content": content,
                }
            )
        page += 1

    rows.sort(key=lambda row: (row["timestamp_ms"], int(row["native_id"])))
    count, sha256 = _write_rows_atomic(output_path, rows)
    result = {
        "live_id": live_id,
        "reference_ms": _vtb_live_reference_ms(live),
        "end_ms": (_safe_int(live.get("EndAt")) or 0) * 1000,
        "title": _clean_text(live.get("Title")),
        "catalog_messages": _safe_int(live.get("Message")) or 0,
        "api_records": total_records,
        "api_pages": total_pages,
        "invalid_records": invalid_records,
        "jsonl_rows": count,
        "sha256": sha256,
    }
    _atomic_json(done_path, result)
    return result


def _consolidate_source(
    output_path: Path,
    raw_dir: Path,
    live_ids: Iterable[str],
) -> tuple[int, str]:
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    digest = hashlib.sha256()
    count = 0
    with temporary.open("w", encoding="utf-8") as output:
        for live_id in live_ids:
            path = raw_dir / f"{live_id}.jsonl"
            with path.open("rb") as source:
                for encoded in source:
                    output.write(encoded.decode("utf-8"))
                    digest.update(encoded)
                    count += 1
    os.replace(temporary, output_path)
    return count, digest.hexdigest()


def command_fetch(args: argparse.Namespace) -> int:
    cutoff_ms = _cutoff_ms(args.cutoff)
    work_dir = args.work_dir.resolve()
    raw_root = work_dir / "raw"
    danmakus_dir = raw_root / "danmakus"
    vtb_dir = raw_root / "vtbcat"
    danmakus_dir.mkdir(parents=True, exist_ok=True)
    vtb_dir.mkdir(parents=True, exist_ok=True)
    client = RateLimitedClient(interval=args.request_interval)
    try:
        _log("读取两源场次目录（trust_env=False）...")
        danmakus_catalog = client.get(
            DANMAKUS_CATALOG_URL, params={"uid": CHANNEL_UID}
        ).json()
        vtb_catalog = client.get(
            VTB_CATALOG_URL, params={"uid": CHANNEL_UID}
        ).json()
        _atomic_json(work_dir / "danmakus_catalog.json", danmakus_catalog)
        _atomic_json(work_dir / "vtbcat_catalog.json", vtb_catalog)
        danmakus_lives = _select_danmakus_lives(danmakus_catalog, cutoff_ms)
        vtb_lives = _select_vtb_lives(vtb_catalog, cutoff_ms)
        if args.max_lives:
            danmakus_lives = danmakus_lives[: args.max_lives]
            vtb_lives = vtb_lives[: args.max_lives]
        _log(
            f"截止 {args.cutoff}：Danmakus {len(danmakus_lives)} 场，"
            f"VTB.cat {len(vtb_lives)} 场"
        )

        emoji_map = _load_emoji_map()
        danmakus_results: list[dict[str, Any]] = []
        for index, live in enumerate(danmakus_lives, 1):
            result = _fetch_danmakus_live(
                client, live, danmakus_dir, cutoff_ms, emoji_map
            )
            danmakus_results.append(result)
            _log(
                f"[Danmakus {index}/{len(danmakus_lives)}] "
                f"{result['live_id']} -> {result['jsonl_rows']} 条消息"
            )

        vtb_results: list[dict[str, Any]] = []
        for index, live in enumerate(vtb_lives, 1):
            result = _fetch_vtb_live(client, live, vtb_dir, cutoff_ms)
            vtb_results.append(result)
            _log(
                f"[VTB.cat {index}/{len(vtb_lives)}] "
                f"{result['live_id']} -> {result['jsonl_rows']} 条消息"
            )

        d_count, d_hash = _consolidate_source(
            work_dir / "danmakus.jsonl",
            danmakus_dir,
            (str(result["live_id"]) for result in danmakus_results),
        )
        v_count, v_hash = _consolidate_source(
            work_dir / "vtbcat.jsonl",
            vtb_dir,
            (str(result["live_id"]) for result in vtb_results),
        )
        manifest = {
            "created_at": datetime.now(TZ).isoformat(),
            "cutoff": args.cutoff,
            "cutoff_ms": cutoff_ms,
            "room_id": ROOM_ID,
            "channel_uid": CHANNEL_UID,
            "danmakus": {
                "lives": danmakus_results,
                "rows": d_count,
                "sha256": d_hash,
            },
            "vtbcat": {
                "lives": vtb_results,
                "rows": v_count,
                "sha256": v_hash,
            },
        }
        _atomic_json(work_dir / "fetch_manifest.json", manifest)
        _log(f"抓取完成：Danmakus={d_count}，VTB.cat={v_count}")
        return 0
    finally:
        client.close()


def _configure_staging(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("PRAGMA temp_store=FILE")
    conn.execute("PRAGMA cache_size=-262144")
    conn.execute("PRAGMA locking_mode=EXCLUSIVE")


def _key_digest(uid: int, content: str) -> bytes:
    digest = hashlib.blake2b(digest_size=16)
    digest.update(str(uid).encode("ascii"))
    digest.update(b"\0")
    digest.update(content.encode("utf-8"))
    return digest.digest()


def _load_source_jsonl(
    conn: sqlite3.Connection, path: Path, expected_source: str
) -> tuple[int, int]:
    source_number = 0 if expected_source == "danmakus" else 1
    batch: list[tuple[Any, ...]] = []
    count = 0
    rejected_nonpositive_time = 0
    for row in _iter_jsonl(path):
        if row.get("source") != expected_source:
            raise ValueError(f"{path} 混入来源 {row.get('source')!r}")
        uid = _safe_int(row.get("uid"))
        timestamp_ms = _safe_int(row.get("timestamp_ms"))
        content = _clean_text(row.get("content"))
        if uid is None or uid <= 0 or timestamp_ms is None or not content:
            raise ValueError(f"{path} 含无效行: {row!r}")
        if timestamp_ms <= 0:
            rejected_nonpositive_time += 1
            continue
        batch.append(
            (
                source_number,
                str(row.get("native_id")),
                str(row.get("live_id")),
                _safe_int(row.get("seq")) or 0,
                timestamp_ms,
                uid,
                _clean_text(row.get("uname")),
                content,
                _key_digest(uid, content),
            )
        )
        count += 1
        if len(batch) >= 20_000:
            conn.executemany(
                """
                INSERT INTO source_rows (
                    source, native_id, live_id, seq, ts_ms,
                    uid, uname, content, match_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                batch,
            )
            batch.clear()
            if count % 200_000 == 0:
                _log(f"  已载入 {expected_source} {count} 行")
    if batch:
        conn.executemany(
            """
            INSERT INTO source_rows (
                source, native_id, live_id, seq, ts_ms,
                uid, uname, content, match_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            batch,
        )
    conn.commit()
    return count, rejected_nonpositive_time


def _candidate_pairs_within(
    left: list[tuple[int, int]],
    right: list[tuple[int, int]],
    window_ms: int,
) -> list[tuple[int, int]]:
    right_times = [row[1] for row in right]
    candidates: list[tuple[int, int, int, int]] = []
    for left_id, left_ts in left:
        start = bisect.bisect_left(right_times, left_ts - window_ms)
        stop = bisect.bisect_right(right_times, left_ts + window_ms)
        for right_id, right_ts in right[start:stop]:
            candidates.append(
                (abs(right_ts - left_ts), max(left_ts, right_ts), left_id, right_id)
            )
    candidates.sort()
    used_left: set[int] = set()
    used_right: set[int] = set()
    result: list[tuple[int, int]] = []
    for _distance, _later, left_id, right_id in candidates:
        if left_id in used_left or right_id in used_right:
            continue
        used_left.add(left_id)
        used_right.add(right_id)
        result.append((left_id, right_id))
    return result


def _candidate_pairs_aligned(
    left: list[tuple[int, int]],
    right: list[tuple[int, int]],
    *,
    offset_ms: int,
    tolerance_ms: int,
) -> list[tuple[int, int, int, int, int]]:
    right_times = [row[1] for row in right]
    candidates: list[tuple[int, int, int, int, int, int]] = []
    for left_id, left_ts in left:
        expected_right_ts = left_ts + offset_ms
        start = bisect.bisect_left(right_times, expected_right_ts - tolerance_ms)
        stop = bisect.bisect_right(right_times, expected_right_ts + tolerance_ms)
        for right_id, right_ts in right[start:stop]:
            residual = abs((right_ts - left_ts) - offset_ms)
            candidates.append(
                (
                    residual,
                    max(left_ts, right_ts),
                    left_id,
                    right_id,
                    left_ts,
                    right_ts,
                )
            )
    candidates.sort()
    used_left: set[int] = set()
    used_right: set[int] = set()
    result: list[tuple[int, int, int, int, int]] = []
    for residual, _later, left_id, right_id, left_ts, right_ts in candidates:
        if left_id in used_left or right_id in used_right:
            continue
        used_left.add(left_id)
        used_right.add(right_id)
        result.append((left_id, right_id, left_ts, right_ts, residual))
    return result


def _load_live_rows_by_key(
    conn: sqlite3.Connection, live_id: str, *, source: int
) -> dict[bytes, list[tuple[int, int]]]:
    result: dict[bytes, list[tuple[int, int]]] = defaultdict(list)
    for row_id, timestamp_ms, match_key in conn.execute(
        """
        SELECT id, ts_ms, match_key
        FROM source_rows
        WHERE source = ? AND live_id = ?
        ORDER BY match_key, ts_ms, id
        """,
        (source, live_id),
    ):
        result[bytes(match_key)].append((int(row_id), int(timestamp_ms)))
    return result


def _deduplicate_exact_source_rows(conn: sqlite3.Connection) -> dict[str, Any]:
    conn.executescript(
        """
        DROP TABLE IF EXISTS exact_source_drops;
        CREATE TABLE exact_source_drops (
            drop_id INTEGER PRIMARY KEY,
            keep_id INTEGER NOT NULL,
            source INTEGER NOT NULL,
            ts_ms INTEGER NOT NULL,
            drop_live TEXT NOT NULL,
            keep_live TEXT NOT NULL
        );

        INSERT INTO exact_source_drops (
            drop_id, keep_id, source, ts_ms, drop_live, keep_live
        )
        WITH ranked AS (
            SELECT
                id AS drop_id,
                MIN(id) OVER (
                    PARTITION BY source, match_key, ts_ms
                ) AS keep_id,
                source,
                ts_ms,
                live_id AS drop_live,
                ROW_NUMBER() OVER (
                    PARTITION BY source, match_key, ts_ms
                    ORDER BY id
                ) AS rank_number
            FROM source_rows
        )
        SELECT
            ranked.drop_id,
            ranked.keep_id,
            ranked.source,
            ranked.ts_ms,
            ranked.drop_live,
            kept.live_id
        FROM ranked
        JOIN source_rows kept ON kept.id = ranked.keep_id
        WHERE ranked.rank_number > 1;
        """
    )
    dropped_by_source = {"danmakus": 0, "vtbcat": 0}
    for source, count in conn.execute(
        """
        SELECT source, COUNT(*)
        FROM exact_source_drops
        GROUP BY source
        """
    ):
        dropped_by_source[
            "danmakus" if int(source) == 0 else "vtbcat"
        ] = int(count)
    duplicate_groups = int(
        conn.execute(
            "SELECT COUNT(DISTINCT keep_id) FROM exact_source_drops"
        ).fetchone()[0]
    )
    cross_live_groups = int(
        conn.execute(
            """
            SELECT COUNT(DISTINCT keep_id)
            FROM exact_source_drops
            WHERE drop_live != keep_live
            """
        ).fetchone()[0]
    )
    invalid_drop_rows = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM exact_source_drops x
            JOIN source_rows dropped ON dropped.id = x.drop_id
            JOIN source_rows kept ON kept.id = x.keep_id
            WHERE dropped.source != kept.source
               OR dropped.ts_ms != kept.ts_ms
               OR dropped.match_key != kept.match_key
            """
        ).fetchone()[0]
    )
    conn.execute(
        """
        DELETE FROM source_rows
        WHERE id IN (SELECT drop_id FROM exact_source_drops)
        """
    )
    conn.commit()
    return {
        "method": "same_source_exact_identity_exact_ms",
        "duplicate_groups": duplicate_groups,
        "dropped_rows": sum(dropped_by_source.values()),
        "dropped_by_source": dropped_by_source,
        "cross_live_groups": cross_live_groups,
        "invalid_drop_rows": invalid_drop_rows,
    }


def _deduplicate_shifted_copies(conn: sqlite3.Connection) -> dict[str, Any]:
    _log("扫描 Danmakus 同源固定 ±8 小时副本...")
    conn.executescript(
        """
        DROP TABLE IF EXISTS live_key_stats;
        CREATE TABLE live_key_stats AS
        SELECT live_id, match_key, COUNT(*) AS n,
               MIN(id) AS one_id, MIN(ts_ms) AS one_ts, MIN(uid) AS uid
        FROM source_rows
        WHERE source = 0
        GROUP BY live_id, match_key;
        CREATE INDEX idx_live_key_stats_match
        ON live_key_stats(match_key, live_id);

        DROP TABLE IF EXISTS intra_pairs;
        CREATE TABLE intra_pairs (
            a_live TEXT NOT NULL,
            b_live TEXT NOT NULL,
            offset_ms INTEGER NOT NULL,
            shared_unique INTEGER NOT NULL,
            aligned_unique INTEGER NOT NULL,
            aligned_uids INTEGER NOT NULL,
            alignment_share REAL NOT NULL,
            PRIMARY KEY (a_live, b_live)
        );

        DROP TABLE IF EXISTS intra_drops;
        CREATE TABLE intra_drops (
            drop_id INTEGER PRIMARY KEY,
            keep_id INTEGER NOT NULL,
            source INTEGER NOT NULL,
            drop_ts_ms INTEGER NOT NULL,
            keep_ts_ms INTEGER NOT NULL,
            a_live TEXT NOT NULL,
            b_live TEXT NOT NULL,
            method TEXT NOT NULL,
            residual_ms INTEGER NOT NULL
        );
        """
    )
    conn.execute(
        """
        WITH pair_stats AS (
            SELECT
                a.live_id AS a_live,
                b.live_id AS b_live,
                COUNT(*) AS shared_unique,
                SUM(CASE WHEN ABS((b.one_ts - a.one_ts) - ?) <= ?
                         THEN 1 ELSE 0 END) AS positive_anchors,
                SUM(CASE WHEN ABS((b.one_ts - a.one_ts) + ?) <= ?
                         THEN 1 ELSE 0 END) AS negative_anchors,
                COUNT(DISTINCT CASE
                    WHEN ABS((b.one_ts - a.one_ts) - ?) <= ? THEN a.uid
                END) AS positive_uids,
                COUNT(DISTINCT CASE
                    WHEN ABS((b.one_ts - a.one_ts) + ?) <= ? THEN a.uid
                END) AS negative_uids
            FROM live_key_stats a
            JOIN live_key_stats b
              ON b.match_key = a.match_key AND b.live_id > a.live_id
            WHERE a.n = 1 AND b.n = 1
            GROUP BY a.live_id, b.live_id
        ), chosen AS (
            SELECT
                a_live,
                b_live,
                CASE WHEN positive_anchors >= negative_anchors
                     THEN ? ELSE -? END AS offset_ms,
                shared_unique,
                MAX(positive_anchors, negative_anchors) AS aligned_unique,
                CASE WHEN positive_anchors >= negative_anchors
                     THEN positive_uids ELSE negative_uids END AS aligned_uids
            FROM pair_stats
        )
        INSERT INTO intra_pairs (
            a_live, b_live, offset_ms, shared_unique,
            aligned_unique, aligned_uids, alignment_share
        )
        SELECT
            a_live, b_live, offset_ms, shared_unique,
            aligned_unique, aligned_uids,
            1.0 * aligned_unique / shared_unique
        FROM chosen
        WHERE aligned_unique >= ?
          AND aligned_uids >= ?
          AND 1.0 * aligned_unique / shared_unique >= ?
        """,
        (
            INTRA_SOURCE_SHIFT_MS,
            LOCAL_ALIGNMENT_TOLERANCE_MS,
            INTRA_SOURCE_SHIFT_MS,
            LOCAL_ALIGNMENT_TOLERANCE_MS,
            INTRA_SOURCE_SHIFT_MS,
            LOCAL_ALIGNMENT_TOLERANCE_MS,
            INTRA_SOURCE_SHIFT_MS,
            LOCAL_ALIGNMENT_TOLERANCE_MS,
            INTRA_SOURCE_SHIFT_MS,
            INTRA_SOURCE_SHIFT_MS,
            INTRA_MIN_UNIQUE_ANCHORS,
            INTRA_MIN_ANCHOR_UIDS,
            INTRA_MIN_ALIGNMENT_SHARE,
        ),
    )
    conn.commit()

    pair_reports: list[dict[str, Any]] = []
    matched_across_candidates = 0
    for (
        a_live,
        b_live,
        offset_ms,
        shared_unique,
        aligned_unique,
        aligned_uids,
        alignment_share,
    ) in conn.execute(
        """
        SELECT a_live, b_live, offset_ms, shared_unique,
               aligned_unique, aligned_uids, alignment_share
        FROM intra_pairs
        ORDER BY aligned_unique DESC, a_live, b_live
        """
    ):
        left_by_key = _load_live_rows_by_key(conn, str(a_live), source=0)
        right_by_key = _load_live_rows_by_key(conn, str(b_live), source=0)
        matched: list[tuple[int, int, int, int, int]] = []
        for match_key in left_by_key.keys() & right_by_key.keys():
            matched.extend(
                _candidate_pairs_aligned(
                    left_by_key[match_key],
                    right_by_key[match_key],
                    offset_ms=int(offset_ms),
                    tolerance_ms=LOCAL_ALIGNMENT_TOLERANCE_MS,
                )
            )

        drop_rows: list[tuple[Any, ...]] = []
        for left_id, right_id, left_ts, right_ts, residual in matched:
            if left_ts <= right_ts:
                keep_id, keep_ts = left_id, left_ts
                drop_id, drop_ts = right_id, right_ts
            else:
                keep_id, keep_ts = right_id, right_ts
                drop_id, drop_ts = left_id, left_ts
            drop_rows.append(
                (
                    drop_id,
                    keep_id,
                    0,
                    drop_ts,
                    keep_ts,
                    str(a_live),
                    str(b_live),
                    "danmakus_shift8h",
                    residual,
                )
            )
        before = conn.total_changes
        conn.executemany(
            """
            INSERT OR IGNORE INTO intra_drops (
                drop_id, keep_id, source, drop_ts_ms, keep_ts_ms,
                a_live, b_live, method, residual_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            drop_rows,
        )
        new_drops = conn.total_changes - before
        matched_across_candidates += len(matched)
        pair_reports.append(
            {
                "a_live": str(a_live),
                "b_live": str(b_live),
                "offset_ms": int(offset_ms),
                "shared_unique": int(shared_unique),
                "aligned_unique": int(aligned_unique),
                "aligned_uids": int(aligned_uids),
                "alignment_share": float(alignment_share),
                "matched_rows": len(matched),
                "new_unique_drops": new_drops,
            }
        )
    conn.commit()

    conn.executescript(
        """
        DROP TABLE IF EXISTS vtb_live_key_stats;
        CREATE TABLE vtb_live_key_stats AS
        SELECT live_id, match_key, COUNT(*) AS n,
               MIN(id) AS one_id, MIN(ts_ms) AS one_ts, MIN(uid) AS uid
        FROM source_rows
        WHERE source = 1
        GROUP BY live_id, match_key;
        CREATE INDEX idx_vtb_live_key_stats_match
        ON vtb_live_key_stats(match_key, live_id);

        DROP TABLE IF EXISTS intra_bridge_pairs;
        CREATE TABLE intra_bridge_pairs (
            danmakus_live TEXT NOT NULL,
            vtbcat_live TEXT NOT NULL,
            shared_unique INTEGER NOT NULL,
            aligned_unique INTEGER NOT NULL,
            aligned_uids INTEGER NOT NULL,
            alignment_share REAL NOT NULL,
            PRIMARY KEY (danmakus_live, vtbcat_live)
        );
        """
    )
    conn.execute(
        """
        WITH later_lives AS (
            SELECT DISTINCT
                CASE WHEN offset_ms > 0 THEN b_live ELSE a_live END AS live_id
            FROM intra_pairs
        ), pair_stats AS (
            SELECT
                d.live_id AS danmakus_live,
                v.live_id AS vtbcat_live,
                COUNT(*) AS shared_unique,
                SUM(CASE WHEN ABS((d.one_ts - v.one_ts) - ?) <= ?
                         THEN 1 ELSE 0 END) AS aligned_unique,
                COUNT(DISTINCT CASE
                    WHEN ABS((d.one_ts - v.one_ts) - ?) <= ? THEN d.uid
                END) AS aligned_uids
            FROM live_key_stats d
            JOIN later_lives l ON l.live_id = d.live_id
            JOIN vtb_live_key_stats v ON v.match_key = d.match_key
            WHERE d.n = 1 AND v.n = 1
            GROUP BY d.live_id, v.live_id
        )
        INSERT INTO intra_bridge_pairs (
            danmakus_live, vtbcat_live, shared_unique,
            aligned_unique, aligned_uids, alignment_share
        )
        SELECT
            danmakus_live, vtbcat_live, shared_unique,
            aligned_unique, aligned_uids,
            1.0 * aligned_unique / shared_unique
        FROM pair_stats
        WHERE aligned_unique >= ?
          AND aligned_uids >= ?
          AND 1.0 * aligned_unique / shared_unique >= ?
        """,
        (
            INTRA_SOURCE_SHIFT_MS,
            LOCAL_ALIGNMENT_TOLERANCE_MS,
            INTRA_SOURCE_SHIFT_MS,
            LOCAL_ALIGNMENT_TOLERANCE_MS,
            INTRA_MIN_UNIQUE_ANCHORS,
            INTRA_MIN_ANCHOR_UIDS,
            INTRA_MIN_ALIGNMENT_SHARE,
        ),
    )
    conn.commit()

    bridge_reports: list[dict[str, Any]] = []
    bridge_matched_across_candidates = 0
    for (
        danmakus_live,
        vtbcat_live,
        shared_unique,
        aligned_unique,
        aligned_uids,
        alignment_share,
    ) in conn.execute(
        """
        SELECT danmakus_live, vtbcat_live, shared_unique,
               aligned_unique, aligned_uids, alignment_share
        FROM intra_bridge_pairs
        ORDER BY aligned_unique DESC, danmakus_live, vtbcat_live
        """
    ):
        danmakus_by_key = _load_live_rows_by_key(
            conn, str(danmakus_live), source=0
        )
        vtbcat_by_key = _load_live_rows_by_key(conn, str(vtbcat_live), source=1)
        matched: list[tuple[int, int, int, int, int]] = []
        for match_key in danmakus_by_key.keys() & vtbcat_by_key.keys():
            matched.extend(
                _candidate_pairs_aligned(
                    danmakus_by_key[match_key],
                    vtbcat_by_key[match_key],
                    offset_ms=-INTRA_SOURCE_SHIFT_MS,
                    tolerance_ms=LOCAL_ALIGNMENT_TOLERANCE_MS,
                )
            )

        drop_rows = []
        for danmakus_id, vtbcat_id, danmakus_ts, vtbcat_ts, residual in matched:
            if danmakus_ts <= vtbcat_ts:
                keep_id, keep_ts = danmakus_id, danmakus_ts
                drop_id, drop_ts = vtbcat_id, vtbcat_ts
                drop_source = 1
            else:
                keep_id, keep_ts = vtbcat_id, vtbcat_ts
                drop_id, drop_ts = danmakus_id, danmakus_ts
                drop_source = 0
            drop_rows.append(
                (
                    drop_id,
                    keep_id,
                    drop_source,
                    drop_ts,
                    keep_ts,
                    str(danmakus_live),
                    str(vtbcat_live),
                    "vtbcat_bridge_shift8h",
                    residual,
                )
            )
        before = conn.total_changes
        conn.executemany(
            """
            INSERT OR IGNORE INTO intra_drops (
                drop_id, keep_id, source, drop_ts_ms, keep_ts_ms,
                a_live, b_live, method, residual_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            drop_rows,
        )
        new_drops = conn.total_changes - before
        bridge_matched_across_candidates += len(matched)
        bridge_reports.append(
            {
                "danmakus_live": str(danmakus_live),
                "vtbcat_live": str(vtbcat_live),
                "shared_unique": int(shared_unique),
                "aligned_unique": int(aligned_unique),
                "aligned_uids": int(aligned_uids),
                "alignment_share": float(alignment_share),
                "matched_rows": len(matched),
                "new_unique_drops": new_drops,
            }
        )
    conn.commit()

    invalid_drops = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM intra_drops
            WHERE drop_ts_ms <= keep_ts_ms OR residual_ms > ?
            """,
            (LOCAL_ALIGNMENT_TOLERANCE_MS,),
        ).fetchone()[0]
    )
    identity_mismatches = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM intra_drops x
            JOIN source_rows d ON d.id = x.drop_id
            JOIN source_rows k ON k.id = x.keep_id
            WHERE d.source != x.source OR d.match_key != k.match_key
            """
        ).fetchone()[0]
    )
    if invalid_drops or identity_mismatches:
        raise RuntimeError(
            f"偏移去重无效删除={invalid_drops}，身份不一致={identity_mismatches}"
        )

    drop_count = int(conn.execute("SELECT COUNT(*) FROM intra_drops").fetchone()[0])
    dropped_by_source = {"danmakus": 0, "vtbcat": 0}
    for source, count in conn.execute(
        "SELECT source, COUNT(*) FROM intra_drops GROUP BY source"
    ):
        dropped_by_source["danmakus" if int(source) == 0 else "vtbcat"] = int(count)
    dropped_by_method = {
        str(method): int(count)
        for method, count in conn.execute(
            "SELECT method, COUNT(*) FROM intra_drops GROUP BY method ORDER BY method"
        )
    }
    conn.execute(
        "DELETE FROM source_rows WHERE id IN (SELECT drop_id FROM intra_drops)"
    )
    conn.commit()
    conn.execute("DROP TABLE live_key_stats")
    conn.execute("DROP TABLE vtb_live_key_stats")
    conn.commit()
    return {
        "method": "shift8h_multi_source_anchor_chain",
        "shift_ms": INTRA_SOURCE_SHIFT_MS,
        "residual_tolerance_ms": LOCAL_ALIGNMENT_TOLERANCE_MS,
        "minimum_unique_anchors": INTRA_MIN_UNIQUE_ANCHORS,
        "minimum_anchor_uids": INTRA_MIN_ANCHOR_UIDS,
        "minimum_alignment_share": INTRA_MIN_ALIGNMENT_SHARE,
        "candidate_live_pairs": len(pair_reports),
        "matched_rows_across_candidates": matched_across_candidates,
        "vtb_bridge_pairs": len(bridge_reports),
        "vtb_bridge_matched_rows": bridge_matched_across_candidates,
        "dropped_rows": drop_count,
        "dropped_by_source": dropped_by_source,
        "dropped_by_method": dropped_by_method,
        "invalid_drop_rows": invalid_drops,
        "identity_mismatches": identity_mismatches,
        "pairs": pair_reports,
        "vtb_bridge_pair_reports": bridge_reports,
    }


def _insert_match_pairs(
    conn: sqlite3.Connection,
    pairs: Iterable[tuple[int, int]],
    method: str,
    *,
    primary: int,
) -> int:
    before = conn.total_changes
    conn.executemany(
        """
        INSERT OR IGNORE INTO matches (d_id, v_id, method, primary_anchor)
        VALUES (?, ?, ?, ?)
        """,
        ((left_id, right_id, method, primary) for left_id, right_id in pairs),
    )
    return conn.total_changes - before


def _load_unmatched_live_rows_by_key(
    conn: sqlite3.Connection, live_id: str, *, source: int
) -> dict[bytes, list[tuple[int, int]]]:
    if source == 0:
        unmatched_clause = "NOT EXISTS (SELECT 1 FROM matches m WHERE m.d_id = r.id)"
    else:
        unmatched_clause = "NOT EXISTS (SELECT 1 FROM matches m WHERE m.v_id = r.id)"
    result: dict[bytes, list[tuple[int, int]]] = defaultdict(list)
    for row_id, timestamp_ms, match_key in conn.execute(
        f"""
        SELECT r.id, r.ts_ms, r.match_key
        FROM source_rows r
        WHERE r.source = ? AND r.live_id = ? AND {unmatched_clause}
        ORDER BY r.match_key, r.ts_ms, r.id
        """,
        (source, live_id),
    ):
        result[bytes(match_key)].append((int(row_id), int(timestamp_ms)))
    return result


def _match_cross_live_offsets(
    conn: sqlite3.Connection,
) -> tuple[int, list[dict[str, Any]]]:
    conn.executescript(
        """
        DROP TABLE IF EXISTS cross_live_key_stats;
        CREATE TABLE cross_live_key_stats AS
        SELECT r.source, r.live_id, r.match_key, COUNT(*) AS n,
               MIN(r.ts_ms) AS one_ts, MIN(r.uid) AS uid
        FROM source_rows r
        WHERE (
            r.source = 0
            AND NOT EXISTS (SELECT 1 FROM matches m WHERE m.d_id = r.id)
        ) OR (
            r.source = 1
            AND NOT EXISTS (SELECT 1 FROM matches m WHERE m.v_id = r.id)
        )
        GROUP BY r.source, r.live_id, r.match_key;
        CREATE INDEX idx_cross_live_key_stats_match
        ON cross_live_key_stats(match_key, source, live_id);

        DROP TABLE IF EXISTS cross_offset_pairs;
        CREATE TABLE cross_offset_pairs (
            danmakus_live TEXT NOT NULL,
            vtbcat_live TEXT NOT NULL,
            offset_ms INTEGER NOT NULL,
            shared_unique INTEGER NOT NULL,
            aligned_unique INTEGER NOT NULL,
            aligned_uids INTEGER NOT NULL,
            alignment_share REAL NOT NULL,
            PRIMARY KEY (danmakus_live, vtbcat_live)
        );
        """
    )
    conn.execute(
        """
        WITH shared AS (
            SELECT
                d.live_id AS danmakus_live,
                v.live_id AS vtbcat_live,
                COUNT(*) AS shared_unique
            FROM cross_live_key_stats d
            JOIN cross_live_key_stats v
              ON d.source = 0 AND v.source = 1
             AND v.match_key = d.match_key
            WHERE d.n = 1 AND v.n = 1
            GROUP BY d.live_id, v.live_id
        ), bins AS (
            SELECT
                d.live_id AS danmakus_live,
                v.live_id AS vtbcat_live,
                CAST(ROUND(1.0 * (v.one_ts - d.one_ts) / ?) AS INTEGER) * ?
                    AS offset_ms,
                COUNT(*) AS aligned_unique,
                COUNT(DISTINCT d.uid) AS aligned_uids
            FROM cross_live_key_stats d
            JOIN cross_live_key_stats v
              ON d.source = 0 AND v.source = 1
             AND v.match_key = d.match_key
            WHERE d.n = 1 AND v.n = 1
            GROUP BY d.live_id, v.live_id, offset_ms
        ), ranked AS (
            SELECT
                b.*,
                s.shared_unique,
                ROW_NUMBER() OVER (
                    PARTITION BY b.danmakus_live, b.vtbcat_live
                    ORDER BY b.aligned_unique DESC, b.aligned_uids DESC,
                             ABS(b.offset_ms), b.offset_ms
                ) AS rank_number
            FROM bins b
            JOIN shared s USING (danmakus_live, vtbcat_live)
        )
        INSERT INTO cross_offset_pairs (
            danmakus_live, vtbcat_live, offset_ms, shared_unique,
            aligned_unique, aligned_uids, alignment_share
        )
        SELECT
            danmakus_live, vtbcat_live, offset_ms, shared_unique,
            aligned_unique, aligned_uids,
            1.0 * aligned_unique / shared_unique
        FROM ranked
        WHERE rank_number = 1
          AND aligned_unique >= ?
          AND aligned_uids >= ?
          AND 1.0 * aligned_unique / shared_unique >= ?
        """,
        (
            CROSS_OFFSET_BIN_MS,
            CROSS_OFFSET_BIN_MS,
            CROSS_MIN_UNIQUE_ANCHORS,
            CROSS_MIN_ANCHOR_UIDS,
            CROSS_MIN_ALIGNMENT_SHARE,
        ),
    )
    conn.commit()

    total_inserted = 0
    reports: list[dict[str, Any]] = []
    for (
        danmakus_live,
        vtbcat_live,
        offset_ms,
        shared_unique,
        aligned_unique,
        aligned_uids,
        alignment_share,
    ) in conn.execute(
        """
        SELECT danmakus_live, vtbcat_live, offset_ms, shared_unique,
               aligned_unique, aligned_uids, alignment_share
        FROM cross_offset_pairs
        ORDER BY aligned_unique DESC, danmakus_live, vtbcat_live
        """
    ):
        danmakus_by_key = _load_unmatched_live_rows_by_key(
            conn, str(danmakus_live), source=0
        )
        vtbcat_by_key = _load_unmatched_live_rows_by_key(
            conn, str(vtbcat_live), source=1
        )
        proposed: list[tuple[int, int, int, int, int]] = []
        for match_key in danmakus_by_key.keys() & vtbcat_by_key.keys():
            proposed.extend(
                _candidate_pairs_aligned(
                    danmakus_by_key[match_key],
                    vtbcat_by_key[match_key],
                    offset_ms=int(offset_ms),
                    tolerance_ms=LOCAL_ALIGNMENT_TOLERANCE_MS,
                )
            )
        inserted = _insert_match_pairs(
            conn,
            ((left_id, right_id) for left_id, right_id, *_rest in proposed),
            "live_offset",
            primary=1,
        )
        conn.commit()
        total_inserted += inserted
        reports.append(
            {
                "danmakus_live": str(danmakus_live),
                "vtbcat_live": str(vtbcat_live),
                "offset_ms": int(offset_ms),
                "shared_unique": int(shared_unique),
                "aligned_unique": int(aligned_unique),
                "aligned_uids": int(aligned_uids),
                "alignment_share": float(alignment_share),
                "proposed_matches": len(proposed),
                "inserted_matches": inserted,
            }
        )
    conn.execute("DROP TABLE cross_live_key_stats")
    conn.commit()
    return total_inserted, reports



def _match_tight(conn: sqlite3.Connection) -> int:
    cursor = conn.execute(
        """
        SELECT match_key, id, source, ts_ms
        FROM source_rows
        ORDER BY match_key, source, ts_ms, id
        """
    )
    current_key: bytes | None = None
    left: list[tuple[int, int]] = []
    right: list[tuple[int, int]] = []
    inserted = 0

    def flush() -> None:
        nonlocal inserted
        if left and right:
            pairs = _candidate_pairs_within(left, right, TIGHT_MATCH_MS)
            inserted += _insert_match_pairs(
                conn, pairs, "tight15s", primary=1
            )

    for key, row_id, source, timestamp_ms in cursor:
        if current_key is not None and key != current_key:
            flush()
            left = []
            right = []
        current_key = key
        target = left if source == 0 else right
        target.append((int(row_id), int(timestamp_ms)))
    flush()
    conn.commit()
    return inserted


def _create_shingles(
    conn: sqlite3.Connection,
    *,
    per_uid: bool,
) -> tuple[int, int]:
    conn.execute("DROP TABLE IF EXISTS shingles")
    conn.execute(
        """
        CREATE TABLE shingles (
            hash BLOB NOT NULL,
            source INTEGER NOT NULL,
            r1 INTEGER NOT NULL,
            r2 INTEGER NOT NULL,
            r3 INTEGER NOT NULL,
            r4 INTEGER,
            start_ms INTEGER NOT NULL,
            end_ms INTEGER NOT NULL
        )
        """
    )
    if per_uid:
        query = """
            SELECT source, uid, ts_ms, id, match_key
            FROM source_rows
            ORDER BY source, uid, ts_ms, id
        """
        length = 3
        max_gap = UID_SEQUENCE_GAP_MS
    else:
        query = """
            SELECT source, live_id, ts_ms, id, match_key
            FROM source_rows
            ORDER BY source, live_id, ts_ms, id
        """
        length = 4
        max_gap = GLOBAL_SEQUENCE_GAP_MS

    batch: list[tuple[Any, ...]] = []
    group: tuple[int, Any] | None = None
    recent: deque[tuple[int, int, bytes]] = deque(maxlen=length)
    generated = 0
    for source, grouping_value, ts_ms, row_id, match_key in conn.execute(query):
        next_group = (int(source), grouping_value)
        if next_group != group:
            recent.clear()
            group = next_group
        elif recent and int(ts_ms) - recent[-1][0] > max_gap:
            recent.clear()
        recent.append((int(ts_ms), int(row_id), bytes(match_key)))
        if len(recent) != length:
            continue
        digest = hashlib.blake2b(digest_size=16)
        for _timestamp, _id, key in recent:
            digest.update(key)
        values = list(recent)
        batch.append(
            (
                digest.digest(),
                int(source),
                values[0][1],
                values[1][1],
                values[2][1],
                values[3][1] if length == 4 else None,
                values[0][0],
                values[-1][0],
            )
        )
        generated += 1
        if len(batch) >= 25_000:
            conn.executemany(
                """
                INSERT INTO shingles (
                    hash, source, r1, r2, r3, r4, start_ms, end_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                batch,
            )
            batch.clear()
    if batch:
        conn.executemany(
            """
            INSERT INTO shingles (
                hash, source, r1, r2, r3, r4, start_ms, end_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            batch,
        )
    conn.execute("CREATE INDEX idx_shingles_hash_source ON shingles(hash, source)")
    conn.execute("DROP TABLE IF EXISTS unique_shingles")
    conn.execute(
        """
        CREATE TABLE unique_shingles AS
        SELECT hash
        FROM shingles
        GROUP BY hash
        HAVING COUNT(*) = 2 AND MIN(source) = 0 AND MAX(source) = 1
        """
    )
    conn.execute("CREATE UNIQUE INDEX idx_unique_shingles ON unique_shingles(hash)")

    method = "uid3" if per_uid else "global4"
    positions = ("r1", "r2", "r3") if per_uid else ("r1", "r2", "r3", "r4")
    inserted = 0
    for position in positions:
        before = conn.total_changes
        conn.execute(
            f"""
            INSERT OR IGNORE INTO matches (d_id, v_id, method, primary_anchor)
            SELECT d.{position}, v.{position}, ?, 1
            FROM unique_shingles u
            JOIN shingles d ON d.hash = u.hash AND d.source = 0
            JOIN shingles v ON v.hash = u.hash AND v.source = 1
            WHERE ABS(v.start_ms - d.start_ms) <= ?
            """,
            (method, MAX_SEQUENCE_OFFSET_MS),
        )
        inserted += conn.total_changes - before
    conn.commit()
    conn.execute("DROP TABLE unique_shingles")
    conn.execute("DROP TABLE shingles")
    conn.commit()
    return generated, inserted


def _unmatched_rows_in_range(
    conn: sqlite3.Connection,
    *,
    source: int,
    start_ms: int,
    end_ms: int,
) -> list[tuple[int, bytes, int]]:
    if source == 0:
        query = """
            SELECT r.id, r.match_key, r.ts_ms
            FROM source_rows r
            LEFT JOIN matches m ON m.d_id = r.id
            WHERE r.source = 0 AND r.ts_ms >= ? AND r.ts_ms < ?
              AND m.d_id IS NULL
            ORDER BY r.ts_ms, r.id
        """
    else:
        query = """
            SELECT r.id, r.match_key, r.ts_ms
            FROM source_rows r
            LEFT JOIN matches m ON m.v_id = r.id
            WHERE r.source = 1 AND r.ts_ms >= ? AND r.ts_ms < ?
              AND m.v_id IS NULL
            ORDER BY r.ts_ms, r.id
        """
    return [
        (int(row_id), bytes(key), int(ts_ms))
        for row_id, key, ts_ms in conn.execute(query, (start_ms, end_ms))
    ]


def _match_anchor_cells(conn: sqlite3.Connection) -> tuple[int, list[dict[str, Any]]]:
    anchors = [
        (int(d_ts), int(v_ts), int(uid), str(method))
        for d_ts, v_ts, uid, method in conn.execute(
            """
            SELECT d.ts_ms, v.ts_ms, d.uid, m.method
            FROM matches m
            JOIN source_rows d ON d.id = m.d_id
            JOIN source_rows v ON v.id = m.v_id
            WHERE m.primary_anchor = 1
            """
        )
    ]
    total_inserted = 0
    cell_reports: list[dict[str, Any]] = []
    for shift in CELL_SHIFTS_MS:
        grouped: dict[tuple[int, int], dict[str, Any]] = {}
        for d_ts, v_ts, uid, method in anchors:
            key = ((d_ts + shift) // CELL_MS, (v_ts + shift) // CELL_MS)
            cell = grouped.setdefault(
                key,
                {"count": 0, "uids": set(), "deltas": [], "sequence": False},
            )
            cell["count"] += 1
            cell["uids"].add(uid)
            cell["deltas"].append(v_ts - d_ts)
            if method in {"uid3", "global4"}:
                cell["sequence"] = True

        valid_cells = [
            (key, value)
            for key, value in grouped.items()
            if value["count"] >= 3
            and (len(value["uids"]) >= 2 or value["sequence"])
        ]
        valid_cells.sort(
            key=lambda item: (-item[1]["count"], item[0][0], item[0][1])
        )
        shift_inserted = 0
        outside_tolerance_candidates = 0
        for (d_bin, v_bin), evidence in valid_cells:
            d_start = d_bin * CELL_MS - shift
            v_start = v_bin * CELL_MS - shift
            left = _unmatched_rows_in_range(
                conn,
                source=0,
                start_ms=d_start,
                end_ms=d_start + CELL_MS,
            )
            right = _unmatched_rows_in_range(
                conn,
                source=1,
                start_ms=v_start,
                end_ms=v_start + CELL_MS,
            )
            if not left or not right:
                continue
            left_by_key: dict[bytes, list[tuple[int, int]]] = defaultdict(list)
            right_by_key: dict[bytes, list[tuple[int, int]]] = defaultdict(list)
            for row_id, match_key, timestamp_ms in left:
                left_by_key[match_key].append((row_id, timestamp_ms))
            for row_id, match_key, timestamp_ms in right:
                right_by_key[match_key].append((row_id, timestamp_ms))

            median_delta = int(statistics.median(evidence["deltas"]))
            proposed: list[tuple[int, int, int]] = []
            for match_key in left_by_key.keys() & right_by_key.keys():
                for d_id, d_ts in left_by_key[match_key]:
                    for v_id, v_ts in right_by_key[match_key]:
                        residual = abs((v_ts - d_ts) - median_delta)
                        if residual <= LOCAL_ALIGNMENT_TOLERANCE_MS:
                            proposed.append((residual, d_id, v_id))
                        else:
                            outside_tolerance_candidates += 1
            proposed.sort()
            used_left: set[int] = set()
            used_right: set[int] = set()
            pairs: list[tuple[int, int]] = []
            for _error, d_id, v_id in proposed:
                if d_id in used_left or v_id in used_right:
                    continue
                used_left.add(d_id)
                used_right.add(v_id)
                pairs.append((d_id, v_id))
            inserted = _insert_match_pairs(
                conn, pairs, f"cell{shift // 1000}s", primary=0
            )
            shift_inserted += inserted
        conn.commit()
        total_inserted += shift_inserted
        cell_reports.append(
            {
                "shift_ms": shift,
                "candidate_cells": len(grouped),
                "validated_cells": len(valid_cells),
                "inserted_matches": shift_inserted,
                "alignment_tolerance_ms": LOCAL_ALIGNMENT_TOLERANCE_MS,
                "outside_tolerance_candidates": outside_tolerance_candidates,
            }
        )
    return total_inserted, cell_reports


def _create_merged_rows(conn: sqlite3.Connection) -> int:
    conn.execute("DROP TABLE IF EXISTS merged_rows")
    conn.execute(
        """
        CREATE TABLE merged_rows (
            uid INTEGER NOT NULL,
            uname TEXT NOT NULL,
            content TEXT NOT NULL,
            ts_ms INTEGER NOT NULL,
            winner_source INTEGER NOT NULL,
            winner_native_id TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO merged_rows (
            uid, uname, content, ts_ms, winner_source, winner_native_id
        )
        SELECT
            CASE WHEN v.ts_ms < d.ts_ms THEN v.uid ELSE d.uid END,
            CASE
                WHEN v.ts_ms < d.ts_ms
                    THEN COALESCE(NULLIF(v.uname, ''), d.uname)
                ELSE COALESCE(NULLIF(d.uname, ''), v.uname)
            END,
            CASE WHEN v.ts_ms < d.ts_ms THEN v.content ELSE d.content END,
            MIN(d.ts_ms, v.ts_ms),
            CASE WHEN v.ts_ms < d.ts_ms THEN 1 ELSE 0 END,
            CASE WHEN v.ts_ms < d.ts_ms THEN v.native_id ELSE d.native_id END
        FROM matches m
        JOIN source_rows d ON d.id = m.d_id
        JOIN source_rows v ON v.id = m.v_id
        """
    )
    conn.execute(
        """
        INSERT INTO merged_rows (
            uid, uname, content, ts_ms, winner_source, winner_native_id
        )
        SELECT r.uid, r.uname, r.content, r.ts_ms, 0, r.native_id
        FROM source_rows r
        LEFT JOIN matches m ON m.d_id = r.id
        WHERE r.source = 0 AND m.d_id IS NULL
        """
    )
    conn.execute(
        """
        INSERT INTO merged_rows (
            uid, uname, content, ts_ms, winner_source, winner_native_id
        )
        SELECT r.uid, r.uname, r.content, r.ts_ms, 1, r.native_id
        FROM source_rows r
        LEFT JOIN matches m ON m.v_id = r.id
        WHERE r.source = 1 AND m.v_id IS NULL
        """
    )
    conn.execute("CREATE INDEX idx_merged_time ON merged_rows(ts_ms)")
    conn.commit()
    return int(conn.execute("SELECT COUNT(*) FROM merged_rows").fetchone()[0])


def _write_merged_jsonl(conn: sqlite3.Connection, path: Path) -> tuple[int, str]:
    temporary = path.with_suffix(path.suffix + ".tmp")
    digest = hashlib.sha256()
    count = 0
    with temporary.open("w", encoding="utf-8") as output:
        for uid, uname, content, ts_ms in conn.execute(
            """
            SELECT uid, uname, content, ts_ms
            FROM merged_rows
            ORDER BY ts_ms, rowid
            """
        ):
            row = {
                "room_id": ROOM_ID,
                "cmd": "DANMU_MSG",
                "uid": int(uid),
                "uname": str(uname),
                "content": str(content),
                "timestamp": int(ts_ms) // 1000,
            }
            encoded = (_json_dump(row) + "\n").encode("utf-8")
            output.write(encoded.decode("utf-8"))
            digest.update(encoded)
            count += 1
    os.replace(temporary, path)
    return count, digest.hexdigest()


def _create_staging_db(merge_conn: sqlite3.Connection, path: Path) -> tuple[int, str]:
    if path.exists():
        path.unlink()
    staging = sqlite3.connect(path)
    try:
        _configure_staging(staging)
        staging.execute(
            """
            CREATE TABLE event (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id INTEGER NOT NULL,
                cmd TEXT NOT NULL,
                uid INTEGER,
                uname TEXT,
                content TEXT,
                gift_name TEXT,
                gift_num INTEGER,
                total_coin INTEGER,
                title TEXT,
                timestamp INTEGER
            )
            """
        )
        batch: list[tuple[Any, ...]] = []
        for uid, uname, content, ts_ms in merge_conn.execute(
            "SELECT uid, uname, content, ts_ms FROM merged_rows ORDER BY ts_ms, rowid"
        ):
            batch.append(
                (
                    ROOM_ID,
                    "DANMU_MSG",
                    int(uid),
                    str(uname),
                    str(content),
                    int(ts_ms) // 1000,
                )
            )
            if len(batch) >= 25_000:
                staging.executemany(
                    """
                    INSERT INTO event (
                        room_id, cmd, uid, uname, content, timestamp
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    batch,
                )
                batch.clear()
        if batch:
            staging.executemany(
                """
                INSERT INTO event (
                    room_id, cmd, uid, uname, content, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                batch,
            )
        staging.commit()
        quick_check = str(staging.execute("PRAGMA quick_check").fetchone()[0])
        count = int(staging.execute("SELECT COUNT(*) FROM event").fetchone()[0])
        return count, quick_check
    finally:
        staging.close()


def _day_bounds_ms(day: str) -> tuple[int, int]:
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=TZ)
    start_ms = int(start.timestamp() * 1000)
    return start_ms, start_ms + 24 * 60 * 60 * 1000


def _build_audit(
    conn: sqlite3.Connection,
    *,
    source_counts: dict[str, int],
    source_rejections: dict[str, int],
    raw_source_counts: dict[str, int],
    exact_source_dedup: dict[str, Any],
    shifted_copy_dedup: dict[str, Any],
    merged_count: int,
    merged_hash: str,
    staging_count: int,
    staging_quick_check: str,
    cell_reports: list[dict[str, Any]],
    cutoff_ms: int,
) -> dict[str, Any]:
    matches = int(conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0])
    method_counts = {
        str(method): int(count)
        for method, count in conn.execute(
            "SELECT method, COUNT(*) FROM matches GROUP BY method ORDER BY method"
        )
    }
    invalid_merged = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM merged_rows
            WHERE uid <= 0 OR content = '' OR ts_ms <= 0 OR ts_ms >= ?
            """,
            (cutoff_ms,),
        ).fetchone()[0]
    )
    winner_counts = {
        ("danmakus" if int(source) == 0 else "vtbcat"): int(count)
        for source, count in conn.execute(
            """
            SELECT winner_source, COUNT(*)
            FROM merged_rows GROUP BY winner_source ORDER BY winner_source
            """
        )
    }
    date_counts: dict[str, Any] = {}
    for day in ("2026-01-26", "2026-03-27", "2026-04-20"):
        start_ms, end_ms = _day_bounds_ms(day)
        sources = {
            ("danmakus" if int(source) == 0 else "vtbcat"): int(count)
            for source, count in conn.execute(
                """
                SELECT source, COUNT(*) FROM source_rows
                WHERE ts_ms >= ? AND ts_ms < ?
                GROUP BY source
                """,
                (start_ms, end_ms),
            )
        }
        merged = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM merged_rows
                WHERE ts_ms >= ? AND ts_ms < ?
                """,
                (start_ms, end_ms),
            ).fetchone()[0]
        )
        matched = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM matches m
                JOIN source_rows d ON d.id = m.d_id
                WHERE d.ts_ms >= ? AND d.ts_ms < ?
                """,
                (start_ms, end_ms),
            ).fetchone()[0]
        )
        date_counts[day] = {
            "source_rows": sources,
            "matched_pairs_by_danmakus_day": matched,
            "merged_rows": merged,
        }

    source_accounting_ok = all(
        source_counts[name]
        == raw_source_counts[name]
        - int(exact_source_dedup["dropped_by_source"].get(name, 0))
        - int(shifted_copy_dedup["dropped_by_source"].get(name, 0))
        for name in ("danmakus", "vtbcat")
    )
    expected_merged = source_counts["danmakus"] + source_counts["vtbcat"] - matches
    status = (
        "ready"
        if merged_count == expected_merged
        and staging_count == merged_count
        and staging_quick_check == "ok"
        and invalid_merged == 0
        and source_accounting_ok
        and int(exact_source_dedup["invalid_drop_rows"]) == 0
        and int(shifted_copy_dedup["invalid_drop_rows"]) == 0
        and int(shifted_copy_dedup["identity_mismatches"]) == 0
        else "failed"
    )
    return {
        "created_at": datetime.now(TZ).isoformat(),
        "status": status,
        "source_rows_before_shifted_dedup": raw_source_counts,
        "source_rows": source_counts,
        "source_accounting_ok": source_accounting_ok,
        "exact_source_dedup": exact_source_dedup,
        "shifted_copy_dedup": shifted_copy_dedup,
        "rejected_nonpositive_timestamps": source_rejections,
        "matched_pairs": matches,
        "match_methods": method_counts,
        "anchor_cells": cell_reports,
        "merged_rows": merged_count,
        "expected_merged_rows": expected_merged,
        "merged_sha256": merged_hash,
        "winner_rows": winner_counts,
        "invalid_merged_rows": invalid_merged,
        "staging_rows": staging_count,
        "staging_quick_check": staging_quick_check,
        "date_checks": date_counts,
    }


def command_merge(args: argparse.Namespace) -> int:
    work_dir = args.work_dir.resolve()
    manifest_path = work_dir / "fetch_manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"缺少 {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    cutoff_ms = int(manifest["cutoff_ms"])
    merge_db = work_dir / "merge.db"
    if merge_db.exists():
        merge_db.unlink()
    conn = sqlite3.connect(merge_db)
    try:
        _configure_staging(conn)
        conn.executescript(
            """
            CREATE TABLE source_rows (
                id INTEGER PRIMARY KEY,
                source INTEGER NOT NULL,
                native_id TEXT NOT NULL,
                live_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                ts_ms INTEGER NOT NULL,
                uid INTEGER NOT NULL,
                uname TEXT NOT NULL,
                content TEXT NOT NULL,
                match_key BLOB NOT NULL,
                UNIQUE(source, native_id)
            );
            CREATE TABLE matches (
                d_id INTEGER NOT NULL UNIQUE,
                v_id INTEGER NOT NULL UNIQUE,
                method TEXT NOT NULL,
                primary_anchor INTEGER NOT NULL,
                FOREIGN KEY(d_id) REFERENCES source_rows(id),
                FOREIGN KEY(v_id) REFERENCES source_rows(id)
            );
            """
        )
        danmakus_count, danmakus_rejected = _load_source_jsonl(
            conn, work_dir / "danmakus.jsonl", "danmakus"
        )
        vtbcat_count, vtbcat_rejected = _load_source_jsonl(
            conn, work_dir / "vtbcat.jsonl", "vtbcat"
        )
        raw_source_counts = {
            "danmakus": danmakus_count,
            "vtbcat": vtbcat_count,
        }
        source_rejections = {
            "danmakus": danmakus_rejected,
            "vtbcat": vtbcat_rejected,
        }
        _log(f"源数据已载入：{raw_source_counts}")
        if any(source_rejections.values()):
            _log(f"已过滤非正时间戳：{source_rejections}")
        conn.executescript(
            """
            CREATE INDEX idx_rows_key_source_time
            ON source_rows(match_key, source, ts_ms, id);
            CREATE INDEX idx_rows_source_uid_time
            ON source_rows(source, uid, ts_ms, id);
            CREATE INDEX idx_rows_source_live_time
            ON source_rows(source, live_id, ts_ms, id);
            CREATE INDEX idx_rows_source_time
            ON source_rows(source, ts_ms, id);
            """
        )
        conn.commit()

        exact_source_dedup = _deduplicate_exact_source_rows(conn)
        _log(
            f"源内精确毫秒去重：删除 {exact_source_dedup['dropped_rows']} 条；"
            f"来源分布：{exact_source_dedup['dropped_by_source']}"
        )
        shifted_copy_dedup = _deduplicate_shifted_copies(conn)
        source_counts = {"danmakus": 0, "vtbcat": 0}
        for source, count in conn.execute(
            "SELECT source, COUNT(*) FROM source_rows GROUP BY source"
        ):
            source_counts["danmakus" if int(source) == 0 else "vtbcat"] = int(count)
        _log(
            f"偏移副本去重：删除 {shifted_copy_dedup['dropped_rows']} 条；"
            f"有效源数据：{source_counts}"
        )

        tight = _match_tight(conn)
        _log(f"严格 15 秒校准匹配：{tight} 对")
        cross_offset_inserted, cross_offset_reports = _match_cross_live_offsets(conn)
        _log(
            f"跨源场次固定偏移：{len(cross_offset_reports)} 对场次，"
            f"新增 {cross_offset_inserted} 对匹配"
        )
        uid_generated, uid_inserted = _create_shingles(conn, per_uid=True)
        _log(
            f"同 UID 三消息序列：生成 {uid_generated} 个指纹，新增 {uid_inserted} 对"
        )
        global_generated, global_inserted = _create_shingles(conn, per_uid=False)
        _log(
            f"全局四消息序列：生成 {global_generated} 个指纹，"
            f"新增 {global_inserted} 对"
        )
        local_inserted, cell_reports = _match_anchor_cells(conn)
        _log(f"可靠局部批次新增匹配：{local_inserted} 对")
        residual_offset_inserted, residual_offset_reports = (
            _match_cross_live_offsets(conn)
        )
        _log(
            f"剩余跨源固定偏移：{len(residual_offset_reports)} 对场次，"
            f"新增 {residual_offset_inserted} 对匹配"
        )

        merged_count = _create_merged_rows(conn)
        merged_count_written, merged_hash = _write_merged_jsonl(
            conn, work_dir / "merged.jsonl"
        )
        if merged_count_written != merged_count:
            raise RuntimeError("merged.jsonl 行数与 staging 表不一致")
        staging_count, staging_quick_check = _create_staging_db(
            conn, work_dir / "staging.db"
        )
        audit = _build_audit(
            conn,
            source_counts=source_counts,
            source_rejections=source_rejections,
            raw_source_counts=raw_source_counts,
            exact_source_dedup=exact_source_dedup,
            shifted_copy_dedup=shifted_copy_dedup,
            merged_count=merged_count,
            merged_hash=merged_hash,
            staging_count=staging_count,
            staging_quick_check=staging_quick_check,
            cell_reports=cell_reports,
            cutoff_ms=cutoff_ms,
        )
        audit["cross_live_offsets"] = {
            "offset_bin_ms": CROSS_OFFSET_BIN_MS,
            "residual_tolerance_ms": LOCAL_ALIGNMENT_TOLERANCE_MS,
            "minimum_unique_anchors": CROSS_MIN_UNIQUE_ANCHORS,
            "minimum_anchor_uids": CROSS_MIN_ANCHOR_UIDS,
            "minimum_alignment_share": CROSS_MIN_ALIGNMENT_SHARE,
            "candidate_live_pairs": (
                len(cross_offset_reports) + len(residual_offset_reports)
            ),
            "inserted_matches": (
                cross_offset_inserted + residual_offset_inserted
            ),
            "passes": [
                {
                    "stage": "after_tight",
                    "candidate_live_pairs": len(cross_offset_reports),
                    "inserted_matches": cross_offset_inserted,
                    "pairs": cross_offset_reports,
                },
                {
                    "stage": "after_sequence_and_cells",
                    "candidate_live_pairs": len(residual_offset_reports),
                    "inserted_matches": residual_offset_inserted,
                    "pairs": residual_offset_reports,
                },
            ],
        }
        audit["sequence_fingerprints"] = {
            "uid3_generated": uid_generated,
            "uid3_inserted_pairs": uid_inserted,
            "global4_generated": global_generated,
            "global4_inserted_pairs": global_inserted,
        }
        _atomic_json(work_dir / "audit.json", audit)
        _log(json.dumps(audit, ensure_ascii=False, indent=2))
        if audit["status"] != "ready":
            raise RuntimeError("离线审计未通过")
        return 0
    finally:
        conn.close()


def _backup_database(
    source_path: Path, backup_path: Path
) -> tuple[int, str]:
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    destination = sqlite3.connect(backup_path)
    last_percent = -1

    def progress(_status: int, remaining: int, total: int) -> None:
        nonlocal last_percent
        percent = int((total - remaining) * 100 / total) if total else 100
        if percent >= last_percent + 10 or remaining == 0:
            _log(f"  完整数据库备份 {percent}%")
            last_percent = percent

    try:
        source.backup(destination, pages=8192, progress=progress, sleep=0.05)
        quick_check = str(destination.execute("PRAGMA quick_check").fetchone()[0])
        pages = int(destination.execute("PRAGMA page_count").fetchone()[0])
        return pages, quick_check
    finally:
        destination.close()
        source.close()


def _backup_target_rows(
    db_path: Path,
    output_path: Path,
    cutoff_seconds: int,
) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        with gzip.open(output_path, "wt", encoding="utf-8") as output:
            for row in conn.execute(
                """
                SELECT id, room_id, cmd, uid, uname, content, gift_name,
                       gift_num, total_coin, title, timestamp
                FROM event
                WHERE room_id = ? AND cmd = 'DANMU_MSG' AND timestamp < ?
                ORDER BY id
                """,
                (ROOM_ID, cutoff_seconds),
            ):
                value = {
                    "id": row[0],
                    "room_id": row[1],
                    "cmd": row[2],
                    "uid": row[3],
                    "uname": row[4],
                    "content": row[5],
                    "gift_name": row[6],
                    "gift_num": row[7],
                    "total_coin": row[8],
                    "title": row[9],
                    "timestamp": row[10],
                }
                encoded = (_json_dump(value) + "\n").encode("utf-8")
                output.write(encoded.decode("utf-8"))
                digest.update(encoded)
                count += 1
                if count % 500_000 == 0:
                    _log(f"  已导出恢复清单 {count} 行")
        return count, digest.hexdigest()
    finally:
        conn.close()


def command_replace(args: argparse.Namespace) -> int:
    work_dir = args.work_dir.resolve()
    db_path = args.db.resolve()
    audit_path = work_dir / "audit.json"
    merge_db = work_dir / "merge.db"
    if not audit_path.exists() or not merge_db.exists():
        raise FileNotFoundError("缺少 audit.json 或 merge.db")
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    if audit.get("status") != "ready":
        raise RuntimeError("audit.json 未标记 ready，拒绝替换")
    cutoff_ms = int(
        json.loads((work_dir / "fetch_manifest.json").read_text(encoding="utf-8"))[
            "cutoff_ms"
        ]
    )
    cutoff_seconds = cutoff_ms // 1000
    backup_dir = args.backup_dir.resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(TZ).strftime("%Y%m%d_%H%M%S")
    full_backup = backup_dir / f"libot_before_danmaku_rebuild_{stamp}.db"
    rows_backup = backup_dir / f"danmaku_before_rebuild_{stamp}.jsonl.gz"

    _log(f"完整备份生产库 -> {full_backup}")
    pages, backup_quick_check = _backup_database(db_path, full_backup)
    if backup_quick_check != "ok":
        raise RuntimeError(f"完整备份 quick_check={backup_quick_check}")
    _log(f"导出待替换弹幕恢复清单 -> {rows_backup}")
    old_count, rows_hash = _backup_target_rows(
        db_path, rows_backup, cutoff_seconds
    )

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout=60000")
    try:
        before_quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
        if before_quick_check != "ok":
            raise RuntimeError(f"生产库替换前 quick_check={before_quick_check}")
        conn.execute("ATTACH DATABASE ? AS rebuild", (str(merge_db),))
        new_count = int(
            conn.execute("SELECT COUNT(*) FROM rebuild.merged_rows").fetchone()[0]
        )
        invalid = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM rebuild.merged_rows
                WHERE uid <= 0 OR content = '' OR ts_ms <= 0 OR ts_ms >= ?
                """,
                (cutoff_ms,),
            ).fetchone()[0]
        )
        if invalid:
            raise RuntimeError(f"合并 staging 有 {invalid} 行无效记录")
        if new_count != int(audit["merged_rows"]):
            raise RuntimeError("merge.db 与 audit.json 行数不一致")

        _log(f"开始事务：删除旧弹幕 {old_count} 行，导入新弹幕 {new_count} 行")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """
                DELETE FROM event
                WHERE room_id = ? AND cmd = 'DANMU_MSG' AND timestamp < ?
                """,
                (ROOM_ID, cutoff_seconds),
            )
            conn.execute(
                """
                INSERT INTO event (
                    room_id, cmd, uid, uname, content, gift_name,
                    gift_num, total_coin, title, timestamp
                )
                SELECT ?, 'DANMU_MSG', uid, uname, content,
                       NULL, NULL, NULL, NULL, CAST(ts_ms / 1000 AS INTEGER)
                FROM rebuild.merged_rows
                ORDER BY ts_ms, rowid
                """,
                (ROOM_ID,),
            )
            actual = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM event
                    WHERE room_id = ? AND cmd = 'DANMU_MSG' AND timestamp < ?
                    """,
                    (ROOM_ID, cutoff_seconds),
                ).fetchone()[0]
            )
            if actual != new_count:
                raise RuntimeError(
                    f"事务内计数不一致：预期 {new_count}，实际 {actual}"
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        conn.execute("DETACH DATABASE rebuild")
        checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        after_quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
        if after_quick_check != "ok":
            raise RuntimeError(f"生产库替换后 quick_check={after_quick_check}")
    finally:
        conn.close()

    report = {
        "completed_at": datetime.now(TZ).isoformat(),
        "db_path": str(db_path),
        "cutoff_seconds": cutoff_seconds,
        "old_rows": old_count,
        "new_rows": new_count,
        "full_backup": str(full_backup),
        "full_backup_pages": pages,
        "full_backup_quick_check": backup_quick_check,
        "rows_backup": str(rows_backup),
        "rows_backup_sha256_uncompressed": rows_hash,
        "wal_checkpoint": list(checkpoint),
        "production_quick_check": after_quick_check,
    }
    report_path = backup_dir / f"danmaku_rebuild_report_{stamp}.json"
    _atomic_json(report_path, report)
    _log(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    fetch = subparsers.add_parser("fetch", help="抓取并暂存两源 JSONL")
    fetch.add_argument("--work-dir", type=Path, required=True)
    fetch.add_argument("--cutoff", default=DEFAULT_CUTOFF)
    fetch.add_argument("--request-interval", type=float, default=0.55)
    fetch.add_argument("--max-lives", type=int)
    fetch.set_defaults(func=command_fetch)

    merge = subparsers.add_parser("merge", help="离线匹配、合并并审计")
    merge.add_argument("--work-dir", type=Path, required=True)
    merge.set_defaults(func=command_merge)

    replace = subparsers.add_parser("replace", help="备份并事务式替换生产数据")
    replace.add_argument("--work-dir", type=Path, required=True)
    replace.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    replace.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    replace.set_defaults(func=command_replace)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
