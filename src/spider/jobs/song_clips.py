from __future__ import annotations

import asyncio
import argparse
import hashlib
import html
import json
import logging
import re
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import urlencode

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.common.bilibili_auth import build_bilibili_cookies
from src.db.song_list import (
    add_song_clip_if_missing,
    init_song_list_db,
    list_songs_without_clips,
    mark_song_clip_search_attempt,
)


logger = logging.getLogger("spider.jobs.song_clips")

SEARCH_URL = "https://api.bilibili.com/x/web-interface/wbi/search/type"
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
UPLOADER_VIDEOS_URL = "https://api.bilibili.com/x/space/wbi/arc/search"
SOURCE_NAME = "bilibili-search"
UPLOADER_SOURCE_NAME = "bilibili-uploader-catalog"
REQUEST_INTERVAL_SECONDS = 6.0
VOUCHER_COOLDOWN_SECONDS = 60.0
MAX_CONSECUTIVE_VOUCHERS = 3
MAX_SONGS_PER_RUN = 60
MAX_VIDEO_DURATION_SECONDS = 20 * 60
TITLE_SIMILARITY_THRESHOLD = 0.72
CHINA_TZ = timezone(timedelta(hours=8))
UPLOADER_CATALOG_CUTOFF = datetime(2025, 2, 1, tzinfo=CHINA_TZ)
KNOWN_CLIP_UPLOADERS = (
    (227078, "射守矢真兔"),
    (319958014, "妖血の翼"),
    (26536763, "めちゃくちゃ炒飯"),
    (1618550, "绯红的宿命"),
    (24274479, "憨憨奶瓶子"),
    (527020128, "某不科学的明日香"),
    (341460113, "我永远单推枣子哥丶"),
    (472341371, "便签-"),
    (2136413888, "L1berty_Rana"),
    (3493077901642666, "最后--年"),
    (3461577896364582, "ゆきことり"),
)
MIXIN_KEY_ENC_TAB = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44,
    52,
)


class BilibiliSearchError(RuntimeError):
    pass


class BilibiliVoucherError(BilibiliSearchError):
    pass


def _plain_title(value: Any) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", str(value or ""))).strip()


def _normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKC", _plain_title(value)).casefold()
    return "".join(character for character in text if character.isalnum())


def _title_variants(song: dict[str, Any]) -> list[str]:
    values = [str(song.get("title") or "")]
    values.extend(re.split(r"[/／]", str(song.get("title_trans") or "")))
    stripped = re.sub(
        r"\s*[（(【\[<].*?[）)】\]>]\s*",
        " ",
        values[0],
    ).strip()
    if stripped:
        values.append(stripped)
    return list(
        dict.fromkeys(
            plain
            for value in values
            if (plain := unicodedata.normalize("NFKC", _plain_title(value)).casefold())
        )
    )


def _candidate_title_chunks(value: Any) -> list[str]:
    title = unicodedata.normalize("NFKC", _plain_title(value)).casefold()
    chunks = [title]
    chunks.extend(
        part
        for part in re.split(r"[【】《》「」\[\]()（）<>|丨·:：/／—_“”\"']+", title)
        if part.strip()
    )
    normalized: list[str] = []
    for chunk in chunks:
        cleaned = re.sub(r"(?<!\d)20\d{2}[./_-]?\d{1,2}[./_-]?\d{1,2}(?!\d)", "", chunk)
        for marker in ("三理mit3uri", "mit3uri", "三理", "歌切", "翻唱", "cover"):
            cleaned = cleaned.replace(marker, "")
        if value_normalized := _normalize(cleaned):
            normalized.append(value_normalized)
    return list(dict.fromkeys(normalized))


def _title_similarity(song: dict[str, Any], candidate_title: Any) -> float:
    variants = [_normalize(value) for value in _title_variants(song)]
    chunks = _candidate_title_chunks(candidate_title)
    best = 0.0
    for variant in variants:
        for chunk in chunks:
            if len(variant) <= 2:
                score = 1.0 if variant == chunk else 0.0
            else:
                score = SequenceMatcher(None, variant, chunk).ratio()
            best = max(best, score)
    return best


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _dates_in_text(value: Any) -> set[date]:
    text = _plain_title(value)
    result: set[date] = set()
    patterns = (
        r"(?<!\d)(20\d{2})[-./年](\d{1,2})[-./月](\d{1,2})日?(?!\d)",
        r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)",
        r"(?<!\d)(\d{2})[-./](\d{1,2})[-./](\d{1,2})(?!\d)",
    )
    for index, pattern in enumerate(patterns):
        for match in re.finditer(pattern, text):
            year, month, day = map(int, match.groups())
            if index == 2:
                year += 2000
            if parsed := _safe_date(year, month, day):
                result.add(parsed)
    return result


def _record_dates(song: dict[str, Any]) -> set[date]:
    result: set[date] = set()
    for value in song.get("records") or []:
        try:
            result.add(date.fromisoformat(str(value)[:10]))
        except ValueError:
            continue
    return result


def _candidate_date_anchors(candidate: dict[str, Any]) -> list[tuple[str, date]]:
    anchors: list[tuple[str, date]] = []
    try:
        pubdate = int(candidate.get("pubdate") or 0)
    except (TypeError, ValueError):
        pubdate = 0
    if pubdate > 0:
        anchors.append(
            ("pubdate", datetime.fromtimestamp(pubdate, CHINA_TZ).date())
        )
    for field in ("title", "description", "desc"):
        anchors.extend(
            (field, parsed)
            for parsed in _dates_in_text(candidate.get(field))
        )
    return list(dict.fromkeys(anchors))


def _matching_date_anchor(
    song: dict[str, Any],
    candidate: dict[str, Any],
) -> tuple[str, date, date] | None:
    records = _record_dates(song)
    for source, candidate_date in _candidate_date_anchors(candidate):
        for record_date in records:
            if abs((candidate_date - record_date).days) <= 1:
                return source, candidate_date, record_date
    return None


def _duration_seconds(value: Any) -> int | None:
    if isinstance(value, (int, float)):
        return int(value)
    parts = str(value or "").strip().split(":")
    if not parts or not all(part.isdigit() for part in parts):
        return None
    total = 0
    for part in parts:
        total = total * 60 + int(part)
    return total


def _candidate_score(
    song: dict[str, Any],
    candidate: dict[str, Any],
    rank: int,
) -> int | None:
    title = _plain_title(candidate.get("title"))
    context = " ".join(
        str(candidate.get(field) or "")
        for field in ("title", "author", "description", "desc", "tag")
    )
    normalized_context = _normalize(context)
    has_mit3uri = any(
        marker in normalized_context
        for marker in ("三理", "mit3uri")
    )
    if not has_mit3uri:
        return None

    similarity = _title_similarity(song, title)
    if similarity < TITLE_SIMILARITY_THRESHOLD:
        return None
    if _matching_date_anchor(song, candidate) is None:
        return None

    bvid = str(candidate.get("bvid") or "").strip()
    if not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid):
        return None
    duration = _duration_seconds(candidate.get("duration"))
    if duration is not None and duration > MAX_VIDEO_DURATION_SECONDS:
        return None

    score = round(similarity * 1000) - rank
    if "歌切" in title:
        score += 30
    if "三理" in normalized_context or "mit3uri" in normalized_context:
        score += 20
    if duration is not None and duration <= 10 * 60:
        score += 5
    return score


def _pick_candidate(
    song: dict[str, Any],
    results: list[dict[str, Any]],
) -> dict[str, Any] | None:
    candidates = [
        (score, item)
        for rank, item in enumerate(results)
        if isinstance(item, dict)
        and (score := _candidate_score(song, item, rank)) is not None
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda pair: pair[0])[1]


class BilibiliSearchClient:
    def __init__(self, request_interval: float = REQUEST_INTERVAL_SECONDS) -> None:
        self.request_interval = request_interval
        self.next_request_at = 0.0
        self.mixin_key: str | None = None
        self.client = httpx.AsyncClient(
            trust_env=False,
            timeout=httpx.Timeout(30, connect=15),
            follow_redirects=True,
            cookies=build_bilibili_cookies(),
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
                ),
                "Referer": "https://search.bilibili.com/",
                "Accept": "application/json, text/plain, */*",
            },
        )

    async def __aenter__(self) -> BilibiliSearchClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.client.aclose()

    async def _wait_for_rate_limit(self) -> None:
        delay = self.next_request_at - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        self.next_request_at = time.monotonic() + self.request_interval

    async def _get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        attempts: int = 4,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            await self._wait_for_rate_limit()
            try:
                response = await self.client.get(url, params=params)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("response is not an object")
                code = int(payload.get("code", -1))
                if code != 0:
                    raise BilibiliSearchError(
                        f"Bilibili code={code} message={payload.get('message')}"
                    )
                data = payload.get("data") or {}
                if isinstance(data, dict) and data.get("v_voucher"):
                    raise BilibiliVoucherError(
                        "Bilibili returned a voucher challenge instead of results"
                    )
                return payload
            except (
                httpx.HTTPError,
                ValueError,
                json.JSONDecodeError,
                BilibiliSearchError,
            ) as exc:
                last_error = exc
                # A voucher is a global anti-abuse challenge, not a transient
                # empty result. Retrying it immediately only extends the
                # challenge and creates false "not found" outcomes.
                if isinstance(exc, BilibiliVoucherError):
                    raise
                if "-403" in str(exc) or "-352" in str(exc):
                    self.mixin_key = None
                if attempt < attempts:
                    await asyncio.sleep(min(12.0, attempt * 2.0))
        if isinstance(last_error, BilibiliVoucherError):
            raise last_error
        raise BilibiliSearchError(f"request failed: {url}: {last_error}")

    async def _load_mixin_key(self) -> str:
        if self.mixin_key is not None:
            return self.mixin_key
        payload = await self._get_json(NAV_URL)
        wbi_img = (payload.get("data") or {}).get("wbi_img") or {}
        img_url = str(wbi_img.get("img_url") or "")
        sub_url = str(wbi_img.get("sub_url") or "")
        if not img_url or not sub_url:
            raise BilibiliSearchError("nav response did not contain WBI keys")
        raw = (
            img_url.rsplit("/", 1)[-1].split(".", 1)[0]
            + sub_url.rsplit("/", 1)[-1].split(".", 1)[0]
        )
        self.mixin_key = "".join(raw[index] for index in MIXIN_KEY_ENC_TAB)[:32]
        return self.mixin_key

    async def _signed_params(self, params: dict[str, Any]) -> dict[str, str]:
        values = {key: value for key, value in params.items() if value is not None}
        values["wts"] = int(time.time())
        cleaned = {
            key: re.sub(r"[!'()*]", "", str(value))
            for key, value in values.items()
        }
        query = urlencode(sorted(cleaned.items()))
        cleaned["w_rid"] = hashlib.md5(
            (query + await self._load_mixin_key()).encode()
        ).hexdigest()
        return cleaned

    async def search_song_clip(
        self,
        song: dict[str, Any],
        *,
        include_fallback_queries: bool = False,
    ) -> dict[str, Any] | None:
        queries = [(str(song["title"]), (1,))]
        if include_fallback_queries:
            queries.append((f"三理Mit3uri {song['title']} 歌切", (1, 2)))
        for query, pages in queries:
            for page in pages:
                payload = await self._get_json(
                    SEARCH_URL,
                    params=await self._signed_params(
                        {
                            "search_type": "video",
                            "keyword": query,
                            "page": page,
                            "page_size": 20,
                        }
                    ),
                )
                results = (payload.get("data") or {}).get("result") or []
                if not isinstance(results, list):
                    continue
                candidate = _pick_candidate(song, results)
                if candidate is not None:
                    result = dict(candidate)
                    result["_search_query"] = query
                    return result
        return None

    async def fetch_uploader_videos(
        self,
        uploader_mid: int,
        uploader_name: str,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        page = 1
        while True:
            payload = await self._get_json(
                UPLOADER_VIDEOS_URL,
                params=await self._signed_params(
                    {
                        "mid": uploader_mid,
                        "pn": page,
                        "ps": 50,
                        "order": "pubdate",
                        "order_avoided": "true",
                        "platform": "web",
                        "web_location": 1550101,
                    }
                ),
            )
            data = payload.get("data") or {}
            videos = (data.get("list") or {}).get("vlist") or []
            if not isinstance(videos, list) or not videos:
                break
            normalized = []
            for raw in videos:
                if not isinstance(raw, dict):
                    continue
                candidate = dict(raw)
                candidate["pubdate"] = raw.get("created")
                candidate["duration"] = raw.get("length")
                candidate["description"] = raw.get("description")
                candidate["author"] = uploader_name
                candidate["uploader_mid"] = uploader_mid
                normalized.append(candidate)
            result.extend(normalized)
            total = int((data.get("page") or {}).get("count") or len(result))
            logger.info(
                "song clip uploader catalog mid=%d name=%s page=%d fetched=%d/%d",
                uploader_mid,
                uploader_name,
                page,
                len(result),
                total,
            )
            created_values = []
            for item in normalized:
                try:
                    created_values.append(int(item.get("pubdate") or 0))
                except (TypeError, ValueError):
                    continue
            if (
                len(result) >= total
                or (
                    created_values
                    and min(created_values)
                    < int(UPLOADER_CATALOG_CUTOFF.timestamp())
                )
            ):
                break
            page += 1
        return result


async def sync_song_clips_from_uploader_catalog(
    *,
    request_interval: float = REQUEST_INTERVAL_SECONDS,
    uploaders: tuple[tuple[int, str], ...] = KNOWN_CLIP_UPLOADERS,
) -> dict[str, Any]:
    """Fetch known clip uploaders once, then match every missing song locally."""

    init_song_list_db()
    songs = list_songs_without_clips()
    all_candidates: dict[str, dict[str, Any]] = {}
    uploader_summaries: list[dict[str, Any]] = []
    async with BilibiliSearchClient(request_interval=request_interval) as client:
        for uploader_mid, uploader_name in uploaders:
            videos = await client.fetch_uploader_videos(uploader_mid, uploader_name)
            uploader_summaries.append(
                {
                    "mid": uploader_mid,
                    "name": uploader_name,
                    "videos": len(videos),
                }
            )
            for candidate in videos:
                bvid = str(candidate.get("bvid") or "").strip()
                if re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid):
                    all_candidates[bvid] = candidate

    scored_matches: list[
        tuple[int, int, str, dict[str, Any], dict[str, Any]]
    ] = []
    for song in songs:
        for bvid, candidate in all_candidates.items():
            score = _candidate_score(song, candidate, 0)
            if score is not None:
                scored_matches.append((score, int(song["id"]), bvid, song, candidate))
    scored_matches.sort(reverse=True, key=lambda item: item[0])

    matched_song_ids: set[int] = set()
    used_bvids: set[str] = set()
    added: list[dict[str, Any]] = []
    for score, song_id, bvid, song, candidate in scored_matches:
        if song_id in matched_song_ids or bvid in used_bvids:
            continue
        date_match = _matching_date_anchor(song, candidate)
        source_payload: dict[str, Any] = {
            "uploader_mid": int(candidate["uploader_mid"]),
            "author": str(candidate.get("author") or ""),
            "video_title": _plain_title(candidate.get("title")),
            "title_similarity": round(
                _title_similarity(song, candidate.get("title")),
                4,
            ),
            "match_score": score,
        }
        if date_match is not None:
            source_payload["date_match"] = {
                "source": date_match[0],
                "candidate_date": date_match[1].isoformat(),
                "record_date": date_match[2].isoformat(),
            }
        if not add_song_clip_if_missing(
            song_id,
            bvid,
            source=UPLOADER_SOURCE_NAME,
            source_payload=source_payload,
        ):
            continue
        matched_song_ids.add(song_id)
        used_bvids.add(bvid)
        item = {
            "song_id": song_id,
            "song_title": song["title"],
            "bvid": bvid,
            "video_title": source_payload["video_title"],
            "author": source_payload["author"],
            "score": score,
        }
        added.append(item)
        logger.info("song clip added from uploader catalog: %s", item)

    result = {
        "missing_before": len(songs),
        "uploader_catalogs": uploader_summaries,
        "unique_candidates": len(all_candidates),
        "added": added,
        "missing_after": len(list_songs_without_clips()),
    }
    logger.info(
        "song clip uploader catalog sync success missing_before=%d candidates=%d "
        "added=%d missing_after=%d",
        result["missing_before"],
        result["unique_candidates"],
        len(added),
        result["missing_after"],
    )
    return result


async def sync_missing_song_clips(
    *,
    limit: int | None = MAX_SONGS_PER_RUN,
    request_interval: float = REQUEST_INTERVAL_SECONDS,
    include_fallback_queries: bool = False,
    voucher_cooldown: float = VOUCHER_COOLDOWN_SECONDS,
    max_consecutive_vouchers: int = MAX_CONSECUTIVE_VOUCHERS,
) -> dict[str, Any]:
    init_song_list_db()
    songs = list_songs_without_clips(limit=limit)
    summary: dict[str, Any] = {
        "searched": 0,
        "added": [],
        "not_found": 0,
        "failed": [],
        "voucher_deferred": [],
        "stopped_by_voucher": False,
    }
    if not songs:
        logger.info("song clip sync skipped: every song already has clips")
        return summary

    logger.info("song clip sync begin batch=%d", len(songs))
    consecutive_vouchers = 0
    async with BilibiliSearchClient(request_interval=request_interval) as client:
        for index, song in enumerate(songs, start=1):
            summary["searched"] += 1
            logger.info(
                "song clip search progress=%d/%d id=%s title=%s",
                index,
                len(songs),
                song["id"],
                song["title"],
            )
            try:
                candidate = await client.search_song_clip(
                    song,
                    include_fallback_queries=include_fallback_queries,
                )
            except BilibiliVoucherError as exc:
                consecutive_vouchers += 1
                mark_song_clip_search_attempt(int(song["id"]), "voucher")
                summary["voucher_deferred"].append(
                    {"song_id": song["id"], "title": song["title"]}
                )
                logger.warning(
                    "song clip search deferred by Bilibili voucher id=%s title=%s "
                    "consecutive=%d/%d: %s",
                    song["id"],
                    song["title"],
                    consecutive_vouchers,
                    max_consecutive_vouchers,
                    exc,
                )
                if consecutive_vouchers >= max_consecutive_vouchers:
                    summary["stopped_by_voucher"] = True
                    break
                await asyncio.sleep(voucher_cooldown)
                continue
            except Exception as exc:
                mark_song_clip_search_attempt(int(song["id"]), "failed")
                summary["failed"].append(
                    {"song_id": song["id"], "title": song["title"], "error": str(exc)}
                )
                logger.warning(
                    "song clip search failed id=%s title=%s: %s",
                    song["id"],
                    song["title"],
                    exc,
                )
                continue
            consecutive_vouchers = 0
            if candidate is None:
                mark_song_clip_search_attempt(int(song["id"]), "not_found")
                summary["not_found"] += 1
                continue
            bvid = str(candidate["bvid"])
            source_payload = {
                "query": str(
                    candidate.get("_search_query")
                    or f"三理Mit3uri {song['title']} 歌切"
                ),
                "video_title": _plain_title(candidate.get("title")),
                "author": str(candidate.get("author") or ""),
                "duration": str(candidate.get("duration") or ""),
                "title_similarity": round(
                    _title_similarity(song, candidate.get("title")),
                    4,
                ),
            }
            date_match = _matching_date_anchor(song, candidate)
            if date_match is not None:
                source_payload["date_match"] = {
                    "source": date_match[0],
                    "candidate_date": date_match[1].isoformat(),
                    "record_date": date_match[2].isoformat(),
                }
            if not add_song_clip_if_missing(
                int(song["id"]),
                bvid,
                source=SOURCE_NAME,
                source_payload=source_payload,
            ):
                mark_song_clip_search_attempt(
                    int(song["id"]),
                    "already_filled",
                )
                continue
            mark_song_clip_search_attempt(int(song["id"]), "added")
            added = {
                "song_id": song["id"],
                "song_title": song["title"],
                "bvid": bvid,
                "video_title": _plain_title(candidate.get("title")),
                "author": str(candidate.get("author") or ""),
            }
            summary["added"].append(added)
            logger.info("song clip added: %s", added)

    logger.info(
        "song clip sync success searched=%d added=%d not_found=%d failed=%d "
        "stopped_by_voucher=%s",
        summary["searched"],
        len(summary["added"]),
        summary["not_found"],
        len(summary["failed"]),
        summary["stopped_by_voucher"],
    )
    return summary


def register_jobs(scheduler: AsyncIOScheduler) -> None:
    init_song_list_db()
    scheduler.add_job(
        sync_missing_song_clips,
        "cron",
        hour=4,
        minute=15,
        id="song_clip_sync",
        name="song_clip_sync",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Search Bilibili for missing Mit3uri song clips.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="scan every currently missing song instead of one nightly batch",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=MAX_SONGS_PER_RUN,
        help=f"maximum songs to scan (default: {MAX_SONGS_PER_RUN})",
    )
    parser.add_argument(
        "--request-interval",
        type=float,
        default=REQUEST_INTERVAL_SECONDS,
        help=f"minimum seconds between Bilibili requests (default: {REQUEST_INTERVAL_SECONDS})",
    )
    parser.add_argument(
        "--fallback-queries",
        action="store_true",
        help="also try the more heavily rate-limited combined-keyword queries",
    )
    parser.add_argument(
        "--uploader-catalog",
        action="store_true",
        help="scan known clip uploaders and match missing songs locally",
    )
    parser.add_argument(
        "--uploader-mid",
        action="append",
        type=int,
        help="with --uploader-catalog, scan only this known uploader (repeatable)",
    )
    parser.add_argument(
        "--voucher-cooldown",
        type=float,
        default=VOUCHER_COOLDOWN_SECONDS,
        help=f"seconds to cool down after one voucher (default: {VOUCHER_COOLDOWN_SECONDS})",
    )
    args = parser.parse_args()
    if args.limit <= 0:
        parser.error("--limit must be greater than zero")
    if args.request_interval < REQUEST_INTERVAL_SECONDS:
        parser.error(
            f"--request-interval must be at least {REQUEST_INTERVAL_SECONDS} seconds"
        )
    if args.voucher_cooldown < VOUCHER_COOLDOWN_SECONDS:
        parser.error(
            f"--voucher-cooldown must be at least {VOUCHER_COOLDOWN_SECONDS} seconds"
        )
    return args


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    cli_args = _parse_args()
    if cli_args.uploader_catalog:
        selected_uploaders = KNOWN_CLIP_UPLOADERS
        if cli_args.uploader_mid:
            selected_mids = set(cli_args.uploader_mid)
            selected_uploaders = tuple(
                uploader
                for uploader in KNOWN_CLIP_UPLOADERS
                if uploader[0] in selected_mids
            )
            unknown_mids = selected_mids - {mid for mid, _name in selected_uploaders}
            if unknown_mids:
                raise SystemExit(
                    "unknown --uploader-mid values: "
                    + ", ".join(map(str, sorted(unknown_mids)))
                )
        result = asyncio.run(
            sync_song_clips_from_uploader_catalog(
                request_interval=cli_args.request_interval,
                uploaders=selected_uploaders,
            )
        )
    else:
        result = asyncio.run(
            sync_missing_song_clips(
                limit=None if cli_args.all else cli_args.limit,
                request_interval=cli_args.request_interval,
                include_fallback_queries=cli_args.fallback_queries,
                voucher_cooldown=cli_args.voucher_cooldown,
            )
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
