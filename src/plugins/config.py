from __future__ import annotations

from pathlib import Path

from src.common.utils import init_logger, load_env_file
from src.db.abbreviation import init_abbreviation_db
from src.db.activity import init_activity_db
from src.db.activity_notice import init_activity_notice_db
from src.db.command_config import init_command_config_db
from src.db.fan_club import init_fan_club_db
from src.db.live_list import init_live_list_db
from src.db.live_notice import init_live_notice_db
from src.db.liver import init_liver_db
from src.db.meme import init_meme_db
from src.db.name_history import init_name_history_index
from src.db.singer_alias import init_singer_alias_db
from src.db.state import init_state_db
from src.db.subscription import init_subscription_db

load_env_file()
logger = init_logger("libot")

ACTIVITY_IMAGE_DIR = (
    Path(__file__).resolve().parents[2] / "data" / "images" / "activity"
)


def init_all_db() -> None:
    try:
        init_command_config_db()
        init_subscription_db()
        init_state_db()
        init_live_notice_db()
        init_activity_notice_db()
        init_liver_db()
        init_activity_db()
        init_fan_club_db()
        init_name_history_index()
        init_singer_alias_db()
        init_abbreviation_db()
        init_live_list_db()
        init_meme_db()
    except Exception:
        logger.exception("数据库初始化失败")
        raise


init_all_db()
