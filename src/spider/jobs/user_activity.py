from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

from apscheduler.triggers.cron import CronTrigger
import httpx

from src.db.sqlite import DEFAULT_DB_PATH
from src.db.user_activity import get_poll_state, init_user_activity_db, known_event_keys, store_user_activity
from src.spider.danmakus_user import MIT3URI_ROOM, MIT3URI_UID, fetch_user_events

logger = logging.getLogger(__name__)
_init_lock = asyncio.Lock()
_initialized_databases: set[Path] = set()
_retry_at = 0.0
_failures = 0


async def collect_user_activity(*, db_path: Path = DEFAULT_DB_PATH, now: int | None = None,
                                full: bool = False, deep: bool = False) -> dict:
    now = int(time.time()) if now is None else now
    async with _init_lock:
        if db_path not in _initialized_databases:
            await asyncio.to_thread(init_user_activity_db, db_path)
            _initialized_databases.add(db_path)
    state = await asyncio.to_thread(get_poll_state, MIT3URI_UID, db_path)

    async def reached_known_page(events: list[dict]) -> bool:
        if state is None:
            return True  # 首次只建立第一页基线，历史交由周期补查。
        return bool(await asyncio.to_thread(known_event_keys, [e["event_key"] for e in events], db_path))

    events, pages = await fetch_user_events(
        since_ms=None if full else (now - 7 * 86400) * 1000,
        stop_after_page=None if full or deep else reached_known_page,
    )
    result = await asyncio.to_thread(store_user_activity, events, MIT3URI_UID, MIT3URI_ROOM,
                                     now, full=full, db_path=db_path)
    result.update(pages=pages, fetched=len(events), full=full)
    log = logger.info if result["inserted"] or full or deep or result["baseline"] else logger.debug
    log("三理跨直播间用户动态采集: %s", result)
    return result


async def poll_user_activity(full: bool = False, deep: bool = False) -> None:
    global _retry_at, _failures
    if time.monotonic() < _retry_at:
        return
    try:
        await collect_user_activity(full=full, deep=deep)
    except Exception as exc:
        _failures += 1
        delay = min(300, 30 * 2 ** min(_failures - 1, 4))
        if isinstance(exc, httpx.HTTPStatusError):
            retry_after = exc.response.headers.get("Retry-After", "")
            if retry_after.isdigit():
                delay = max(delay, int(retry_after))
        _retry_at = time.monotonic() + delay
        logger.warning("Danmakus 用户动态采集失败，%s 秒后重试 full=%s deep=%s: %s", delay, full, deep, exc)
    else:
        _retry_at = 0.0
        _failures = 0


def register_jobs(scheduler) -> None:
    scheduler.add_job(
        poll_user_activity, "interval", seconds=10,
        id="mit3uri_user_activity", replace_existing=True, max_instances=1,
        coalesce=True, misfire_grace_time=10,
        next_run_time=datetime.now().astimezone() + timedelta(seconds=5),
    )
    scheduler.add_job(
        poll_user_activity, "interval", minutes=15, kwargs={"deep": True},
        id="mit3uri_user_activity_recent", replace_existing=True,
        max_instances=1, coalesce=True, misfire_grace_time=60,
    )
    scheduler.add_job(
        poll_user_activity, CronTrigger(hour=7, minute=20, timezone="Asia/Shanghai"),
        kwargs={"full": True}, id="mit3uri_user_activity_full", replace_existing=True,
        max_instances=1, coalesce=True, misfire_grace_time=3600,
    )
