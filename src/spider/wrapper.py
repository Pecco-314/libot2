from __future__ import annotations

from typing import Any

from src.db import liver
from src.spider import api
from src.spider.models import LiverStats, MasterInfo, RoomInfo, SpaceHistory


def _get_data(response: dict[str, Any]) -> dict[str, Any]:
    try:
        return response["body"]["data"]
    except Exception:
        raise ValueError(f"invalid response data: {response}")


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


async def get_space_history(uid: int) -> list[SpaceHistory]:
    response = await api.get_space_history(uid)
    data = _get_data(response)
    cards = data["cards"]
    result: list[SpaceHistory] = []
    for card_entry in cards:
        desc = card_entry["desc"]
        card_data = card_entry["card"]
        emoji_info = card_entry["display"]["emoji_info"]
        if emoji_info is None:
            emoji_details = []
        else:
            emoji_details = emoji_info.get("emoji_details", [])
        uid = desc["uid"]
        uname = desc["user_profile"]["info"]["uname"]
        activity_id = desc["dynamic_id_str"]
        result.append(
            SpaceHistory(
                activity_id=activity_id,
                uid=uid,
                uname=uname,
                timestamp=desc["timestamp"],
                dy_type=desc["type"],
                orig_type=desc["orig_type"],
                card=card_data,
                emoji_details=emoji_details,
            )
        )
    return result