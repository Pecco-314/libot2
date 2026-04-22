from __future__ import annotations

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Event
from nonebot.matcher import Matcher
from nonebot.params import CommandArg

from src.common.nb import get_group_id, parse_user_id
from src.db.manager import (
    group_manager_required,
    add_manager,
    count_managers,
    init_manager_db,
    is_manager,
    list_managers,
    remove_manager,
)
from src.db.subscription import (
    get_subscription,
    init_subscription_db,
    list_subscribed_room_ids,
    remove_subscription,
    set_subscription,
)
from src.spider.wrapper import get_room_info

try:
    init_manager_db()
except Exception:
    pass

try:
    init_subscription_db()
except Exception:
    pass


help_cmd = on_command("帮助", priority=5)
manager_help_cmd = on_command("管理员帮助", priority=5, block=True)
manager_list_cmd = on_command("查看管理员", aliases={"管理员列表"}, priority=5, block=True)
manager_add_cmd = on_command("添加管理员", priority=5, block=True)
manager_remove_cmd = on_command("删除管理员", priority=5, block=True)
sub_show_cmd = on_command("查看订阅", priority=5, block=True)
sub_set_cmd = on_command("设置订阅", aliases={"订阅直播"}, priority=5, block=True)
sub_remove_cmd = on_command("删除订阅", aliases={"取消订阅"}, priority=5, block=True)


@help_cmd.handle()
async def handle_help():
    await help_cmd.finish(
        "/帮助 - 显示帮助信息\n"
    )


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


async def _format_room_name(room_id: int) -> str:
    try:
        room_info = await get_room_info(room_id)
    except Exception:
        return f"房间号 {room_id}"

    uname = str(room_info.get("uname") or "")
    return uname if uname else f"房间号 {room_id}"


@sub_show_cmd.handle()
@group_manager_required
async def handle_show_subscription(matcher: Matcher, event: Event):
    group_id = get_group_id(event)
    if group_id is None:
        await matcher.finish("请在群聊中使用该命令")

    room_id = get_subscription(group_id)
    if room_id is None:
        await matcher.finish("本群尚未设置订阅")

    await matcher.finish(f"当前订阅：{await _format_room_name(room_id)}")


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
    await matcher.finish(f"订阅已设置：{await _format_room_name(room_id)}")


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