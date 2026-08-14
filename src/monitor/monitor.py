from __future__ import annotations

import argparse
import asyncio
import base64
import http.cookies
import json
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
from src.common.bilibili_auth import get_bilibili_auth
from src.common.utils import load_env_file, init_logger
from src.db.sqlite import connect_sqlite, write_transaction
from src.db.subscription import list_subscribed_room_ids

import blivedm.models.web as web_models

logger = init_logger("monitor")
TRACKED_CMDS = {
    "DANMU_MSG",
    "SEND_GIFT",
    "SEND_GIFT_V2",
    "GUARD_BUY",
    "SUPER_CHAT_MESSAGE",
    "LIVE",
    "PREPARING",
    "ROOM_CHANGE",
    "ONLINE_RANK_COUNT",
}


@dataclass(slots=True)
class MonitorConfig:
    rooms: list[int]
    rooms_from_db: bool
    database: Path
    run_seconds: int
    verbose: bool


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

    return MonitorConfig(
        rooms=rooms,
        rooms_from_db=rooms_from_db,
        database=db_path,
        run_seconds=run_seconds,
        verbose=verbose,
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
            _execute_write(
                conn,
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_event_sc_unique
                ON event(room_id, uid, content, total_coin, timestamp)
                WHERE cmd = 'SUPER_CHAT_MESSAGE'
                """,
            )
            _execute_write(
                conn,
                """
                CREATE TABLE IF NOT EXISTS name_history (
                    uid INTEGER,
                    uname TEXT,
                    first_seen INTEGER,
                    UNIQUE(uid, uname)
                )
                """,
            )
            _execute_write(
                conn,
                """
                CREATE INDEX IF NOT EXISTS idx_nh_name_uid
                ON name_history(uname, uid)
                """,
            )
            _execute_write(
                conn,
                """
                CREATE INDEX IF NOT EXISTS idx_nh_uid_time
                ON name_history(uid, first_seen)
                """,
            )

    def insert_many(self, rows: list[tuple[Any, ...]]) -> None:
        if not rows:
            return
        with write_transaction(self.db_path) as conn:
            _execute_many_write(
                conn,
                """
                INSERT OR IGNORE INTO event (
                    room_id, cmd, uid, uname, content, gift_name, gift_num,
                    total_coin, title, timestamp
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            name_first_seen: dict[tuple[int, str], int] = {}
            for row in rows:
                if (
                    row[2] is None
                    or row[3] is None
                    or not str(row[3]).strip()
                    or row[9] is None
                ):
                    continue
                key = (int(row[2]), str(row[3]))
                timestamp = int(row[9])
                previous = name_first_seen.get(key)
                if previous is None or timestamp < previous:
                    name_first_seen[key] = timestamp
            name_rows = [
                (uid, uname, first_seen)
                for (uid, uname), first_seen in name_first_seen.items()
            ]
            if name_rows:
                _execute_many_write(
                    conn,
                    """
                    INSERT OR IGNORE INTO name_history (uid, uname, first_seen)
                    VALUES (?, ?, ?)
                    """,
                    list(name_rows),
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


def _payload_for_log(command: dict[str, Any], limit: int = 12000) -> str:
    try:
        payload = json.dumps(command, ensure_ascii=False, default=str)
    except Exception:
        payload = repr(command)
    if len(payload) > limit:
        return f"{payload[:limit]}...<truncated {len(payload) - limit} chars>"
    return payload


def _int_value(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _timestamp_seconds(value: Any) -> int:
    timestamp = _int_value(value)
    if timestamp >= 10_000_000_000:
        return timestamp // 1000
    if timestamp <= 0:
        return int(datetime.now().timestamp())
    return timestamp


def _read_proto_varint(payload: bytes, pos: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        if pos >= len(payload):
            raise ValueError("protobuf varint 截断")
        byte = payload[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, pos
        shift += 7
        if shift > 63:
            raise ValueError("protobuf varint 过长")


def _decode_proto_fields(payload: bytes) -> dict[int, list[int | bytes]]:
    fields: dict[int, list[int | bytes]] = {}
    pos = 0
    while pos < len(payload):
        key, pos = _read_proto_varint(payload, pos)
        field_number = key >> 3
        wire_type = key & 0x07
        if field_number <= 0:
            raise ValueError("protobuf 字段号非法")
        if wire_type == 0:
            value, pos = _read_proto_varint(payload, pos)
        elif wire_type == 1:
            end = pos + 8
            value = payload[pos:end]
            pos = end
        elif wire_type == 2:
            size, pos = _read_proto_varint(payload, pos)
            end = pos + size
            value = payload[pos:end]
            pos = end
        elif wire_type == 5:
            end = pos + 4
            value = payload[pos:end]
            pos = end
        else:
            raise ValueError(f"不支持的 protobuf wire type: {wire_type}")
        if pos > len(payload):
            raise ValueError("protobuf 字段截断")
        fields.setdefault(field_number, []).append(value)
    return fields


def _proto_int(fields: dict[int, list[int | bytes]], number: int, default: int = 0) -> int:
    for value in reversed(fields.get(number, [])):
        if isinstance(value, int):
            return value
    return default


def _proto_bytes(fields: dict[int, list[int | bytes]], number: int) -> bytes:
    for value in reversed(fields.get(number, [])):
        if isinstance(value, bytes):
            return value
    raise ValueError(f"protobuf 缺少 bytes 字段 {number}")


def _extract_v2_gift_row(room_id: int, command: dict[str, Any]) -> tuple[Any, ...]:
    data = command.get("data")
    if not isinstance(data, dict):
        raise TypeError(f"SEND_GIFT_V2 data 不是对象: {type(data).__name__}")
    encoded = data.get("pb")
    if not isinstance(encoded, str) or not encoded:
        raise ValueError("SEND_GIFT_V2 缺少 data.pb")
    payload = base64.b64decode(encoded, validate=True)
    root_fields = _decode_proto_fields(payload)
    gift_fields = _decode_proto_fields(_proto_bytes(root_fields, 10))
    uid = _proto_int(root_fields, 1)
    uname = _proto_bytes(root_fields, 2).decode("utf-8")
    gift_name = _proto_bytes(gift_fields, 2).decode("utf-8")
    gift_num = max(1, _proto_int(gift_fields, 3, 1))
    price = _proto_int(gift_fields, 5)
    total_coin = (
        _proto_int(gift_fields, 7)
        or _proto_int(gift_fields, 6)
        or price * gift_num
    )
    timestamp = _timestamp_seconds(_proto_int(gift_fields, 10))
    if not uid or not gift_name:
        raise ValueError(f"SEND_GIFT_V2 缺少关键字段: uid={uid} gift_name={gift_name!r}")
    logger.info(
        "房间 %d 收到礼物，parser=protobuf-v2 uid=%d uname=%s "
        "gift_name=%s gift_num=%d total_coin=%d timestamp=%d",
        room_id,
        uid,
        uname,
        gift_name,
        gift_num,
        total_coin,
        timestamp,
    )
    return (
        int(room_id),
        "SEND_GIFT",
        uid,
        uname,
        None,
        gift_name,
        gift_num,
        total_coin,
        None,
        timestamp,
    )


def _extract_row(room_id: int, command: dict[str, Any]) -> tuple[Any, ...] | None:
    cmd = _normalized_cmd(command)
    if cmd not in TRACKED_CMDS:
        return None
    if cmd == "SEND_GIFT_V2":
        try:
            return _extract_v2_gift_row(room_id, command)
        except Exception:
            logger.exception(
                "房间 %d 解析 SEND_GIFT_V2 失败，事件不会入库；raw=%s",
                room_id,
                _payload_for_log(command),
            )
            return None


    uid: int | None = None
    uname: str | None = None
    content: str | None = None
    gift_name: str | None = None
    gift_num: int | None = None
    total_coin: int | None = None
    title: str | None = None

    def _sanitize_text(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value)
        text = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
        return " ".join(text.split())

    try:
        if cmd == "DANMU_MSG":
            msg = web_models.DanmakuMessage.from_command(command["info"])
            uid = int(msg.uid)
            uname = msg.uname
            content = _sanitize_text(msg.msg)
            timestamp = msg.timestamp // 1000
            logger.info("房间 %d 收到弹幕，uid=%d uname=%s content=%s", room_id, uid, uname, content)
        elif cmd == "SEND_GIFT":
            data = command.get("data")
            if not isinstance(data, dict):
                raise TypeError(f"SEND_GIFT data 不是对象: {type(data).__name__}")
            logger.info(
                "房间 %d 收到 SEND_GIFT 原始信号，data_keys=%s raw=%s",
                room_id,
                sorted(data.keys()),
                _payload_for_log(command),
            )
            try:
                msg = web_models.GiftMessage.from_command(data)
                uid = int(msg.uid)
                uname = msg.uname
                gift_name = msg.gift_name
                gift_num = int(msg.num)
                total_coin = int(msg.total_coin)
                timestamp = _timestamp_seconds(msg.timestamp)
                parser = "blivedm"
            except Exception as exc:
                logger.warning(
                    "房间 %d blivedm 解析 SEND_GIFT 失败，改用兼容解析：%s: %s",
                    room_id,
                    type(exc).__name__,
                    exc,
                )
                uid = _int_value(data.get("uid"))
                uname = str(data.get("uname") or data.get("username") or "")
                gift_name = str(data.get("giftName") or data.get("gift_name") or "")
                gift_num = max(1, _int_value(data.get("num") or data.get("gift_num"), 1))
                total_coin = _int_value(data.get("total_coin"))
                if total_coin <= 0:
                    total_coin = _int_value(data.get("price")) * gift_num
                timestamp = _timestamp_seconds(
                    data.get("timestamp") or data.get("send_time") or command.get("send_time")
                )
                parser = "fallback"
            if not uid or not gift_name:
                raise ValueError(
                    f"SEND_GIFT 缺少关键字段: uid={uid} gift_name={gift_name!r} "
                    f"total_coin={total_coin}"
                )
            logger.info(
                "房间 %d 收到礼物，parser=%s uid=%d uname=%s gift_name=%s "
                "gift_num=%d total_coin=%d timestamp=%d",
                room_id,
                parser,
                uid,
                uname,
                gift_name,
                gift_num,
                total_coin,
                timestamp,
            )
        elif cmd == "GUARD_BUY":
            msg = web_models.GuardBuyMessage.from_command(command["data"])
            uid = int(msg.uid)
            uname = msg.username
            gift_name = msg.gift_name
            gift_num = int(msg.num)
            total_coin = int(msg.price) * int(msg.num)
            timestamp = msg.start_time
            logger.info("房间 %d 收到大航海（command=%s）", room_id, command)
        elif cmd == "SUPER_CHAT_MESSAGE":
            msg = web_models.SuperChatMessage.from_command(command["data"])
            uid = int(msg.uid)
            uname = msg.uname
            content = _sanitize_text(msg.message)
            total_coin = int(msg.price)
            gift_name = msg.gift_name
            gift_num = 1
            timestamp = msg.start_time
            logger.info("房间 %d 收到醒目留言（command=%s）", room_id, command)
        elif cmd == "LIVE":
            timestamp = command.get("live_time")
            logger.info("房间 %d 进入直播", room_id)
        elif cmd == "PREPARING":
            timestamp = command.get("send_time") // 1000
            logger.info("房间 %d 结束直播", room_id)
        elif cmd == "ROOM_CHANGE":
            data = command.get("data")
            title = data.get("title")
            timestamp = int(datetime.now().timestamp())
            logger.info("房间 %d 房间标题变更，title=%s", room_id, title)
        elif cmd == "ONLINE_RANK_COUNT":
            data = command.get("data") if isinstance(command.get("data"), dict) else {}
            count = data.get("count")
            if isinstance(count, str) and count.isdigit():
                count = int(count)
            if not isinstance(count, int):
                count = 0
            content = count
            timestamp = int(datetime.now().timestamp())
            logger.info("房间 %d 在线观众数=%s", room_id, content)
    except Exception:
        logger.exception(
            "房间 %d 解析事件失败 cmd=%s，事件不会入库；raw=%s",
            room_id,
            cmd,
            _payload_for_log(command),
        )
        return None

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
            try:
                Path("data/.monitor_heartbeat").touch()
            except Exception:
                pass
            return
        if cmd not in TRACKED_CMDS and any(
            token in cmd.upper() for token in ("GIFT", "COMBO", "BLIND_BOX")
        ):
            logger.warning(
                "房间 %d 收到未识别的礼物相关命令 cmd=%s raw=%s",
                client.room_id,
                cmd,
                _payload_for_log(command),
            )

        row = _extract_row(client.room_id, command)
        if row is None:
            return
        try:
            self.queue.put_nowait(row)
            if row[1] == "SEND_GIFT":
                logger.info(
                    "房间 %d 礼物已入队，queue_size=%d uid=%s gift_name=%s",
                    client.room_id,
                    self.queue.qsize(),
                    row[2],
                    row[5],
                )
        except asyncio.QueueFull:
            logger.warning("事件队列已满，丢弃一条消息")


def _replace_session_cookies(
    session: aiohttp.ClientSession,
    cookie_values: dict[str, str],
) -> None:
    session.cookie_jar.clear(
        lambda morsel: str(morsel["domain"])
        .lstrip(".")
        .endswith("bilibili.com")
    )

    cookies = http.cookies.SimpleCookie()
    for name, value in cookie_values.items():
        if not name or value is None:
            continue
        try:
            cookies[name] = value
            cookies[name]["domain"] = ".bilibili.com"
            cookies[name]["path"] = "/"
        except http.cookies.CookieError:
            logger.warning("忽略非法 Bilibili Cookie 名称: %s", name)

    if cookies:
        session.cookie_jar.update_cookies(cookies)


def _build_session(cookie_values: dict[str, str]) -> aiohttp.ClientSession:
    session = aiohttp.ClientSession(
        headers={
            "Accept-Encoding": "gzip, deflate",
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://live.bilibili.com/",
            "Origin": "https://live.bilibili.com",
        }
    )
    _replace_session_cookies(session, cookie_values)
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
            gift_rows = [row for row in buffer if row[1] == "SEND_GIFT"]
            for row in gift_rows:
                logger.info(
                    "房间 %s 礼物已落库，uid=%s gift_name=%s gift_num=%s "
                    "total_coin=%s timestamp=%s",
                    row[0],
                    row[2],
                    row[5],
                    row[6],
                    row[7],
                    row[9],
                )
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

    auth = get_bilibili_auth()
    auth_revision = auth.revision
    session = _build_session(auth.cookies)
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

    async def _sync_auth_loop() -> None:
        nonlocal auth_revision
        while not stop_event.is_set():
            try:
                latest_auth = get_bilibili_auth()
                if latest_auth.revision != auth_revision:
                    _replace_session_cookies(session, latest_auth.cookies)
                    auth_revision = latest_auth.revision
                    logger.info(
                        "已原地更新 monitor CookieJar，revision=%d",
                        auth_revision,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("热加载 Bilibili Cookie 失败: %s", exc)

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=5)
            except asyncio.TimeoutError:
                continue

    sync_task: asyncio.Task[None] | None = None
    auth_sync_task = asyncio.create_task(_sync_auth_loop())
    try:
        for room_id in config.rooms:
            await _start_room(room_id)

        if config.rooms_from_db:
            sync_task = asyncio.create_task(_sync_rooms_loop())

        logger.info("monitor 已启动完成，共监听 %d 个房间", len(clients))
        await stop_event.wait()
    finally:
        auth_sync_task.cancel()
        try:
            await auth_sync_task
        except asyncio.CancelledError:
            pass
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

    logger.info("启动 monitor，rooms=%s db=%s", config.rooms, config.database)
    auth = get_bilibili_auth()
    logger.info(
        "Cookie 状态：SESSDATA=%s refresh_token=%s revision=%d",
        "已设置" if auth.has_sessdata else "未设置",
        "已设置" if auth.refresh_token else "未设置",
        auth.revision,
    )
    if not auth.has_sessdata:
        logger.warning(
            "COOKIE 中未设置 SESSDATA：可连接，但用户名可能打码、UID 可能为 0"
        )

    asyncio.run(run_monitor(config))


if __name__ == "__main__":
    main()
