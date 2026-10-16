import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock

from src.service_watchdog import check, latest_online


class WatchdogTests(unittest.TestCase):
    def test_failure_dedup_reminder_recovery(self):
        send = Mock()
        state = check(1000, 500, {}, send)
        self.assertTrue(state['alerted'])
        state = check(1030, 500, state, send)
        self.assertEqual(send.call_count, 1)
        state = check(4600, 500, state, send)
        self.assertEqual(send.call_count, 2)
        state = check(4630, 4620, state, send)
        self.assertFalse(state['alerted'])
        self.assertEqual(send.call_count, 3)

    def test_failed_mail_does_not_mark_sent(self):
        state = {}
        with self.assertRaises(OSError):
            check(1000, 0, state, Mock(side_effect=OSError))
        self.assertEqual(state, {})

    def test_rotation_and_unrelated_activity(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'logs').mkdir()
            (root / 'logs/libot.log').write_text('unrelated activity\n')
            (root / 'logs/libot.log.1').write_text(
                '2026-10-15 17:00:00,001 - libot.scheduler - DEBUG - Bot is online\n')
            self.assertGreater(latest_online(root), 0)

    def test_healthy_no_mail(self):
        send = Mock()
        self.assertTrue(check(1000, 990, {}, send)['healthy'])
        send.assert_not_called()
