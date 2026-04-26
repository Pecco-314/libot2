from __future__ import annotations

from dataclasses import dataclass
from typing import Any

@dataclass
class MasterInfo:
    uid: int
    uname: str
    room_id: int

@dataclass
class RoomInfo:
    room_id: int
    uid: int
    title: str
    live_status: int

@dataclass
class LiverStats:
    room_id: int
    uid: int
    uname: str
    fans_num: int
    guard_num: int
    fan_club_num: int

@dataclass
class SpaceHistory:
    activity_id: int
    uid: int
    uname: str
    timestamp: int
    dy_type: int
    orig_type: int
    card: dict[str, Any]
    emoji_details: list[dict[str, Any]]