from __future__ import annotations

import logging
import re
import asyncio
from datetime import datetime, timedelta

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, Event, MessageSegment, Message
from nonebot.matcher import Matcher
from nonebot.params import CommandArg
from datetime import datetime
from nonebot import on_command
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message

from src.db.song_list import search_songs_by_title, add_new_song, add_song_record
from src.render.superchat import get_daily_superchat_images, get_user_superchat_images
from src.render.help import render_help_image, render_admin_help_image
from src.render.stats import render_fans_trend, render_guards_trend, render_fan_club_trend, render_concurrent_trend
from src.render.song import render_songs_by_keyword, render_random_song, render_songs_by_singer, render_songs_by_date
from src.render.danmaku_rank import render_danmaku_rank, build_danmaku_rank_items
from src.render.danmaku_logs import render_event_pages
from src.render.dc import render_dc_images
from src.render.live_sessions import render_live_sessions_image
from src.render.live_list import render_live_list_image
from src.spider.api import get_room_info as _api_get_room_info, get_master_info as _api_get_master_info
from src.db.liver import get_name_by_uid as _db_get_name_by_uid
from src.db.live_list import add_live_list, remove_live_list, get_live_list, get_live_list_info, update_live_list_tags
from src.db.manager import ensure_initial_manager, is_manager
from src.plugins.utils import INITIAL_MANAGER_QQ
from src.spider.wrapper import (
    get_name_by_roomid,
    get_name_by_uid,
    get_uid_by_roomid,
    get_fans_num,
    get_guard_num,
    get_fan_club_num,
)
from src.common.utils import ROOT
from src.db.manager import (
    add_manager,
    count_managers,
    list_managers,
    remove_manager,
    is_manager,
)
from src.db.subscription import (
    get_subscription,
    remove_subscription,
    set_subscription,
    get_subscription_feature,
    set_subscription_feature,
)
from src.db.stats import get_stat_start_date
from src.db.state import get_state, set_state
from src.db.liver import upsert_liver
from src.db.event import (
    list_name_history_by_name_or_uid,
    list_online_counts,
    list_live_sessions_by_date,
    get_latest_live_session,
    list_session_events,
    get_latest_uid_by_uname,
    list_events_by_uid,
    list_recent_events_by_uid,
)
from src.db.live_list import add_live_list, remove_live_list, get_live_list
from src.capture.guesser import guess_song
from src.spider.jobs.lyrics import sync_and_clean_lyrics

from .utils import (
    get_group_id,
    parse_user_id,
    _parse_room_id,
    _format_name,
    group_manager_required,
    subscription_dev_required,
)

logger = logging.getLogger("libot.commands")

help_cmd = on_command("帮助", priority=5)
superchat_cmd = on_command("查SC", aliases={"查sc", "查Sc"}, priority=5, block=True)
manager_help_cmd = on_command("管理员帮助", priority=5, block=True)
feature_enable_cmd = on_command("打开功能", priority=5, block=True)
feature_disable_cmd = on_command("关闭功能", priority=5, block=True)
feature_status_cmd = on_command("功能状态", priority=5, block=True)
manager_list_cmd = on_command("查看管理员", aliases={"管理员列表"}, priority=5, block=True)
manager_add_cmd = on_command("添加管理员", priority=5, block=True)
manager_remove_cmd = on_command("删除管理员", priority=5, block=True)
sub_show_cmd = on_command("查看订阅", priority=5, block=True)
sub_set_cmd = on_command("设置订阅", aliases={"订阅直播"}, priority=5, block=True)
sub_remove_cmd = on_command("删除订阅", aliases={"取消订阅"}, priority=5, block=True)
nickname_set_cmd = on_command("设置昵称", priority=5, block=True)
name_history_cmd = on_command("曾用名", aliases={"查曾用名"}, priority=5, block=True)
fans_trend_cmd = on_command("查粉丝", priority=5, block=True)
guards_trend_cmd = on_command("查舰长", aliases={"查大航海"}, priority=5, block=True)
club_trend_cmd = on_command("查粉丝团", priority=5, block=True)
concurrent_cmd = on_command("查同接", priority=5, block=True)
danmaku_rank_cmd = on_command("弹幕榜", priority=5, block=True)
events_cmd = on_command("查弹幕", priority=5, block=True)
song_search_cmd = on_command("查歌曲", priority=5, block=True)
song_singer_cmd = on_command("查歌手", priority=5, block=True)
song_list_cmd = on_command("查歌单", priority=5, block=True)
random_search_cmd = on_command("随机歌曲", priority=5, block=True)
now_playing_cmd = on_command("在唱什么", aliases={"正在演唱"}, priority=5, block=True)
dc_cmd = on_command("斗虫", priority=5, block=True)
live_list_add_cmd = on_command("添加直播", aliases={"增加直播"}, priority=5, block=True)
live_list_remove_cmd = on_command("删除直播", priority=5, block=True)
live_list_show_cmd = on_command("开播列表", aliases={"开播"}, priority=5, block=True)
live_list_add_tag_cmd = on_command("添加标签", aliases={"增加标签"}, priority=5, block=True)
live_list_set_tag_cmd = on_command("修改标签", priority=5, block=True)
live_sessions_cmd = on_command("直播记录", aliases={"查直播"}, priority=5, block=True)
cmd_add_song = on_command("新增歌曲", priority=5, block=True)
cmd_generate_list = on_command("生成歌单", priority=5, block=True)
cmd_update_list = on_command("提交歌单", aliases={"提交"}, priority=5, block=True)

@help_cmd.handle()
async def handle_help(matcher: Matcher):
    try:
        result = await render_help_image()
    except Exception as exc:
        logger.error("渲染帮助图片失败: %s", exc)
        await matcher.finish("图片渲染失败")

    await matcher.finish(MessageSegment.image(file=str(result["image_path"])))


@superchat_cmd.handle()
async def handle_superchat(matcher: Matcher, bot: Bot, event: Event, arg=CommandArg()):
    group_id = get_group_id(event)
    if group_id is None:
        await matcher.finish("请在群聊中使用该命令")

    room_id = get_subscription(group_id)
    if room_id is None:
        await matcher.finish("请先设置订阅")

    query = arg.extract_plain_text().strip()
    if not query:
        images = await get_daily_superchat_images(room_id, datetime.now(), chunk_size=40)
        empty_message = "今天没有找到醒目留言"
    elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", query):
        try:
            day = datetime.strptime(query, "%Y-%m-%d")
        except ValueError:
            await matcher.finish("日期格式错误，正确格式：YYYY-MM-DD")
        images = await get_daily_superchat_images(room_id, day, chunk_size=40)
        empty_message = f"{query} 没有找到醒目留言"
    else:
        if query.isdigit():
            uid = int(query)
        else:
            uid = get_latest_uid_by_uname(room_id, query)
            if uid is None:
                await matcher.finish(f"未找到用户：{query}")

        user_name = await get_name_by_uid(uid) or query
        images = await get_user_superchat_images(
            room_id,
            uid,
            user_name,
            chunk_size=40,
        )
        empty_message = f"没有找到 {user_name}（UID {uid}）的醒目留言"

    if not images:
        await matcher.finish(empty_message)
    
    nodes = []
    for img in images:
        nodes.append({
            "type": "node",
            "data": {
                "name": "Libot",
                "uin": bot.self_id,
                "content": MessageSegment.image(file=str(img))
            }
        })
    
    try:
        await bot.call_api("send_group_forward_msg", group_id=group_id, messages=nodes)
    except Exception as e:
        logger.error("发送醒目留言群转发消息失败: %s", e)


@manager_help_cmd.handle()
@group_manager_required
async def handle_manager_help(matcher: Matcher, event: Event):
    try:
        result = await render_admin_help_image()
    except Exception as exc:
        logger.error("渲染管理员帮助图片失败: %s", exc)
        await matcher.finish("图片渲染失败")

    message = Message([
        MessageSegment.text("管理员帮助："),
        MessageSegment.image(file=str(result["image_path"])),
    ])
    await matcher.finish(message)


def _parse_feature_name(text: str) -> str | None:
    normalized = text.strip()
    if normalized in {"测试", "功能测试"}:
        return "测试"
    if normalized in {"艾特全体", "@全体"}:
        return "艾特全体"
    if normalized in {"退群通知", "退群提醒"}:
        return "退群通知"
    if normalized in {"进群欢迎", "进群通知", "欢迎"}:
        return "进群欢迎"
    return normalized


FEATURE_REGISTRY = {
    "测试": {"col": "dev", "name": "测试功能"},
    "艾特全体": {"col": "mention_all", "name": "开播通知艾特全体"},
    "退群通知": {"col": "leave_notice", "name": "退群通知"},
    "进群欢迎": {"col": "join_notice", "name": "进群欢迎"},
    "听歌识曲": {"col": "enable_asr", "name": "听歌识曲"},
}

async def _handle_feature_toggle(matcher: Matcher, event: Event, feature: str, enabled: bool):
    group_id = get_group_id(event)
    if group_id is None:
        await matcher.finish("请在群聊中使用该命令")

    if feature not in FEATURE_REGISTRY:
        features_str = "、".join(FEATURE_REGISTRY.keys())
        await matcher.finish(f"用法：/打开功能 <功能>\n支持的功能有：{features_str}")

    config = FEATURE_REGISTRY[feature]
    col_name = config["col"]
    feature_name = config["name"]

    # 操作数据库
    ok = set_subscription_feature(group_id, col_name, enabled)
    if not ok:
        await matcher.finish(f"请先设置订阅，再{'开启' if enabled else '关闭'}{feature_name}")

    await matcher.finish(f"已{'开启' if enabled else '关闭'}{feature_name}")


async def _handle_feature_status(matcher: Matcher, event: Event, feature: str):
    group_id = get_group_id(event)
    if group_id is None:
        await matcher.finish("请在群聊中使用该命令")

    if feature not in FEATURE_REGISTRY:
        features_str = "、".join(FEATURE_REGISTRY.keys())
        await matcher.finish(f"用法：/功能状态 <功能>\n支持的功能有：{features_str}")

    config = FEATURE_REGISTRY[feature]
    
    # 从数据库获取状态
    is_enabled = get_subscription_feature(group_id, config["col"])
    
    status = "已开启" if is_enabled else "已关闭"
    await matcher.finish(f"{config['name']}：{status}")


@feature_enable_cmd.handle()
@group_manager_required
async def handle_feature_enable(matcher: Matcher, event: Event, arg=CommandArg()):
    feature = _parse_feature_name(arg.extract_plain_text())
    if not feature:
        await matcher.finish("用法：/打开功能 <功能>")
    await _handle_feature_toggle(matcher, event, feature, True)


@feature_disable_cmd.handle()
@group_manager_required
async def handle_feature_disable(matcher: Matcher, event: Event, arg=CommandArg()):
    feature = _parse_feature_name(arg.extract_plain_text())
    if not feature:
        await matcher.finish("用法：/关闭功能 <功能>")
    await _handle_feature_toggle(matcher, event, feature, False)


@feature_status_cmd.handle()
@group_manager_required
async def handle_feature_status(matcher: Matcher, event: Event, arg=CommandArg()):
    feature = _parse_feature_name(arg.extract_plain_text())
    if not feature:
        await matcher.finish("用法：/功能状态 <功能>")
    await _handle_feature_status(matcher, event, feature)


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


@name_history_cmd.handle()
async def handle_name_history(matcher: Matcher, event: Event, arg=CommandArg()):
    query = arg.extract_plain_text().strip()
    if not query:
        await matcher.finish("用法：/曾用名 <UID/用户名>")
    history = list_name_history_by_name_or_uid(query)
    if not history:
        await matcher.finish(f"没有找到符合条件的用户")
    result = f"找到{len(history)}个用户：\n"
    for (i, entry) in enumerate(history, start=1):
        names = entry["history"]
        try:
            current_name = await get_name_by_uid(entry["uid"])
        except Exception:
            logger.warning(f"查询用户 {entry['uid']} 的当前名称失败")
            current_name = names[-1]
        result += f"{i}. {current_name} ({', '.join(names)})\n"
    await matcher.finish(result)


async def _handle_stats_query(matcher: Matcher, event: Event, arg: MessageSegment, stat_type: str):
    group_id = get_group_id(event)
    if group_id is None:
        await matcher.finish("请在群聊中使用该命令")

    # 获取本群订阅的房间号
    room_id = get_subscription(group_id)
    if not room_id:
        await matcher.finish("本群未设置订阅，请先订阅后再查询")

    # 提取参数中的天数，如果没写默认查过去 1 天
    query_text = arg.extract_plain_text().strip().rstrip("天日")
    days = 1
    if query_text.isdigit():
        days = int(query_text)
    stat_start_date = get_stat_start_date(room_id, stat_type)
    days_since_stat_start = (datetime.now() - stat_start_date).days

    uname = await get_name_by_roomid(room_id) or str(room_id)

    current_value: int | None = None
    try:
        uid = await get_uid_by_roomid(room_id)
        if stat_type == "fans":
            current_value = await get_fans_num(uid)
        elif stat_type == "guards":
            current_value = await get_guard_num(room_id, uid)
        else:
            current_value = await get_fan_club_num(uid)
    except Exception as exc:
        logger.warning("查询实时%s数据失败: %s", stat_type, exc)

    # 路由到对应的渲染逻辑
    if stat_type == "fans":
        data = await render_fans_trend(room_id, days, uname)
    elif stat_type == "guards":
        data = await render_guards_trend(room_id, days, uname)
    else:
        data = await render_fan_club_trend(room_id, days, uname)

    if not data:
        await matcher.finish("数据不足，生成失败")

    now = data["end_value"]
    delta = data["end_value"] - data["begin_value"]
    image_path = data["path"]

    stat_name = ""
    if stat_type == "fans":
        stat_name = "粉丝"
    elif stat_type == "guards":
        stat_name = "大航海"
    else:
        stat_name = "粉丝团"
    display_value = current_value if current_value is not None else now
    text = f"{uname}的{stat_name}数：{display_value} ({delta:+})"
    if days > days_since_stat_start:
        text += f"\n（数据从{stat_start_date.strftime('%Y-%m-%d')}开始）"

    message = Message([
        MessageSegment.text(text),
        MessageSegment.image(file=str(image_path)),
    ])
    
    await matcher.finish(message)


@fans_trend_cmd.handle()
async def handle_fans_trend(matcher: Matcher, event: Event, arg=CommandArg()):
    await _handle_stats_query(matcher, event, arg, "fans")


@guards_trend_cmd.handle()
async def handle_guards_trend(matcher: Matcher, event: Event, arg=CommandArg()):
    await _handle_stats_query(matcher, event, arg, "guards")


@club_trend_cmd.handle()
async def handle_club_trend(matcher: Matcher, event: Event, arg=CommandArg()):
    await _handle_stats_query(matcher, event, arg, "club")


def _parse_concurrent_args(text: str) -> tuple[str | None, int | None]:
    parts = [p for p in text.strip().split() if p]
    if not parts:
        return None, None
    date_str = parts[0]
    session_index = None
    if len(parts) > 1 and parts[1].isdigit():
        session_index = int(parts[1])
    return date_str, session_index


def _resolve_live_session(room_id: int, date_str: str | None, session_index: int | None) -> tuple[dict, str, bool]:
    if date_str is None:
        target_session = get_latest_live_session(room_id)
        if target_session is None:
            raise ValueError("没有找到直播记录")
        ongoing = bool(target_session.get("ongoing"))
        session_label = "当前直播" if ongoing else "最近直播"
        return target_session, session_label, ongoing

    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        raise ValueError("日期格式错误，正确格式：YYYY-MM-DD")

    sessions = list_live_sessions_by_date(room_id, date_str)
    if not sessions:
        raise ValueError("没有找到对应日期的直播记录")

    if session_index is None:
        target_session = sessions[-1]
        session_index_label = len(sessions)
    else:
        if session_index <= 0 or session_index > len(sessions):
            raise ValueError("场次超出范围")
        target_session = sessions[session_index - 1]
        session_index_label = session_index

    session_label = f"{date_str}第{session_index_label}场直播"
    ongoing = bool(target_session.get("ongoing"))
    return target_session, session_label, ongoing


@concurrent_cmd.handle()
async def handle_concurrent(matcher: Matcher, event: Event, arg=CommandArg()):
    group_id = get_group_id(event)
    if group_id is None:
        await matcher.finish("请在群聊中使用该命令")

    room_id = get_subscription(group_id)
    if room_id is None:
        await matcher.finish("请先设置订阅")

    raw = arg.extract_plain_text()
    date_str, session_index = _parse_concurrent_args(raw)

    try:
        target_session, session_label, ongoing = _resolve_live_session(room_id, date_str, session_index)
    except ValueError as exc:
        await matcher.finish(str(exc))

    start_ts = int(target_session["start_ts"])
    end_ts = target_session["end_ts"]
    if end_ts is None:
        end_ts = int(datetime.now().timestamp())
    end_ts = int(end_ts)

    records = list_online_counts(room_id, start_ts, end_ts)
    if not records or len(records) < 2:
        await matcher.finish("同接数据不足")

    times = [datetime.fromtimestamp(r["timestamp"]) for r in records]
    values = [int(r["count"]) for r in records]

    uname = await get_name_by_roomid(room_id) or str(room_id)

    title = f"{uname} 同接趋势（{session_label}）"
    chart = await render_concurrent_trend(times, values, title)
    if not chart:
        await matcher.finish("数据不足，生成失败")

    avg_value = int(round(sum(values) / len(values)))
    max_value = max(values)
    message_text = f"{session_label}平均同接：{avg_value}，最高同接：{max_value}"
    if session_label == "当前直播":
        message_text = f"{message_text}，当前同接：{values[-1]}"

    message = Message([
        MessageSegment.text(message_text),
        MessageSegment.image(file=str(chart["path"])),
    ])
    await matcher.finish(message)


@danmaku_rank_cmd.handle()
async def handle_danmaku_rank(matcher: Matcher, event: Event, arg=CommandArg()):
    group_id = get_group_id(event)
    if group_id is None:
        await matcher.finish("请在群聊中使用该命令")

    room_id = get_subscription(group_id)
    if room_id is None:
        await matcher.finish("请先设置订阅")

    raw = arg.extract_plain_text()
    date_str, session_index = _parse_concurrent_args(raw)

    try:
        target_session, session_label, _ongoing = _resolve_live_session(room_id, date_str, session_index)
    except ValueError as exc:
        await matcher.finish(str(exc))

    start_ts = int(target_session["start_ts"])
    end_ts = target_session["end_ts"]
    if end_ts is None:
        end_ts = int(datetime.now().timestamp())
    end_ts = int(end_ts)

    rows = list_session_events(room_id, start_ts, end_ts)
    if not rows:
        await matcher.finish("暂无弹幕数据")

    top_items = build_danmaku_rank_items(rows, limit=20)
    if not top_items:
        await matcher.finish("暂无弹幕数据")

    uname = await get_name_by_roomid(room_id) or str(room_id)
    title = f"{uname} 弹幕榜（{session_label}）"
    result = await render_danmaku_rank(title, top_items)

    message = Message([
        MessageSegment.text(f"{session_label}弹幕榜："),
        MessageSegment.image(file=str(result["image_path"]))
    ])
    await matcher.finish(message)


def _parse_events_args(text: str) -> tuple[str | None, str | None]:
    parts = [p for p in text.strip().split() if p]
    if not parts:
        return None, None
    target = parts[0]
    date_str = parts[1] if len(parts) > 1 else None
    return target, date_str


@events_cmd.handle()
async def handle_events(matcher: Matcher, bot: Bot, event: Event, arg=CommandArg()):
    group_id = get_group_id(event)
    if group_id is None:
        await matcher.finish("请在群聊中使用该命令")

    room_id = get_subscription(group_id)
    if room_id is None:
        await matcher.finish("请先设置订阅")

    args = arg.extract_plain_text().strip().split()
    if not args:
        await matcher.finish("用法：/查弹幕 <UID/用户名> [日期/数量]")

    query_user = args[0]
    param = args[1] if len(args) > 1 else None

    # 获取 UID
    if query_user.isdigit():
        uid = int(query_user)
    else:
        uid = get_latest_uid_by_uname(room_id, query_user)
        if uid is None:
            await matcher.finish(f"未找到用户：{query_user}")

    limit = None
    start_ts = None
    end_ts = None
    title_suffix = ""

    if not param:
        param = "100" # 默认100条
    if param.isdigit():
        limit = int(param)
        if limit <= 0 or limit > 2000:
            await matcher.finish("查询数量必须在1到2000之间")
        title_suffix = f"最近 {limit} 条"
    else:
        try:
            target_date = datetime.strptime(param, "%Y-%m-%d").date()
            start_dt = datetime.combine(target_date, datetime.min.time())
            end_dt = datetime.combine(target_date, datetime.max.time())
            start_ts = int(start_dt.timestamp())
            end_ts = int(end_dt.timestamp())
            title_suffix = param
        except ValueError:
            await matcher.finish("参数格式错误，请输入正确的数量或日期(YYYY-MM-DD)")

    # 根据解析结果执行不同的查询
    if limit is not None:
        events = list_recent_events_by_uid(room_id, uid, limit)
    else:
        events = list_events_by_uid(room_id, uid, start_ts, end_ts)

    if not events:
        await matcher.finish("暂无记录")

    uname = await get_name_by_uid(uid) or str(uid)
    title = f"{uname} 的弹幕记录（{title_suffix}）"

    pages = await render_event_pages(title, events, show_date=(limit is not None))
    if not pages:
        await matcher.finish("暂无记录")

    nodes = []
    for img in pages:
        nodes.append({
            "type": "node",
            "data": {
                "name": "Libot",
                "uin": bot.self_id,
                "content": MessageSegment.image(file=str(img)),
            },
        })

    try:
        await bot.call_api("send_group_forward_msg", group_id=group_id, messages=nodes)
    except Exception as exc:
        logger.error("发送弹幕记录失败: %s", exc)


@song_search_cmd.handle()
async def handle_song_search(bot: Bot, event: Event, matcher: Matcher, arg=CommandArg()):
    keyword = arg.extract_plain_text().strip()
    if not keyword:
        await matcher.finish("用法：/查歌曲 <歌名>")

    try:
        results = await render_songs_by_keyword(keyword)
    except Exception as e:
        logger.error(f"渲染歌曲卡片失败: {e}")
        await matcher.finish("图片渲染失败")

    if not results:
        await matcher.finish(f"未找到与“{keyword}”相关的演唱记录")

    # 1. 准备合并转发的消息节点
    forward_nodes = []
    
    # 第一条节点：文字汇总提示
    maxsize = 5
    if len(results) < maxsize:
        content = f"找到 {len(results)} 首与“{keyword}”相关的歌曲："
    else:
        content = f"找到 {len(results)} 首与“{keyword}”相关的歌曲（已达到搜索上限）："
    forward_nodes.append({
        "type": "node",
        "data": {
            "name": "LiBot",
            "uin": bot.self_id,
            "content": content
        }
    })
    
    # 后续节点：每首歌一张图片
    for i, res in enumerate(results, start=1):
        forward_nodes.append({
            "type": "node",
            "data": {
                "name": "LiBot",
                "uin": bot.self_id,
                "content": [MessageSegment.text(f"{i}. {res['data']['title']}"),
                            MessageSegment.image(file=str(res["image_path"]))]
            }
        })

    group_id = getattr(event, "group_id", None)

    try:
        if group_id:
            await bot.call_api(
                "send_group_forward_msg",
                group_id=group_id,
                messages=forward_nodes
            )
        else:
            await matcher.finish("请在群聊中使用该命令")
    except Exception as e:
        logger.error(f"发送合并转发消息失败: {e}")


@song_list_cmd.handle()
async def handle_song_list(matcher: Matcher, arg=CommandArg()):
    date_str = arg.extract_plain_text().strip()
    if not date_str:
        await matcher.finish("用法：/查歌单 <日期>，日期格式：YYYY-MM-DD")

    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        await matcher.finish("日期格式错误，正确格式：YYYY-MM-DD")

    try:
        result = await render_songs_by_date(target_date)
    except Exception as exc:
        logger.error("渲染日期歌单失败: %s", exc)
        await matcher.finish("图片渲染失败")

    if not result:
        await matcher.finish(f"{date_str} 没有找到歌单记录")

    message = Message([
        MessageSegment.text(f"{date_str} 的歌单："),
        MessageSegment.image(file=str(result["image_path"])),
    ])
    await matcher.finish(message)


@song_singer_cmd.handle()
async def handle_song_singer(matcher: Matcher, arg=CommandArg()):
    singer = arg.extract_plain_text().strip()
    if not singer:
        await matcher.finish("用法：/查歌手 <歌手名>")

    try:
        result = await render_songs_by_singer(singer)
    except Exception as exc:
        logger.error("渲染歌手歌曲列表失败: %s", exc)
        await matcher.finish("图片渲染失败")

    if not result:
        await matcher.finish(f"未找到与“{singer}”相关的歌曲")

    message = Message([
        MessageSegment.text(f"歌手 {singer} 的歌曲列表："),
        MessageSegment.image(file=str(result["image_path"])),
    ])
    await matcher.finish(message)


@random_search_cmd.handle()
async def handle_random_song(matcher: Matcher, arg=CommandArg()):
    count = arg.extract_plain_text().strip()
    lowest_count = 3
    if count.isdigit():
        lowest_count = int(count)
    
    try:
        result = await render_random_song(lowest_count)
    except Exception as e:
        logger.error(f"渲染歌曲卡片失败: {e}")
        await matcher.finish("图片渲染失败")

    if not result:
        await matcher.finish(f"未找到演唱次数大于等于{lowest_count}的歌曲")

    message = Message([
        MessageSegment.text(f"随机抽取到歌曲：{result['data']['title']}"),
        MessageSegment.image(file=str(result["image_path"])),
    ])
    
    await matcher.finish(message)


@now_playing_cmd.handle()
async def handle_now_playing(matcher: Matcher, event: Event, arg=CommandArg()):
    group_id = get_group_id(event)
    if group_id is None:
        await matcher.finish("请在群聊中使用该命令")

    room_id = get_subscription(group_id)
    if room_id is None:
        await matcher.finish("请先设置订阅")
    
    time_str = arg.extract_plain_text().strip()
    if time_str:
        try:
            target_ts = int(datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S").timestamp())
        except ValueError:
            await matcher.finish("日期时间格式错误，正确格式：YYYY-MM-DD HH:MM:SS")
    else:
        target_ts = int(datetime.now().timestamp())
    try:
        results = guess_song(room_id, target_ts)
    except Exception as e:
        logger.error(f"歌曲匹配时发生异常: {e}")
        await matcher.finish("歌曲匹配时发生异常")
    if not results:
        await matcher.finish("未找到匹配的歌曲")
    message = "当前可能在唱的歌曲：\n"
    for i, res in enumerate(results, start=1):
        message += f"{i}. {res['title']} - {res['singer']} ({res['final_score']:.2%})\n"
    await matcher.finish(message)

@dc_cmd.handle()
async def handle_dc(matcher: Matcher, bot: Bot, event: Event, arg=CommandArg()):
    group_id = get_group_id(event)
    if group_id is None:
        await matcher.finish("请在群聊中使用该命令")
        
    args = arg.extract_plain_text().strip().split()
    
    # 默认选项设置
    filter_type = "vr"
    time_str = datetime.now().strftime("%Y-%m")
    
    # 解析参数选项
    for a in args:
        a_upper = a.upper()
        if a_upper in ["VR", "PSP", "VRPSP", "ALL"]:
            filter_type = a_upper.lower()
            if filter_type == "vrpsp":
                filter_type = "all"
        elif re.match(r"^20\d{2}$", a):  # 例如 2026
            time_str = a
        elif re.match(r"^20\d{2}-(0[1-9]|1[0-2])$", a):  # 例如 2026-06
            time_str = a
        elif re.match(r"^20\d{2}(0[1-9]|1[0-2])$", a):  # 兼容 202606 这种连续格式
            time_str = f"{a[:4]}-{a[4:]}"
            
    try:
        images = await render_dc_images(filter_type, time_str)
    except Exception as e:
        logger.error(f"渲染斗虫图片失败: {e}")
        await matcher.finish("数据获取或图片渲染失败，请稍后再试。")
        
    if not images:
        await matcher.finish(f"未找到 {filter_type.upper()} 在 {time_str} 的相关营收数据")
        
    # 构造合并转发节点
    nodes = []
    info = f"统计区间：{time_str} 社团：{filter_type.upper()}"
    nodes.append({
        "type": "node",
        "data": {
            "name": "LiBot",
            "uin": bot.self_id,
            "content": info
        }
    })
    for img in images:
        nodes.append({
            "type": "node",
            "data": {
                "name": "Libot",
                "uin": bot.self_id,
                "content": MessageSegment.image(file=str(img))
            }
        })
    
    try:
        await bot.call_api("send_group_forward_msg", group_id=group_id, messages=nodes)
    except Exception as e:
        logger.error(f"发送群转发消息失败: {e}")


@live_list_add_cmd.handle()
async def handle_live_list_add(matcher: Matcher, event: Event, arg=CommandArg()):
    args = arg.extract_plain_text().strip().split()
    if not args:
        await matcher.finish("用法：/添加直播 <直播间号> [标签...]")
        
    room_id_str = args[0]
    if not room_id_str.isdigit():
        await matcher.finish("用法：/添加直播 <直播间号> [标签...]\n房间号必须为数字。")
    room_id = int(room_id_str)
    
    # 标签去重，保持唯一性
    tags = list(set(t.lower() for t in args[1:]))
    user_id = int(event.get_user_id())
        
    try:
        resp = await _api_get_room_info(room_id)
        if not resp.get("ok") or not resp.get("body", {}).get("data"):
            raise RuntimeError("网络请求失败")
            
        data = resp["body"]["data"]
        real_room_id = int(data["room_id"])
        uid = int(data["uid"])
    except Exception as e:
        logger.error(f"添加开播列表解析失败: {e}")
        await matcher.finish("网络请求失败，请稍后再试")
        
    name = _db_get_name_by_uid(uid)
    if not name:
        try:
            m_resp = await _api_get_master_info(uid)
            if m_resp.get("ok"):
                name = m_resp["body"]["data"]["info"]["uname"]
        except Exception:
            pass
            
    name_str = name or str(real_room_id)
    added = add_live_list(real_room_id, name_str, user_id, tags)
    
    if not added:
        await matcher.finish("该直播间已在列表中")
        
    tag_info = f" 标签: {' '.join(tags)}" if tags else ""
    msg = f"已添加到开播列表：{name_str}{tag_info}"
    if real_room_id != room_id:
        msg += f"\n(自动转为长号：{real_room_id})"
        
    await matcher.finish(msg)


async def _resolve_info_from_args(args: list[str]) -> dict | None:
    if not args[0].isdigit():
        return None
    room_id = int(args[0])
    info = get_live_list_info(room_id)
    if not info:
        try:
            resp = await _api_get_room_info(room_id)
            if resp.get("ok") and "data" in resp.get("body", {}):
                real_room_id = int(resp["body"]["data"]["room_id"])
                info = get_live_list_info(real_room_id)
        except Exception:
            pass
    return info


def _check_modify_permission(info: dict, event: Event) -> bool:
    user_id = int(event.get_user_id())
    group_id = get_group_id(event)
    is_mgr = False
    if group_id is not None:
        if INITIAL_MANAGER_QQ is not None:
            ensure_initial_manager(group_id, INITIAL_MANAGER_QQ)
        is_mgr = is_manager(group_id, user_id)
    return is_mgr or user_id == info["adder_uid"]


@live_list_add_tag_cmd.handle()
async def handle_live_list_add_tag(matcher: Matcher, event: Event, arg=CommandArg()):
    args = arg.extract_plain_text().strip().split()
    if len(args) < 2:
        await matcher.finish("用法：/增加标签 <直播间号> <标签...>")
        
    info = await _resolve_info_from_args(args)
    if not info:
        await matcher.finish("该直播间不在列表中")
        
    if not _check_modify_permission(info, event):
        await matcher.finish("权限不足：仅群管理员或该直播间的添加者可修改")
        
    new_tags = list(set(t.lower() for t in args[1:]))
    updated_tags = list(set(info["tags"] + new_tags))
    update_live_list_tags(info["room_id"], updated_tags)
    await matcher.finish(f"已增加标签，当前标签：{' '.join(updated_tags) if updated_tags else '无'}")


@live_list_set_tag_cmd.handle()
async def handle_live_list_set_tag(matcher: Matcher, event: Event, arg=CommandArg()):
    args = arg.extract_plain_text().strip().split()
    if len(args) < 2:
        await matcher.finish("用法：/修改标签 <直播间号> <标签...>")
        
    info = await _resolve_info_from_args(args)
    if not info:
        await matcher.finish("该直播间不在列表中")
        
    if not _check_modify_permission(info, event):
        await matcher.finish("权限不足：仅群管理员或该直播间的添加者可修改")
        
    new_tags = list(set(t.lower() for t in args[1:]))
    update_live_list_tags(info["room_id"], new_tags)
    await matcher.finish(f"已修改标签，当前标签：{' '.join(new_tags)}")


@live_list_remove_cmd.handle()
async def handle_live_list_remove(matcher: Matcher, event: Event, arg=CommandArg()):
    args = arg.extract_plain_text().strip().split()
    if not args:
        await matcher.finish("用法：/删除直播 <直播间号>")
        
    info = await _resolve_info_from_args(args)
    if not info:
        await matcher.finish("该直播间不在列表中")
        
    if not _check_modify_permission(info, event):
        await matcher.finish("权限不足：仅群管理员或该直播间的添加者可删除")

    removed = remove_live_list(info["room_id"])
    if removed:
        await matcher.finish(f"已从开播列表中删除直播间：{info['room_id']}")
    else:
        await matcher.finish("系统错误，未能删除")


@live_list_show_cmd.handle()
async def handle_live_list_show(matcher: Matcher, event: Event, arg=CommandArg()):
    args = arg.extract_plain_text().strip().split()
    filter_tag = args[0] if args else None
    
    rooms_info = get_live_list()
    if not rooms_info:
        await matcher.finish("目前尚未添加任何直播间，请使用 /添加直播 <直播间号> 增加主播哦。")
        
    # 如果指定了标签，则进行本地过滤
    if filter_tag:
        filter_tag_lower = filter_tag.lower()
        rooms_info = [
            r for r in rooms_info 
            if filter_tag_lower in [t.lower() for t in r["tags"]]
        ]
        if not rooms_info:
            await matcher.finish(f"当前列表中未找到包含标签 [{filter_tag}] 的直播间。")
        
    try:
        image_path = await render_live_list_image(rooms_info, filter_tag)
    except Exception as e:
        logger.error(f"渲染开播列表失败: {e}")
        await matcher.finish("查询或渲染失败，请稍后再试。")
        
    if not image_path:
        # 当数据全被 API 给过滤掉(都处于未开播状态)的情况
        msg = f"当前 {'标签 ['+filter_tag+'] 内的' if filter_tag else ''}列表未发现正在直播的主播！"
        await matcher.finish(msg)
        
    await matcher.finish(MessageSegment.image(file=str(image_path)))

@live_sessions_cmd.handle()
async def handle_live_sessions(matcher: Matcher, event: Event, arg=CommandArg()):
    group_id = get_group_id(event)
    if group_id is None:
        await matcher.finish("请在群聊中使用该命令")

    room_id = get_subscription(group_id)
    if room_id is None:
        await matcher.finish("请先设置订阅")
        
    arg_str = arg.extract_plain_text().strip()
    
    if not arg_str:
        month_str = datetime.now().strftime("%Y%m")
    else:
        if re.match(r"^20\d{2}-(0[1-9]|1[0-2])$", arg_str):
            month_str = arg_str.replace("-", "")
        elif re.match(r"^20\d{2}(0[1-9]|1[0-2])$", arg_str):
            month_str = arg_str
        elif re.match(r"^(0[1-9]|1[0-2])$", arg_str):
            month_str = f"{datetime.now().year}{arg_str}"
        else:
            await matcher.finish("月份格式错误，请使用如 202509 或 2025-09 的格式")
            
    try:
        image_path = await render_live_sessions_image(room_id, month_str)
    except Exception as e:
        logger.error(f"渲染直播记录图片失败: {e}")
        await matcher.finish("数据获取或图片渲染失败，请稍后再试。")
        
    if not image_path:
        await matcher.finish(f"未找到房间 {room_id} 在 {month_str} 的直播记录")
        
    await matcher.finish(MessageSegment.image(file=str(image_path)))

@cmd_add_song.handle()
@group_manager_required
async def handle_add_song(event: Event, arg: Message = CommandArg()):
    raw_text = arg.extract_plain_text().strip()
    if not raw_text:
        await cmd_add_song.finish(
            "请提供歌曲信息，参数使用 | 隔开，格式如下：\n"
            "/新增歌曲 歌名 | 歌手 | 语言 | 翻译名(可选)"
        )
        
    parts = [p.strip() for p in re.split(r'[|｜]', raw_text)]
    title = parts[0]
    
    if not title:
        await cmd_add_song.finish("新增失败：歌名不能为空！")
        
    singer = parts[1] if len(parts) > 1 else ""
    language = parts[2] if len(parts) > 2 else ""
    title_trans = parts[3] if len(parts) > 3 else ""
    
    try:
        new_id = add_new_song(title, singer, language, title_trans)
        await cmd_add_song.send(f"已成功添加新歌：{title}" )
        asyncio.create_task(sync_and_clean_lyrics())
    except Exception as e:
        await cmd_add_song.finish(f"添加新歌失败：{e}")

@cmd_generate_list.handle()
async def handle_generate_list(event: GroupMessageEvent, arg: Message = CommandArg()):
    raw_text = arg.extract_plain_text().strip()
    
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    if not lines:
        await cmd_generate_list.finish(
            "请提供歌单文本，每行一首。\n"
            "例如：\n/生成歌单 2025-03-01\n南风过隙\nLove 2000"
        )
        
    record_date = datetime.now().strftime('%Y-%m-%d')
    first_line = lines[0]
    is_date_line = False
    
    # 智能解析首行的日期
    if first_line in ["今天", "今日"]:
        record_date = datetime.now().strftime('%Y-%m-%d')
        is_date_line = True
    elif first_line in ["昨天", "昨日"]:
        record_date = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
        is_date_line = True
    elif first_line in ["前天"]:
        record_date = (datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d')
        is_date_line = True
    else:
        # 正则匹配形如 2026-07-15, 2026/07/15, 07-15, 07/15, 7.15 等格式
        date_match = re.match(r"^(?:(20\d{2})[-/.])?(1[0-2]|0?[1-9])[-/.]([12]\d|3[01]|0?[1-9])$", first_line)
        if date_match:
            year_str = date_match.group(1)
            month = int(date_match.group(2))
            day = int(date_match.group(3))
            
            if year_str:
                year = int(year_str)
            else:
                year = datetime.now().year
                # 跨年处理：如果当前是1月，输入的月份是12月，自动推断为去年
                if datetime.now().month == 1 and month == 12:
                    year -= 1
                    
            record_date = f"{year}-{month:02d}-{day:02d}"
            is_date_line = True
            
    # 如果第一行是日期，将其从歌单中移除
    if is_date_line:
        lines.pop(0)
        
    if not lines:
        await cmd_generate_list.finish(
            "请提供歌单文本，每行一首。\n"
            "例如：\n/生成歌单 2025-03-01\n南风过隙\nLove 2000"
        )
         
    # 继续生成草稿
    result_lines = [f"[{record_date}]"]
    
    for line in lines:
        clean_line = re.sub(r"^\d+[\.、]\s*", "", line)
        
        # 兼容中英文竖杠，分割出歌名和可选的歌手
        parts = [p.strip() for p in re.split(r'[|｜]', clean_line)]
        search_title = parts[0]
        search_singer = parts[1] if len(parts) > 1 else None
        
        # 携带可选的歌手参数进行精确防碰撞搜索
        search_res = search_songs_by_title(search_title, singer=search_singer, limit=1)
        if search_res:
            song = search_res[0]
            result_lines.append(f"{song['id']} # {song['title']} - {song['original_singer']}")
        else:
            result_lines.append(f"? # {line} (未找到歌曲)")
            
    reply_msg = "\n".join(result_lines)
    
    await cmd_generate_list.finish(reply_msg)

@cmd_update_list.handle()
async def handle_update_list(event: GroupMessageEvent, arg: Message = CommandArg()):
    text = arg.extract_plain_text().strip()
    
    if event.reply:
        reply_text = event.reply.message.extract_plain_text().strip()
        text = reply_text + "\n" + text
        
    if not text:
        await cmd_update_list.finish("请回复或提供歌单文本。")

    lines = [line.strip() for line in text.split("\n") if line.strip()]
    record_date = datetime.now().strftime('%Y-%m-%d')
    added_records = 0
    errors = []

    for line in lines:
        if line.startswith(("已生成歌单", "如需修改")):
            continue
            
        # 匹配日期，例如：[2026-07-15]
        date_match = re.match(r"^\[(\d{4}-\d{2}-\d{2})\]", line)
        if date_match:
            record_date = date_match.group(1)
            continue
            
        # 只处理已有 ID 的记录
        id_match = re.match(r"^(\d+)\s*#", line)
        if id_match:
            song_id = int(id_match.group(1))
            try:
                add_song_record(song_id, record_date)
                added_records += 1
            except Exception as e:
                errors.append(f"添加记录失败 - 歌曲ID {song_id} ({e})")
            continue

    if errors:
        msg = "更新歌单记录时出现错误。"
        logger.error("更新歌单记录时出现错误: %s", "; ".join(errors))
    else:
        msg = "更新成功！"        
        
    await cmd_update_list.finish(msg)