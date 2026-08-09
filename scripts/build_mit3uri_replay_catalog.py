#!/usr/bin/env python3
"""Conservatively merge 三理Mit3uri replay BVs into live sessions.

This script deliberately does not use same-day/same-title equality as a
session key. Exact catalog/title timestamps establish session instances first.
Date-only videos are assigned one-to-one using title and duration evidence;
uncertain rows remain in ``unresolved_recordings``.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RAW = ROOT / "data" / "mit3uri_replay_raw.json"
DEFAULT_DB = ROOT / "data" / "libot.db"
DEFAULT_OUTPUT = ROOT / "data" / "mit3uri_replay_catalog.json"
DEFAULT_MERGE_OVERRIDES = (
    ROOT / "scripts" / "resources" / "mit3uri_replay_session_merges.json"
)
TZ = timezone(timedelta(hours=8))
ROOM_ID = 1967216004
MIN_AUDITED_SESSION_SECONDS = 6 * 60
DATABASE_SESSION_COVERAGE_START = date(2026, 4, 20)
DATABASE_RECONNECT_GAP_SECONDS = 5 * 60

SOURCE_RANK = {"danmakus": 0, "vtbcat": 1, "database": 2, "replay_title": 3}
PRECISION_RANK = {"second": 0, "minute": 1, "hour": 2, "date": 3}

COMPACT_PATTERN = re.compile(r"(?P<date>20\d{6})[-_](?P<time>\d{6})(?:[-_]\d+)?")
CHINESE_PATTERN = re.compile(
    r"(?P<year>20\d{2})年\s*(?P<month>\d{1,2})月\s*(?P<day>\d{1,2})日"
    r"(?:\s*(?P<hour>\d{1,2})点"
    r"(?:(?P<minute>\d{1,2})分|场)?)?"
)
SEPARATED_PATTERN = re.compile(
    r"(?P<year>20\d{2})[-./](?P<month>\d{1,2})[-./](?P<day>\d{1,2})"
    r"(?:\s+(?P<hour>\d{1,2})[:_](?P<minute>\d{1,2})"
    r"(?:[:_](?P<second>\d{1,2}))?)?"
)


@dataclass
class ParsedTitle:
    day: date | None
    start_hint: datetime | None
    precision: str | None
    display_title: str
    match_title: str


@dataclass
class Evidence:
    source: str
    native_id: str
    start: datetime
    end: datetime | None
    title: str


@dataclass
class Session:
    internal_id: int
    day: date
    start: datetime | None
    start_precision: str
    title: str
    evidence: list[Evidence] = field(default_factory=list)
    recordings: list[dict[str, Any]] = field(default_factory=list)
    replay_only: bool = False


def _local_datetime(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, TZ)


def _local_walltime_timestamp(timestamp: float) -> datetime:
    """Decode a local wall-clock value that was serialized as a UTC epoch."""
    return datetime.fromtimestamp(timestamp, timezone.utc).replace(tzinfo=TZ)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def _parse_iso(value: Any) -> datetime | None:
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
    return parsed.astimezone(TZ)


def _remove_datetime(text: str) -> str:
    text = COMPACT_PATTERN.sub(" ", text)
    text = CHINESE_PATTERN.sub(" ", text)
    text = SEPARATED_PATTERN.sub(" ", text)
    return text


def _display_title(text: str) -> str:
    value = _remove_datetime(text)
    value = re.sub(r"^录制-\d+-", "", value)
    value = re.sub(
        r"【\s*(?:直播回放|弹幕版|三理录播|三理Mit3uri\s*录播|"
        r"三理Mit3uri录播|三理Mit3uri)\s*】",
        "",
        value,
    )
    value = re.sub(r"^\s*(?:录制出错请移步[）)]?)", "", value)
    value = re.sub(r"\s+", " ", value).strip(" _-—：:，,")
    return value or text.strip()


def _match_title(text: str) -> str:
    value = _remove_datetime(text).lower()
    value = re.sub(r"【[^】]*】|\[[^\]]*]", "", value)
    value = re.sub(
        r"三理mit3uri|mit3uri|三理|直播回放|直播录播|录播|弹幕版|"
        r"录制出错请移步|录制|点场",
        "",
        value,
    )
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value)


def _title_similarity(left: str, right: str) -> float:
    a = _match_title(left)
    b = _match_title(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if min(len(a), len(b)) >= 4 and (a in b or b in a):
        containment = min(len(a), len(b)) / max(len(a), len(b))
        return max(0.82, containment)
    return SequenceMatcher(None, a, b).ratio()


def parse_replay_title(title: str) -> ParsedTitle:
    compact = COMPACT_PATTERN.search(title)
    if compact:
        dt = datetime.strptime(
            compact.group("date") + compact.group("time"), "%Y%m%d%H%M%S"
        ).replace(tzinfo=TZ)
        return ParsedTitle(
            dt.date(), dt, "second", _display_title(title), _match_title(title)
        )

    chinese = CHINESE_PATTERN.search(title)
    if chinese:
        values = {
            key: int(value) for key, value in chinese.groupdict().items() if value
        }
        day = date(values["year"], values["month"], values["day"])
        hour = values.get("hour")
        if hour is None:
            return ParsedTitle(
                day, None, "date", _display_title(title), _match_title(title)
            )
        minute = values.get("minute")
        precision = "minute" if minute is not None else "hour"
        dt = datetime.combine(day, datetime.min.time(), TZ).replace(
            hour=hour, minute=minute or 0
        )
        return ParsedTitle(
            day, dt, precision, _display_title(title), _match_title(title)
        )

    separated = SEPARATED_PATTERN.search(title)
    if separated:
        values = {
            key: int(value)
            for key, value in separated.groupdict().items()
            if value is not None
        }
        day = date(values["year"], values["month"], values["day"])
        if "hour" not in values:
            return ParsedTitle(
                day, None, "date", _display_title(title), _match_title(title)
            )
        precision = "second" if "second" in values else "minute"
        dt = datetime.combine(day, datetime.min.time(), TZ).replace(
            hour=values["hour"],
            minute=values["minute"],
            second=values.get("second", 0),
        )
        return ParsedTitle(
            day, dt, precision, _display_title(title), _match_title(title)
        )

    return ParsedTitle(None, None, None, _display_title(title), _match_title(title))


def _load_raw(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("raw_archives")
    sources = payload.get("sources")
    if not isinstance(rows, list) or not isinstance(sources, list):
        raise ValueError(f"{path} 缺少 raw_archives/sources")
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        bvid = str(raw.get("bvid") or "").strip()
        if not bvid or bvid in seen:
            continue
        seen.add(bvid)
        title = str(raw.get("title") or "").strip()
        parsed = parse_replay_title(title)
        normalized.append(
            {
                **raw,
                "bvid": bvid,
                "title": title,
                "duration_seconds": int(raw.get("duration_seconds") or 0),
                "parsed_day": parsed.day.isoformat() if parsed.day else None,
                "parsed_start": _iso(parsed.start_hint),
                "parsed_precision": parsed.precision,
                "display_title": parsed.display_title,
                "match_title": parsed.match_title,
            }
        )
    return normalized, sources


def _load_merge_overrides(
    path: Path,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, str]],
    list[dict[str, str]],
]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError(f"{path} 的 schema_version 必须为 1")
    groups = payload.get("groups")
    if not isinstance(groups, list):
        raise ValueError(f"{path} 缺少 groups 列表")

    normalized: list[dict[str, Any]] = []
    claimed: dict[str, int] = {}
    for index, value in enumerate(groups, 1):
        if not isinstance(value, dict):
            raise ValueError(f"{path} groups[{index}] 必须是对象")
        anchor_bvid = str(value.get("anchor_bvid") or "").strip()
        merge_bvids = [
            str(item).strip()
            for item in value.get("merge_bvids") or []
            if str(item).strip()
        ]
        reason = str(value.get("reason") or "").strip()
        if not anchor_bvid or not merge_bvids or not reason:
            raise ValueError(
                f"{path} groups[{index}] 必须提供 anchor_bvid/merge_bvids/reason"
            )
        bvids = [anchor_bvid, *merge_bvids]
        if len(set(bvids)) != len(bvids):
            raise ValueError(f"{path} groups[{index}] 内存在重复 BVID")
        for bvid in bvids:
            previous = claimed.get(bvid)
            if previous is not None:
                raise ValueError(
                    f"{path} 的 {bvid} 同时出现在 groups[{previous}] 和 "
                    f"groups[{index}]"
                )
            claimed[bvid] = index
        normalized.append(
            {
                "anchor_bvid": anchor_bvid,
                "merge_bvids": merge_bvids,
                "reason": reason,
            }
        )
    excluded_external = payload.get("excluded_external_bvids") or []
    if not isinstance(excluded_external, list):
        raise ValueError(f"{path} excluded_external_bvids 必须是列表")
    normalized_external: list[dict[str, str]] = []
    for index, value in enumerate(excluded_external, 1):
        if not isinstance(value, dict):
            raise ValueError(
                f"{path} excluded_external_bvids[{index}] 必须是对象"
            )
        bvid = str(value.get("bvid") or "").strip()
        reason = str(value.get("reason") or "").strip()
        if not bvid or not reason:
            raise ValueError(
                f"{path} excluded_external_bvids[{index}] 必须提供 bvid/reason"
            )
        if bvid in claimed:
            raise ValueError(f"{path} 的 {bvid} 同时用于合并和外部直播排除")
        claimed[bvid] = -index
        normalized_external.append({"bvid": bvid, "reason": reason})
    start_time_overrides = payload.get("start_time_overrides") or []
    if not isinstance(start_time_overrides, list):
        raise ValueError(f"{path} start_time_overrides 必须是列表")
    normalized_starts: list[dict[str, str]] = []
    for index, value in enumerate(start_time_overrides, 1):
        if not isinstance(value, dict):
            raise ValueError(f"{path} start_time_overrides[{index}] 必须是对象")
        bvid = str(value.get("bvid") or "").strip()
        start_time = str(value.get("start_time") or "").strip()
        reason = str(value.get("reason") or "").strip()
        if not bvid or not start_time or not reason:
            raise ValueError(
                f"{path} start_time_overrides[{index}] "
                "必须提供 bvid/start_time/reason"
            )
        parsed = _parse_iso(start_time)
        if parsed is None:
            raise ValueError(
                f"{path} start_time_overrides[{index}] 的时间无效"
            )
        if bvid in claimed:
            raise ValueError(f"{path} 的 {bvid} 同时用于多种人工覆盖")
        claimed[bvid] = -(len(normalized_external) + index)
        normalized_starts.append(
            {
                "bvid": bvid,
                "start_time": parsed.isoformat(timespec="seconds"),
                "reason": reason,
            }
        )
    return normalized, normalized_external, normalized_starts


def _load_danmakus(
    path: Path,
    reference_evidence: list[Evidence],
) -> list[Evidence]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    lives = (payload.get("data") or {}).get("lives")
    if not isinstance(lives, list):
        raise ValueError(f"{path} 缺少 data.lives")
    result: list[Evidence] = []
    for live in lives:
        if not isinstance(live, dict):
            continue
        start_ms = live.get("startDate")
        if not isinstance(start_ms, (int, float)) or start_ms <= 0:
            continue
        stop_ms = live.get("stopDate")
        raw_start = _local_datetime(float(start_ms) / 1000)
        raw_end = (
            _local_datetime(float(stop_ms) / 1000)
            if isinstance(stop_ms, (int, float)) and stop_ms > start_ms
            else None
        )
        title = str(live.get("title") or "").strip()

        # Danmakus changed timestamp conventions around 2026-01-24. Older
        # rows usually encode Beijing wall time as UTC; newer rows use real
        # Unix timestamps. Only a near-exact independent timestamp may
        # override that date-based default.
        matching_references: list[Evidence] = []
        for item in reference_evidence:
            if (
                min(
                    abs((raw_start - item.start).total_seconds()),
                    abs((raw_start - timedelta(hours=8) - item.start).total_seconds()),
                )
                <= 12 * 3600
                and _title_similarity(title, item.title) >= 0.58
            ):
                matching_references.append(item)

        unshifted_distance = min(
            (
                abs((raw_start - item.start).total_seconds())
                for item in matching_references
            ),
            default=float("inf"),
        )
        shifted_start = raw_start - timedelta(hours=8)
        shifted_distance = min(
            (
                abs((shifted_start - item.start).total_seconds())
                for item in matching_references
            ),
            default=float("inf"),
        )
        shifted_end = raw_end - timedelta(hours=8) if raw_end else None

        def overlap_score(
            candidate_start: datetime,
            candidate_end: datetime | None,
        ) -> float:
            if candidate_end is None or candidate_end <= candidate_start:
                return 0.0
            duration = (candidate_end - candidate_start).total_seconds()
            scores = []
            for item in matching_references:
                if item.end is None or item.end <= item.start:
                    continue
                overlap = (
                    min(candidate_end, item.end) - max(candidate_start, item.start)
                ).total_seconds()
                if overlap > 0:
                    scores.append(overlap / duration)
            return max(scores, default=0.0)

        unshifted_overlap = overlap_score(raw_start, raw_end)
        shifted_overlap = overlap_score(shifted_start, shifted_end)
        default_shifted = raw_start.date() < date(2026, 1, 24)
        if shifted_overlap >= 0.80 and shifted_overlap > unshifted_overlap + 0.20:
            start = shifted_start
            end = shifted_end
        elif unshifted_overlap >= 0.80 and unshifted_overlap > shifted_overlap + 0.20:
            start = raw_start
            end = raw_end
        elif (
            not default_shifted
            and shifted_distance <= 15 * 60
            and shifted_distance + 5 * 60 < unshifted_distance
        ):
            start = shifted_start
            end = shifted_end
        elif (
            default_shifted
            and unshifted_distance <= 15 * 60
            and unshifted_distance + 5 * 60 < shifted_distance
        ):
            start = raw_start
            end = raw_end
        elif default_shifted:
            start = shifted_start
            end = shifted_end
        else:
            start = raw_start
            end = raw_end
        result.append(
            Evidence(
                "danmakus",
                str(live.get("liveId") or ""),
                start,
                end,
                title,
            )
        )

    # Collapse exact duplicates and overlapping/short reconnect segments.
    # Same title alone is insufficient: two long, non-overlapping streams
    # remain separate even when they occur on the same date.
    deduplicated: list[Evidence] = []
    for item in sorted(result, key=lambda value: (value.start, value.native_id)):
        item_duration = (
            (item.end - item.start).total_seconds() if item.end is not None else None
        )
        duplicate = next(
            (
                existing
                for existing in reversed(deduplicated[-8:])
                if _title_similarity(existing.title, item.title) >= 0.75
                and (
                    item_duration is None or item_duration > MIN_AUDITED_SESSION_SECONDS
                )
                and (
                    existing.end is None
                    or (existing.end - existing.start).total_seconds()
                    > MIN_AUDITED_SESSION_SECONDS
                )
                and (
                    abs((existing.start - item.start).total_seconds()) <= 90
                    or (
                        existing.end is not None
                        and item.end is not None
                        and (
                            item.start <= existing.end
                            or (
                                item.start - existing.end <= timedelta(minutes=20)
                                and (
                                    existing.end - existing.start
                                    <= timedelta(minutes=30)
                                    or item.end - item.start <= timedelta(minutes=30)
                                )
                            )
                        )
                    )
                )
            ),
            None,
        )
        if duplicate is None:
            deduplicated.append(item)
        else:
            duplicate.start = min(duplicate.start, item.start)
            if item.end and (not duplicate.end or item.end > duplicate.end):
                duplicate.end = item.end
            if item.native_id not in duplicate.native_id.split(","):
                duplicate.native_id += f",{item.native_id}"
    return deduplicated


def _load_vtbcat(path: Path) -> list[Evidence]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    lives = payload.get("Lives")
    if not isinstance(lives, list):
        raise ValueError(f"{path} 缺少 Lives")
    result: list[Evidence] = []
    for live in lives:
        if not isinstance(live, dict):
            continue
        start_at = live.get("StartAt")
        start = (
            _local_walltime_timestamp(float(start_at))
            if isinstance(start_at, (int, float)) and start_at > 0
            else _parse_iso(live.get("CreatedAt"))
        )
        if start is None:
            continue
        end_value = live.get("EndAt")
        end = (
            _local_datetime(float(end_value))
            if isinstance(end_value, (int, float)) and end_value > start.timestamp()
            else None
        )
        if end and end - start > timedelta(days=1):
            end = None
        result.append(
            Evidence(
                "vtbcat",
                str(live.get("ID") or ""),
                start,
                end,
                str(live.get("Title") or "").strip(),
            )
        )
    return result


def _is_auditable_evidence(item: Evidence) -> bool:
    if item.end is None:
        return True
    return (item.end - item.start).total_seconds() > MIN_AUDITED_SESSION_SECONDS


def _load_database(path: Path) -> list[Evidence]:
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as conn:
        markers = conn.execute(
            """
            SELECT cmd, timestamp, id
            FROM event
            WHERE room_id = ? AND cmd IN ('LIVE', 'PREPARING')
            ORDER BY timestamp, id
            """,
            (ROOM_ID,),
        ).fetchall()
        room_changes = conn.execute(
            """
            SELECT timestamp, title
            FROM event
            WHERE room_id = ? AND cmd = 'ROOM_CHANGE' AND title IS NOT NULL
            ORDER BY timestamp, id
            """,
            (ROOM_ID,),
        ).fetchall()

    sessions: list[tuple[int, int | None]] = []
    current: int | None = None
    for cmd, timestamp, _row_id in markers:
        if timestamp is None:
            continue
        ts = int(timestamp)
        if cmd == "LIVE":
            if current is None:
                current = ts
        elif current is not None:
            if ts >= current:
                sessions.append((current, ts))
            current = None
    if current is not None:
        sessions.append((current, int(datetime.now(TZ).timestamp())))

    collapsed_sessions: list[tuple[int, int | None]] = []
    for start_ts, end_ts in sessions:
        if (
            collapsed_sessions
            and collapsed_sessions[-1][1] is not None
            and start_ts - int(collapsed_sessions[-1][1])
            <= DATABASE_RECONNECT_GAP_SECONDS
        ):
            previous_start, previous_end = collapsed_sessions[-1]
            collapsed_sessions[-1] = (
                previous_start,
                max(int(previous_end), end_ts) if end_ts is not None else None,
            )
        else:
            collapsed_sessions.append((start_ts, end_ts))
    sessions = collapsed_sessions

    result: list[Evidence] = []
    for index, (start_ts, end_ts) in enumerate(sessions):
        candidates = [
            (abs(int(ts) - start_ts), str(title))
            for ts, title in room_changes
            if start_ts - 15 * 60 <= int(ts) <= start_ts + 30 * 60
        ]
        title = min(candidates)[1] if candidates else ""
        result.append(
            Evidence(
                "database",
                str(index),
                _local_datetime(start_ts),
                _local_datetime(end_ts) if end_ts else None,
                title,
            )
        )
    return result


def _session_reference_start(session: Session) -> datetime | None:
    if session.start and session.start_precision != "second":
        return session.start
    # VTB.cat StartAt preserves the actual live start even when its crawler
    # CreatedAt (and Danmakus coverage) begins later.
    for source in ("vtbcat", "danmakus", "database"):
        values = [item.start for item in session.evidence if item.source == source]
        if values:
            return min(values)
    return session.start


def _session_reference_duration(session: Session) -> int | None:
    catalog_duration: int | None = None
    for source in ("vtbcat", "danmakus", "database"):
        for item in session.evidence:
            if item.source == source and item.end and item.end > item.start:
                catalog_duration = int((item.end - item.start).total_seconds())
                break
        if catalog_duration is not None:
            break
    if not session.recordings:
        return catalog_duration
    by_priority: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in session.recordings:
        by_priority[int(row["source_priority"])].append(row)
    replay_candidates: list[int] = []
    for source_rows in by_priority.values():
        if any(
            str(row.get("match_method", "")).startswith("same_source_segment")
            for row in source_rows
        ):
            replay_candidates.append(
                sum(int(row["duration_seconds"]) for row in source_rows)
            )
        else:
            # The same UP often publishes a normal version and a danmaku
            # version. They are alternatives, not consecutive pieces.
            replay_candidates.append(
                max(int(row["duration_seconds"]) for row in source_rows)
            )
    replay_duration = max(replay_candidates)
    return max(catalog_duration or 0, replay_duration) or None


def _add_evidence(session: Session, evidence: Evidence) -> None:
    session.evidence.append(evidence)
    reference = _session_reference_start(session)
    if reference:
        session.start = reference
        session.start_precision = "second"
        session.day = reference.date()
    preferred = min(
        session.evidence,
        key=lambda item: (
            SOURCE_RANK.get(item.source, 99),
            0 if item.title else 1,
        ),
    )
    if preferred.title:
        session.title = preferred.title


def build_baseline(evidence_rows: list[Evidence]) -> list[Session]:
    sessions: list[Session] = []
    next_id = 1
    for evidence in sorted(
        evidence_rows,
        key=lambda item: (SOURCE_RANK[item.source], item.start, item.native_id),
    ):
        candidates: list[tuple[float, float, Session]] = []
        for session in sessions:
            reference = _session_reference_start(session)
            if reference is None or reference.date() != evidence.start.date():
                continue
            delta = abs((reference - evidence.start).total_seconds())
            similarity = _title_similarity(session.title, evidence.title)
            same_source = any(
                item.source == evidence.source for item in session.evidence
            )
            interval_overlaps = []
            if evidence.end is not None and evidence.end > evidence.start:
                evidence_duration = (evidence.end - evidence.start).total_seconds()
                for item in session.evidence:
                    if item.end is None or item.end <= item.start:
                        continue
                    overlap = (
                        min(item.end, evidence.end) - max(item.start, evidence.start)
                    ).total_seconds()
                    if overlap > 0:
                        shorter = min(
                            evidence_duration,
                            (item.end - item.start).total_seconds(),
                        )
                        interval_overlaps.append(overlap / shorter)
            strong_interval_overlap = (
                bool(interval_overlaps)
                and max(interval_overlaps) >= 0.80
                and similarity >= 0.58
            )
            interval_continuity = similarity >= 0.75 and any(
                item.end is not None
                and item.start - timedelta(minutes=2)
                <= evidence.start
                <= item.end + timedelta(minutes=20)
                for item in session.evidence
            )
            if same_source and not (
                delta <= 90 or interval_continuity or strong_interval_overlap
            ):
                continue
            if (
                delta <= 90
                or delta <= 5 * 60
                or (delta <= 25 * 60 and similarity >= 0.28)
                or interval_continuity
                or strong_interval_overlap
            ):
                candidates.append((delta, -similarity, session))
        if candidates:
            _add_evidence(
                min(candidates, key=lambda item: (item[0], item[1]))[2], evidence
            )
            continue
        sessions.append(
            Session(
                next_id,
                evidence.start.date(),
                evidence.start,
                "second",
                evidence.title,
                evidence=[evidence],
            )
        )
        next_id += 1
    return sessions


def _recording_start(row: dict[str, Any]) -> datetime | None:
    return _parse_iso(row.get("parsed_start"))


def _attach(
    session: Session,
    row: dict[str, Any],
    method: str,
    confidence: str,
) -> None:
    previous_best_priority = min(
        (int(item["source_priority"]) for item in session.recordings),
        default=999,
    )
    previous_reference_start = _session_reference_start(session)
    previous_reference_duration = _session_reference_duration(session)
    session.recordings.append(
        {
            **row,
            "match_method": method,
            "match_confidence": confidence,
        }
    )
    if session.replay_only:
        parsed_start = _recording_start(row)
        precision = str(row.get("parsed_precision") or "date")
        row_priority = int(row["source_priority"])
        if parsed_start and (
            session.start is None
            or row_priority < previous_best_priority
            or (
                row_priority == previous_best_priority
                and PRECISION_RANK[precision] < PRECISION_RANK[session.start_precision]
            )
        ):
            session.start = parsed_start
            session.start_precision = precision
    else:
        parsed_start = _recording_start(row)
        row_duration = int(row["duration_seconds"])
        if (
            int(row["source_priority"]) == 1
            and parsed_start
            and previous_reference_start
            and parsed_start < previous_reference_start - timedelta(minutes=30)
            and previous_reference_start - parsed_start <= timedelta(hours=8)
            and (
                previous_reference_duration is None
                or row_duration
                >= previous_reference_duration
                + int((previous_reference_start - parsed_start).total_seconds() * 0.65)
            )
        ):
            session.start = parsed_start
            session.start_precision = str(row.get("parsed_precision") or "hour")
    if not session.title:
        session.title = str(row.get("display_title") or row.get("title") or "")


def _same_source_count(session: Session, row: dict[str, Any]) -> int:
    priority = int(row["source_priority"])
    return sum(int(item["source_priority"]) == priority for item in session.recordings)


def _independent_interval(session: Session) -> tuple[datetime, datetime] | None:
    intervals = [
        (item.start, item.end)
        for item in session.evidence
        if item.source in {"danmakus", "vtbcat", "database"}
        and item.end is not None
        and item.end > item.start
    ]
    if not intervals:
        return None
    return min(start for start, _end in intervals), max(
        end for _start, end in intervals
    )


def _hint_interval(row: dict[str, Any]) -> tuple[datetime, datetime] | None:
    start = _recording_start(row)
    if start is None:
        return None
    precision = str(row.get("parsed_precision") or "")
    width = {
        "second": timedelta(seconds=1),
        "minute": timedelta(minutes=1),
        "hour": timedelta(hours=1),
    }.get(precision)
    return (start, start + width) if width else None


def _recording_possible_intervals(
    row: dict[str, Any],
) -> list[tuple[datetime, datetime]]:
    hint = _hint_interval(row)
    duration = int(row.get("duration_seconds") or 0)
    if hint is None or duration <= 0:
        return []
    hint_start, hint_end = hint
    direct = (hint_start, hint_end + timedelta(seconds=duration))
    result = [direct]

    # Some recorder titles contain the capture/end timestamp rather than the
    # start timestamp. It cannot be a start if the resulting video would end
    # after its Bilibili publication time.
    pubdate = row.get("pubdate")
    if isinstance(pubdate, (int, float)) and pubdate > 0:
        published = _local_datetime(float(pubdate))
        if direct[1] > published + timedelta(minutes=15):
            result.append((hint_start - timedelta(seconds=duration), hint_end))
    return result


def _same_source_segment_supported(
    session: Session,
    row: dict[str, Any],
    candidates: list[tuple[float, float, float, Session]],
) -> bool:
    """Require independent interval evidence before joining same-UP BVs."""
    if not _same_source_count(session, row):
        return False
    independent = _independent_interval(session)
    hint = _hint_interval(row)
    if independent is None or hint is None:
        return False
    independent_start, independent_end = independent
    hint_start, hint_end = hint
    if hint_end < independent_start - timedelta(
        minutes=20
    ) or hint_start > independent_end + timedelta(minutes=20):
        return False
    if _title_similarity(str(row["display_title"]), session.title) < 0.85:
        return False

    reference_duration = _session_reference_duration(session)
    if not reference_duration:
        return False
    same_source_total = sum(
        int(item["duration_seconds"])
        for item in session.recordings
        if int(item["source_priority"]) == int(row["source_priority"])
    ) + int(row["duration_seconds"])
    if not 0.70 <= same_source_total / reference_duration <= 1.30:
        return False

    # If the hint also fits another independently timed, similarly titled
    # session, the title is ambiguous and the BV must stay separate.
    for _delta, title_neg, _duration_neg, other in candidates:
        if other is session or -title_neg < 0.75:
            continue
        other_interval = _independent_interval(other)
        if other_interval is None:
            continue
        other_start, other_end = other_interval
        if not (
            hint_end < other_start - timedelta(minutes=20)
            or hint_start > other_end + timedelta(minutes=20)
        ):
            return False
    return True


def _candidate_sessions(
    sessions: list[Session], row: dict[str, Any]
) -> list[tuple[float, float, float, Session]]:
    day_text = row.get("parsed_day")
    if not day_text:
        return []
    day = date.fromisoformat(str(day_text))
    hint = _recording_start(row)
    precision = row.get("parsed_precision")
    result: list[tuple[float, float, float, Session]] = []
    for session in sessions:
        reference = _session_reference_start(session)
        if session.day != day:
            if (
                hint is None
                or reference is None
                or abs((hint - reference).total_seconds()) > 8 * 3600
            ):
                continue
        title_score = _title_similarity(str(row["display_title"]), session.title)
        duration = _session_reference_duration(session)
        row_duration = int(row["duration_seconds"])
        duration_score = (
            min(duration, row_duration) / max(duration, row_duration)
            if duration and row_duration
            else 0.0
        )
        if hint and reference:
            delta = abs((hint - reference).total_seconds())
            if precision in {"minute", "second"} and delta > 8 * 3600:
                continue
            if precision == "hour" and delta > 8 * 3600:
                continue
        else:
            delta = 24 * 60 * 60
        result.append((delta, -title_score, -duration_score, session))
    return sorted(result, key=lambda item: (item[0], item[1], item[2]))


def attach_precise_recordings(
    sessions: list[Session],
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    unresolved: list[dict[str, Any]] = []
    next_id = max((session.internal_id for session in sessions), default=0) + 1
    precise = sorted(
        rows,
        key=lambda row: (
            str(row.get("parsed_day") or ""),
            int(row["source_priority"]),
            PRECISION_RANK.get(str(row.get("parsed_precision")), 99),
            str(row["bvid"]),
        ),
    )
    for row in precise:
        precision = row.get("parsed_precision")
        if precision not in {"second", "minute", "hour"}:
            unresolved.append(row)
            continue
        candidates = _candidate_sessions(sessions, row)
        chosen: Session | None = None
        method = ""
        confidence = ""
        if candidates:
            best_delta, best_title_neg, best_duration_neg, best = candidates[0]
            best_title = -best_title_neg
            best_duration = -best_duration_neg
            if precision in {"second", "minute"}:
                if best_delta <= 10 * 60 or (
                    best_delta <= 45 * 60 and best_title >= 0.45
                ):
                    chosen = best
                    method = "title_exact_time"
                    confidence = "high" if best_delta <= 5 * 60 else "medium"
                elif (
                    len(
                        fingerprints := [
                            item
                            for item in candidates
                            if item[0] <= 8 * 3600
                            and -item[1] >= 0.96
                            and -item[2] >= 0.94
                        ]
                    )
                    == 1
                ):
                    chosen = fingerprints[0][3]
                    method = "cross_source_title_duration_fingerprint"
                    confidence = "medium"
                elif (
                    best_delta <= 8 * 3600
                    and best_title >= 0.88
                    and best_duration >= 0.94
                    and (
                        len(candidates) == 1
                        or (
                            best_title - (-candidates[1][1]) >= 0.12
                            or best_duration - (-candidates[1][2]) >= 0.12
                        )
                    )
                ):
                    chosen = best
                    method = "cross_day_title_duration"
                    confidence = "medium"
            elif precision == "hour":
                same_hour = [
                    item
                    for item in candidates
                    if _session_reference_start(item[3]) and item[0] <= 90 * 60
                ]
                if len(same_hour) == 1:
                    chosen = same_hour[0][3]
                    method = "title_hour_unique"
                    confidence = "high" if -same_hour[0][1] >= 0.45 else "medium"
                elif same_hour:
                    margin = (
                        (-same_hour[0][1]) - (-same_hour[1][1])
                        if len(same_hour) > 1
                        else 1.0
                    )
                    if best_title >= 0.60 and margin >= 0.15:
                        chosen = same_hour[0][3]
                        method = "title_hour_disambiguated"
                        confidence = "medium"
                if chosen is None:
                    exact_title = [
                        item
                        for item in candidates
                        if item[0] <= 8 * 3600 and -item[1] >= 0.96 and -item[2] >= 0.94
                    ]
                    if len(exact_title) == 1:
                        chosen = exact_title[0][3]
                        best_title = -exact_title[0][1]
                        method = "title_hour_duration_fingerprint"
                        confidence = "medium"
                if chosen is None:
                    strong = [
                        item
                        for item in candidates
                        if item[0] <= 8 * 3600 and -item[1] >= 0.88 and -item[2] >= 0.94
                    ]
                    if len(strong) == 1:
                        chosen = strong[0][3]
                        best_title = -strong[0][1]
                        method = "title_hour_duration_unique"
                        confidence = "medium"
            if chosen and _same_source_count(chosen, row):
                if not _same_source_segment_supported(chosen, row, candidates):
                    chosen = None
                else:
                    method = "same_source_segment_independent_interval"
                    confidence = "high"
            if chosen is None:
                supported_segments = [
                    item[3]
                    for item in candidates
                    if _same_source_segment_supported(item[3], row, candidates)
                ]
                if len(supported_segments) == 1:
                    chosen = supported_segments[0]
                    method = "same_source_segment_independent_interval"
                    confidence = "high"
        if chosen:
            _attach(chosen, row, method, confidence)
            continue

        hint = _recording_start(row)
        assert hint is not None
        session = Session(
            next_id,
            hint.date(),
            hint,
            str(precision),
            str(row["display_title"]),
            replay_only=True,
        )
        _attach(session, row, "replay_title_new_session", "medium")
        sessions.append(session)
        next_id += 1
    return unresolved, next_id


def attach_date_only_recordings(
    sessions: list[Session],
    rows: list[dict[str, Any]],
    next_id: int,
) -> tuple[list[dict[str, Any]], int]:
    unresolved: list[dict[str, Any]] = []
    used_by_source_session: set[tuple[int, int]] = {
        (int(row["source_priority"]), session.internal_id)
        for session in sessions
        for row in session.recordings
    }
    for row in sorted(
        rows,
        key=lambda item: (
            str(item.get("parsed_day") or ""),
            int(item["source_priority"]),
            str(item["bvid"]),
        ),
    ):
        if not row.get("parsed_day"):
            unresolved.append({**row, "unresolved_reason": "title_has_no_date"})
            continue
        candidates = _candidate_sessions(sessions, row)
        scored: list[tuple[float, float, float, Session]] = []
        for _delta, title_neg, duration_neg, session in candidates:
            title_score = -title_neg
            duration_score = -duration_neg
            if (
                int(row["source_priority"]),
                session.internal_id,
            ) in used_by_source_session:
                continue
            score = 0.68 * title_score + 0.32 * duration_score
            scored.append((score, title_score, duration_score, session))
        scored.sort(key=lambda item: (-item[0], -item[1], -item[2]))

        chosen: Session | None = None
        method = ""
        confidence = ""
        if scored:
            best = scored[0]
            margin = best[0] - scored[1][0] if len(scored) > 1 else 1.0
            if (
                best[1] >= 0.45
                and (best[2] >= 0.58 or best[1] >= 0.88)
                and margin >= 0.10
            ):
                chosen = best[3]
                method = "date_title_duration_assignment"
                confidence = "high" if best[1] >= 0.75 and best[2] >= 0.75 else "medium"
        if chosen:
            _attach(chosen, row, method, confidence)
            used_by_source_session.add(
                (int(row["source_priority"]), chosen.internal_id)
            )
            continue

        # Cross-UP deduplication without a catalog timestamp is allowed only
        # for a near-identical title and duration. Same-UP rows stay distinct.
        day = date.fromisoformat(str(row["parsed_day"]))
        replay_candidates: list[tuple[float, float, Session]] = []
        for session in sessions:
            if not session.replay_only or session.day != day:
                continue
            if any(
                int(item["source_priority"]) == int(row["source_priority"])
                for item in session.recordings
            ):
                continue
            title_score = _title_similarity(str(row["display_title"]), session.title)
            reference_duration = _session_reference_duration(session)
            duration_score = (
                min(reference_duration, int(row["duration_seconds"]))
                / max(reference_duration, int(row["duration_seconds"]))
                if reference_duration and int(row["duration_seconds"])
                else 0.0
            )
            if title_score >= 0.88 and duration_score >= 0.78:
                replay_candidates.append((title_score, duration_score, session))
        replay_candidates.sort(key=lambda item: (-item[0], -item[1]))
        if len(replay_candidates) == 1:
            chosen = replay_candidates[0][2]
            _attach(chosen, row, "cross_up_title_duration", "medium")
            used_by_source_session.add(
                (int(row["source_priority"]), chosen.internal_id)
            )
            continue

        session = Session(
            next_id,
            day,
            None,
            "date",
            str(row["display_title"]),
            replay_only=True,
        )
        _attach(session, row, "date_only_new_session", "low")
        sessions.append(session)
        next_id += 1
        unresolved.append(
            {
                **row,
                "unresolved_reason": (
                    "ambiguous_date_only_candidates"
                    if scored or len(replay_candidates) > 1
                    else "date_only_no_independent_time_evidence"
                ),
                "candidate_internal_ids": [item[3].internal_id for item in scored[:5]],
            }
        )
    return unresolved, next_id


def _recording_priorities(session: Session) -> set[int]:
    return {int(row["source_priority"]) for row in session.recordings}


def _merge_session_into(anchor: Session, other: Session) -> None:
    anchor_priority = min(_recording_priorities(anchor), default=999)
    other_priority = min(_recording_priorities(other), default=999)
    if other_priority < anchor_priority:
        anchor.start = other.start
        anchor.start_precision = other.start_precision
        anchor.title = other.title
        anchor.day = other.day
    anchor.evidence.extend(other.evidence)
    anchor.recordings.extend(other.recordings)
    anchor.replay_only = not anchor.evidence
    if anchor.recordings:
        preferred = min(
            anchor.recordings,
            key=lambda row: (
                int(row["source_priority"]),
                0 if row.get("display_title") else 1,
            ),
        )
        if preferred.get("display_title"):
            anchor.title = str(preferred["display_title"])
    reference = _session_reference_start(anchor)
    if reference:
        anchor.day = reference.date()
    database_starts = [
        item.start for item in anchor.evidence if item.source == "database"
    ]
    if anchor.day >= DATABASE_SESSION_COVERAGE_START and database_starts:
        anchor.start = min(database_starts)
        anchor.start_precision = "second"
        anchor.day = anchor.start.date()


def apply_merge_overrides(
    sessions: list[Session],
    groups: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], set[str]]:
    """Merge manually audited recordings using stable BVID anchors."""
    audit: list[dict[str, Any]] = []
    resolved_bvids: set[str] = set()
    for group in groups:
        anchor_bvid = str(group["anchor_bvid"])
        requested_bvids = [anchor_bvid, *group["merge_bvids"]]
        session_by_bvid = {
            str(row["bvid"]): session
            for session in sessions
            for row in session.recordings
            if str(row["bvid"]) in requested_bvids
        }
        missing = [bvid for bvid in requested_bvids if bvid not in session_by_bvid]
        if missing:
            raise ValueError(
                "人工场次合并覆盖引用了目录中不存在的 BVID: "
                + ", ".join(missing)
            )

        anchor = session_by_bvid[anchor_bvid]
        others: list[Session] = []
        for bvid in group["merge_bvids"]:
            other = session_by_bvid[str(bvid)]
            if other is anchor or other in others:
                continue
            others.append(other)

        audit.append(
            {
                "method": "manual_bvid_override",
                "anchor_bvid": anchor_bvid,
                "merge_bvids": list(group["merge_bvids"]),
                "reason": str(group["reason"]),
                "kept_internal_id": anchor.internal_id,
                "merged_internal_ids": [item.internal_id for item in others],
                "already_merged": not others,
            }
        )
        for other in others:
            _merge_session_into(anchor, other)
            sessions.remove(other)
        resolved_bvids.update(requested_bvids)
    return audit, resolved_bvids


def apply_start_time_overrides(
    sessions: list[Session],
    overrides: list[dict[str, str]],
) -> tuple[list[dict[str, Any]], set[str]]:
    """Apply exact own-room recording timestamps audited from video metadata."""

    audit: list[dict[str, Any]] = []
    resolved_bvids: set[str] = set()
    for value in overrides:
        bvid = value["bvid"]
        matches = [
            session
            for session in sessions
            if any(str(row["bvid"]) == bvid for row in session.recordings)
        ]
        if len(matches) != 1:
            raise ValueError(
                f"开播时间覆盖 {bvid} 应命中一个场次，实际命中 {len(matches)} 个"
            )
        session = matches[0]
        start = _parse_iso(value["start_time"])
        if start is None:
            raise ValueError(f"开播时间覆盖 {bvid} 的时间无效")
        if session.evidence:
            raise ValueError(
                f"开播时间覆盖 {bvid} 已带独立场次证据，应修正匹配而非覆盖"
            )
        session.start = start
        session.start_precision = "second"
        session.day = start.date()
        audit.append(
            {
                "method": "manual_bvid_start_time",
                "bvid": bvid,
                "start_time": _iso(start),
                "reason": value["reason"],
                "internal_id": session.internal_id,
            }
        )
        resolved_bvids.add(bvid)
    return audit, resolved_bvids


def exclude_external_recordings(
    sessions: list[Session],
    exclusions: list[dict[str, str]],
) -> tuple[list[Session], list[dict[str, Any]]]:
    """Remove recordings made in another liver's room from own-room totals."""

    excluded_sessions: list[Session] = []
    audit: list[dict[str, Any]] = []
    for value in exclusions:
        bvid = value["bvid"]
        matches = [
            session
            for session in sessions
            if any(str(row["bvid"]) == bvid for row in session.recordings)
        ]
        if len(matches) != 1:
            raise ValueError(
                f"外部直播排除 {bvid} 应命中一个场次，实际命中 {len(matches)} 个"
            )
        session = matches[0]
        if session.evidence:
            raise ValueError(
                f"外部直播排除 {bvid} 已带三理主直播间独立证据，拒绝排除"
            )
        other_bvids = [
            str(row["bvid"])
            for row in session.recordings
            if str(row["bvid"]) != bvid
        ]
        if other_bvids:
            raise ValueError(
                f"外部直播排除 {bvid} 与其他录播已合并: {other_bvids}"
            )
        sessions.remove(session)
        excluded_sessions.append(session)
        audit.append(
            {
                "method": "manual_external_room_exclusion",
                "bvid": bvid,
                "reason": value["reason"],
                "internal_id": session.internal_id,
                "date": session.day.isoformat(),
                "title": session.title,
            }
        )
    return excluded_sessions, audit


def _independent_intervals_are_distinct(left: Session, right: Session) -> bool:
    left_intervals = [
        item
        for item in left.evidence
        if item.source == "danmakus" and item.end is not None
    ]
    right_intervals = [
        item
        for item in right.evidence
        if item.source == "danmakus" and item.end is not None
    ]
    if not left_intervals or not right_intervals:
        return False
    return all(
        a.end + timedelta(minutes=20) < b.start
        or b.end + timedelta(minutes=20) < a.start
        for a in left_intervals
        for b in right_intervals
    )


def consolidate_sessions(
    sessions: list[Session],
) -> tuple[list[dict[str, Any]], set[str]]:
    audit: list[dict[str, Any]] = []
    resolved_bvids: set[str] = set()
    while True:
        proposals: list[tuple[float, str, Session, Session]] = []

        # A replay-only row may carry the title used later in the same live,
        # while the independently timed row keeps the opening title. Permit a
        # title-agnostic merge only for a mutual, unique same-day duration
        # fingerprint from disjoint recording UPs. Ambiguous matches remain
        # separate for manual review.
        replay_only_sessions = [
            item for item in sessions if item.recordings and not item.evidence
        ]
        catalog_backed_sessions = [
            item for item in sessions if item.recordings and item.evidence
        ]

        def duration_fingerprint(left: Session, right: Session) -> bool:
            left_duration = _session_reference_duration(left)
            right_duration = _session_reference_duration(right)
            if not left_duration or not right_duration:
                return False
            return abs(left_duration - right_duration) <= max(
                90,
                int(max(left_duration, right_duration) * 0.02),
            )

        replay_matches: dict[int, list[Session]] = {}
        catalog_matches: dict[int, list[Session]] = {}
        for replay_session in replay_only_sessions:
            replay_mids = {
                int(row["source_mid"]) for row in replay_session.recordings
            }
            matches: list[Session] = []
            for catalog_session in catalog_backed_sessions:
                if replay_session.day != catalog_session.day:
                    continue
                catalog_mids = {
                    int(row["source_mid"]) for row in catalog_session.recordings
                }
                if replay_mids & catalog_mids:
                    continue
                if duration_fingerprint(replay_session, catalog_session):
                    matches.append(catalog_session)
                    catalog_matches.setdefault(catalog_session.internal_id, []).append(
                        replay_session
                    )
            replay_matches[replay_session.internal_id] = matches

        for replay_session in replay_only_sessions:
            matches = replay_matches.get(replay_session.internal_id, [])
            if len(matches) != 1:
                continue
            catalog_session = matches[0]
            if len(catalog_matches.get(catalog_session.internal_id, [])) != 1:
                continue
            proposals.append(
                (
                    121,
                    "mutual_unique_cross_up_duration_title_change",
                    catalog_session,
                    replay_session,
                )
            )

        # Title changes during a stream can make the catalog and replay names
        # completely different. Use duration alone only when the catalog
        # session has independent evidence and exactly one same-day replay
        # candidate is within 2%; otherwise leave it unresolved.
        for catalog_session in sessions:
            if catalog_session.recordings or not any(
                item.source in {"danmakus", "vtbcat"}
                for item in catalog_session.evidence
            ):
                continue
            catalog_duration = _session_reference_duration(catalog_session)
            if not catalog_duration:
                continue
            duration_matches = []
            for recorded_session in sessions:
                if (
                    not recorded_session.recordings
                    or recorded_session.day != catalog_session.day
                ):
                    continue
                replay_duration = _session_reference_duration(recorded_session)
                if not replay_duration:
                    continue
                if abs(catalog_duration - replay_duration) <= max(
                    180, int(max(catalog_duration, replay_duration) * 0.02)
                ):
                    duration_matches.append(recorded_session)
            if len(duration_matches) == 1:
                proposals.append(
                    (
                        120,
                        "unique_same_day_catalog_duration",
                        duration_matches[0],
                        catalog_session,
                    )
                )

            # A combined replay title can preserve multiple room-title changes
            # from one live. This is stronger than a same-title match: require
            # at least two distinct independently observed titles, all covered
            # by one and only one date-only recording.
            evidence_titles = {
                _match_title(item.title)
                for item in catalog_session.evidence
                if item.title and _match_title(item.title)
            }
            title_matches = [
                recorded_session
                for recorded_session in sessions
                if recorded_session.recordings
                and recorded_session.day == catalog_session.day
                and _session_reference_start(recorded_session) is None
                and len(evidence_titles) >= 2
                and all(
                    _title_similarity(title, recorded_session.title) >= 0.82
                    for title in evidence_titles
                )
            ]
            if len(title_matches) == 1:
                proposals.append(
                    (
                        115,
                        "combined_title_covers_independent_title_changes",
                        title_matches[0],
                        catalog_session,
                    )
                )

        for left_index, left in enumerate(sessions):
            for right in sessions[left_index + 1 :]:
                if abs((left.day - right.day).days) > 1:
                    continue
                if 1 in _recording_priorities(left) and 1 in _recording_priorities(
                    right
                ):
                    # Two official replay BVs are independent unless they were
                    # already joined by the segment-sum rule.
                    continue
                if _independent_intervals_are_distinct(left, right):
                    continue

                overlap_ratios: list[float] = []
                for left_item in left.evidence:
                    if left_item.source != "danmakus" or left_item.end is None:
                        continue
                    for right_item in right.evidence:
                        if right_item.source != "danmakus" or right_item.end is None:
                            continue
                        overlap = (
                            min(left_item.end, right_item.end)
                            - max(left_item.start, right_item.start)
                        ).total_seconds()
                        shorter = min(
                            (left_item.end - left_item.start).total_seconds(),
                            (right_item.end - right_item.start).total_seconds(),
                        )
                        if overlap > 0 and shorter > 0:
                            overlap_ratios.append(overlap / shorter)
                if overlap_ratios and max(overlap_ratios) >= 0.80:
                    anchor, other = (
                        (left, right)
                        if min(_recording_priorities(left), default=999)
                        <= min(_recording_priorities(right), default=999)
                        else (right, left)
                    )
                    proposals.append(
                        (
                            125 + max(overlap_ratios),
                            "overlapping_danmakus_intervals",
                            anchor,
                            other,
                        )
                    )

                left_start = _session_reference_start(left)
                right_start = _session_reference_start(right)
                left_duration = _session_reference_duration(left)
                right_duration = _session_reference_duration(right)
                title_score = _title_similarity(left.title, right.title)
                duration_score = (
                    min(left_duration, right_duration)
                    / max(left_duration, right_duration)
                    if left_duration and right_duration
                    else 0.0
                )

                # A recording covering a later partial catalog/reconnect entry
                # is strong evidence that both entries are one logical live.
                for anchor, other, anchor_start, other_start, anchor_duration in (
                    (left, right, left_start, right_start, left_duration),
                    (right, left, right_start, left_start, right_duration),
                ):
                    if anchor.recordings and not other.recordings:
                        independent = _independent_interval(other)
                        if independent is not None:
                            independent_start, independent_end = independent
                            independent_duration = (
                                independent_end - independent_start
                            ).total_seconds()
                            interval_overlaps = []
                            for row in anchor.recordings:
                                for (
                                    recording_start,
                                    recording_end,
                                ) in _recording_possible_intervals(row):
                                    overlap = (
                                        min(recording_end, independent_end)
                                        - max(recording_start, independent_start)
                                    ).total_seconds()
                                    if overlap > 0 and independent_duration > 0:
                                        interval_overlaps.append(
                                            overlap / independent_duration
                                        )
                            if (
                                interval_overlaps
                                and max(interval_overlaps) >= 0.70
                                and title_score >= 0.75
                            ):
                                proposals.append(
                                    (
                                        122 + max(interval_overlaps),
                                        "recording_interval_covers_catalog",
                                        anchor,
                                        other,
                                    )
                                )

                    if (
                        anchor.recordings
                        and not other.recordings
                        and anchor_start
                        and other_start
                        and anchor_duration
                        and anchor_start - timedelta(minutes=5)
                        <= other_start
                        <= anchor_start
                        + timedelta(seconds=anchor_duration)
                        + timedelta(minutes=20)
                    ):
                        database_only_blank = (
                            not other.title
                            and other.evidence
                            and all(
                                item.source == "database" for item in other.evidence
                            )
                        )
                        if title_score >= 0.90:
                            proposals.append(
                                (
                                    110 + title_score,
                                    "recording_covers_partial",
                                    anchor,
                                    other,
                                )
                            )
                        elif database_only_blank:
                            proposals.append(
                                (
                                    100,
                                    "recording_covers_database_reconnect",
                                    anchor,
                                    other,
                                )
                            )

                if not left.recordings or not right.recordings:
                    continue
                delta = (
                    abs((left_start - right_start).total_seconds())
                    if left_start and right_start
                    else None
                )
                near_identical_duration = (
                    left_duration is not None
                    and right_duration is not None
                    and abs(left_duration - right_duration)
                    <= max(90, int(max(left_duration, right_duration) * 0.012))
                )
                if (
                    near_identical_duration
                    and left.day == right.day
                    and title_score >= 0.30
                    and (delta is None or delta <= 2 * 3600)
                ):
                    proposals.append(
                        (
                            95 + title_score,
                            "cross_up_duration_fingerprint",
                            left,
                            right,
                        )
                    )
                    continue
                if left_start and right_start and left_duration and right_duration:
                    left_end = left_start + timedelta(seconds=left_duration)
                    right_end = right_start + timedelta(seconds=right_duration)
                    overlaps = left_start <= right_end and right_start <= left_end
                    if overlaps and title_score >= 0.75 and duration_score >= 0.55:
                        proposals.append(
                            (
                                85 + title_score + duration_score,
                                "cross_up_overlapping_title_duration",
                                left,
                                right,
                            )
                        )

        if not proposals:
            break
        proposals.sort(key=lambda item: -item[0])
        _score, method, anchor, other = proposals[0]
        if anchor not in sessions or other not in sessions:
            continue
        resolved_bvids.update(
            str(row["bvid"]) for row in anchor.recordings + other.recordings
        )
        audit.append(
            {
                "method": method,
                "kept_internal_id": anchor.internal_id,
                "merged_internal_id": other.internal_id,
                "kept_date": anchor.day.isoformat(),
                "merged_date": other.day.isoformat(),
                "left_title": anchor.title,
                "right_title": other.title,
                "kept_duration_seconds": _session_reference_duration(anchor),
                "merged_duration_seconds": _session_reference_duration(other),
                "kept_bvids": [
                    str(row["bvid"]) for row in anchor.recordings
                ],
                "merged_bvids": [
                    str(row["bvid"]) for row in other.recordings
                ],
            }
        )
        _merge_session_into(anchor, other)
        sessions.remove(other)
    return audit, resolved_bvids


def _serialize_session(session: Session, session_id: str) -> dict[str, Any]:
    recordings = sorted(
        session.recordings,
        key=lambda row: (
            int(row["source_priority"]),
            str(row["bvid"]),
        ),
    )
    priorities = sorted({int(row["source_priority"]) for row in recordings})
    preferred_priority = priorities[0] if priorities else None
    preferred_rows = [
        row for row in recordings if int(row["source_priority"]) == preferred_priority
    ]
    if any(
        str(row.get("match_method", "")).startswith("same_source_segment")
        for row in preferred_rows
    ):
        preferred_bvid_mode = "segments"
    elif len(preferred_rows) > 1:
        preferred_bvid_mode = "alternatives"
    elif preferred_rows:
        preferred_bvid_mode = "single"
    else:
        preferred_bvid_mode = None
    duration = _session_reference_duration(session)
    evidence = sorted(
        session.evidence,
        key=lambda item: (SOURCE_RANK.get(item.source, 99), item.native_id),
    )
    return {
        "session_id": session_id,
        "title": session.title,
        "date": session.day.isoformat(),
        "start_time": _iso(_session_reference_start(session)),
        "start_time_precision": session.start_precision,
        "duration_seconds": duration,
        "duration_hms": (
            f"{duration // 3600:02d}:{duration % 3600 // 60:02d}:{duration % 60:02d}"
            if duration is not None
            else None
        ),
        "preferred_source_priority": preferred_priority,
        "preferred_bvid_mode": preferred_bvid_mode,
        "preferred_bvids": [row["bvid"] for row in preferred_rows],
        "recordings": [
            {
                "bvid": row["bvid"],
                "title": row["title"],
                "duration_seconds": row["duration_seconds"],
                "source_priority": row["source_priority"],
                "source_up_name": row["up_name"],
                "source_mid": row["source_mid"],
                "source_url": row["source_url"],
                "match_method": row["match_method"],
                "match_confidence": row["match_confidence"],
            }
            for row in recordings
        ],
        "historical_sources": [
            {
                "source": item.source,
                "native_id": item.native_id,
                "start_time": _iso(item.start),
                "end_time": _iso(item.end),
                "title": item.title,
            }
            for item in evidence
        ],
        "has_danmakus": any(item.source == "danmakus" for item in evidence),
        "has_vtbcat": any(item.source == "vtbcat" for item in evidence),
        "has_database_session": any(item.source == "database" for item in evidence),
        "replay_only_session": session.replay_only and not evidence,
    }


def build_catalog(
    raw_path: Path,
    db_path: Path,
    danmakus_path: Path,
    vtbcat_path: Path,
    merge_overrides_path: Path,
) -> dict[str, Any]:
    rows, sources = _load_raw(raw_path)
    (
        merge_overrides,
        external_exclusions,
        start_time_overrides,
    ) = _load_merge_overrides(merge_overrides_path)
    vtbcat_evidence = _load_vtbcat(vtbcat_path)
    database_evidence = _load_database(db_path)
    danmakus_evidence = _load_danmakus(
        danmakus_path,
        vtbcat_evidence + database_evidence,
    )
    all_evidence = danmakus_evidence + vtbcat_evidence + database_evidence
    excluded_short_evidence = [
        item for item in all_evidence if not _is_auditable_evidence(item)
    ]
    evidence = [item for item in all_evidence if _is_auditable_evidence(item)]
    sessions = build_baseline(evidence)
    unresolved, next_id = attach_precise_recordings(sessions, rows)
    date_unresolved, _next_id = attach_date_only_recordings(
        sessions, unresolved, next_id
    )
    start_override_audit, start_override_resolved_bvids = (
        apply_start_time_overrides(sessions, start_time_overrides)
    )
    override_audit, override_resolved_bvids = apply_merge_overrides(
        sessions,
        merge_overrides,
    )
    consolidation_audit, resolved_bvids = consolidate_sessions(sessions)
    resolved_bvids.update(override_resolved_bvids)
    resolved_bvids.update(start_override_resolved_bvids)
    excluded_external_sessions, external_exclusion_audit = (
        exclude_external_recordings(sessions, external_exclusions)
    )
    resolved_bvids.update(
        value["bvid"] for value in external_exclusions
    )
    date_unresolved = [
        row for row in date_unresolved if str(row["bvid"]) not in resolved_bvids
    ]

    sessions.sort(
        key=lambda item: (
            item.day,
            _session_reference_start(item)
            or datetime.combine(item.day, datetime.max.time(), TZ),
            item.internal_id,
        )
    )
    dirty_monitored_sessions = [
        item
        for item in sessions
        if item.day >= DATABASE_SESSION_COVERAGE_START
        and (_session_reference_duration(item) or 0) > MIN_AUDITED_SESSION_SECONDS
        and (
            not any(evidence.source == "database" for evidence in item.evidence)
            or _session_reference_start(item) is None
            or item.start_precision != "second"
        )
    ]
    if dirty_monitored_sessions:
        details = "; ".join(
            f"{item.day.isoformat()} {item.title} "
            f"({','.join(str(row['bvid']) for row in item.recordings)})"
            for item in dirty_monitored_sessions
        )
        raise ValueError(
            "数据库 LIVE/PREPARING 覆盖期仍存在无秒级主直播间锚点的计数场次；"
            "请合并同场切片、修正精度或显式排除外部直播: " + details
        )
    audited_sessions = [
        item
        for item in sessions
        if (_session_reference_duration(item) or 0) > MIN_AUDITED_SESSION_SECONDS
    ]
    excluded_short_recording_sessions = [
        _serialize_session(item, f"EXCLUDED-SHORT-{index:04d}")
        for index, item in enumerate(
            (
                item
                for item in sessions
                if item.recordings
                and (_session_reference_duration(item) or 0)
                <= MIN_AUDITED_SESSION_SECONDS
            ),
            1,
        )
    ]
    serialized_external_sessions = [
        _serialize_session(item, f"EXCLUDED-EXTERNAL-{index:04d}")
        for index, item in enumerate(
            sorted(
                excluded_external_sessions,
                key=lambda item: (item.day, item.internal_id),
            ),
            1,
        )
    ]
    serialized = [
        _serialize_session(session, f"MIT3URI-{index:04d}")
        for index, session in enumerate(audited_sessions, 1)
    ]
    unresolved_bvids = {str(row["bvid"]) for row in date_unresolved}
    recorded_sessions = [item for item in serialized if item["recordings"]]
    missing = [item for item in serialized if not item["recordings"]]
    title_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in serialized:
        normalized_title = _match_title(str(item["title"]))
        if normalized_title:
            title_groups[(str(item["date"]), normalized_title)].append(item)
    same_title_collision_groups = [
        {
            "date": day,
            "normalized_title": normalized_title,
            "sessions": [
                {
                    "session_id": item["session_id"],
                    "start_time": item["start_time"],
                    "duration_hms": item["duration_hms"],
                    "title": item["title"],
                }
                for item in items
            ],
        }
        for (day, normalized_title), items in sorted(title_groups.items())
        if len(items) > 1
    ]
    absent_vtb = [item for item in recorded_sessions if not item["has_vtbcat"]]
    absent_both = [
        item
        for item in recorded_sessions
        if not item["has_vtbcat"] and not item["has_danmakus"]
    ]
    exact_baseline_dates = [
        item["date"]
        for item in serialized
        if item["has_danmakus"] or item["has_vtbcat"]
    ]
    return {
        "generated_at": datetime.now(TZ).isoformat(),
        "timezone": "Asia/Shanghai",
        "channel_uid": 2030198123,
        "room_id": ROOM_ID,
        "merge_policy": {
            "same_date_and_title_is_not_a_session_key": True,
            "date_only_assignment": (
                "one-to-one per UP using independent session time, title, and duration"
            ),
            "same_up_segments": (
                "merged only when the timestamp hint overlaps an independent "
                "live interval, summed duration fits, and no competing interval fits"
            ),
            "same_title_alone_never_merges_sessions": True,
            "same_up_multiple_versions_are_alternatives_unless_segment_proven": True,
            "manual_bvid_merge_overrides": str(merge_overrides_path),
            "external_room_recordings": (
                "retained in the database with included_in_total=0"
            ),
            "database_coverage_invariant": (
                f"no included replay-only session on or after "
                f"{DATABASE_SESSION_COVERAGE_START.isoformat()}"
            ),
            "database_reconnect_gap_seconds": DATABASE_RECONNECT_GAP_SECONDS,
            "title_change_duration_fingerprint": (
                "same day, disjoint recording UPs, one replay-only row, and a "
                "mutual unique duration match within max(90 seconds, 2%)"
            ),
            "minimum_audited_session_seconds": MIN_AUDITED_SESSION_SECONDS,
            "uncertain_rows_are_retained": True,
        },
        "sources": sources,
        "consolidation_audit": [
            *start_override_audit,
            *override_audit,
            *consolidation_audit,
            *external_exclusion_audit,
        ],
        "coverage": {
            "bilibili_source_rows": len(rows),
            "unique_bvids": len({str(row["bvid"]) for row in rows}),
            "sessions_total": len(serialized),
            "sessions_with_recording": len(recorded_sessions),
            "sessions_without_recording": len(missing),
            "same_title_collision_groups": len(same_title_collision_groups),
            "excluded_source_records_at_most_0_1h": len(excluded_short_evidence),
            "excluded_recording_sessions_at_most_0_1h": len(
                excluded_short_recording_sessions
            ),
            "excluded_external_room_sessions": len(
                serialized_external_sessions
            ),
            "recorded_sessions_absent_vtbcat": len(absent_vtb),
            "recorded_sessions_absent_vtbcat_and_danmakus": len(absent_both),
            "unresolved_recordings": len(unresolved_bvids),
            "independent_catalog_coverage_start": (
                min(exact_baseline_dates) if exact_baseline_dates else None
            ),
            "independent_catalog_coverage_end": (
                max(exact_baseline_dates) if exact_baseline_dates else None
            ),
        },
        "sessions": serialized,
        "excluded_short_source_records": [
            {
                "source": item.source,
                "native_id": item.native_id,
                "start_time": _iso(item.start),
                "end_time": _iso(item.end),
                "duration_seconds": (
                    int((item.end - item.start).total_seconds())
                    if item.end is not None
                    else None
                ),
                "title": item.title,
            }
            for item in excluded_short_evidence
        ],
        "excluded_short_recording_sessions": excluded_short_recording_sessions,
        "excluded_external_recording_sessions": serialized_external_sessions,
        "same_title_collision_groups": same_title_collision_groups,
        "sessions_without_recording": missing,
        "recorded_sessions_absent_vtbcat": absent_vtb,
        "recorded_sessions_absent_vtbcat_and_danmakus": absent_both,
        "unresolved_recordings": date_unresolved,
        "limitations": [
            (
                "Danmakus/VTB.cat 的独立场次目录始于 2025-04-27；更早时期只能"
                "确认已有录播，无法仅凭这些数据证明某个未录播场次曾经存在。"
            ),
            (
                "仅写日期且无法唯一匹配精确场次的 BV 被保留为独立低置信场次，"
                "同时列入 unresolved_recordings，没有因同标题而强行合并。"
            ),
            (
                "时长不超过 0.1 小时的开播记录及录播占位不计入场次总数，"
                "但分别完整保留在 excluded_short_source_records 和 "
                "excluded_short_recording_sessions。"
            ),
            (
                "明确来自其他主播房间的联动或活动录播保留在 "
                "excluded_external_recording_sessions，不计入三理主直播间场次。"
            ),
        ],
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw", type=Path, default=DEFAULT_RAW)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--danmakus-catalog", type=Path, required=True)
    parser.add_argument("--vtbcat-catalog", type=Path, required=True)
    parser.add_argument(
        "--merge-overrides",
        type=Path,
        default=DEFAULT_MERGE_OVERRIDES,
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    payload = build_catalog(
        args.raw.resolve(),
        args.db.resolve(),
        args.danmakus_catalog.resolve(),
        args.vtbcat_catalog.resolve(),
        args.merge_overrides.resolve(),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(args.output)
    print(json.dumps(payload["coverage"], ensure_ascii=False, indent=2))
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
