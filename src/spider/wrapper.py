from __future__ import annotations

import json
from typing import Any

from src.spider import api


def _body_data(response: dict[str, Any]) -> dict[str, Any]:
    body = response.get("body")
    if not isinstance(body, dict):
        return {}
    data = body.get("data")
    return data if isinstance(data, dict) else {}


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _parse_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _extract_images(item: dict[str, Any]) -> list[str]:
    pictures = item.get("pictures")
    if not isinstance(pictures, list):
        return []

    image_urls: list[str] = []
    for picture in pictures:
        if not isinstance(picture, dict):
            continue
        image_url = str(picture.get("img_src") or "")
        if image_url:
            image_urls.append(image_url)
    return image_urls


async def get_room_info(room_id: int) -> dict[str, Any]:
    response = await api.get_room_info(room_id)
    data = _body_data(response)
    uid = _to_int(data.get("uid"))
    uname = str(data.get("uname") or "")
    if uid > 0 and not uname:
        uname = await get_uname(uid)
    return {
        "room_id": room_id,
        "uid": uid,
        "title": str(data.get("title") or ""),
        "live_status": _to_int(data.get("live_status")),
        "uname": uname,
        "raw": response,
    }


async def get_uname(uid: int) -> str:
    data = _body_data(await api.get_master_info(uid))
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    return str(info.get("uname") or "")


async def get_master_info(uid: int) -> str:
    return await get_uname(uid)


async def get_fans_num(uid: int) -> int:
    data = _body_data(await api.get_master_info(uid))
    return _to_int(data.get("follower_num"))


async def get_guard_num(room_id: int, uid: int) -> int:
    response = await api.get_guard_top_list(room_id, uid)
    data = _body_data(response)
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    return _to_int(info.get("num"))


async def get_activated_medal_count(target_id: int) -> int:
    response = await api.get_activated_medal_info(target_id)
    data = _body_data(response)
    return _to_int(data.get("fans_medal_count"))


async def get_liver_stats(room_id: int) -> dict[str, Any]:
    room_info = await get_room_info(room_id)
    uid = _to_int(room_info.get("uid"))
    if uid <= 0:
        return {
            "room_id": room_id,
            "uid": 0,
            "uname": str(room_info.get("uname") or ""),
            "fans_num": 0,
            "guard_num": 0,
            "fan_club_num": 0,
        }

    fans_num = await get_fans_num(uid)
    guard_num = await get_guard_num(room_id, uid)
    fan_club_num = await get_activated_medal_count(uid)
    return {
        "room_id": room_id,
        "uid": uid,
        "uname": str(room_info.get("uname") or ""),
        "fans_num": fans_num,
        "guard_num": guard_num,
        "fan_club_num": fan_club_num,
    }


async def get_space_history(uid: int, *, offset_dynamic_id: int = 0, need_top: int = 0) -> list[dict[str, Any]]:
    response = await api.get_space_history(
        uid,
        offset_dynamic_id=offset_dynamic_id,
        need_top=need_top,
    )
    data = _body_data(response)
    cards = data.get("cards")
    if not isinstance(cards, list):
        return []

    result: list[dict[str, Any]] = []
    for card_entry in cards:
        if not isinstance(card_entry, dict):
            continue

        card_data = _parse_json_object(card_entry.get("card"))
        item = _parse_json_object(card_data.get("item")) if card_data.get("item") is not None else {}
        if not item:
            item_candidate = card_data.get("item")
            item = item_candidate if isinstance(item_candidate, dict) else {}

        description = str(item.get("description") or "")
        upload_time = _to_int(item.get("upload_time"))
        images = _extract_images(item)

        result.append(
            {
                "description": description,
                "upload_time": upload_time,
                "images": images,
            }
        )

    return result


__all__ = [
    "get_room_info",
    "get_uname",
    "get_fans_num",
    "get_master_info",
    "get_guard_num",
    "get_activated_medal_count",
    "get_liver_stats",
    "get_space_history",
]