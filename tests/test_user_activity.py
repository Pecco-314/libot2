from __future__ import annotations

import ast
import asyncio
import logging
from pathlib import Path
import sqlite3
import tempfile
import time
from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import httpx

from src.common.event_text import format_event_text
from src.common.push_features import OWN_ROOM_ACTIVITY, OTHER_ROOM_ACTIVITY, room_activity_feature
from src.db.command_config import init_command_config_db, set_command_enabled, get_command_config
from src.db.user_activity import (finish_delivery, get_poll_state, init_user_activity_db,
                                  list_pending_deliveries, store_user_activity, skip_delivery)
from src.spider.danmakus_user import MIT3URI_UID as UID, MIT3URI_ROOM as ROOM, fetch_user_events, normalize_event
from src.spider.jobs.user_activity import collect_user_activity, register_jobs
from src.spider.jobs import user_activity as job

NOW = 1_790_000_000


def raw(kind=0, text="晚安", timestamp=NOW, **extra):
    return {"uId": UID, "uName": "三理Mit3uri", "type": kind, "sendDate": timestamp * 1000,
            "message": text, "uploadAccountId": 1, **extra}


def normalized(kind=0, room=100, timestamp=NOW, text="晚安", **extra):
    return normalize_event(raw(kind, text, timestamp, **extra), {"roomId": room, "uName": "测试主播"}, {"liveId": "live"}, UID)


class UserActivityDBTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "test.db"
        with sqlite3.connect(self.db) as conn:
            conn.executescript(f"""CREATE TABLE subscription(group_id INTEGER PRIMARY KEY,room_id INTEGER);
                INSERT INTO subscription VALUES (1,{ROOM}),(2,{ROOM}),(3,100);""")
        init_user_activity_db(self.db)

    def save(self, events, now=NOW, full=False):
        return store_user_activity(events, UID, ROOM, now, full=full, db_path=self.db)

    def test_baseline_and_restart_dedup(self):
        self.assertTrue(self.save([normalized()])["baseline"])
        self.assertEqual(list_pending_deliveries(NOW, db_path=self.db), [])
        e = normalized(timestamp=NOW + 5)
        self.assertEqual(self.save([e], NOW+10)["queued"], 2)
        init_user_activity_db(self.db)
        self.assertEqual(self.save([e], NOW+11)["inserted"], 0)
        self.assertEqual([r["group_id"] for r in list_pending_deliveries(NOW+12, db_path=self.db)], [1, 2])

    def test_enter_own_danmaku_unknown_and_old_are_silent(self):
        self.save([], full=True)
        records = [normalized(4), normalized(room=ROOM), normalized(99), normalized(timestamp=NOW-10)]
        self.assertEqual(self.save(records)["queued"], 0)
        with sqlite3.connect(self.db) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM user_activity").fetchone()[0], 4)

    def test_each_recipient_retries_independently_and_unsubscribe(self):
        self.save([], full=True)
        self.save([normalized(2, text="舰长", count=1, price=138)])
        rows = list_pending_deliveries(NOW, db_path=self.db)
        finish_delivery(rows[0]["id"], NOW, db_path=self.db)
        finish_delivery(rows[1]["id"], NOW, "offline", self.db)
        self.assertEqual(list_pending_deliveries(NOW+1, db_path=self.db), [])
        self.assertEqual(len(list_pending_deliveries(NOW+31, db_path=self.db)), 1)
        with sqlite3.connect(self.db) as c:
            c.execute("UPDATE subscription SET room_id=100 WHERE group_id=2")
        self.assertEqual(list_pending_deliveries(NOW+32, db_path=self.db), [])

    def test_failed_transaction_does_not_advance_cursor(self):
        self.save([], full=True)
        wrong = normalized(); wrong["uid"] = 9
        with self.assertRaises(ValueError): self.save([normalized(), wrong], NOW+1)
        self.assertEqual(get_poll_state(UID, self.db)["last_poll"], NOW)
        self.assertEqual(list_pending_deliveries(NOW+1, db_path=self.db), [])

    def test_late_new_event_in_old_session_is_not_discarded(self):
        self.save([], full=True)
        self.save([normalized(timestamp=NOW+50)], NOW+100)
        self.assertEqual(self.save([normalized(timestamp=NOW+20)], NOW+200)["queued"], 2)
        self.assertEqual(self.save([normalized(timestamp=NOW+201)], NOW+90000)["queued"], 0)

    def test_unchanged_fast_poll_is_read_only(self):
        event = normalized()
        self.save([event])
        with patch('src.db.user_activity.write_transaction', side_effect=AssertionError('unexpected write')):
            self.assertEqual(self.save([event], NOW+10)['inserted'], 0)
        self.save([event], NOW+60)
        self.assertEqual(get_poll_state(UID, self.db)['last_poll'], NOW+60)
        self.assertEqual(self.save([normalized(timestamp=NOW+61)], NOW+61)['queued'], 2)

    def test_slow_full_sweep_cannot_move_poll_time_backwards(self):
        self.save([])
        self.save([normalized(timestamp=NOW+100)], NOW+100)
        self.save([], NOW+20, full=True)
        self.assertEqual(get_poll_state(UID, self.db)['last_poll'], NOW+100)

    def test_group_switch_covers_all_interactions_and_keeps_records(self):
        init_command_config_db(self.db)
        self.save([])
        set_command_enabled(1, OTHER_ROOM_ACTIVITY, False, 0, self.db)
        events = [normalized(kind, timestamp=NOW+kind+1) for kind in range(4)]
        self.assertEqual(self.save(events, NOW+10)["queued"], 4)
        rows = list_pending_deliveries(NOW+10, db_path=self.db)
        self.assertEqual({r["group_id"] for r in rows}, {2})
        self.assertEqual({r["event"]["cmd"] for r in rows},
                         {"DANMU_MSG", "SEND_GIFT", "GUARD_BUY", "SUPER_CHAT_MESSAGE"})
        with sqlite3.connect(self.db) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM user_activity").fetchone()[0], 4)
        set_command_enabled(1, OTHER_ROOM_ACTIVITY, True, 0, self.db)
        self.assertEqual(self.save(events, NOW+11)["queued"], 0)
        self.assertEqual(self.save([normalized(timestamp=NOW+12)], NOW+12)["queued"], 2)

    def test_own_room_switch_is_independent(self):
        init_command_config_db(self.db)
        self.save([])
        set_command_enabled(1, OWN_ROOM_ACTIVITY, False, 0, self.db)
        self.assertTrue(get_command_config(1, OTHER_ROOM_ACTIVITY, self.db).enabled)
        self.assertEqual(self.save([normalized(1, room=ROOM)], NOW+1)["queued"], 1)
        self.assertEqual(self.save([normalized(1, timestamp=NOW+2)], NOW+2)["queued"], 2)

    def test_skipped_delivery_never_replays(self):
        self.save([])
        self.save([normalized()], NOW+1)
        rows = list_pending_deliveries(NOW+1, db_path=self.db)
        skip_delivery(rows[0]["id"], NOW+2, self.db)
        self.assertEqual([r["id"] for r in list_pending_deliveries(NOW+3, db_path=self.db)], [rows[1]["id"]])


class UserActivityParserTests(unittest.TestCase):
    def test_fast_schedule_and_deep_sweeps(self):
        from unittest.mock import Mock
        scheduler = Mock()
        register_jobs(scheduler)
        regular, recent, full = scheduler.add_job.call_args_list
        self.assertEqual(regular.kwargs['seconds'], 10)
        self.assertEqual(regular.kwargs['max_instances'], 1)
        self.assertEqual(recent.kwargs['minutes'], 15)
        self.assertEqual(recent.kwargs['kwargs'], {'deep': True})
        self.assertEqual(full.kwargs['kwargs'], {'full': True})
        self.assertEqual(str(full.args[1].timezone), 'Asia/Shanghai')

    def test_ids_ignore_collector_and_merged_session_metadata(self):
        a = normalized()
        b = normalize_event(raw(uploadAccountId=2), {"roomId":100,"uName":"new"}, {"liveId":"merged"}, UID)
        self.assertEqual(a["event_key"], b["event_key"])
        self.assertNotEqual(a["event_key"], normalized(timestamp=NOW+1)["event_key"])

    def test_types_and_shared_text(self):
        gift = normalized(1, text="小花花", price=.1, count=3)
        self.assertEqual(format_event_text(gift)[0], "送出了小花花*3")
        guard = normalized(2, text="舰长", price=138)
        self.assertEqual(format_event_text(guard)[0], "开通了舰长")
        sc = normalized(3, text="生日快乐", price=30)
        self.assertEqual(format_event_text(sc)[0], "醒目留言（￥30）：生日快乐")
        with self.assertRaises(ValueError): normalize_event(raw(uId=1), {"roomId":100}, {}, UID)


def record(start, events, live_id="live"):
    return {"channel":{"roomId":100,"uName":"测试主播"},
            "live":{"liveId":live_id,"startDate":start * 1000}, "danmakus":events}


class UserActivityFetchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        p = patch('src.spider.danmakus_user._request_lock', asyncio.Lock())
        p.start()
        self.addCleanup(p.stop)

    async def fetch(self, pages, **kwargs):
        def handler(request):
            page = int(request.url.params["pageNum"])
            value = pages[page]
            if isinstance(value, int): return httpx.Response(value)
            return httpx.Response(200, json={"code":200,"data":{"pageNum":page,"pageSize":50,
                "hasMore":page+1 < len(pages),"data":{"records":value}}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch_user_events(client=client, delay=0, **kwargs)

    async def test_paging_and_duplicate_uploads(self):
        events, pages = await self.fetch([[record(NOW,[raw(),raw(uploadAccountId=2)])],
                                         [record(NOW-5,[raw(timestamp=NOW+1)],"older")]])
        self.assertEqual(len(events), 2)
        self.assertEqual(pages, 2)

    async def test_http_failure_or_redirect_has_no_partial_result(self):
        for status in [503, 302]:
            with self.assertRaises(httpx.HTTPStatusError):
                await self.fetch([[record(NOW,[raw()])],status])

    async def test_scan_includes_new_events_in_old_sessions(self):
        events, pages = await self.fetch([[record(NOW-100,[raw()])], [record(NOW-200,[raw(timestamp=NOW+1)],"old")],
                                         [record(NOW-300,[raw(timestamp=NOW+2)],"oldest")]], since_ms=(NOW-150)*1000)
        self.assertEqual(pages, 2)
        self.assertEqual(len(events), 2)

    async def test_safety_limit_and_repeated_page(self):
        page = [record(NOW,[raw()])]
        with self.assertRaisesRegex(ValueError,"上限"): await self.fetch([page, page], max_pages=1)
        with self.assertRaisesRegex(ValueError,"重复"): await self.fetch([page, page])

    async def test_no_state_update_on_network_error(self):
        with patch('src.spider.jobs.user_activity.init_user_activity_db'), \
             patch('src.spider.jobs.user_activity.get_poll_state', return_value=None), \
             patch('src.spider.jobs.user_activity.fetch_user_events', side_effect=ValueError('failed')), \
             patch('src.spider.jobs.user_activity.store_user_activity') as save:
            with self.assertRaises(ValueError): await collect_user_activity(now=NOW)
            save.assert_not_called()

    async def test_first_page_overlap_stops_but_keeps_new_messages(self):
        seen = {normalized()['event_key']}
        async def stop(events): return any(e['event_key'] in seen for e in events)
        events, pages = await self.fetch([[record(NOW,[raw(),raw(timestamp=NOW+1)])], 503], stop_after_page=stop)
        self.assertEqual(pages, 1)
        self.assertEqual(len(events), 2)

    async def test_burst_without_overlap_fetches_next_page(self):
        seen = {normalized()['event_key']}
        async def stop(events): return any(e['event_key'] in seen for e in events)
        events, pages = await self.fetch([[record(NOW+10,[raw(timestamp=NOW+10)],'new')],
                                         [record(NOW,[raw()])],503], stop_after_page=stop)
        self.assertEqual(pages, 2)
        self.assertEqual(len(events), 2)

    async def test_deep_and_fast_requests_are_serialized(self):
        active = peak = 0
        async def handler(request):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(.01)
            active -= 1
            return httpx.Response(200,json={'code':200,'data':{'pageNum':0,'pageSize':50,'hasMore':False,
                'data':{'records':[record(NOW,[raw()])]}}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await asyncio.gather(fetch_user_events(client=client),fetch_user_events(client=client))
        self.assertEqual(peak, 1)

    async def test_collector_passes_persistent_overlap_callback(self):
        async def fetch(**kwargs):
            self.assertIsNotNone(kwargs['stop_after_page'])
            self.assertTrue(await kwargs['stop_after_page']([normalized()]))
            return [normalized()], 1
        with patch.object(job,'init_user_activity_db'), patch.object(job,'get_poll_state',return_value={'last_poll':NOW}), \
             patch.object(job,'known_event_keys',return_value={normalized()['event_key']}) as known, \
             patch.object(job,'fetch_user_events',side_effect=fetch), \
             patch.object(job,'store_user_activity',return_value={'inserted':0,'queued':0,'baseline':False}):
            await collect_user_activity(now=NOW+10)
            known.assert_called_once()

    async def test_rate_limit_backoff_respects_retry_after(self):
        response = httpx.Response(429,headers={'Retry-After':'90'},request=httpx.Request('GET','https://test.invalid'))
        failure = httpx.HTTPStatusError('rate limited', request=response.request,response=response)
        with patch.object(job,'_retry_at',0.0),patch.object(job,'_failures',0), \
             patch.object(job.time,'monotonic',return_value=100) as clock, \
             patch.object(job,'collect_user_activity',side_effect=failure) as collect:
            await job.poll_user_activity()
            self.assertEqual(job._retry_at,190)
            clock.return_value=110
            await job.poll_user_activity()
            self.assertEqual(collect.await_count,1)
            clock.return_value=191
            collect.side_effect=None
            await job.poll_user_activity()
            self.assertEqual(collect.await_count,2)
            self.assertEqual(job._retry_at,0)


class UserActivityDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_delivery_preserves_failed_recipient(self):
        # 加载真实投递函数，替换装饰器与线上依赖，不初始化 bot 或生产数据库。
        tree = ast.parse(Path('src/plugins/user_activity.py').read_text())
        funcs = [n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))]
        for n in funcs: n.decorator_list=[]
        bot = SimpleNamespace(send_group_msg=AsyncMock(side_effect=[None, RuntimeError('failed')]))
        rows=[{"id":1,"group_id":1,"event":normalized()}, {"id":2,"group_id":2,"event":normalized()}]
        completed=[]
        env={"asyncio":asyncio,"time":time,"datetime":datetime,"ZoneInfo":ZoneInfo,
             "room_activity_feature":room_activity_feature,"is_push_enabled":lambda *args:True,
             "format_event_text":format_event_text,"get_bots":lambda:{1:bot},
             "list_pending_deliveries":lambda now:rows,"finish_delivery":lambda *args:completed.append(args),
             "MessageSegment":SimpleNamespace(text=lambda s:s),"logger":logging.getLogger('test')}
        for row in rows: row["subscription_room"] = ROOM
        exec(compile(ast.Module(body=funcs,type_ignores=[]),'user_activity.py','exec'),env)
        await env['deliver_user_activity']()
        self.assertEqual(len(completed[0]),2)
        self.assertEqual(completed[1][2], 'failed')
        text=bot.send_group_msg.await_args_list[0].kwargs['message']
        self.assertIn('三理Mit3uri在测试主播的直播间发送了弹幕：晚安',text)

    async def test_delivery_rechecks_switch_for_every_event_type(self):
        tree = ast.parse(Path('src/plugins/user_activity.py').read_text())
        funcs = [n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))]
        for n in funcs: n.decorator_list=[]
        bot = SimpleNamespace(send_group_msg=AsyncMock())
        rows = [{"id":i+1,"group_id":1,"subscription_room":ROOM,"event":normalized(i)} for i in range(4)]
        completed, skipped = [], []
        env={"asyncio":asyncio,"time":time,"datetime":datetime,"ZoneInfo":ZoneInfo,
             "format_event_text":format_event_text,"get_bots":lambda:{1:bot},
             "room_activity_feature":room_activity_feature,"is_push_enabled":lambda *args:False,
             "list_pending_deliveries":lambda now:rows,"finish_delivery":lambda *args:completed.append(args),
             "skip_delivery":lambda *args:skipped.append(args),
             "MessageSegment":SimpleNamespace(text=lambda s:s),"logger":logging.getLogger('test')}
        exec(compile(ast.Module(body=funcs,type_ignores=[]),'user_activity.py','exec'),env)
        await env['deliver_user_activity']()
        bot.send_group_msg.assert_not_called()
        self.assertEqual([x[0] for x in skipped], [1,2,3,4])
        self.assertEqual(completed, [])
