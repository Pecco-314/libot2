import ast
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from src.common import time_args as t
from src.common.danmaku_rank import parse_rank_period
from src.common.stats_period import parse_stats_period
from src.common.qq_reactions import parse_period as reaction_period
from src.common.chat_summary import parse_period as summary_period
from tests.command_sources import command_tree


class TimeArgsTests(unittest.TestCase):
    now = datetime(2026, 1, 1, 0, 30, tzinfo=t.BEIJING)

    def test_dates_midnight_and_new_year(self):
        for text in ('昨天', '昨日', '12-31', '12/31', '12.31', '2025-12-31'):
            self.assertEqual(t.normalize_date(text, now=self.now), '2025-12-31')
        self.assertEqual(t.normalize_date('今天', now=datetime(2025, 12, 31, 16, tzinfo=timezone.utc)), '2026-01-01')
        for value in ('123456', '2026-02-30', '2025-13-01', 'hello'):
            with self.assertRaises(ValueError):
                t.parse_date(value, now=self.now)
        self.assertFalse(t.is_date_arg('123456'))

    def test_datetime_clock_policies(self):
        self.assertEqual(t.parse_datetime('昨天 23:00', now=self.now), t.parse_datetime('23:00', now=self.now, past_clock=True))
        self.assertEqual(t.parse_datetime('23:00', now=self.now).day, 1)
        self.assertEqual(t.parse_datetime('12/31 23:00', now=self.now).year, 2025)

    def test_index_strict(self):
        self.assertEqual(t.parse_date_index('昨天 2'), ('昨天', 2))
        for value in ('昨天 0', '昨天 2 extra', '昨天 abc'):
            with self.assertRaises(ValueError):
                t.parse_date_index(value)

    def test_duration_and_month(self):
        for value, seconds in [('1天', 86400), ('24小时', 86400), ('1小时30分钟', 5400), ('0.5分钟', 30), ('5秒', 5)]:
            self.assertEqual(t.parse_duration(value), seconds)
        self.assertEqual(t.parse_month('上月', now=self.now), (2025, 12))
        for value in ('202512', '2025-12', '2025/12'):
            self.assertEqual(t.parse_month(value), (2025, 12))

    def test_relative_dates_in_calendar_consumers(self):
        expected = t.calendar_span('昨天', now=self.now)
        start, end = (int(point.timestamp()) for point in expected)
        self.assertEqual(parse_rank_period('前天 昨天', self.now)[1], end)
        self.assertIsNone(parse_rank_period('昨天 2', self.now))
        period = parse_stats_period('昨天', self.now)
        self.assertEqual((period.start_ts, period.end_ts), (start, end))
        self.assertEqual(reaction_period('昨天', self.now)[:2], (start, end))
        self.assertEqual(summary_period('昨天 23:30', self.now)[0], start + 23 * 3600 + 1800)

    def test_event_and_keyword_parser_integration(self):
        names = {'_parse_events_args', '_parse_content_search_args'}
        nodes = [n for n in command_tree().body if isinstance(n, ast.FunctionDef) and n.name in names]
        import re
        env = dict(re=re, datetime=datetime, _CONTENT_SEARCH_MAX_KEYWORD_LENGTH=50, _CONTENT_SEARCH_REVEAL_WORD='reveal',
                   normalize_date=t.normalize_date, is_date_arg=t.is_date_arg)
        exec(compile(ast.Module(body=nodes, type_ignores=[]), '<time-handlers>', 'exec'), env)
        with patch.object(t, 'beijing_now', return_value=self.now):
            self.assertEqual(env['_parse_events_args']('123456 昨天 10'), ('123456', '2025-12-31', 10))
            result = env['_parse_content_search_args']('"你好 昨天" 昨天 10')
            self.assertEqual(result[:3], ('你好 昨天', '2025-12-31', 10))

    def test_session_and_cd_wrappers(self):
        import re
        from unittest.mock import Mock
        names = {'_resolve_live_session', '_parse_concurrent_args', '_parse_duration'}
        nodes = [n for n in command_tree().body if isinstance(n, ast.FunctionDef) and n.name in names]
        lookup = Mock(return_value=[{'start_ts': 1, 'end_ts': 2}, {'start_ts': 3, 'end_ts': 4}])
        env = dict(re=re, normalize_date=t.normalize_date, parse_date_index=t.parse_date_index,
                   parse_duration=t.parse_duration, list_live_sessions_by_date=lookup)
        exec(compile(ast.Module(body=nodes, type_ignores=[]), '<time-wrappers>', 'exec'), env)
        with patch.object(t, 'beijing_now', return_value=self.now):
            date, index = env['_parse_concurrent_args']('昨天 2')
            session, label, ongoing = env['_resolve_live_session'](1, date, index)
            self.assertEqual(session['start_ts'], 3)
            self.assertEqual(label, '2025-12-31第2场直播')
            lookup.assert_called_once_with(1, '2025-12-31')
        for value in ('1天', '24小时'):
            self.assertEqual(env['_parse_duration'](value), 86400)
        self.assertEqual(env['_parse_duration']('关闭'), 0)
        self.assertIsNone(env['_parse_duration']('乱写'))

    def test_blind_box_boundaries_remain_inclusive(self):
        import re
        names = {'_resolve_blind_box_range'}
        nodes = [n for n in command_tree().body if isinstance(n, ast.FunctionDef) and n.name in names]
        now = datetime(2026, 10, 26, 12, tzinfo=t.BEIJING)
        env = dict(re=re, BLIND_BOX_DATA_START='2026-04-11', beijing_now=lambda: now,
                   parse_date=t.parse_date, calendar_span=t.calendar_span)
        exec(compile(ast.Module(body=nodes, type_ignores=[]), '<blind-box>', 'exec'), env)
        start, end, label = env['_resolve_blind_box_range'](1, '2026-04')
        self.assertEqual(start, int(t.parse_date('2026-04-11').timestamp()))
        self.assertEqual(end, int(t.parse_date('2026-05-01').timestamp()) - 1)
        self.assertIn('统计始于04-11', label)
