import ast
import asyncio
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.common.stats_period import TZ, parse_stats_period
from src.db.stats import _ensure_stats_schema, list_stats
from tests.command_sources import command_tree


class StatsPeriodTests(unittest.TestCase):
    now = datetime(2026, 10, 14, 12, 34, 56, tzinfo=TZ)

    def parse(self, text):
        return parse_stats_period(text, self.now)

    def test_legacy_days_and_default_keep_rolling_window(self):
        for text, days in [("", 1), ("30", 30), ("30天", 30), ("30日", 30),
                           ("近30天", 30), ("近1月", 30), ("2026天", 2026)]:
            with self.subTest(text=text):
                period = self.parse(text)
                self.assertEqual(period.start_ts, int((self.now - timedelta(days=days)).timestamp()))
                self.assertEqual(period.end_ts, int(self.now.timestamp()) + 1)
                self.assertEqual(period.days, days)
                self.assertTrue(period.use_current)

    def test_year_month_and_day(self):
        for text, start, end in [
            ("2025", datetime(2025, 1, 1, tzinfo=TZ), datetime(2026, 1, 1, tzinfo=TZ)),
            ("2025-08", datetime(2025, 8, 1, tzinfo=TZ), datetime(2025, 9, 1, tzinfo=TZ)),
            ("2025-8-9", datetime(2025, 8, 9, tzinfo=TZ), datetime(2025, 8, 10, tzinfo=TZ)),
            ("2024-02-01 2024-02-29", datetime(2024, 2, 1, tzinfo=TZ), datetime(2024, 3, 1, tzinfo=TZ)),
            ("2025-08 2025-09", datetime(2025, 8, 1, tzinfo=TZ), datetime(2025, 10, 1, tzinfo=TZ)),
        ]:
            with self.subTest(text=text):
                period = self.parse(text)
                self.assertEqual((period.start_ts, period.end_ts), (int(start.timestamp()), int(end.timestamp())))
                self.assertFalse(period.use_current)
                self.assertIsNone(period.days)

    def test_separators_and_inclusive_end(self):
        expected = self.parse("2025-08-01 2025-08-31")
        for separator in ("至", "～", "~", "  "):
            self.assertEqual(self.parse(f"2025-08-01{separator}2025-08-31"), expected)
        self.assertEqual(expected.label, "2025-08-01～2025-08-31")

    def test_current_calendar_period_is_clipped_but_uses_stored_counts(self):
        for text in ("2026", "2026-10", "本月", "今年", "2026-10-01 2026-10-31"):
            with self.subTest(text=text):
                period = self.parse(text)
                self.assertEqual(period.end_ts, int(self.now.timestamp()) + 1)
                self.assertTrue(period.label.endswith("2026-10-14"))
                self.assertFalse(period.use_current)

    def test_naive_now_means_beijing(self):
        self.assertEqual(parse_stats_period("30", self.now.replace(tzinfo=None)), self.parse("30"))

    def test_invalid_input_does_not_silently_fall_back_to_today(self):
        for text in ("0", "-1", "近0天", "随便", "2025-13", "2025-02-29", "2027",
                     "2025-09-01 2025-08-31", "2025-08-01 2", "2025-08-01 2025-08-02 x", "9999"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.parse(text)


class StatsDatabaseTests(unittest.TestCase):
    def test_beijing_boundaries_and_stable_same_second_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "stats.db"
            with sqlite3.connect(path) as conn:
                _ensure_stats_schema(conn)
                conn.executemany(
                    "INSERT INTO stats(room_id,uid,uname,fans_num,guard_num,fan_club_num,created_at) VALUES(?,1,'test',?,?,-1,?)",
                    [
                        (1, 99, 9, "2025-07-31 15:59:59"),
                        (1, -1, 10, "2025-07-31 16:00:00"),
                        (1, 100, -1, "2025-07-31 16:00:00"),
                        (1, 120, 12, "2025-08-31 15:59:59"),
                        (1, 9999, 99, "2025-08-31 16:00:00"),
                        (2, 8888, 88, "2025-08-01 00:00:00"),
                    ],
                )
            period = parse_stats_period("2025-08", datetime(2026, 1, 1, tzinfo=TZ))
            rows = list_stats(1, start_ts=period.start_ts, end_ts=period.end_ts, db_path=path)
            self.assertEqual([r["fans_num"] for r in rows], [-1, 100, 120])
            self.assertEqual([r["created_at"] for r in rows], [
                "2025-08-01 00:00:00", "2025-08-01 00:00:00", "2025-08-31 23:59:59",
            ])
            with sqlite3.connect(path) as conn:
                plan = conn.execute("EXPLAIN QUERY PLAN SELECT * FROM stats WHERE room_id=? AND created_at>=? AND created_at<? ORDER BY created_at,id", (1,"2025-07-31 16:00:00","2025-08-31 16:00:00")).fetchall()
            self.assertTrue(any("idx_stats_room_time" in row[3] for row in plan))

    def test_invalid_ranges(self):
        for args in ({}, {"days": 0}, {"start_ts": 100}, {"end_ts": 100},
                     {"start_ts": 100, "end_ts": 100}, {"start_ts": 101, "end_ts": 100}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                list_stats(1, **args)


class StatsHandlerTests(unittest.IsolatedAsyncioTestCase):
    def environment(self, result=True):
        node = next(n for n in command_tree().body if isinstance(n, ast.AsyncFunctionDef)
                    and n.name == "_handle_stats_query")
        sample = {
            "path": "/tmp/stat.png", "begin_value": 100, "end_value": 150,
            "begin_time": datetime(2025, 8, 2, 12), "end_time": datetime(2025, 8, 29, 18),
        } if result else None
        env = {
            "Matcher": object, "Event": object, "MessageSegment": SimpleNamespace(text=lambda t: t, image=lambda **kw: kw),
            "Message": list, "asyncio": asyncio, "datetime": datetime, "STATS_TZ": TZ,
            "parse_stats_period": lambda raw: parse_stats_period(raw, datetime(2026, 10, 14, 12, tzinfo=TZ)),
            "get_query_room_id": Mock(return_value=1),
            "get_stat_start_date": Mock(return_value=datetime(2025, 3, 8)),
            "get_name_by_roomid": AsyncMock(return_value="三理"),
            "get_uid_by_roomid": AsyncMock(return_value=2),
            "get_fans_num": AsyncMock(return_value=90000),
            "get_guard_num": AsyncMock(return_value=900),
            "get_fan_club_num": AsyncMock(return_value=9000),
            "render_fans_trend": AsyncMock(return_value=sample),
            "render_guards_trend": AsyncMock(return_value=sample),
            "render_fan_club_trend": AsyncMock(return_value=sample), "logger": Mock(),
        }
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<stats-handler>", "exec"), env)
        return env

    async def invoke(self, env, raw, kind):
        matcher = SimpleNamespace(finish=AsyncMock())
        await env["_handle_stats_query"](matcher, object(), Mock(extract_plain_text=lambda: raw), kind)
        matcher.finish.assert_awaited_once()
        return matcher.finish.call_args.args[0]

    async def test_explicit_period_keeps_reply_to_one_line_and_uses_historical_delta(self):
        for kind, renderer in [("fans", "render_fans_trend"), ("guards", "render_guards_trend"),
                               ("club", "render_fan_club_trend")]:
            with self.subTest(kind=kind):
                env = self.environment()
                message = await self.invoke(env, "2025-08-01 2025-08-31", kind)
                self.assertIn("150 (+50)", message[0])
                self.assertEqual(len(message[0].splitlines()), 1)
                self.assertEqual(len(message), 2)
                env["get_uid_by_roomid"].assert_not_awaited()
                for name in ("get_fans_num", "get_guard_num", "get_fan_club_num"):
                    env[name].assert_not_awaited()
                period = env["parse_stats_period"]("2025-08")
                env[renderer].assert_awaited_once_with(1, None, "三理", start_ts=period.start_ts,
                                                      end_ts=period.end_ts, period_label=period.label)

    async def test_rolling_days_keeps_current_value_and_recorded_baseline(self):
        env = self.environment()
        message = await self.invoke(env, "30", "fans")
        self.assertIn("90000 (+89900)", message[0])
        env["get_fans_num"].assert_awaited_once_with(2)
        self.assertEqual(env["render_fans_trend"].call_args.args, (1, 30, "三理"))

    async def test_current_failure_and_invalid_value_fall_back_to_recorded_value(self):
        for value in (-1, RuntimeError("unavailable")):
            env = self.environment()
            if isinstance(value, Exception):
                env["get_guard_num"].side_effect = value
            else:
                env["get_guard_num"].return_value = value
            message = await self.invoke(env, "", "guards")
            self.assertIn("150 (+50)", message[0])

    async def test_invalid_or_empty_period_does_not_call_current_api(self):
        env = self.environment()
        message = await self.invoke(env, "2025-13", "fans")
        self.assertIn("日期范围无效", message)
        env["get_stat_start_date"].assert_not_called()
        env["render_fans_trend"].assert_not_awaited()
        env = self.environment(result=False)
        message = await self.invoke(env, "2025-08", "fans")
        self.assertIn("有效记录不足两条", message)
        env["get_uid_by_roomid"].assert_not_awaited()

    async def test_period_before_first_record_reports_actual_coverage(self):
        env = self.environment()
        message = await self.invoke(env, "2025", "fans")
        self.assertIn("数据从2025-03-08开始", message[0])
        self.assertEqual(len(message[0].splitlines()), 2)

    async def test_user_example_has_only_value_and_short_coverage_note(self):
        env = self.environment()
        env["render_guards_trend"].return_value.update(
            begin_value=150, end_value=782,
            begin_time=datetime(2025, 3, 8, 22, 57), end_time=datetime(2025, 5, 1, 12),
        )
        message = await self.invoke(env, "2025-03-01 2025-05-01", "guards")
        self.assertEqual(message[0], "三理的大航海数：782 (+632)\n（数据从2025-03-08开始）")
        self.assertEqual(len(message), 2)
        self.assertEqual(message[1], {"file": "/tmp/stat.png"})


class StatsRenderTests(unittest.IsolatedAsyncioTestCase):
    async def test_range_is_passed_to_database_and_title(self):
        from src.render import stats
        with patch.object(stats, "list_stats", return_value=[
            {"created_at": "2025-08-01 00:00:00", "fans_num": 100, "guard_num": -1},
            {"created_at": "2025-08-31 23:59:59", "fans_num": 200, "guard_num": -1},
        ]) as query, patch.object(stats, "_base_render", return_value={}) as render:
            await stats.render_fans_trend(1, None, "三理", start_ts=100, end_ts=200,
                                          period_label="2025-08-01～2025-08-31")
        query.assert_called_once_with(1, None, start_ts=100, end_ts=200)
        self.assertEqual(render.call_args.args[1], [100, 200])
        self.assertIn("2025-08-01～2025-08-31", render.call_args.args[-1])

    async def test_unknown_metric_at_same_second_does_not_hide_known_value(self):
        from src.render.stats import _filter_invalid_data
        times = [datetime(2025, 8, 1), datetime(2025, 8, 1), datetime(2025, 8, 2)]
        self.assertEqual(_filter_invalid_data(times, [-1, 100, 150]), ([times[0], times[-1]], [100, 150]))

    async def test_real_render_metadata_and_file_names_keep_different_titles_separate(self):
        from src.render import stats
        times = [datetime(2025, 8, 2), datetime(2025, 8, 29)]
        with tempfile.TemporaryDirectory() as folder, patch.object(stats, "ROOT", Path(folder)):
            a = stats._base_render(times, [100, 150], "fans", "#E54D4D", "A 2025-08")
            b = stats._base_render(times, [100, 150], "fans", "#E54D4D", "B 2025-08")
            self.assertNotEqual(a["path"], b["path"])
            self.assertTrue(a["path"].is_file())
            self.assertEqual((a["begin_value"], a["end_value"]), (100, 150))
            self.assertEqual((a["begin_time"], a["end_time"]), tuple(times))


if __name__ == "__main__":
    unittest.main()
