"""主动通知的独立群开关，复用 command_config 持久化。"""
from src.db.command_config import get_command_config

OWN_ROOM_ACTIVITY = "本房互动转发"
OTHER_ROOM_ACTIVITY = "外房互动转发"
PUSH_FEATURE_ALIASES = {
    OWN_ROOM_ACTIVITY: OWN_ROOM_ACTIVITY,
    "本房弹幕转发": OWN_ROOM_ACTIVITY,
    "本房弹幕": OWN_ROOM_ACTIVITY,
    "主播弹幕转发": OWN_ROOM_ACTIVITY,
    OTHER_ROOM_ACTIVITY: OTHER_ROOM_ACTIVITY,
    "外房弹幕转发": OTHER_ROOM_ACTIVITY,
    "外房弹幕": OTHER_ROOM_ACTIVITY,
    "跨房弹幕转发": OTHER_ROOM_ACTIVITY,
    "跨房互动转发": OTHER_ROOM_ACTIVITY,
}


def room_activity_feature(room_id: int, own_room: int) -> str:
    return OWN_ROOM_ACTIVITY if room_id == own_room else OTHER_ROOM_ACTIVITY


def is_push_enabled(group_id: int, feature: str) -> bool:
    return get_command_config(group_id, feature).enabled
