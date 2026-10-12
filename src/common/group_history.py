"""从 NapCat 群历史补充24小时消息缓存；不回放命令、不发送群消息。"""

from __future__ import annotations

import asyncio
import time

from nonebot.adapters.onebot.v11 import Message, MessageSegment

from src.db import recall


def history_row(message, group_id, start, end):
    """历史图片仅保存占位，不下载图片或展开合并转发。"""
    try:
        if message.get('message_type') != 'group' or int(message.get('group_id', 0)) != group_id:
            return None
        sent_at = int(message['time'])
        uid = int(message['user_id'])
        mid = int(message['message_id'])
        if not start <= sent_at < end or uid <= 0:
            return None
        sender = message.get('sender') or {}
        segments = []
        raw = message.get('message') or []
        parsed = Message(raw) if isinstance(raw,str) else [
            MessageSegment(s['type'],s.get('data') or {}) for s in raw
        ]
        for segment in parsed:
            if segment.type == 'image':
                segments.append({'type': 'image', 'file': ''})
            elif segment.type == 'text':
                segments.append({'type': 'text', 'text': str(segment.data.get('text', ''))})
            elif segment.type == 'at':
                segments.append({'type': 'text', 'text': f"@{segment.data.get('qq', '')}"})
            else:
                segments.append({'type': 'text', 'text': f'[{segment.type}]'})
        if not segments:
            return None
        return dict(group_id=group_id,message_id=mid,user_id=uid,sent_at=sent_at,
                    nickname=str(sender.get('card') or sender.get('nickname') or uid),segments=segments)
    except (KeyError, TypeError, ValueError):
        return None


async def recover_group_history(call_api, group_id, since, *, cache_path=None, now=None,
                                max_pages=40, page_size=100, delay=0.5):
    """向前分页，按群号及消息ID去重；分页无进展时返回明确的未完成状态。"""
    now = int(time.time()) if now is None else int(now)
    since = max(int(since), now-recall.TTL)
    group_id = int(group_id)
    cursor = None
    visited = set()
    result = dict(group_id=group_id,inserted=0,fetched=0,pages=0,oldest=None,complete=False)
    for _ in range(max_pages):
        params = dict(group_id=str(group_id),count=page_size,reverse_order=True,
                      disable_get_url=True,parse_mult_msg=False)
        if cursor is not None:
            params['message_seq'] = str(cursor)
        data = await call_api('get_group_msg_history', **params)
        messages = data.get('messages') or []
        if not messages:
            result['complete'] = True
            break
        valid = [m for m in messages if history_row(m,group_id,0,2**63) is not None]
        if not valid:
            break
        oldest = min(valid,key=lambda m: (int(m['time']),int(m.get('real_seq') or m['message_id'])))
        result['pages'] += 1
        result['fetched'] += len(messages)
        result['oldest'] = int(oldest['time'])
        rows = [r for m in valid if (r := history_row(m,group_id,since,now+1)) is not None]
        result['inserted'] += await asyncio.to_thread(recall.save_history_messages, rows, cache_path)
        if int(oldest['time']) < since:
            result['complete'] = True
            break
        next_cursor = int(oldest['message_id'])
        if next_cursor in visited:
            break
        visited.add(next_cursor)
        cursor = next_cursor
        await asyncio.sleep(delay)
    return result
