from __future__ import annotations

import asyncio
from typing import Any

from src.db import liver
from src.spider import api
from src.spider.models import LiverStats, MasterInfo, RoomInfo, SpaceHistory


def _get_data(response: dict[str, Any]) -> dict[str, Any]:
    body = response.get("body") or {}
    data = body.get("data")
    if not isinstance(data, dict):
        raise ValueError(f"invalid response data: code={body.get('code')} message={body.get('message')}")
    return data


async def get_master_info(uid: int) -> MasterInfo:
    response = await api.get_master_info(uid)
    data = _get_data(response)
    return MasterInfo(
        uid=uid,
        uname=str(data["info"]["uname"]),
        room_id=int(data["room_id"]),
    )


async def get_room_info(room_id: int) -> RoomInfo:
    response = await api.get_room_info(room_id)
    data = _get_data(response)
    return RoomInfo(
        room_id=room_id,
        uid=int(data["uid"]),
        title=str(data["title"]),
        live_status=int(data["live_status"]),
    )


async def get_fans_num(uid: int) -> int:
    response = await api.get_master_info(uid)
    data = _get_data(response)
    return int(data["follower_num"])


async def get_guard_num(room_id: int, uid: int) -> int:
    response = await api.get_guard_top_list(room_id, uid)
    data = _get_data(response)
    return int(data["info"]["num"])


async def get_fan_club_num(uid: int) -> int:
    response = await api.get_activated_medal_info(uid)
    data = _get_data(response)
    return int(data["fans_medal_count"])


async def get_uid_by_roomid(room_id: int) -> int:
    uid = liver.get_uid_by_roomid(room_id)
    if uid is not None:
        return uid
    room_info = await get_room_info(room_id)
    return room_info.uid


async def get_roomid_by_uid(uid: int) -> int:
    room_id = liver.get_roomid_by_uid(uid)
    if room_id is not None:
        return room_id
    master_info = await get_master_info(uid)
    return master_info.room_id


async def get_name_by_uid(uid: int) -> str:
    uname = liver.get_name_by_uid(uid)
    if uname is not None:
        return uname
    master_info = await get_master_info(uid)
    return master_info.uname


async def get_name_by_roomid(roomid: int) -> str:
    uname = liver.get_name_by_roomid(roomid)
    if uname is not None:
        return uname
    uid = await get_uid_by_roomid(roomid)
    master_info = await get_master_info(uid)
    return master_info.uname


async def get_stats(room_id: int) -> LiverStats:
    room_info = await get_room_info(room_id)
    uid = room_info.uid
    uname = await get_name_by_uid(uid)
    return LiverStats(
        room_id=room_id,
        uid=uid,
        uname=uname,
        fans_num=await get_fans_num(uid),
        guard_num=await get_guard_num(room_id, uid),
        fan_club_num=await get_fan_club_num(uid),
    )


def _space_history_items(items, uid: int) -> list[SpaceHistory]:
    result: list[SpaceHistory] = []
    
    for item in items:
        modules = item.get("modules") or {}
        author = modules.get("module_author") or {}
        
        activity_id = item.get("id_str")
        uname = author.get("name", "未知")
        author_uid = author.get("mid", uid)
        timestamp = int(author.get("pub_ts", 0))
        dy_type = item.get("type", "UNKNOWN")
        
        result.append(
            SpaceHistory(
                activity_id=activity_id,
                uid=author_uid,
                uname=uname,
                timestamp=timestamp,
                dy_type=dy_type,
                item=item
            )
        )
    return result


async def get_space_history(uid: int, *, since: int | None = None, max_pages=10) -> list[SpaceHistory]:
    """平时只访问首页；有历史边界时分页追赶到边界，置顶动态不充当边界。"""
    offset = ''
    seen_offsets = set()
    result = {}
    for page in range(max_pages):
        response = await api.get_space_history(uid, offset)
        data = _get_data(response)
        raw_items = data.get('items') or []
        for item in _space_history_items(raw_items, uid):
            if item.activity_id:
                result[item.activity_id] = item
        crossed = False
        for raw_item in raw_items:
            modules = raw_item.get('modules') or {}
            pinned = (modules.get('module_tag') or {}).get('text') == '置顶'
            timestamp = int((modules.get('module_author') or {}).get('pub_ts') or 0)
            if since is not None and not pinned and 0 < timestamp <= since:
                crossed = True
        next_offset = str(data.get('offset') or '')
        if since is None or crossed or not data.get('has_more'):
            return list(result.values())
        if not raw_items:
            raise RuntimeError('动态分页意外返回空页，保留边界等待后续补抓')
        if not next_offset or next_offset in seen_offsets:
            raise RuntimeError('动态分页未到达历史边界，offset未前进')
        seen_offsets.add(next_offset)
        offset = next_offset
        if page+1 < max_pages:
            await asyncio.sleep(1)
    raise RuntimeError('动态分页未到达历史边界，保留边界等待后续补抓')
