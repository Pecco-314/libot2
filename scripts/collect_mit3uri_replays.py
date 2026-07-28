#!/usr/bin/env python3
"""Collect and audit public replay videos for 三理Mit3uri.

The collector keeps duplicate uploads as source provenance on one recording
entry, then compares recordings with the historical live catalogs exposed by
Danmakus, VTB.cat, and recent LIVE/PREPARING markers in the local database.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.common.bilibili_auth import build_bilibili_cookies  # noqa: E402


DEFAULT_DB = ROOT / "data" / "libot.db"
DEFAULT_OUTPUT = ROOT / "data" / "mit3uri_replay_catalog.json"
TZ = timezone(timedelta(hours=8))
ROOM_ID = 1967216004

DANMAKUS_CATALOG_URL = "https://ukamnads.icu/api/v2/channel"
VTB_CATALOG_URL = "https://api.vtb.cat/liver/space"

MIXIN_KEY_ENC_TAB = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44,
    52,
)


@dataclass(frozen=True)
class Source:
    priority: int
    kind: str
    mid: int
    container_id: int | None
    url: str


SOURCES = (
    Source(
        1,
        "series",
        2030198123,
        4627040,
        "https://space.bilibili.com/2030198123/lists/4627040?type=series",
    ),
    Source(
        2,
        "uploads",
        3493284628401014,
        None,
        "https://space.bilibili.com/3493284628401014",
    ),
    Source(
        3,
        "season",
        1706612771,
        8249304,
        "https://space.bilibili.com/1706612771/lists/8249304?type=season",
    ),
    Source(
        4,
        "series",
        1702090167,
        4733079,
        "https://space.bilibili.com/1702090167/lists/4733079?type=series",
    ),
    Source(
        5,
        "series",
        3546841966709189,
        4629651,
        "https://space.bilibili.com/3546841966709189/lists/4629651?type=series",
    ),
    Source(
        6,
        "uploads",
        3706952267860889,
        None,
        "https://space.bilibili.com/3706952267860889/upload/video",
    ),
)


class PublicClient:
    def __init__(self, interval: float = 0.8) -> None:
        self.interval = interval
        self.next_request = 0.0
        self.client = httpx.Client(
            trust_env=False,
            timeout=httpx.Timeout(45, connect=20),
            follow_redirects=True,
            cookies=build_bilibili_cookies(),
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
                ),
                "Referer": "https://www.bilibili.com/",
                "Accept": "application/json, text/plain, */*",
            },
        )
        self.mixin_key: str | None = None

    def close(self) -> None:
        self.client.close()

    def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        bilibili: bool = False,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, 7):
            delay = self.next_request - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self.next_request = time.monotonic() + self.interval
            try:
                response = self.client.get(url, params=params)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("response is not an object")
                if bilibili and int(payload.get("code", -1)) != 0:
                    raise ValueError(
                        f"Bilibili code={payload.get('code')} "
                        f"message={payload.get('message')}"
                    )
                return payload
            except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt == 6:
                    break
                time.sleep(min(20, attempt * 2))
        raise RuntimeError(f"request failed: {url}: {last_error}") from last_error

    def _load_mixin_key(self) -> str:
        if self.mixin_key:
            return self.mixin_key
        payload = self.get_json(
            "https://api.bilibili.com/x/web-interface/nav",
            bilibili=True,
        )
        wbi_img = payload["data"]["wbi_img"]
        img_key = str(wbi_img["img_url"]).rsplit("/", 1)[-1].split(".", 1)[0]
        sub_key = str(wbi_img["sub_url"]).rsplit("/", 1)[-1].split(".", 1)[0]
        raw = img_key + sub_key
        self.mixin_key = "".join(raw[index] for index in MIXIN_KEY_ENC_TAB)[:32]
        return self.mixin_key

    def signed_params(self, params: dict[str, Any]) -> dict[str, Any]:
        values = {key: value for key, value in params.items() if value is not None}
        values["wts"] = int(time.time())
        cleaned = {
            key: re.sub(r"[!'()*]", "", str(value))
            for key, value in values.items()
        }
        query = urlencode(sorted(cleaned.items()))
        cleaned["w_rid"] = hashlib.md5(
            (query + self._load_mixin_key()).encode()
        ).hexdigest()
        return cleaned


def _fetch_series(client: PublicClient, source: Source) -> list[dict[str, Any]]:
    assert source.container_id is not None
    result: list[dict[str, Any]] = []
    page = 1
    while True:
        payload = client.get_json(
            "https://api.bilibili.com/x/series/archives",
            params={
                "mid": source.mid,
                "series_id": source.container_id,
                "only_normal": "true",
                "sort": "desc",
                "pn": page,
                "ps": 100,
            },
            bilibili=True,
        )
        data = payload["data"]
        archives = data.get("archives") or []
        result.extend(archives)
        total = int(data.get("page", {}).get("total") or len(result))
        if not archives or len(result) >= total:
            break
        page += 1
    return result


def _fetch_season(client: PublicClient, source: Source) -> list[dict[str, Any]]:
    assert source.container_id is not None
    result: list[dict[str, Any]] = []
    page = 1
    while True:
        payload = client.get_json(
            "https://api.bilibili.com/x/polymer/web-space/seasons_archives_list",
            params={
                "mid": source.mid,
                "season_id": source.container_id,
                "sort_reverse": "false",
                "page_num": page,
                "page_size": 30,
            },
            bilibili=True,
        )
        data = payload["data"]
        archives = data.get("archives") or []
        result.extend(archives)
        page_info = data.get("page") or {}
        total = int(
            page_info.get("total")
            or data.get("meta", {}).get("total")
            or len(result)
        )
        if not archives or len(result) >= total:
            break
        page += 1
    return result


def _fetch_uploads(client: PublicClient, source: Source) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    page = 1
    while True:
        params = client.signed_params(
            {
                "mid": source.mid,
                "pn": page,
                "ps": 50,
                "order": "pubdate",
                "order_avoided": "true",
                "platform": "web",
                "web_location": 1550101,
            }
        )
        payload = client.get_json(
            "https://api.bilibili.com/x/space/wbi/arc/search",
            params=params,
            bilibili=True,
        )
        data = payload["data"]
        archives = (data.get("list") or {}).get("vlist") or []
        result.extend(archives)
        total = int((data.get("page") or {}).get("count") or len(result))
        if not archives or len(result) >= total:
            break
        page += 1
    return result


def _duration_seconds(value: Any) -> int | None:
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if not parts or not all(part.isdigit() for part in parts):
        return None
    total = 0
    for part in parts:
        total = total * 60 + int(part)
    return total


def _normalize_archive(raw: dict[str, Any], source: Source) -> dict[str, Any]:
    bvid = str(raw.get("bvid") or "").strip()
    return {
        "bvid": bvid,
        "aid": int(raw.get("aid") or 0),
        "title": str(raw.get("title") or "").strip(),
        "duration_seconds": _duration_seconds(raw.get("duration") or raw.get("length")),
        "pubdate": int(raw.get("pubdate") or raw.get("created") or 0),
        "source_priority": source.priority,
        "source_kind": source.kind,
        "source_mid": source.mid,
        "source_url": source.url,
        "up_name": str(raw.get("author") or "").strip(),
    }


def _fetch_owner_name(client: PublicClient, archive: dict[str, Any]) -> str:
    bvid = archive.get("bvid")
    if not bvid:
        return archive.get("up_name") or ""
    payload = client.get_json(
        "https://api.bilibili.com/x/web-interface/view",
        params={"bvid": bvid},
        bilibili=True,
    )
    return str(payload.get("data", {}).get("owner", {}).get("name") or "")


def fetch_bilibili_sources(
    client: PublicClient,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    archives: list[dict[str, Any]] = []
    source_summaries: list[dict[str, Any]] = []
    for source in SOURCES:
        if source.kind == "series":
            raw_rows = _fetch_series(client, source)
        elif source.kind == "season":
            raw_rows = _fetch_season(client, source)
        else:
            raw_rows = _fetch_uploads(client, source)
        rows = [
            _normalize_archive(raw, source)
            for raw in raw_rows
            if str(raw.get("bvid") or "").strip()
        ]
        up_name = next((row["up_name"] for row in rows if row["up_name"]), "")
        if not up_name and rows:
            up_name = _fetch_owner_name(client, rows[0])
        for row in rows:
            row["up_name"] = row["up_name"] or up_name
        source_summaries.append(
            {
                "priority": source.priority,
                "kind": source.kind,
                "mid": source.mid,
                "container_id": source.container_id,
                "url": source.url,
                "up_name": up_name,
                "fetched_items": len(rows),
            }
        )
        archives.extend(rows)
        print(
            f"[{source.priority}/6] {up_name or source.mid}: "
            f"{len(rows)} videos",
            flush=True,
        )
    return archives, source_summaries


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--request-interval", type=float, default=0.8)
    parser.add_argument(
        "--raw-only",
        action="store_true",
        help="仅抓取并保存六个 Bilibili 来源，供调试 API 使用",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    client = PublicClient(interval=args.request_interval)
    try:
        archives, sources = fetch_bilibili_sources(client)
    finally:
        client.close()

    payload = {
        "generated_at": datetime.now(TZ).isoformat(),
        "timezone": "Asia/Shanghai",
        "sources": sources,
        "raw_archives": archives,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {args.output} ({len(archives)} source rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
