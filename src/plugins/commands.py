from __future__ import annotations

import logging
from datetime import datetime

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, Event, MessageSegment, Message
from nonebot.matcher import Matcher
from nonebot.params import CommandArg

from src.render.superchat import get_daily_superchat_images
from src.render.stats import render_fans_trend, render_guards_trend, render_fan_club_trend
from src.render.song import render_songs_by_keyword, render_random_song
from src.spider.wrapper import get_name_by_roomid, get_name_by_uid
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
    set_subscription_dev,
    set_subscription,
    is_subscription_dev_enabled,
)
from src.db.liver import upsert_liver
from src.db.event import list_name_history_by_name_or_uid

from .utils import (
    get_group_id,
    parse_user_id,
    _parse_room_id,
    _format_name,
    group_manager_required,
)

logger = logging.getLogger("libot.commands")

help_cmd = on_command("帮助", priority=5)
superchat_cmd = on_command("查SC", aliases={"查sc", "查Sc"}, priority=5, block=True)
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
name_history_cmd = on_command("曾用名", aliases={"查曾用名"}, priority=5, block=True)
fans_trend_cmd = on_command("查粉丝", priority=5, block=True)
guards_trend_cmd = on_command("查舰长", aliases={"查大航海"}, priority=5, block=True)
club_trend_cmd = on_command("查粉丝团", priority=5, block=True)
song_search_cmd = on_command("查歌曲", priority=5, block=True)
random_search_cmd = on_command("随机歌曲", priority=5, block=True)


@help_cmd.handle()
async def handle_help(matcher: Matcher):
    await matcher.finish(
        "/帮助 - 显示帮助信息\n"
        "/查SC [日期] - 查看醒目留言列表，默认当天\n"
        "/曾用名 <UID/用户名> - 查询用户的曾用名\n"
        "/查粉丝 [天数] - 查询订阅主播粉丝数趋势，默认1天\n"
        "/查舰长 [天数] - 查询订阅主播大航海数趋势，默认1天\n"
        "/查粉丝团 [天数] - 查询订阅主播粉丝团人数趋势，默认1天\n"
        "/查歌曲 <歌名> - 查询歌曲的演唱记录\n"
        "/随机歌曲 [最少演唱次数] - 随机抽取一首演唱过的歌曲，可设置最少演唱次数，默认3次\n"
    )


@superchat_cmd.handle()
async def handle_superchat(matcher: Matcher, bot: Bot, event: Event, arg=CommandArg()):
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

    images = await get_daily_superchat_images(room_id, day, chunk_size=40)
    if not images:
        await matcher.finish("没有找到醒目留言")
    
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
    await matcher.finish(
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
        if not (1 <= days <= 7):
            await matcher.finish("查询天数请限制在 1 到 7 天以内")

    uname = await get_name_by_roomid(room_id) or str(room_id)

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

    message = Message([
        MessageSegment.text(f"{uname}的{stat_name}数：{now} ({delta:+})"),
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