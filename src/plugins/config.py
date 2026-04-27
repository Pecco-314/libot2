from __future__ import annotations

import os
from pathlib import Path

from src.common.utils import load_env_file, init_logger
from src.db.activity import init_activity_db
from src.db.manager import init_manager_db
from src.db.state import init_state_db
from src.db.subscription import init_subscription_db
from src.db.liver import init_liver_db

load_env_file()
init_logger("libot")

_ENV_MANAGER_QQ = os.getenv("MANAGER_QQ", "").strip()
INITIAL_MANAGER_QQ = int(_ENV_MANAGER_QQ) if _ENV_MANAGER_QQ.isdigit() else None

ACTIVITY_IMAGE_DIR = Path(__file__).resolve().parents[2] / "data" / "images" / "activity"

def init_all_db() -> None:
    try:
        init_manager_db()
        init_subscription_db()
        init_state_db()
        init_liver_db()
        init_activity_db()
    except Exception:
        pass

init_all_db()