from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx

from src.common.bilibili_auth import build_bilibili_cookies


MEDAL_WALL_URL = "https://api.live.bilibili.com/xlive/web-ucenter/user/MedalWall"
RISK_CODES = {-352, -412, -509}
CACHE_TTL_SECONDS = 300.0

_cache: dict[int, tuple[float, dict[str, Any]]] = {}


class MedalWallError(RuntimeError):
    pass


def _parse_wall(uid: int, payload: dict[str, Any]) -> dict[str, Any]:
    code = int(payload.get("code", -1))
    message = str(payload.get("message") or payload.get("msg") or "")
    if code != 0:
        raise MedalWallError(f"Bilibili code={code}: {message}")

    data = payload.get("data") or {}
    if not isinstance(data, dict):
        raise MedalWallError("medal wall data is not an object")
    medals: list[dict[str, Any]] = []
    for row in data.get("list") or []:
        if not isinstance(row, dict):
            continue
        medal = row.get("medal_info") or {}
        target_uid = int(medal.get("target_id") or 0)
        level = int(medal.get("level") or 0)
        if target_uid <= 0 or level <= 0:
            continue
        medals.append(
            {
                "target_uid": target_uid,
                "target_name": str(row.get("target_name") or target_uid),
                "level": level,
                "guard_level": int(medal.get("guard_level") or 0),
            }
        )
    medals.sort(key=lambda row: (-int(row["level"]), int(row["target_uid"])))
    return {
        "uid": int(uid),
        "uname": str(data.get("name") or ""),
        "medals": medals,
        "hidden": bool(int(data.get("close_space_medal") or 0)),
        "wearing_only": bool(int(data.get("only_show_wearing") or 0)),
    }


async def get_public_medal_wall(uid: int) -> dict[str, Any]:
    now = time.monotonic()
    cached = _cache.get(int(uid))
    if cached is not None and now - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Origin": "https://live.bilibili.com",
        "Referer": "https://live.bilibili.com/",
    }
    last_error: Exception | None = None
    async with httpx.AsyncClient(
        trust_env=False,
        cookies=build_bilibili_cookies(),
        headers=headers,
        timeout=httpx.Timeout(30.0),
        follow_redirects=True,
    ) as client:
        for attempt in range(3):
            try:
                response = await client.get(MEDAL_WALL_URL, params={"target_id": uid})
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise MedalWallError("response is not a JSON object")
                code = int(payload.get("code", -1))
                if code in RISK_CODES:
                    raise MedalWallError(
                        f"Bilibili code={code}: {payload.get('message') or ''}"
                    )
                result = _parse_wall(uid, payload)
                _cache[int(uid)] = (time.monotonic(), result)
                return result
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, json.JSONDecodeError, MedalWallError) as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))
    raise MedalWallError(f"medal wall request failed: {last_error}")


__all__ = ["MedalWallError", "get_public_medal_wall"]
