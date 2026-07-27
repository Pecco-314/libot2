from __future__ import annotations

import logging
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.common.bilibili_auth import (
    get_bilibili_auth,
    save_refreshed_bilibili_auth,
)
from src.spider.api import get_activated_medal_info
from src.spider.auth_refresh import (
    CookieRefreshError,
    cookie_needs_refresh,
    refresh_bilibili_auth,
)
from src.common.utils import send_notification_email, ROOT

logger = logging.getLogger("spider.jobs.cookie_monitor")

EXPIRED_LOCK_FILE = ROOT / "data" / ".cookie_expired.lock"
REFRESH_LOCK_FILE = ROOT / "data" / ".cookie_refresh_failed.lock"


def _notify_once(lock_file: Path, subject: str, content: str) -> None:
    if lock_file.exists():
        return
    try:
        send_notification_email(subject=subject, content=content)
    except Exception as exc:
        logger.error("failed to send Cookie notification: %s", exc)
        return
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file.touch()


async def check_cookie_status() -> None:
    try:
        auth = get_bilibili_auth()
        refresh_needed: bool | None = None
        try:
            refresh_needed = await cookie_needs_refresh(auth)
        except CookieRefreshError as exc:
            logger.warning("failed to check whether Cookie needs refresh: %s", exc)

        if refresh_needed:
            if not auth.refresh_token:
                logger.warning(
                    "Bilibili Cookie needs refresh but "
                    "BILI_REFRESH_TOKEN is missing"
                )
                _notify_once(
                    REFRESH_LOCK_FILE,
                    "Bilibili Cookie 即将过期",
                    "检测到 Bilibili Cookie 需要刷新，但尚未配置 "
                    "BILI_REFRESH_TOKEN。\n"
                    "请从浏览器 localStorage 的 ac_time_value 获取刷新令牌，"
                    "并更新 .env.prod。",
                )
            else:
                try:
                    refreshed = await refresh_bilibili_auth(auth)
                    auth = save_refreshed_bilibili_auth(
                        auth,
                        refreshed.cookies,
                        refreshed.refresh_token,
                    )
                except CookieRefreshError as exc:
                    logger.error("failed to refresh Bilibili Cookie: %s", exc)
                    _notify_once(
                        REFRESH_LOCK_FILE,
                        "Bilibili Cookie 自动刷新失败",
                        "Bilibili Cookie 自动刷新失败，请检查日志，"
                        "必要时重新登录并更新 COOKIE 与 "
                        "BILI_REFRESH_TOKEN。",
                    )
                else:
                    logger.info(
                        "Bilibili Cookie refreshed successfully, revision=%d",
                        auth.revision,
                    )
                    REFRESH_LOCK_FILE.unlink(missing_ok=True)
                    EXPIRED_LOCK_FILE.unlink(missing_ok=True)
        elif refresh_needed is False:
            REFRESH_LOCK_FILE.unlink(missing_ok=True)

        data = await get_activated_medal_info(1)
        code = data.get("code")

        if code == -101:
            logger.warning("Bilibili Cookie expired (code: -101)")
            _notify_once(
                EXPIRED_LOCK_FILE,
                "Bilibili 账号登录失效通知",
                "检测到 Bilibili Cookie 已失效 (code: -101)。\n"
                "请重新登录，并更新 .env.prod 中的 COOKIE 与 "
                "BILI_REFRESH_TOKEN。",
            )
        elif code == 0:
            if EXPIRED_LOCK_FILE.exists():
                logger.info("Cookie status recovered, removing lock file")
                EXPIRED_LOCK_FILE.unlink(missing_ok=True)

    except Exception as exc:
        logger.exception("failed to check cookie status: %s", exc)


def register_jobs(scheduler: AsyncIOScheduler) -> None:
    scheduler.add_job(
        check_cookie_status,
        CronTrigger(minute="*/5"),
        id="cookie_monitor",
        name="cookie_monitor",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )
