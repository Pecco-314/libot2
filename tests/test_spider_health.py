import io
import logging
import sqlite3
import tempfile
import threading
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from scripts import backfill_name_history_index as names
from src.spider import cron, jobs
from src.spider.jobs import activity, stats


class SpiderLoggingTests(unittest.TestCase):
    def test_all_project_names_and_scheduler_errors_share_one_formatted_handler(self):
        root = logging.getLogger()
        logger = logging.getLogger("spider")
        tracked = [root, logger] + [logging.getLogger(name) for name in ("httpx", "httpcore", "apscheduler")]
        saved = [(item, list(item.handlers), item.level, item.propagate) for item in tracked]
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        handler.baseFilename = str(Path(cron.__file__).resolve().parents[2] / "logs/spider.log")
        handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))

        def initialize(name):
            logger.addHandler(handler)
            return logger

        try:
            root.handlers = []
            logger.handlers = []
            logger.propagate = True
            with patch.object(cron, "init_logger", side_effect=initialize) as init:
                cron.configure_logging()
                cron.configure_logging()
                init.assert_called_once()
            logging.getLogger("src.spider.jobs.user_activity").warning("connection failed")
            logging.getLogger("src.spider.jobs.chat_summary").info("summary completed")
            logging.getLogger("spider.jobs.stats").info("stats completed")
            logging.getLogger("apscheduler.executors.default").error("job failed")
            logging.getLogger("httpx").info("http chatter")
            rows = output.getvalue().splitlines()
            self.assertEqual(len(rows), 4)
            for row in rows:
                self.assertRegex(row, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
            self.assertNotIn("http chatter", output.getvalue())
        finally:
            for item, handlers, level, propagate in saved:
                item.handlers, item.level, item.propagate = handlers, level, propagate
            handler.close()

    def test_default_registry_does_not_start_danmakus_requests(self):
        registered = [name for name in vars(jobs) if name.startswith("register_") and name != "register_jobs"]
        self.assertNotIn("register_user_activity_jobs", registered)
        scheduler = Mock()
        with ExitStack() as stack:
            mocks = [stack.enter_context(patch.object(jobs, name)) for name in registered]
            jobs.register_jobs(scheduler)
            for mock in mocks:
                mock.assert_called_once_with(scheduler)


class SpiderAsyncDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_activity_database_work_runs_off_event_loop(self):
        main_thread = threading.get_ident()
        writes = []
        item = SimpleNamespace(activity_id="dynamic", uid=2, uname="test", timestamp=10,
                               dy_type="DYNAMIC_TYPE_WORD", item={})
        localizer = AsyncMock()
        localizer.__aenter__.return_value = localizer
        localizer.localize.return_value = ({}, [], True)

        def exists(activity_id):
            self.assertNotEqual(threading.get_ident(), main_thread)
            return False

        def insert(**kwargs):
            self.assertNotEqual(threading.get_ident(), main_thread)
            writes.append(kwargs)
            return True

        with patch.object(activity, "list_subscribed_room_ids", return_value=[100]), \
                patch.object(activity, "get_uid_by_roomid", return_value=2), \
                patch.object(activity, "get_latest_activity_timestamp", return_value=5), \
                patch.object(activity, "get_space_history", return_value=[item]), \
                patch.object(activity, "ActivityAssetLocalizer", return_value=localizer), \
                patch.object(activity, "activity_exists", side_effect=exists), \
                patch.object(activity, "insert_activity", side_effect=insert):
            await activity.collect_activity()
        self.assertEqual(len(writes), 1)

    async def test_stats_write_runs_off_event_loop(self):
        main_thread = threading.get_ident()
        sample = SimpleNamespace(room_id=100, uid=2, uname="test", fans_num=10,
                                 guard_num=2, fan_club_num=8)

        def insert(**kwargs):
            self.assertNotEqual(threading.get_ident(), main_thread)

        with patch.object(stats, "list_subscribed_room_ids", return_value=[100]), \
                patch.object(stats, "get_stats", return_value=sample), \
                patch.object(stats, "insert_stats", side_effect=insert) as save:
            await stats.collect_stats()
        save.assert_called_once()


class SpiderNameIndexTests(unittest.TestCase):
    def test_final_count_does_not_hold_the_database_write_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.db"
            with sqlite3.connect(path) as conn:
                conn.execute("CREATE TABLE event(id INTEGER PRIMARY KEY,uid INTEGER,uname TEXT,timestamp INTEGER)")
                conn.execute("INSERT INTO event VALUES(1,2,'name',10)")
            seen = []
            connect = names.connect_sqlite

            def trace(statement):
                if statement == "SELECT COUNT(*) FROM name_history":
                    with sqlite3.connect(path, timeout=0.1) as writer:
                        writer.execute("BEGIN IMMEDIATE")
                        writer.rollback()
                        seen.append(True)

            def traced_connect(*args, **kwargs):
                conn = connect(*args, **kwargs)
                conn.set_trace_callback(trace)
                return conn

            with patch.object(names, "connect_sqlite", side_effect=traced_connect), redirect_stdout(io.StringIO()):
                names.backfill(path, 5)
            self.assertEqual(seen, [True])


if __name__ == "__main__":
    unittest.main()
