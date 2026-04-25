from __future__ import annotations

import argparse
import asyncio
import http.cookies
import json
import logging
import os
import signal
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from datetime import datetime

import aiohttp
import blivedm
from blivedm import handlers
from src.common.env import load_env_file
from src.db.sqlite import connect_sqlite, write_transaction
from src.db.subscription import list_subscribed_room_ids

import blivedm.models.web as web_models

logger = logging.getLogger("live-monitor")
ROOT = Path(__file__).resolve().parents[2]
LOG_PATH = ROOT / "logs" / "monitor.log"
TRACKED_CMDS = {
    "DANMU_MSG",
    "SEND_GIFT",
    "GUARD_BUY",
    "SUPER_CHAT_MESSAGE",
    "LIVE",
    "PREPARING",
    "ROOM_CHANGE",
}


@dataclass(slots=True)
class MonitorConfig:
    rooms: list[int]
    rooms_from_db: bool
    database: Path
    run_seconds: int
    verbose: bool
    sessdata: str
    buvid3: str


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s | %(message)s")
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(level)

    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)


def _execute_write(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple[Any, ...] = (),
    *,
    retries: int = 3,
    sleep_seconds: float = 0.25,
) -> sqlite3.Cursor:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            return conn.execute(sql, params)
        except sqlite3.OperationalError as exc:
            last_error = exc
            if "locked" not in str(exc).lower() or attempt + 1 >= retries:
                raise
            time.sleep(sleep_seconds)
    assert last_error is not None
    raise last_error


def _execute_many_write(
    conn: sqlite3.Connection,
    sql: str,
    rows: list[tuple[Any, ...]],
    *,
    retries: int = 3,
    sleep_seconds: float = 0.25,
) -> None:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            conn.executemany(sql, rows)
            return
        except sqlite3.OperationalError as exc:
            last_error = exc
            if "locked" not in str(exc).lower() or attempt + 1 >= retries:
                raise
            time.sleep(sleep_seconds)
    assert last_error is not None
    raise last_error


def _parse_rooms_text(text: str) -> list[int]:
    rooms: list[int] = []
    for part in text.split(","):
        item = part.strip()
        if not item:
            continue
        if not item.isdigit():
            raise ValueError(f"非法房间号: {item}")
        rooms.append(int(item))
    return rooms


def _load_config(config_path: Path | None, args: argparse.Namespace) -> MonitorConfig:
    raw: dict[str, Any] = {}
    if config_path is not None and config_path.exists():
        raw = json.loads(config_path.read_text(encoding="utf-8"))

    if args.database:
        db_path = Path(args.database)
    else:
        db_path = Path(str(raw.get("database", "data/libot.db")))

    rooms: list[int] = []
    rooms_from_db = False
    if args.rooms:
        rooms = _parse_rooms_text(args.rooms)
    elif isinstance(raw.get("rooms"), list):
        rooms = [int(x) for x in raw.get("rooms", []) if str(x).isdigit()]
    else:
        rooms = list_subscribed_room_ids()
        rooms_from_db = True

    if not rooms:
        raise ValueError("未配置直播间号。请先在 subscription 表中添加订阅，或通过 --rooms 传入")

    run_seconds = int(raw.get("run_seconds", 0))
    verbose = bool(raw.get("verbose", False))

    if args.run_seconds is not None:
        run_seconds = args.run_seconds
    if args.verbose:
        verbose = True

    sessdata = os.getenv("SESSDATA", "")
    buvid3 = os.getenv("BUVID3", "")

    return MonitorConfig(
        rooms=rooms,
        rooms_from_db=rooms_from_db,
        database=db_path,
        run_seconds=run_seconds,
        verbose=verbose,
        sessdata=sessdata,
        buvid3=buvid3,
    )


class MetricsDB:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = connect_sqlite(self.db_path)
        self.conn.execute("PRAGMA temp_store=MEMORY")
        try:
            self._init_tables()
        except TimeoutError as e:
            logger.warning("初始化监控数据库时遇锁，继续启动并等待后续写入重试：%s", e)

    def _init_tables(self) -> None:
        with write_transaction(self.db_path) as conn:
            _execute_write(
                conn,
                """
                CREATE TABLE IF NOT EXISTS event (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_id INTEGER NOT NULL,
                    cmd TEXT NOT NULL,
                    uid INTEGER,
                    uname TEXT,
                    content TEXT,
                    gift_name TEXT,
                    gift_num INTEGER,
                    total_coin INTEGER,
                    title TEXT,
                    timestamp TIMESTAMP
                )
                """,
            )
            _execute_write(
                conn,
                """
                CREATE INDEX IF NOT EXISTS idx_event_room_time
                ON event(room_id, timestamp)
                """,
            )
            _execute_write(
                conn,
                """
                CREATE INDEX IF NOT EXISTS idx_event_cmd_time
                ON event(cmd, timestamp)
                """,
            )

    def insert_many(self, rows: list[tuple[Any, ...]]) -> None:
        if not rows:
            return
        with write_transaction(self.db_path) as conn:
            _execute_many_write(
                conn,
                """
                INSERT INTO event (
                    room_id, cmd, uid, uname, content, gift_name, gift_num,
                    total_coin, title, timestamp
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )


    def close(self) -> None:
        self.conn.close()


def _normalized_cmd(command: dict[str, Any]) -> str:
    cmd = str(command.get("cmd", ""))
    pos = cmd.find(":")
    return cmd[:pos] if pos != -1 else cmd


def _extract_heartbeat_popularity(command: dict[str, Any]) -> int | None:
    data = command.get("data")
    if not isinstance(data, dict):
        return None

    popularity_value = data.get("popularity")
    if isinstance(popularity_value, int):
        return popularity_value
    if isinstance(popularity_value, str) and popularity_value.isdigit():
        return int(popularity_value)
    return None


def _extract_row(room_id: int, command: dict[str, Any]) -> tuple[Any, ...] | None:
    cmd = _normalized_cmd(command)
    if cmd not in TRACKED_CMDS:
        return None

    uid: int | None = None
    uname: str | None = None
    content: str | None = None
    gift_name: str | None = None
    gift_num: int | None = None
    total_coin: int | None = None
    title: str | None = None

    try:
        if cmd == "DANMU_MSG":
            msg = web_models.DanmakuMessage.from_command(command["info"])
            uid = int(msg.uid)
            uname = msg.uname
            content = msg.msg
            timestamp = msg.timestamp // 1000
        elif cmd == "SEND_GIFT":
            msg = web_models.GiftMessage.from_command(command["data"])
            uid = int(msg.uid)
            uname = msg.uname
            gift_name = msg.gift_name
            gift_num = int(msg.num)
            total_coin = int(msg.total_coin)
            timestamp = msg.timestamp // 1000
        elif cmd == "GUARD_BUY":
            msg = web_models.GuardBuyMessage.from_command(command["data"])
            uid = int(msg.uid)
            uname = msg.username
            gift_name = msg.gift_name
            gift_num = int(msg.num)
            total_coin = int(msg.price) * int(msg.num)
            timestamp = msg.timestamp // 1000
        elif cmd == "SUPER_CHAT_MESSAGE":
            msg = web_models.SuperChatMessage.from_command(command["data"])
            uid = int(msg.uid)
            uname = msg.uname
            content = msg.message
            total_coin = int(msg.price)
            gift_name = msg.gift_name
            gift_num = 1
            timestamp = msg.timestamp // 1000
        elif cmd == "LIVE":
            logger.info("房间 %d 进入直播（command=%s）", room_id, command)
            timestamp = command.get("live_time")
        elif cmd == "PREPARING":
            logger.info("房间 %d 结束直播（command=%s）", room_id, command)
            timestamp = command.get("send_time") // 1000
        elif cmd == "ROOM_CHANGE":
            logger.info("房间 %d 变更标题（command=%s）", room_id, command)
            data = command.get("data")
            title = data.get("title")
            timestamp = int(datetime.now().timestamp())
    except Exception:
        pass

    return (
        int(room_id),
        cmd,
        uid,
        uname,
        content,
        gift_name,
        gift_num,
        total_coin,
        title,
        timestamp,
    )


class RawEventHandler(handlers.HandlerInterface):
    def __init__(self, queue: asyncio.Queue[tuple[Any, ...]]):
        self.queue = queue

    def handle(self, client: blivedm.BLiveClient, command: dict[str, Any]):
        cmd = _normalized_cmd(command)
        if cmd == "_HEARTBEAT":
            popularity = _extract_heartbeat_popularity(command)
            logger.info(
                "room=%d heartbeat popularity=%s",
                client.room_id,
                popularity if popularity is not None else "unknown",
            )
            return

        row = _extract_row(client.room_id, command)
        if row is None:
            return
        try:
            self.queue.put_nowait(row)
        except asyncio.QueueFull:
            logger.warning("事件队列已满，丢弃一条消息")


def _build_session(sessdata: str, buvid3: str) -> aiohttp.ClientSession:
    session = aiohttp.ClientSession(
        headers={
            "Accept-Encoding": "gzip, deflate",
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://live.bilibili.com/",
            "Origin": "https://live.bilibili.com",
        }
    )

    cookies = http.cookies.SimpleCookie()
    if sessdata:
        cookies["SESSDATA"] = sessdata
        cookies["SESSDATA"]["domain"] = "bilibili.com"
    if buvid3:
        cookies["buvid3"] = buvid3
        cookies["buvid3"]["domain"] = "bilibili.com"

    if cookies:
        session.cookie_jar.update_cookies(cookies)
    return session


async def _writer_loop(
    db: MetricsDB,
    queue: asyncio.Queue[tuple[Any, ...]],
    stop_event: asyncio.Event,
    flush_interval: float = 1.0,
    batch_size: int = 200,
) -> None:
    buffer: list[tuple[Any, ...]] = []
    last_flush = time.monotonic()

    while not stop_event.is_set() or not queue.empty():
        timeout = max(0.1, flush_interval - (time.monotonic() - last_flush))
        try:
            buffer.append(await asyncio.wait_for(queue.get(), timeout=timeout))
        except asyncio.TimeoutError:
            pass

        now = time.monotonic()
        if buffer and (len(buffer) >= batch_size or now - last_flush >= flush_interval):
            try:
                db.insert_many(buffer)
            except TimeoutError as e:
                logger.warning("写入监控事件遇锁，保留缓冲等待下次刷新：%s", e)
                await asyncio.sleep(0.5)
                continue
            except sqlite3.OperationalError as e:
                if "locked" in str(e).lower():
                    logger.warning("写入监控事件被 SQLite 锁住，保留缓冲等待下次刷新：%s", e)
                    await asyncio.sleep(0.5)
                    continue
                raise
            logger.info("已写入事件批次=%d", len(buffer))
            buffer.clear()
            last_flush = now

    if buffer:
        try:
            db.insert_many(buffer)
        except TimeoutError as e:
            logger.warning("退出前写入事件遇锁，放弃最后 %d 条：%s", len(buffer), e)
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower():
                logger.warning("退出前写入事件被 SQLite 锁住，放弃最后 %d 条：%s", len(buffer), e)
            else:
                raise
        else:
            logger.info("退出前写入事件 %d 条", len(buffer))


async def run_monitor(config: MonitorConfig) -> None:
    db = MetricsDB(config.database)
    queue: asyncio.Queue[tuple[Any, ...]] = asyncio.Queue(maxsize=10000)
    stop_event = asyncio.Event()

    session = _build_session(config.sessdata, config.buvid3)
    loop = asyncio.get_running_loop()

    def _request_stop() -> None:
        if not stop_event.is_set():
            logger.info("收到停止信号，准备退出...")
            stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            pass

    if config.run_seconds > 0:

        async def _auto_stop() -> None:
            logger.info("配置了自动退出时长：%d 秒", config.run_seconds)
            await asyncio.sleep(config.run_seconds)
            _request_stop()

        asyncio.create_task(_auto_stop())

    writer_task = asyncio.create_task(_writer_loop(db, queue, stop_event))
    clients: dict[int, blivedm.BLiveClient] = {}

    async def _start_room(room_id: int) -> None:
        if room_id in clients:
            return
        client = blivedm.BLiveClient(room_id, session=session)
        client.set_handler(RawEventHandler(queue))
        client.start()
        clients[room_id] = client
        logger.info("已启动监听 room_id=%d", room_id)

    async def _stop_room(room_id: int) -> None:
        client = clients.pop(room_id, None)
        if client is None:
            return
        await client.stop_and_close()
        logger.info("已停止监听 room_id=%d", room_id)

    async def _sync_rooms() -> None:
        if not config.rooms_from_db:
            return

        latest_rooms = list_subscribed_room_ids()
        latest_room_set = set(latest_rooms)
        current_room_set = set(clients.keys())

        for room_id in sorted(latest_room_set - current_room_set):
            await _start_room(room_id)

        for room_id in sorted(current_room_set - latest_room_set):
            await _stop_room(room_id)

    async def _sync_rooms_loop() -> None:
        if not config.rooms_from_db:
            return

        while not stop_event.is_set():
            try:
                await _sync_rooms()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("同步订阅房间失败: %s", exc)

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=5)
            except asyncio.TimeoutError:
                continue

    sync_task: asyncio.Task[None] | None = None
    try:
        for room_id in config.rooms:
            await _start_room(room_id)

        if config.rooms_from_db:
            sync_task = asyncio.create_task(_sync_rooms_loop())

        logger.info("monitor 已启动完成，共监听 %d 个房间", len(clients))
        await stop_event.wait()
    finally:
        if sync_task is not None:
            sync_task.cancel()
            try:
                await sync_task
            except asyncio.CancelledError:
                pass
        for room_id in list(clients.keys()):
            await _stop_room(room_id)
        await writer_task
        await session.close()
        db.close()
        logger.info("监听器已退出")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="B站直播监听（独立于 NoneBot 插件）")
    parser.add_argument("--config", type=str, default="", help="可选配置文件路径（JSON）")
    parser.add_argument("--rooms", type=str, default="", help="房间号列表（逗号分隔）")
    parser.add_argument(
        "--rooms-from-db",
        action="store_true",
        help="启动时从 subscription 表读取当前订阅房间号",
    )
    parser.add_argument(
        "--database",
        type=str,
        default="",
        help="可选数据库路径，默认 data/libot.db",
    )
    parser.add_argument(
        "--run-seconds",
        type=int,
        default=None,
        help="可选覆盖配置中的运行时长，0 为持续运行",
    )
    parser.add_argument("--verbose", action="store_true", help="可选覆盖配置，开启调试日志")
    return parser.parse_args()


def main() -> None:
    load_env_file()
    args = _parse_args()
    config_path = Path(args.config) if args.config else None
    config = _load_config(config_path, args)
    _setup_logging(config.verbose)

    logger.info("启动 monitor，rooms=%s db=%s", config.rooms, config.database)
    logger.info(
        "Cookie 状态：SESSDATA=%s",
        "已设置" if config.sessdata else "未设置",
    )
    if not config.sessdata:
        logger.warning("未设置 SESSDATA：可连接，但用户名可能打码、UID 可能为 0")

    asyncio.run(run_monitor(config))


if __name__ == "__main__":
    main()
