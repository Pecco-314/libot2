import asyncio
import logging
from datetime import datetime
from pathlib import Path

import httpx
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, PrivateMessageEvent, Message, MessageSegment
from nonebot.adapters.onebot.v11.permission import PRIVATE
from nonebot.params import CommandArg

from src.common.utils import ROOT
from src.db.group_leave import init_group_leave_db, record_leave, enrich_leave, recent_leaves
from .utils import send_forward_message

logger = logging.getLogger(__name__)
init_group_leave_db()
leave_query = on_command('查退群', permission=PRIVATE, priority=5, block=True, force_whitespace=True)


async def cache_leave(bot, event):
    record_id = await asyncio.to_thread(record_leave, int(event.group_id), int(event.user_id),
                                        int(event.time), str(event.sub_type))
    nickname = str(event.user_id)
    avatar = None
    try:
        info = await asyncio.wait_for(bot.get_stranger_info(user_id=event.user_id), 8)
        nickname = info.get('nickname') or nickname
    except Exception:
        logger.warning('退群昵称获取失败 user_id=%s', event.user_id)
    try:
        path = ROOT / 'data' / 'images' / 'group_leave' / f'{record_id}.jpg'
        async with httpx.AsyncClient(timeout=8, trust_env=False) as client:
            response = await client.get(f'https://q1.qlogo.cn/g?b=qq&nk={int(event.user_id)}&s=640')
            response.raise_for_status()
            if not response.headers.get('content-type', '').startswith('image/'):
                raise ValueError('头像响应不是图片')
            path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(path.write_bytes, response.content)
        avatar = str(path)
    except Exception:
        logger.warning('退群头像缓存失败 user_id=%s', event.user_id)
    await asyncio.to_thread(enrich_leave, record_id, nickname, avatar)
    return nickname


@leave_query.handle()
async def query(bot: Bot, event: PrivateMessageEvent, arg=CommandArg()):
    parts = arg.extract_plain_text().split()
    if len(parts) > 2 or any(not part.isdigit() for part in parts):
        await leave_query.finish('用法：查退群 [群号] [数量，默认10，最多30]')
    target = int(parts[0]) if parts else None
    limit = int(parts[1]) if len(parts) == 2 else 10
    if not 1 <= limit <= 30:
        await leave_query.finish('数量应为1～30')
    groups = await bot.get_group_list()
    allowed = {}
    for group in groups:
        group_id = int(group['group_id'])
        if target is not None and group_id != target:
            continue
        try:
            member = await asyncio.wait_for(bot.get_group_member_info(
                group_id=group_id, user_id=event.user_id, no_cache=True), 5)
            if int(member.get('user_id', 0)) == event.user_id:
                allowed[group_id] = group.get('group_name') or str(group_id)
        except Exception:
            continue
    if not allowed:
        await leave_query.finish('未能确认你在可查询的群中，请检查群号或稍后再试')
    rows = await asyncio.to_thread(recent_leaves, list(allowed), limit)
    if not rows:
        await leave_query.finish('暂无退群记录，记录从本功能启用后开始积累')
    nodes = []
    for row in rows:
        kind = '被移出群' if row['subtype'] == 'kick' else '退群'
        when = datetime.fromtimestamp(row['timestamp']).strftime('%Y-%m-%d %H:%M:%S')
        content = Message(MessageSegment.text(
            f"{row['nickname'] or row['user_id']}（QQ：{row['user_id']}）\n"
            f"{allowed[row['group_id']]}（{row['group_id']}）\n{when} {kind}\n"))
        if row['avatar_path'] and Path(row['avatar_path']).is_file():
            content += MessageSegment.image(file=row['avatar_path'])
        else:
            content += MessageSegment.image(file=f"https://q1.qlogo.cn/g?b=qq&nk={row['user_id']}&s=640")
        nodes.append({'type':'node', 'data':{'name':'Libot','uin':bot.self_id,'content':content}})
    await send_forward_message(bot, event, nodes)
