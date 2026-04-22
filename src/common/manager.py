from __future__ import annotations

import os
import sqlite3
from functools import wraps
from typing import Any, Callable, Coroutine

from nonebot.adapters.onebot.v11 import Event
from nonebot.matcher import Matcher

from src.common.util import load_env_file, get_group_id
from src.common.sqlite import connect_sqlite, execute_write, write_transaction

load_env_file()

_ENV_MANAGER_QQ = os.getenv("MANAGER_QQ", "").strip()
INITIAL_MANAGER_QQ = int(_ENV_MANAGER_QQ) if _ENV_MANAGER_QQ.isdigit() else None


def init_manager_db() -> None:
    with write_transaction() as conn:
        execute_write(
            conn,
            """
            CREATE TABLE IF NOT EXISTS manager (
                group_id INTEGER NOT NULL,
                manager_qq INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (group_id, manager_qq)
            )
            """,
        )


def ensure_initial_manager(group_id: int) -> bool:
    if INITIAL_MANAGER_QQ is None:
        return False

    with write_transaction() as conn:
        execute_write(
            conn,
            "INSERT OR IGNORE INTO manager (group_id, manager_qq) VALUES (?, ?)",
            (group_id, INITIAL_MANAGER_QQ),
        )
    return True


def is_manager(group_id: int, user_id: int) -> bool:
    with connect_sqlite() as conn:
        row = conn.execute(
            "SELECT 1 FROM manager WHERE group_id = ? AND manager_qq = ?",
            (group_id, user_id),
        ).fetchone()
    return row is not None


def list_managers(group_id: int) -> list[int]:
    with connect_sqlite() as conn:
        rows = conn.execute(
            "SELECT manager_qq FROM manager WHERE group_id = ? ORDER BY manager_qq ASC",
            (group_id,),
        ).fetchall()
    return [int(row[0]) for row in rows]


def count_managers(group_id: int) -> int:
    with connect_sqlite() as conn:
        row = conn.execute(
            "SELECT COUNT(1) FROM manager WHERE group_id = ?",
            (group_id,),
        ).fetchone()
    return int(row[0]) if row else 0


def add_manager(group_id: int, user_id: int) -> bool:
    with write_transaction() as conn:
        cur = execute_write(
            conn,
            "INSERT OR IGNORE INTO manager (group_id, manager_qq) VALUES (?, ?)",
            (group_id, user_id),
        )
    return cur.rowcount > 0


def remove_manager(group_id: int, user_id: int) -> bool:
    with write_transaction() as conn:
        cur = execute_write(
            conn,
            "DELETE FROM manager WHERE group_id = ? AND manager_qq = ?",
            (group_id, user_id),
        )
    return cur.rowcount > 0


Handler = Callable[..., Coroutine[Any, Any, None]]


def group_manager_required(func: Handler) -> Handler:
    @wraps(func)
    async def wrapper(*args: Any, **kwargs: Any):
        matcher = next((arg for arg in args if isinstance(arg, Matcher)), None)
        event = next((arg for arg in args if isinstance(arg, Event)), None)

        if matcher is None:
            matcher = kwargs.get("matcher")
        if event is None:
            event = kwargs.get("event")

        if isinstance(matcher, Matcher) and isinstance(event, Event):
            group_id = get_group_id(event)
            if group_id is None:
                await matcher.finish("请在群聊中使用该命令")
                return

            if INITIAL_MANAGER_QQ is None:
                await matcher.finish("未配置 MANAGER_QQ，无法初始化管理员")
                return

            ensure_initial_manager(group_id)

            user_id = int(event.get_user_id())
            if not is_manager(group_id, user_id):
                await matcher.finish("权限不足：该命令仅管理员可用")
                return

        return await func(*args, **kwargs)

    return wrapper  # type: ignore[return-value]
