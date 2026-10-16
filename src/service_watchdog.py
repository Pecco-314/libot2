"""独立检查 bot 的端到端在线心跳；不操作业务进程或 PID 文件。"""

import argparse
from datetime import datetime
import json
import logging
from pathlib import Path
import re
import time

from src.common.utils import ROOT, send_notification_email

PATTERN = re.compile(rb"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ - libot.scheduler - DEBUG - Bot is online")
STALE_SECONDS = 180
REMINDER_SECONDS = 3600
STATE = ROOT / "data" / "service_watchdog.json"
logger = logging.getLogger(__name__)


def latest_online(root=ROOT):
    """读取当前和轮转日志，使用心跳时间而非文件修改时间。"""
    latest = 0.0
    for name in ("libot.log", "libot.log.1"):
        try:
            with (root / "logs" / name).open("rb") as handle:
                handle.seek(0, 2)
                handle.seek(max(0, handle.tell() - 4 * 1024 * 1024))
                content = handle.read()
            for match in PATTERN.finditer(content):
                stamp = datetime.strptime(match[1].decode(), "%Y-%m-%d %H:%M:%S").timestamp()
                latest = max(latest, stamp)
        except FileNotFoundError:
            continue
    return latest


def check(now, heartbeat, state, send=send_notification_email):
    state = dict(state)
    healthy = 0 <= now - heartbeat <= STALE_SECONDS
    if healthy:
        if state.get("alerted"):
            send("[恢复] libot 连接恢复", "libot 与 NapCat 的在线检查已恢复正常。")
        return {"checked_at": now, "healthy": True, "alerted": False}
    if not state.get("alerted") or now - state.get("last_email", 0) >= REMINDER_SECONDS:
        last = datetime.fromtimestamp(heartbeat).isoformat(" ") if heartbeat else "未找到"
        send("[警告] libot/NapCat 在线心跳中断",
             f"超过 {STALE_SECONDS} 秒未收到成功的在线检查。最后心跳：{last}。\n"
             "可能是 libot 退出、事件循环阻塞或 NapCat 离线。独立守护仍在运行，未自动重启业务。")
        state.update(alerted=True, last_email=now)
    state.update(checked_at=now, healthy=False)
    return state


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="只读检测，不发邮件")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.check:
        heartbeat = latest_online()
        print(json.dumps({"last_online": heartbeat, "age_seconds": time.time() - heartbeat}))
        return
    while True:
        try:
            state = json.loads(STATE.read_text()) if STATE.exists() else {}
            state = check(time.time(), latest_online(), state)
            temporary = STATE.with_suffix(".tmp")
            temporary.write_text(json.dumps(state))
            temporary.replace(STATE)
            logger.info("online heartbeat healthy=%s", state["healthy"])
        except Exception:
            # 发送失败不写入已通知状态，下一轮继续尝试。
            logger.exception("watchdog check failed")
        time.sleep(30)


if __name__ == "__main__":
    main()
