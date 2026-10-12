import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
from nonebot import on_command, on_message, on_notice
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, GroupRecallNoticeEvent, PrivateMessageEvent, Message, MessageSegment
from nonebot.adapters.onebot.v11.permission import PRIVATE
from nonebot.params import CommandArg
from nonebot_plugin_apscheduler import scheduler

from src.common.utils import ROOT
from src.db import recall as db
from .utils import send_forward_message

logger = logging.getLogger(__name__)
ASSETS = ROOT / 'data' / 'images' / 'recall'
ASSETS.mkdir(parents=True,exist_ok=True)
db.init_db()
capture = on_message(priority=1,block=False)
recall_notice = on_notice(priority=1,block=False)
query = on_command('查撤回',permission=PRIVATE,priority=5,block=True,force_whitespace=True)
downloads = asyncio.Semaphore(4)
tasks = set()


async def download(url, filename, sent_at):
    host = (urlparse(url).hostname or '').lower()
    if urlparse(url).scheme != 'https' or not (host.endswith('.qq.com') or host.endswith('.qpic.cn')):
        return
    path = ASSETS / filename
    try:
        async with downloads:
            if time.time()-sent_at >= db.TTL:
                return
            async with httpx.AsyncClient(timeout=12, trust_env=False) as client:
                async with client.stream('GET',url) as response:
                    response.raise_for_status()
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content)>20*1024*1024:
                            raise ValueError('image exceeds 20 MiB')
            if time.time()-sent_at < db.TTL:
                await asyncio.to_thread(path.write_bytes,content)
    except Exception as exc:
        logger.warning('撤回图片缓存失败 file=%s error=%s',filename,type(exc).__name__)


@capture.handle()
async def cache_message(event: GroupMessageEvent):
    sent_at = int(event.time)
    if time.time()-sent_at >= db.TTL:
        return
    segments, images = [], []
    for index, segment in enumerate(event.message):
        if segment.type == 'image':
            filename = f'{sent_at}_{event.group_id}_{event.message_id}_{index}.img'
            segments.append({'type':'image','file':filename})
            images.append((segment.data.get('url') or segment.data.get('file') or '',filename))
        elif segment.type == 'text':
            segments.append({'type':'text','text':str(segment.data.get('text',''))})
        elif segment.type == 'at':
            segments.append({'type':'text','text':f"@{segment.data.get('qq','')}"})
        else:
            segments.append({'type':'text','text':f'[{segment.type}]'})
    await asyncio.to_thread(db.save_message,event.group_id,event.message_id,event.user_id,sent_at,
                            event.sender.card or event.sender.nickname or str(event.user_id),segments)
    for url,filename in images:
        if len(tasks) >= 200:
            logger.warning('撤回图片下载队列已满 file=%s',filename)
            continue
        task = asyncio.create_task(download(url,filename,sent_at))
        tasks.add(task)
        task.add_done_callback(tasks.discard)


@recall_notice.handle()
async def record_recall(event: GroupRecallNoticeEvent):
    await asyncio.to_thread(db.mark_recalled,event.group_id,event.message_id,event.user_id,
                            int(event.time),event.operator_id)


def cleanup():
    now = int(time.time())
    db.expire(now)
    for path in ASSETS.glob('*.img'):
        if int(path.name.split('_',1)[0]) < now-db.TTL:
            path.unlink(missing_ok=True)


@scheduler.scheduled_job('interval',minutes=10,id='recall_cache_cleanup',max_instances=1)
async def cleanup_cache():
    await asyncio.to_thread(cleanup)


@query.handle()
async def show_recalled(bot: Bot,event: PrivateMessageEvent,arg=CommandArg()):
    text = arg.extract_plain_text().strip()
    if not text.isdigit() or len(text)>12:
        await query.finish('用法：查撤回 QQ号')
    allowed = {}
    for group in await bot.get_group_list():
        gid = int(group['group_id'])
        try:
            member = await asyncio.wait_for(bot.get_group_member_info(group_id=gid,user_id=event.user_id,no_cache=True),5)
            if int(member.get('user_id',0)) == event.user_id:
                allowed[gid] = group.get('group_name') or str(gid)
        except Exception:
            continue
    rows = await asyncio.to_thread(db.list_recalled,int(text),list(allowed),int(time.time()))
    if not rows:
        await query.finish('在你当前所在的群中，没有查到该用户24小时内的撤回记录（仅记录功能启用后的消息）')
    nodes = []
    for row in rows:
        when = datetime.fromtimestamp(row['recalled_at']).strftime('%m-%d %H:%M:%S')
        content = Message(MessageSegment.text(f"{row['nickname'] or row['user_id']}（{row['user_id']}）\n"
            f"{allowed[row['group_id']]}（{row['group_id']}）\n{when} 撤回\n"))
        for segment in json.loads(row['segments'] or '[]'):
            if segment['type']=='image':
                path = ASSETS / segment['file']
                content += MessageSegment.image(file=str(path)) if path.is_file() else MessageSegment.text('[图片未缓存成功]')
            else:
                content += MessageSegment.text(segment['text'])
        if row['segments'] is None:
            content += MessageSegment.text('[未缓存到原消息]')
        nodes.append({'type':'node','data':{'name':'Libot','uin':bot.self_id,'content':content}})
    for start in range(0,len(nodes),50):
        await send_forward_message(bot,event,nodes[start:start+50])
