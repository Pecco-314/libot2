from __future__ import annotations

import os
import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path
from functools import wraps

from nonebot import get_bots, on_command
from nonebot.adapters.onebot.v11 import Event, Message, MessageSegment, GroupMessageEvent
from nonebot.matcher import Matcher
from nonebot.params import CommandArg
from nonebot_plugin_apscheduler import scheduler

from src.common.env import load_env_file
from src.common.render import render_bilibili_card
from src.common.superchat import get_daily_superchat_image
from src.db.activity import get_max_activity_id, init_activity_db, list_activities_after
from src.db.event import get_newest_live_event, is_streaming_event, is_duplicate_room_change
from src.db.manager import (
    ensure_initial_manager,
    add_manager,
    count_managers,
    init_manager_db,
    is_manager,
    list_managers,
    remove_manager,
)
from src.db.state import get_state, init_state_db, set_state
from src.db.subscription import (
    get_subscription,
    init_subscription_db,
    list_subscribed_group_ids,
    is_subscription_dev_enabled,
    remove_subscription,
    set_subscription_dev,
    set_subscription,
)
from src.db.liver import upsert_liver, init_liver_db
from src.spider.wrapper import get_room_uname


logger = logging.getLogger("libot.libot")
ACTIVITY_IMAGE_DIR = Path(__file__).resolve().parents[2] / "data" / "images" / "activity"

load_env_file()

_ENV_MANAGER_QQ = os.getenv("MANAGER_QQ", "").strip()
INITIAL_MANAGER_QQ = int(_ENV_MANAGER_QQ) if _ENV_MANAGER_QQ.isdigit() else None

try:
    init_manager_db()
    init_subscription_db()
    init_state_db()
    init_liver_db()
    init_activity_db()
except Exception:
    pass


def get_group_id(event: Event) -> int | None:
    if isinstance(event, GroupMessageEvent):
        return int(event.group_id)
    group_id = getattr(event, "group_id", None)
    return int(group_id) if group_id is not None else None


def parse_user_id(arg: Message) -> int | None:
    text = arg.extract_plain_text().strip()
    return int(text) if text.isdigit() else None


def group_manager_required(func):
    @wraps(func)
    async def wrapper(*args, **kwargs):
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

    return wrapper


def subscription_dev_required(func):
    @wraps(func)
    async def wrapper(*args, **kwargs):
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
                return None
            if not is_subscription_dev_enabled(group_id):
                await matcher.finish("本群未开启测试功能")
                return None
            return await func(*args, **kwargs)

        room_id = kwargs.get("room_id")
        if room_id is None and args:
            room_id = args[0]

        if isinstance(room_id, int):
            enabled_groups = [
                group_id
                for group_id in list_subscribed_group_ids(room_id)
                if is_subscription_dev_enabled(group_id)
            ]
            if not enabled_groups:
                return None

        return await func(*args, **kwargs)

    return wrapper


help_cmd = on_command("帮助", priority=5)
superchat_cmd = on_command("查SC", priority=5, block=True)
manager_help_cmd = on_command("管理员帮助", priority=5, block=True)
manager_list_cmd = on_command("查看管理员", aliases={"管理员列表"}, priority=5, block=True)
manager_add_cmd = on_command("添加管理员", priority=5, block=True)
manager_remove_cmd = on_command("删除管理员", priority=5, block=True)
sub_show_cmd = on_command("查看订阅", priority=5, block=True)
sub_set_cmd = on_command("设置订阅", aliases={"订阅直播"}, priority=5, block=True)
sub_remove_cmd = on_command("删除订阅", aliases={"取消订阅"}, priority=5, block=True)
nickname_set_cmd = on_command("设置昵称", priority=5, block=True)
test_enable_cmd = on_command("开启测试", priority=5, block=True)
test_disable_cmd = on_command("关闭测试", priority=5, block=True)
test_status_cmd = on_command("测试状态", priority=5, block=True)


@help_cmd.handle()
async def handle_help():
    await help_cmd.finish(
        "/帮助 - 显示帮助信息\n"
        "/查SC [日期] - 查看醒目留言列表，默认当天\n"
    )


@superchat_cmd.handle()
async def handle_superchat(matcher: Matcher, event: Event, arg=CommandArg()):
    group_id = get_group_id(event)
    if group_id is None:
        await matcher.finish("请在群聊中使用该命令")

    room_id = get_subscription(group_id)
    if room_id is None:
        await matcher.finish("请先设置订阅")

    date_str = arg.extract_plain_text().strip()
    if date_str:
        try:
            day = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            await matcher.finish("日期格式错误，正确格式：YYYY-MM-DD")
    else:
        day = datetime.now()

    image = get_daily_superchat_image(room_id, day)
    if image is None:
        await matcher.finish("没有找到醒目留言")
    else:
        await matcher.finish(MessageSegment.image(file=str(image)))


@manager_help_cmd.handle()
@group_manager_required
async def handle_manager_help(event: Event):
    await manager_help_cmd.finish(
        "/管理员帮助 - 显示管理员帮助信息\n"
        "/查看管理员 - 查看当前群管理员\n"
        "/添加管理员 <QQ号> - 添加群管理员\n"
        "/删除管理员 <QQ号> - 删除群管理员\n"
        "/查看订阅 - 查看当前群订阅\n"
        "/设置订阅 <房间号> - 设置当前群订阅\n"
        "/删除订阅 - 删除当前群订阅\n"
        "/设置昵称 - 修改当前订阅主播的昵称\n"
        "/开启测试 - 开启本群测试功能\n"
        "/关闭测试 - 关闭本群测试功能\n"
        "/测试状态 - 查看本群测试功能状态\n"
    )


@manager_list_cmd.handle()
@group_manager_required
async def handle_manager_list(matcher: Matcher, event: Event):
    group_id = get_group_id(event)
    if group_id is None:
        await matcher.finish("请在群聊中使用该命令")

    managers = list_managers(group_id)
    msg = "当前群管理员：\n" + "\n".join(str(user_id) for user_id in managers)
    await matcher.finish(msg)


@manager_add_cmd.handle()
@group_manager_required
async def handle_manager_add(matcher: Matcher, event: Event, arg=CommandArg()):
    group_id = get_group_id(event)
    if group_id is None:
        await matcher.finish("请在群聊中使用该命令")

    user_id = parse_user_id(arg)
    if user_id is None:
        await matcher.finish("用法：/添加管理员 <QQ号>")

    added = add_manager(group_id, user_id)
    if added:
        await matcher.finish(f"已添加群管理员：{user_id}")
    else:
        await matcher.finish(f"群管理员已存在：{user_id}")


@manager_remove_cmd.handle()
@group_manager_required
async def handle_manager_remove(matcher: Matcher, event: Event, arg=CommandArg()):
    group_id = get_group_id(event)
    if group_id is None:
        await matcher.finish("请在群聊中使用该命令")

    user_id = parse_user_id(arg)
    if user_id is None:
        await matcher.finish("用法：/删除管理员 <QQ号>")

    if is_manager(group_id, user_id) and count_managers(group_id) <= 1:
        await matcher.finish("至少需要保留一位管理员，无法删除最后一个管理员")

    removed = remove_manager(group_id, user_id)
    if removed:
        await matcher.finish(f"已删除群管理员：{user_id}")
    else:
        await matcher.finish(f"群管理员不存在：{user_id}")


def _parse_room_id(arg) -> int | None:
    text = arg.extract_plain_text().strip()
    return int(text) if text.isdigit() else None


async def _format_name(room_id: int | None) -> str:
    if room_id is None:
        return "主播"
    try:
        uname = await get_room_uname(room_id)
    except Exception:
        return f"房间{room_id}"

    return uname if uname else f"房间{room_id}"


@sub_show_cmd.handle()
@group_manager_required
async def handle_show_subscription(matcher: Matcher, event: Event):
    group_id = get_group_id(event)
    if group_id is None:
        await matcher.finish("请在群聊中使用该命令")

    room_id = get_subscription(group_id)
    if room_id is None:
        await matcher.finish("本群尚未设置订阅")

    await matcher.finish(f"当前订阅：{await _format_name(room_id)}")


@sub_set_cmd.handle()
@group_manager_required
async def handle_set_subscription(matcher: Matcher, event: Event, arg=CommandArg()):
    group_id = get_group_id(event)
    if group_id is None:
        await matcher.finish("请在群聊中使用该命令")

    room_id = _parse_room_id(arg)
    if room_id is None:
        await matcher.finish("用法：/设置订阅 <房间号>")

    set_subscription(group_id, room_id)
    await matcher.finish(f"订阅已设置：{await _format_name(room_id)}")


@nickname_set_cmd.handle()
@group_manager_required
async def handle_set_nickname(matcher: Matcher, event: Event, arg=CommandArg()):
    group_id = get_group_id(event)
    if group_id is None:
        await matcher.finish("请在群聊中使用该命令")
    
    room_id = get_subscription(group_id)
    if room_id is None:
        await matcher.finish("请先设置订阅")
    
    nickname = arg.extract_plain_text().strip()
    if not nickname:
        await matcher.finish("用法：/设置昵称 <昵称>")
    upsert_liver(room_id=room_id, uid=None, uname=None, nickname=nickname)
    await matcher.finish(f"昵称已设置：{nickname}")


@test_enable_cmd.handle()
@group_manager_required
async def handle_test_enable(matcher: Matcher, event: Event):
    group_id = get_group_id(event)
    if group_id is None:
        await matcher.finish("请在群聊中使用该命令")

    if not set_subscription_dev(group_id, True):
        await matcher.finish("请先设置订阅，再开启测试功能")
        return
    await matcher.finish("已开启测试功能")


@test_disable_cmd.handle()
@group_manager_required
async def handle_test_disable(matcher: Matcher, event: Event):
    group_id = get_group_id(event)
    if group_id is None:
        await matcher.finish("请在群聊中使用该命令")

    if not set_subscription_dev(group_id, False):
        await matcher.finish("请先设置订阅，再关闭测试功能")
        return
    await matcher.finish("已关闭测试功能")


@test_status_cmd.handle()
@group_manager_required
async def handle_test_status(matcher: Matcher, event: Event):
    group_id = get_group_id(event)
    if group_id is None:
        await matcher.finish("请在群聊中使用该命令")

    enabled = is_subscription_dev_enabled(group_id)
    await matcher.finish("本群测试功能：已开启" if enabled else "本群测试功能：已关闭")


@sub_remove_cmd.handle()
@group_manager_required
async def handle_remove_subscription(matcher: Matcher, event: Event):
    group_id = get_group_id(event)
    if group_id is None:
        await matcher.finish("请在群聊中使用该命令")

    removed = remove_subscription(group_id)
    if removed:
        await matcher.finish("已删除本群订阅")
    else:
        await matcher.finish("本群没有可删除的订阅")


async def send_to_room(room_id: int, message: str) -> None:
    group_ids = list_subscribed_group_ids(room_id)
    if not group_ids:
        return

    bots = list(get_bots().values())
    if not bots:
        logger.warning("没有可用 bot，暂不发送 room_id=%d 的消息", room_id)
        return

    bot = bots[0]
    for group_id in group_ids:
        try:
            await bot.call_api("send_group_msg", group_id=group_id, message=message)
        except Exception as exc:
            logger.warning("发送群消息失败 room_id=%d group_id=%d: %s", room_id, group_id, exc)



async def send_activity_to_room(room_id: int, message) -> None:
    group_ids = list_subscribed_group_ids(room_id)
    if not group_ids:
        return

    bots = list(get_bots().values())
    if not bots:
        logger.warning("没有可用 bot，暂不发送 activity room_id=%d 的消息", room_id)
        return

    bot = bots[0]
    for group_id in group_ids:
        try:
            await bot.call_api("send_group_msg", group_id=group_id, message=message)
        except Exception as exc:
            logger.warning("发送 activity 群消息失败 room_id=%d group_id=%d: %s", room_id, group_id, exc)


def _get_last_activity_id() -> int:
    last_activity_id = 0
    last_activity_id_str = get_state("last_activity_id")
    if last_activity_id_str is not None and last_activity_id_str.isdigit():
        last_activity_id = int(last_activity_id_str)
    return last_activity_id


async def _ensure_last_activity_id_initialized() -> None:
    if get_state("last_activity_id") is not None:
        return
    set_state("last_activity_id", str(get_max_activity_id()))


def _activity_image_path(activity: dict[str, object]) -> Path:
    activity_id = str(activity.get("activity_id") or activity.get("id") or "activity")
    return ACTIVITY_IMAGE_DIR / f"{activity_id}.png"


async def _render_activity_image(activity: dict[str, object]) -> Path | None:
    image_path = _activity_image_path(activity)
    image_path.parent.mkdir(parents=True, exist_ok=True)
    if image_path.exists():
        return image_path

    try:
        image = await asyncio.to_thread(
            render_bilibili_card,
            str(activity.get("card_json_str") or ""),
            int(activity.get("dy_type") or 0),
            int(activity.get("orig_type") or 0),
            int(activity.get("timestamp") or 0),
            activity.get("emoji_details") if isinstance(activity.get("emoji_details"), list) else [],
        )
        await asyncio.to_thread(image.save, image_path)
        return image_path
    except Exception as exc:
        logger.warning(
            "渲染 activity 图片失败 activity_id=%s room_id=%s: %s",
            activity.get("activity_id"),
            activity.get("room_id"),
            exc,
        )
        return None



async def _build_message(row: dict[str, object]) -> str | None:
    name = await _format_name(row.get("room_id"))
    cmd = row.get("cmd")
    if cmd == "LIVE":
        return f"{name}开播了！"
    if cmd == "PREPARING":
        return f"{name}下播了..."
    if cmd == "ROOM_CHANGE":
        title = row.get("title")
        if isinstance(title, str) and title.strip():
            return f"{name}把直播标题修改为：{title.strip()}"
    return None


@scheduler.scheduled_job(
    "interval",
    seconds=1,
    id="libot_live_event_watcher",
    name="libot_live_event_watcher",
    max_instances=1,
    coalesce=True,
    misfire_grace_time=1,
)
async def watch_live_events() -> None:
    row = get_newest_live_event()
    if row is None:
        return
    row_id = row.get("id")
    room_id = row.get("room_id")
    if is_streaming_event(row) or is_duplicate_room_change(row):
        set_state("last_event_id", str(row_id))
        return
    last_event_id = 0
    last_event_id_str = get_state("last_event_id")
    if last_event_id_str is not None and last_event_id_str.isdigit():
        last_event_id = int(last_event_id_str)
    if row_id <= last_event_id:
        return
    set_state("last_event_id", str(row_id))
    message = await _build_message(row)
    if message is None:
        return
    await send_to_room(room_id, message)


@scheduler.scheduled_job(
    "interval",
    seconds=1,
    id="libot_activity_watcher",
    name="libot_activity_watcher",
    max_instances=1,
    coalesce=True,
    misfire_grace_time=1,
)
async def watch_activities() -> None:
    await _ensure_last_activity_id_initialized()

    last_activity_id = _get_last_activity_id()
    rows = list_activities_after(last_activity_id)
    if not rows:
        return

    for row in rows:
        timestamp = int(row.get("timestamp") or 0)
        upload_time = datetime.fromtimestamp(timestamp)
        activity_id = row.get("id")
        if datetime.now() - upload_time > timedelta(minutes=10):
            set_state("last_activity_id", activity_id)
            continue
        logger.info("发现新动态，开始渲染")
        image_path = await _render_activity_image(row)
        if image_path is None:
            set_state("last_activity_id", activity_id)
            continue

        uname = str(row.get("uname") or "UP主")
        message = Message([
            MessageSegment.text(f"{uname}发布了新动态！"),
            MessageSegment.image(file=str(image_path)),
        ])
        await send_activity_to_room(int(row.get("room_id") or 0), message)
        set_state("last_activity_id", activity_id)