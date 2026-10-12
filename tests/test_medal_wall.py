import unittest

from src.spider.medal_wall import _parse_wall


class MedalWallVisibilityTests(unittest.TestCase):
    def test_visibility_flags(self):
        for closed, wearing, hidden in [
            (0, 0, False),
            (1, 0, True),
            (0, 1, True),
            (1, 1, True),
            ("0", "1", True),
        ]:
            with self.subTest(closed=closed, wearing=wearing):
                wall = _parse_wall(99, {
                    "code": 0,
                    "data": {
                        "close_space_medal": closed,
                        "only_show_wearing": wearing,
                        "list": [],
                    },
                })
                self.assertEqual(wall["hidden"], hidden)
                self.assertEqual(wall["wearing_only"], bool(int(wearing)))

    def test_missing_flags_default_to_public(self):
        self.assertFalse(_parse_wall(99, {"code": 0, "data": {}})["hidden"])
