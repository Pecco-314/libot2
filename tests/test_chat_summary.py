import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, AsyncMock

from src.common.chat_summary import (
    TZ,
    parse_period,
    prepare_messages,
    api_config,
    summarize,
    correct_generation_labels,
    validate_result,
)
from src.db import chat_context
from src.common.chat_summary_commands import command_context, identify_command
from src.render.help import get_help_sections, get_admin_help_sections


class ChatPeriodTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 10, 3, 0, tzinfo=TZ)

    def test_default_and_units(self):
        start, end, _ = parse_period("", self.now)
        self.assertEqual(end - start, 3601)
        self.assertEqual(
            parse_period("30分钟", self.now)[1] - parse_period("30分钟", self.now)[0],
            1801,
        )
        self.assertEqual(
            parse_period("6小时", self.now)[0], int(self.now.timestamp()) - 21600
        )

    def test_intervals_and_midnight(self):
        a = parse_period("23:30 01:30", self.now)
        self.assertEqual(a[1] - a[0], 7201)
        self.assertEqual(
            parse_period("00:00-01:00", self.now)[:2],
            parse_period("00:00 01:00", self.now)[:2],
        )
        self.assertEqual(
            parse_period("2026-10-10 00:00 2026-10-10 01:00", self.now)[:2],
            parse_period("00:00 01:00", self.now)[:2],
        )
        for raw in (
            "0分钟",
            "25小时",
            "9" * 400 + "小时",
            "00:00 04:00",
            "错误",
            "2026-10-08T12:00 2026-10-08T13:00",
        ):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_period(raw, self.now)

    def test_single_start_times_and_longer_durations(self):
        now = datetime(2026, 10, 13, 16, 30, tzinfo=TZ)
        expected = int(now.replace(hour=8, minute=0).timestamp())
        for text in ('08:00', '2026-10-13 08:00', '2026-10-13T08:00', '2026-10-13 8:00'):
            with self.subTest(text=text):
                start, end, label = parse_period(text, now)
                self.assertEqual(start, expected)
                self.assertEqual(end, int(now.timestamp()) + 1)
                self.assertEqual(label, '10-13 08:00—16:30')
        self.assertEqual(parse_period('12小时', now)[0], int(now.timestamp()) - 43200)
        self.assertEqual(parse_period('24小时', now)[0], int(now.timestamp()) - 86400)
        with self.assertRaises(ValueError):
            parse_period('2026-10-13 17:00', now)

    def test_bare_clock_uses_latest_occurrence_and_labels_both_dates(self):
        start, end, label = parse_period('08:00', self.now)
        self.assertEqual(start, int(datetime(2026, 10, 9, 8, 0, tzinfo=TZ).timestamp()))
        self.assertEqual(label, '10-09 08:00—10-10 03:00')
        self.assertEqual(end, int(self.now.timestamp()) + 1)
        for text in ('2026-10-09 23:30 2026-10-10 01:30',
                     '2026-10-09 23:30至2026-10-10 01:30', '23:30~01:30'):
            self.assertEqual(parse_period(text, self.now)[:2],
                             parse_period('23:30 01:30', self.now)[:2])

    def test_endpoint_and_no_credentials_in_url(self):
        with patch.dict(os.environ, {"API_HOST": "example.com", "API_KEY": "test"}):
            self.assertEqual(
                api_config()[0],
                "https://example.com/compatible-mode/v1/chat/completions",
            )
        with patch.dict(
            os.environ, {"API_HOST": "https://example.com/v1", "API_KEY": "test"}
        ):
            self.assertEqual(api_config()[0], "https://example.com/v1/chat/completions")
        with patch.dict(
            os.environ, {"API_HOST": "http://example.com", "API_KEY": "test"}
        ):
            with self.assertRaises(ValueError):
                api_config()


class ChatContextTests(unittest.TestCase):
    def test_topic_prompt_allows_twelve_without_filling_quota(self):
        from src.common.chat_summary import SYSTEM_PROMPT

        self.assertIn('最多12个有效主题', SYSTEM_PROMPT)
        self.assertIn('允许只有1个、2个主题', SYSTEM_PROMPT)
        self.assertIn('不为凑数量补内容', SYSTEM_PROMPT)

    def test_reader_summary_omits_negative_recognition_reports(self):
        from src.common.chat_summary import SYSTEM_PROMPT

        for instruction in (
            '直接陈述有依据的讨论', '未见xxx', 'xxx未识别成功', '不是xxx',
            '不向读者汇报缺失信息、识别状态或排除过程',
            '不通过机械删除否定词改变原意',
        ):
            self.assertIn(instruction, SYSTEM_PROMPT)
        self.assertIn('略过依赖未读取媒体才能判断的细节', SYSTEM_PROMPT)

    def test_group_names_distinguish_colloquial_and_technical_references(self):
        from src.common.chat_summary import SYSTEM_PROMPT
        from src.common.chat_summary_payload import compact_payload
        from src.db import chat_summary as summary_db

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "context.db"
            summary_db.init_db(path)
            context = summary_db.load_context(path, 605275779)
            terms = {term["term"]: term for term in context["terms"]}
            self.assertIn("QQ群605275779", terms["嘟群"]["meaning"])
            self.assertTrue(terms["嘟群"]["keep_original"])
            self.assertIn("可能指其他社群", terms["主群"]["meaning"])
            self.assertTrue(terms["主群"]["needs_context"])
            packed, _ = compact_payload({"context": context})
            group_terms = [term for term in packed["context"]["terms"]
                           if term[0] in {"嘟群", "主群"}]
            self.assertEqual(len(group_terms), 2)
            self.assertIn("程序配置中的“主群”等技术标签", SYSTEM_PROMPT)

    def test_help_context_matches_rendered_help_and_recognizes_aliases(self):
        expected = [
            {"usage": usage, "meaning": meaning}
            for section in get_help_sections() + get_admin_help_sections()
            for usage, meaning in section["items"]
        ]
        self.assertEqual(command_context(), expected)
        for text, command in (
            ("/addmeme 标签 语录", "新增梗"),
            ("choose 甲 乙", "抉择"),
            ("/查sc 今天", "查SC"),
            ("被贴表情榜 7天", "被贴表情榜"),
            ("查直播 2026-08", "直播记录"),
        ):
            self.assertEqual(identify_command(text), command)
        self.assertIsNone(identify_command("我觉得来点老婆只是娱乐"))
        self.assertIsNone(identify_command("来点老婆"))

    def test_entertainment_calls_are_retained_with_command_context(self):
        texts = [
            "来点 老婆",
            "/来点 老公",
            "来点 抽老婆",
            "抉择 甲 乙",
            "我觉得来点老婆只是娱乐",
            "大家正在讨论老婆池",
            "/查歌曲 好听",
            "addmeme 标签 这是素材",
            "来点 老婆\n为什么这个功能报错了",
        ]
        rows = [
            {
                "message_id": i,
                "user_id": 123456,
                "sent_at": 100 + i,
                "nickname": "甲",
                "segments": json.dumps([{"type": "text", "text": text}]),
            }
            for i, text in enumerate(texts, 1)
        ]
        messages, _ = prepare_messages(rows, {})
        self.assertEqual([m["id"] for m in messages], [str(i) for i in range(1, 10)])
        self.assertEqual(messages[0]["bot_command"], "来点")
        self.assertNotIn("bot_command", messages[4])
        self.assertEqual(messages[7]["bot_command"], "新增梗")

    def test_aliases_are_group_scoped_and_survive_name_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "context.db"
            chat_context.init_db(path)
            chat_context.set_aliases(1, 12, "甲", ["pc"], db_path=path)
            chat_context.observe_names(1, [{"user_id": 12, "nickname": "新名字"}], path)
            people = chat_context.list_people(1, path)
            self.assertEqual(people[12]["name"], "甲")
            self.assertIn("新名字", people[12]["aliases"])
            self.assertIn("pc", people[12]["aliases"])
            self.assertIn("pc", people[12]["confirmed_aliases"])
            self.assertNotIn("新名字", people[12]["confirmed_aliases"])
            self.assertNotIn(12, chat_context.list_people(2, path))

    def test_cached_messages_exclude_recalls_and_other_groups(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "recall.db"
            with sqlite3.connect(path) as c:
                c.execute(
                    "CREATE TABLE message(group_id,message_id,user_id,sent_at,nickname,segments,recalled_at)"
                )
                c.executemany(
                    "INSERT INTO message VALUES(?,?,?,?,?,?,?)",
                    [
                        (1, 1, 12, 100, "甲", "[]", None),
                        (1, 2, 12, 101, "甲", "[]", 102),
                        (2, 3, 12, 101, "甲", "[]", None),
                    ],
                )
            self.assertEqual(
                [
                    r["message_id"]
                    for r in chat_context.list_messages(1, 100, 102, path)
                ],
                [1],
            )

    def test_message_preparation_hides_qq_and_filters_commands(self):
        rows = [
            {
                "message_id": i,
                "user_id": 123456,
                "sent_at": 100,
                "nickname": "甲",
                "segments": json.dumps([{"type": "text", "text": text}]),
            }
            for i, text in enumerate(
                ["@987654 pc去哪了", "在聊什么 1小时", "[image]"], 1
            )
        ]
        messages, people = prepare_messages(
            rows, {987654: {"name": "Pecco", "aliases": ["pc"]}}
        )
        self.assertEqual(len(messages), 1)
        self.assertNotIn("987654", messages[0]["text"])
        self.assertEqual(len(people), 1)

    def test_unread_image_marks_later_text_without_image_recognition(self):
        rows = [
            {
                "message_id": 1,
                "user_id": 123456,
                "sent_at": 100,
                "nickname": "甲",
                "segments": json.dumps([{"type": "image", "file": "unused.img"}]),
            },
            {
                "message_id": 2,
                "user_id": 123456,
                "sent_at": 110,
                "nickname": "甲",
                "segments": json.dumps([{"type": "text", "text": "开播2分钟1w同接"}]),
            },
        ]
        messages, _ = prepare_messages(
            rows, {123456: {"name": "主名", "aliases": ["甲"]}}
        )
        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0]["nearby_unread_image"])
        self.assertEqual(messages[0]["name"], "主名")


class SummaryEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_generation_retains_twelve_separate_topics(self):
        messages = [{'id': str(i), 'text': f'讨论{i}'} for i in range(1, 14)]
        value = {
            'topics': [
                {'title': f'话题{i}', 'summary': f'内容{i}', 'message_ids': [str(i)]}
                for i in range(1, 14)
            ],
            'terms_to_check': [],
        }
        with patch('src.common.chat_summary._complete', AsyncMock(return_value=(value, {}))):
            result = await summarize(messages, {}, '测试', review=False, context={'terms': []})
        self.assertEqual([topic['title'] for topic in result['topics']],
                         [f'话题{i}' for i in range(1, 13)])

    def test_invalid_topics_do_not_use_valid_topic_slots(self):
        value = {
            'topics': [
                {'title': '无依据', 'summary': '内容', 'message_ids': ['unknown']},
                *[
                    {'title': f'话题{i}', 'summary': '内容', 'message_ids': [str(i)]}
                    for i in range(1, 13)
                ],
            ],
            'terms_to_check': [],
        }
        validate_result(value, {str(i) for i in range(1, 13)})
        self.assertEqual(len(value['topics']), 12)
        self.assertEqual(value['topics'][-1]['title'], '话题12')

    def test_generation_labels_only_correct_known_notation(self):
        value = {'topics': [{'title': 'VR287期生与277期生',
                             'summary': '257期生和VR197期生，297期生；277条弹幕、1277期生。'}]}
        correct_generation_labels(value)
        self.assertEqual(value['topics'][0]['title'], '28期生与27期生')
        self.assertEqual(value['topics'][0]['summary'], '25期生和19期生，29期生；277条弹幕、1277期生。')

    async def test_help_is_sent_to_model_and_command_terms_are_not_requested(self):
        value = {
            "topics": [],
            "terms_to_check": [
                {"term": "addmeme", "possible_meaning": "不知道", "message_ids": ["1"]},
                {"term": "新简称", "possible_meaning": "不知道", "message_ids": ["1"]},
            ],
        }
        complete = AsyncMock(return_value=(value, {}))
        with patch("src.common.chat_summary._complete", complete):
            result = await summarize(
                [{"id": "1", "text": "addmeme 新简称 素材"}],
                {},
                "测试",
                review=False,
                context={"terms": []},
            )
        self.assertEqual(
            complete.call_args.args[0]["context"]["bot_commands"], command_context()
        )
        self.assertEqual(
            [item["term"] for item in result["terms_to_check"]], ["新简称"]
        )

    async def test_known_and_ignored_terms_are_not_asked_again(self):
        value = {
            "topics": [],
            "terms_to_check": [
                {"term": "千工", "possible_meaning": "不知道", "message_ids": ["1"]},
                {"term": "忽略词", "possible_meaning": "不知道", "message_ids": ["1"]},
                {"term": "真未知", "possible_meaning": "不知道", "message_ids": ["1"]},
            ],
        }
        context = {
            "terms": [{"term": "千春/千工", "meaning": "主播"}],
            "ignored_terms": ["忽略词"],
        }
        with patch(
            "src.common.chat_summary._complete", AsyncMock(return_value=(value, {}))
        ):
            result = await summarize(
                [{"id": "1", "text": "测试"}], {}, "测试", review=False, context=context
            )
        self.assertEqual(
            [item["term"] for item in result["terms_to_check"]], ["真未知"]
        )

    async def test_invalid_evidence_not_published(self):
        value = {
            "topics": [
                {"title": "话题", "summary": "内容", "message_ids": ["1", "unknown"]},
                {"title": "捏造", "summary": "内容", "message_ids": ["missing"]},
            ],
            "terms_to_check": [],
        }
        with patch(
            "src.common.chat_summary._complete", AsyncMock(return_value=(value, {}))
        ):
            result = await summarize([{"id": "1", "text": "测试"}], {}, "一小时")
        self.assertEqual(len(result["topics"]), 1)
        self.assertEqual(result["topics"][0]["message_ids"], ["1"])
        self.assertTrue(result["evidence_reviewed"])

    async def test_verified_glossary_is_used_in_generation_and_review(self):
        value = {
            "topics": [
                {"title": "互动", "summary": "群友复读xxlb和onp", "message_ids": ["1"]}
            ],
            "terms_to_check": [],
        }
        complete = AsyncMock(return_value=(value, {}))
        with patch("src.common.chat_summary._complete", complete):
            await summarize([{"id": "1", "text": "xxlb onp"}], {}, "一小时")
        for call in complete.call_args_list:
            context = call.args[0]["context"]
            self.assertNotIn("people", context)
            terms = {t["term"]: t for t in context["terms"]}
            self.assertTrue(terms["nbz"]["keep_original"])
            self.assertTrue(terms["onp"]["keep_original"])
            self.assertEqual(terms["xxlb"]["meaning"], "谢谢老板。")
            self.assertIn("哎小呜Awu", terms["a5"]["meaning"])
            self.assertIn("VR197", terms["a5"]["meaning"])
