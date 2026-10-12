import ast
import asyncio
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from src.common.danmaku_rank import TZ, parse_rank_period, build_danmaku_rank_items
from src.db.danmaku_rank import query_danmaku_rank


class PeriodTests(unittest.TestCase):
    now = datetime(2026, 10, 7, 12, 34, 56, tzinfo=TZ)

    def bounds(self, text):
        a, b, _ = parse_rank_period(text, self.now)
        return datetime.fromtimestamp(a, TZ), datetime.fromtimestamp(b, TZ)

    def test_session_compatibility(self):
        for text in ('', '2026-08-01', '2026-08-01 2', '2026-8-1 1'):
            self.assertIsNone(parse_rank_period(text, self.now))

    def test_year_and_month(self):
        a, b = self.bounds('2026')
        self.assertEqual(a, datetime(2026, 1, 1, tzinfo=TZ))
        self.assertEqual(int(b.timestamp()), int(self.now.timestamp()) + 1)
        a, b = self.bounds('2026-08')
        self.assertEqual((a.day, a.month, b.day, b.month), (1, 8, 1, 9))
        a, b = self.bounds('2025')
        self.assertEqual((a.year, b.year), (2025, 2026))

    def test_relative_periods(self):
        self.assertEqual(self.bounds('近1月'), self.bounds('近30天'))
        self.assertEqual(self.bounds('近1月')[0], datetime(2026, 9, 8, tzinfo=TZ))
        self.assertEqual(self.bounds('本月')[0].day, 1)
        self.assertEqual(self.bounds('今年'), self.bounds('2026'))
        self.assertEqual(self.bounds('上月'), self.bounds('2026-09'))

    def test_inclusive_end_and_leap_year(self):
        a, b = self.bounds('2024-02-01 2024-02-29')
        self.assertEqual((b-a).days, 29)
        self.assertEqual(b, datetime(2024, 3, 1, tzinfo=TZ))
        self.assertEqual(self.bounds('2026-08 2026-09')[1], datetime(2026, 10, 1, tzinfo=TZ))

    def test_invalid(self):
        for text in ('2026-13', '2027', '2026-09-01 2026-08-31', '近0月',
                     '2026-08 2', '2026-08 x', '2026-08 1 extra', '9999', '0000'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_rank_period(text, self.now)


class QueryTests(unittest.TestCase):
    def test_streamed_results_keep_existing_counting_and_boundaries(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'test.db'
            with sqlite3.connect(path) as c:
                c.executescript('CREATE TABLE event(id INTEGER PRIMARY KEY, room_id INTEGER, timestamp INTEGER, uid INTEGER, uname TEXT, content TEXT, cmd TEXT); CREATE INDEX idx_event_room_time ON event(room_id,timestamp);')
                rows = [(1, 1, 100, 1, '旧名', '打call三理三理三理', 'DANMU_MSG'),
                        (2, 1, 101, 1, '新名', '晚安理理理理理理', 'DANMU_MSG'),
                        (3, 1, 102, 1, '', None, 'SEND_GIFT'),
                        (4, 1, 100, 2, '乙', '看你', 'DANMU_MSG'),
                        (5, 1, 103, 2, '乙', '[UPOWER_2030198123_看你]', 'DANMU_MSG'),
                        (6, 1, 104, 3, '边界外', '晚安', 'DANMU_MSG'),
                        (7, 2, 101, 4, '其他房', '晚安', 'DANMU_MSG'),
                        (8, 1, 101, 0, '无UID', '晚安', 'DANMU_MSG'),
                        (9, 1, 99, 5, '之前', '晚安', 'DANMU_MSG')]
                c.executemany('INSERT INTO event VALUES (?,?,?,?,?,?,?)', rows)
            result = query_danmaku_rank(1, 100, 104, db_path=path)
            self.assertEqual(result, [
                {'rank': 1, 'uname': '新名', 'count': 3, 'dc': 2, 'gn': 1, 'ky': 0},
                {'rank': 2, 'uname': '乙', 'count': 2, 'dc': 0, 'gn': 0, 'ky': 2},
            ])
            self.assertEqual(query_danmaku_rank(1, 104, 104, db_path=path), [])
            self.assertEqual(len(query_danmaku_rank(1, 100, 104, 1, path)), 1)

    def test_generator_and_ties(self):
        rows = ({'uid': uid, 'uname': name, 'content': ''} for uid, name in [(1, 'B'), (2, 'A')])
        self.assertEqual([r['uname'] for r in build_danmaku_rank_items(rows)], ['A', 'B'])


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_period_and_session_routing(self):
        tree = ast.parse(Path('src/plugins/commands.py').read_text())
        fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'handle_danmaku_rank')
        fn.decorator_list = []
        class Finished(Exception):
            pass
        for raw, period in [('2026-08', True), ('2026-08-01 2', False), ('', False)]:
            with self.subTest(raw=raw):
                query = Mock(return_value=[])
                resolver = Mock(return_value=({'start_ts': 100, 'end_ts': 200}, '场次', False))
                env = dict(asyncio=asyncio, datetime=datetime, Matcher=object, Event=object,
                           CommandArg=lambda: None, get_query_room_id=lambda e: 1,
                           parse_rank_period=parse_rank_period, _parse_concurrent_args=lambda x: (None, None),
                           _resolve_live_session=resolver, query_danmaku_rank=query)
                exec(compile(ast.Module(body=[fn], type_ignores=[]), '<handler>', 'exec'), env)
                matcher = Mock(finish=AsyncMock(side_effect=Finished))
                with self.assertRaises(Finished):
                    await env['handle_danmaku_rank'](matcher, object(), Mock(extract_plain_text=lambda: raw))
                self.assertEqual(resolver.call_count, 0 if period else 1)
                query.assert_called_once()
                if not period:
                    query.assert_called_once_with(1, 100, 201)
