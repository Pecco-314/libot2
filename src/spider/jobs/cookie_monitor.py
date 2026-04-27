from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.spider.api import get_activated_medal_info
from src.common.utils import send_notification_email, ROOT

logger = logging.getLogger("spider.jobs.cookie_monitor")

# 定义锁文件路径，存放在 data 目录下
LOCK_FILE = ROOT / "data" / ".cookie_expired.lock"

async def check_cookie_status() -> None:
    try:
        data = await get_activated_medal_info(1)
        code = data.get("code")

        if code == -101:
            # 检测到未登录状态
            if not LOCK_FILE.exists():
                logger.warning("SESSDATA expired, triggering email notification")
                send_notification_email(
                    subject="Bilibili 账号登录失效通知",
                    content="爬虫检测到 Bilibili SESSDATA 已经过期 (code: -101)。\n请及时重新获取并更新环境变量中的 SESSDATA 字段。"
                )
                # 创建锁文件
                LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
                LOCK_FILE.touch()
            else:
                logger.debug("cookie is still expired, skipped sending email due to lock file")
                
        elif code == 0:
            # 请求正常，说明 Cookie 是有效的
            if LOCK_FILE.exists():
                # 如果存在锁文件，说明刚刚更新了 Cookie 恢复了状态，将其清除
                logger.info("Cookie status recovered, removing lock file")
                LOCK_FILE.unlink(missing_ok=True)
                
    except Exception as exc:
        logger.error("failed to check cookie status: %s", exc)

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