from __future__ import annotations

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Event
from nonebot.matcher import Matcher
from nonebot.params import CommandArg

from src.common.manager import (
    get_group_id,
    group_manager_required,
    add_manager,
    count_managers,
    init_manager_db,
    is_manager,
    list_managers,
    remove_manager,
)
from src.common.util import parse_user_id

try:
    init_manager_db()
except Exception:
    pass


help_cmd = on_command("帮助", priority=5)
manager_help_cmd = on_command("管理员帮助", priority=5, block=True)
manager_list_cmd = on_command("查看管理员", aliases={"管理员列表"}, priority=5, block=True)
manager_add_cmd = on_command("添加管理员", priority=5, block=True)
manager_remove_cmd = on_command("删除管理员", priority=5, block=True)


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