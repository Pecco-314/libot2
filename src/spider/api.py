from __future__ import annotations

import os
import asyncio
import logging
from typing import Any

import httpx

from src.common.env import load_env_file

load_env_file()

logger = logging.getLogger("spider.api")

BILI_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://live.bilibili.com/",
    "Origin": "https://live.bilibili.com",
    "Accept": "application/json, text/plain, */*",
}


def build_cookies() -> dict[str, str]:
    cookies: dict[str, str] = {}
    sessdata = os.getenv("BILI_SESSDATA", "").strip()
    if sessdata:
        cookies["SESSDATA"] = sessdata
    return cookies


async def request_json(
    url: str,
    params: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
) -> dict[str, Any]:
    request_headers = headers or BILI_HEADERS
    request_cookies = cookies if cookies is not None else build_cookies()

    body: dict[str, Any] | Any = {}
    last_error: Exception | None = None
    for attempt in range(5):
        try:
            async with httpx.AsyncClient(timeout=10.0, headers=request_headers, cookies=request_cookies) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                body = resp.json()
            break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_error = exc
            if attempt >= 4:
                raise
            logger.warning("request_json retry(%d/5) url=%s params=%s error=%s", attempt + 1, url, params, exc)
            await asyncio.sleep(3)

    code = int(body.get("code", -1)) if isinstance(body, dict) else -1
    return {
        "ok": code == 0,
        "url": url,
        "params": params,
        "cookies_used": bool(request_cookies),
        "code": code,
        "message": str((body.get("message") or body.get("msg") or "") if isinstance(body, dict) else ""),
        "body": body,
    }


async def get_room_info(room_id: int) -> dict[str, Any]:
    return await request_json(
        "https://api.live.bilibili.com/room/v1/Room/get_info",
        {"room_id": room_id},
        headers=BILI_HEADERS,
    )


async def get_master_info(uid: int) -> dict[str, Any]:
    return await request_json(
        "https://api.live.bilibili.com/live_user/v1/Master/info",
        {"uid": uid},
        headers=BILI_HEADERS,
    )


async def get_guard_top_list(room_id: int, uid: int) -> dict[str, Any]:
    return await request_json(
        "https://api.live.bilibili.com/xlive/app-room/v2/guardTab/topList",
        {
            "roomid": room_id,
            "ruid": uid,
            "page": 1,
            "page_size": 1,
        },
        headers=BILI_HEADERS,
    )


async def get_activated_medal_info(target_id: int) -> dict[str, Any]:
    return await request_json(
        "https://api.live.bilibili.com/xlive/app-ucenter/v1/fansMedal/GetActivatedMedalInfo",
        {"target_id": target_id},
        headers=BILI_HEADERS,
    )


async def get_space_history(uid: int, *, offset_dynamic_id: int = 0, need_top: int = 0) -> dict[str, Any]:
    return await request_json(
        "https://api.vc.bilibili.com/dynamic_svr/v1/dynamic_svr/space_history",
        {
            "host_uid": uid,
            "offset_dynamic_id": offset_dynamic_id,
            "need_top": need_top,
            "platform": "web",
        },
        headers=BILI_HEADERS,
    )


__all__ = [
    "BILI_HEADERS",
    "build_cookies",
    "request_json",
    "get_room_info",
    "get_master_info",
    "get_guard_top_list",
    "get_activated_medal_info",
    "get_space_history",
]