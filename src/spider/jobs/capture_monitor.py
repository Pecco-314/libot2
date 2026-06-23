from __future__ import annotations

import os
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from src.common.utils import send_notification_email, ROOT

logger = logging.getLogger("spider.jobs.capture_monitor")

LOG_FILE = ROOT / "logs" / "capture.log"
LOCK_FILE = ROOT / "data" / ".capture_stuck.lock"

# 定义判定卡死的超时时间（秒）
TIMEOUT_SECONDS = 60

def get_last_log_info() -> tuple[datetime | None, str]:
    """从后往前读取日志文件的最后一行，并解析出时间戳和整行内容"""
    if not LOG_FILE.exists():
        return None, ""

    try:
        with open(LOG_FILE, "rb") as f:
            f.seek(0, os.SEEK_END)
            pos = f.tell()
            
            if pos == 0:
                return None, ""
            
            buffer = bytearray()
            while pos > 0:
                pos -= 1
                f.seek(pos)
                char = f.read(1)
                if char == b'\n' and len(buffer) > 0:
                    break
                buffer.extend(char)
            
            last_line = buffer[::-1].decode("utf-8", errors="ignore").strip()
            
            if not last_line:
                return None, ""
            
            time_str = last_line.split(" - ")[0]
            log_time = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S,%f")
            return log_time, last_line
            
    except Exception as exc:
        logger.error("failed to parse last log line: %s", exc)
        return None, ""

async def check_capture_status() -> None:
    try:
        last_time, last_line = get_last_log_info()
        if not last_time:
            return

        # 检查是否为正常停止状态
        if "stopped ffmpeg capture" in last_line:
            if LOCK_FILE.exists():
                logger.info("Capture module is naturally stopped, removing lock file")
                LOCK_FILE.unlink(missing_ok=True)
            return

        now = datetime.now()
        time_diff = (now - last_time).total_seconds()

        if time_diff > TIMEOUT_SECONDS:
            if not LOCK_FILE.exists():
                logger.warning(f"Capture module stuck detected ({int(time_diff)}s), triggering email notification")
                send_notification_email(
                    subject="Capture模块运行卡死告警",
                    content=(
                        f"监控检测到 capture.log 已经有 {int(time_diff)} 秒未更新。\n"
                        f"最后一条日志时间：{last_time}\n"
                        f"最后一条日志内容：{last_line}\n"
                        "请及时检查服务状态。"
                    )
                )
                LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
                LOCK_FILE.touch()
            else:
                logger.debug("capture is still stuck, skipped sending email due to lock file")
                
        else:
            if LOCK_FILE.exists():
                logger.info("Capture module status recovered, removing lock file")
                LOCK_FILE.unlink(missing_ok=True)

    except Exception as exc:
        logger.error("failed to check capture status: %s", exc)

def register_jobs(scheduler: AsyncIOScheduler) -> None:
    scheduler.add_job(
        check_capture_status,
        # 使用 IntervalTrigger 每10秒执行一次检查
        IntervalTrigger(seconds=10),
        id="capture_monitor",
        name="capture_monitor",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=15,
    )