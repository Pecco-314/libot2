import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.common import chat_summary_service as service
from src.db import chat_context
from src.db import chat_summary as db


def rows(count=500, *, first_id=1, start=None):
    start = int(time.time()) - 1000 if start is None else start
    return [
        {
            "message_id": i + first_id,
            "user_id": 12345,
            "sent_at": start + i,
            "nickname": "甲",
            "segments": json.dumps([{"type": "text", "text": f"讨论{i}"}]),
        }
        for i in range(count)
    ]


def result(message_id="1"):
    return {
        "topics": [
            {"title": "讨论", "summary": "交流话题", "message_ids": [str(message_id)]}
        ],
        "terms_to_check": [
            {
                "term": "待解释",
                "possible_meaning": "不确定",
                "message_ids": [str(message_id)],
            }
        ],
        "message_count": 500,
        "period": "测试",
        "usage": [],
    }


class Fixtures:
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "context.db"
        self.cache = Path(self.folder.name) / "recall.db"
        db.init_db(self.path)
        with sqlite3.connect(self.cache) as conn:
            conn.execute(
                "CREATE TABLE message(group_id,message_id,user_id,sent_at,nickname,segments,recalled_at)"
            )

    def tearDown(self):
        self.folder.cleanup()

    def insert(self, records, group=1):
        with sqlite3.connect(self.cache) as conn:
            conn.executemany(
                "INSERT INTO message VALUES(?,?,?,?,?,?,NULL)",
                [
                    (
                        group,
                        r["message_id"],
                        r["user_id"],
                        r["sent_at"],
                        r["nickname"],
                        r["segments"],
                    )
                    for r in records
                ],
            )

    def complete(self, records):
        bid = db.stage_batch(1, records, self.path)
        claim = db.claim_batch(bid, db_path=self.path)
        db.finish_batch(
            bid,
            result(records[0]["message_id"]),
            db.revision(self.path),
            claim["token"],
            self.path,
        )
        return bid


class BatchDBTests(Fixtures, unittest.TestCase):
    def unknown(self, term, records):
        db.record_unknown_terms(
            1,
            {
                "terms_to_check": [
                    {"term": term, "possible_meaning": "不确定", "message_ids": []}
                ]
            },
            db_path=self.path,
            rows=records,
        )

    def text_rows(self, texts, *, first_id=1, start=None):
        records = rows(len(texts), first_id=first_id, start=start)
        for row, text in zip(records, texts):
            row["segments"] = json.dumps([{"type": "text", "text": text}])
        return records

    def test_mentions_count_distinct_messages_not_evidence_rounds(self):
        records = self.text_rows(["汐汐汐汐", "今天汐汐来了", "这条无关"])
        self.unknown("汐汐", records)
        self.unknown("汐汐", records)
        db.record_term_mentions(1, records, self.path)
        self.assertEqual(db.get_unknown(1, "汐汐", self.path)["mention_count"], 2)
        tomorrow = self.text_rows(["汐汐"], start=records[0]["sent_at"] + 86400)
        db.record_term_mentions(1, tomorrow, self.path)
        self.assertEqual(db.get_unknown(1, "汐汐", self.path)["mention_count"], 3)
        with db.connection(self.path) as conn:
            self.assertEqual(
                {r[1] for r in conn.execute("PRAGMA table_info(chat_term_mention)")},
                {"group_id", "term", "message_id", "sent_at"},
            )

    def test_mention_matching_aliases_ascii_boundaries_and_entertainment(self):
        records = self.text_rows(
            [
                "502来了",
                "2502",
                "代号abcx",
                "ABC来了",
                "来点 老婆",
                "老婆池这个功能怎么实现",
            ]
        )
        self.unknown("502/abc", records)
        self.unknown("老婆", records)
        self.assertEqual(db.get_unknown(1, "502/abc", self.path)["mention_count"], 2)
        self.assertEqual(db.get_unknown(1, "老婆", self.path)["mention_count"], 1)

    def test_pending_mentions_accumulate_before_model_batch_is_ready(self):
        self.unknown("汐汐", [])
        records = self.text_rows(["汐汐", "汐汐", "无关"])
        self.insert(records)
        service.stage_ready(1, db_path=self.path, cache_path=self.cache)
        service.stage_ready(1, db_path=self.path, cache_path=self.cache)
        self.assertEqual(db.get_unknown(1, "汐汐", self.path)["mention_count"], 2)
        db.dismiss_term(1, "汐汐", self.path)
        db.record_term_mentions(1, self.text_rows(["汐汐"], first_id=100), self.path)
        self.assertEqual(db.get_unknown(1, "汐汐", self.path)["mention_count"], 2)

    def test_original_text_backfill_is_idempotent_and_excludes_bot_and_expired(self):
        self.unknown("汐汐", [])
        now = int(time.time())
        fresh = self.text_rows(["汐汐", "汐汐", "无关"], start=now - 500)
        fresh[1]["user_id"] = 999
        expired = self.text_rows(["汐汐"], first_id=50, start=now - 90000)
        self.insert(fresh + expired)
        stats = db.backfill_term_mentions(
            db_path=self.path, cache_path=self.cache, bot_qq="999", now=now
        )
        self.assertEqual(stats["added_mentions"], 1)
        stats = db.backfill_term_mentions(
            db_path=self.path, cache_path=self.cache, bot_qq="999", now=now
        )
        self.assertEqual(stats["added_mentions"], 0)
        self.assertEqual(db.get_unknown(1, "汐汐", self.path)["mention_count"], 1)

    def test_legacy_schema_migrates_without_guessing_mentions_from_evidence(self):
        self.unknown("汐汐", [])
        with db.connection(self.path) as conn:
            conn.execute("DROP TABLE chat_term_mention")
            conn.execute("ALTER TABLE chat_unknown_term DROP COLUMN mention_count")
        db.init_db(self.path)
        self.assertEqual(db.get_unknown(1, "汐汐", self.path)["mention_count"], 0)

    def test_499_waits_500_stages_once(self):
        records = rows()
        self.insert(records[:499])
        first = service.stage_ready(1, db_path=self.path, cache_path=self.cache)
        self.assertEqual(first, {"staged": [], "tail_count": 499})
        self.insert(records[499:])
        self.assertEqual(
            len(
                service.stage_ready(1, db_path=self.path, cache_path=self.cache)[
                    "staged"
                ]
            ),
            1,
        )
        self.assertEqual(
            service.stage_ready(1, db_path=self.path, cache_path=self.cache)["staged"],
            [],
        )

    def test_low_activity_messages_older_than_six_hours_count(self):
        self.insert(rows(start=int(time.time()) - 28000))
        self.assertEqual(
            len(
                service.stage_ready(1, db_path=self.path, cache_path=self.cache)[
                    "staged"
                ]
            ),
            1,
        )

    def test_bot_and_summary_commands_do_not_count(self):
        records = rows(502)
        records[0]["user_id"] = 999
        records[1]["segments"] = json.dumps(
            [{"type": "text", "text": "/在聊什么 1小时"}]
        )
        self.insert(records)
        staged = service.stage_ready(
            1, db_path=self.path, cache_path=self.cache, bot_qq="999"
        )
        self.assertEqual(len(staged["staged"]), 1)
        self.assertNotIn(1, db.member_ids(staged["staged"], self.path))
        self.assertNotIn(2, db.member_ids(staged["staged"], self.path))

    def test_concurrent_staging_and_overlapping_batches(self):
        records = rows()
        with ThreadPoolExecutor(max_workers=2) as executor:
            ids = list(
                executor.map(lambda _: db.stage_batch(1, records, self.path), range(2))
            )
        self.assertEqual(ids[0], ids[1])
        self.assertIsNone(
            db.stage_batch(
                1, rows(first_id=2, start=records[0]["sent_at"] + 1), self.path
            )
        )

    def test_message_ids_can_be_reused_on_another_day(self):
        old = rows(start=int(time.time()) - 70000)
        self.assertNotEqual(
            db.stage_batch(1, old, self.path), db.stage_batch(1, rows(), self.path)
        )

    def test_unknown_cache_reuse_keeps_batch_provenance(self):
        db.record_unknown_terms(1, result(), 21, self.path)
        db.record_unknown_terms(1, result(), None, self.path)
        item = db.get_unknown(1, "待解释", self.path)
        self.assertEqual(item["occurrence_count"], 1)
        self.assertEqual(json.loads(item["evidence"])["batch_id"], 21)

    def test_complete_batch_does_not_restore_raw_payload(self):
        records = rows()
        bid = self.complete(records)
        self.assertEqual(db.stage_batch(1, records, self.path), bid)
        with db.connection(self.path) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM chat_summary_payload").fetchone()[0],
                0,
            )

    def test_query_keeps_later_batch_in_the_same_second(self):
        timestamp = int(time.time()) - 5
        query_rows = rows(200, start=timestamp)
        later_rows = rows(500, first_id=201, start=timestamp)
        for record in query_rows + later_rows:
            record["sent_at"] = timestamp
        bid = db.stage_batch(1, later_rows, self.path)
        claim = db.claim_cycle(1, self.path)
        db.finish_query(1, query_rows, result(), 1, claim["claim_token"], self.path)
        self.assertEqual(db.get_batch(bid, self.path)["status"], "pending")
        self.assertIsNotNone(db.claim_batch(bid, db_path=self.path))

    def test_query_cancels_partially_overlapping_batch(self):
        timestamp = int(time.time()) - 5
        query_rows = rows(200, start=timestamp)
        overlapping_rows = rows(500, first_id=101, start=timestamp)
        for record in query_rows + overlapping_rows:
            record["sent_at"] = timestamp
        bid = db.stage_batch(1, overlapping_rows, self.path)
        claim = db.claim_cycle(1, self.path)
        db.finish_query(1, query_rows, result(), 1, claim["claim_token"], self.path)
        self.assertEqual(db.get_batch(bid, self.path)["status"], "cancelled")
        self.assertNotIn(201, db.assigned_ids(1, timestamp, self.path))

    def test_initialization_restores_latest_success_per_group(self):
        first = self.complete(rows(start=int(time.time()) - 2000))
        latest = self.complete(rows(first_id=501))
        with db.connection(self.path) as conn:
            conn.execute("DELETE FROM chat_summary_cycle")
        db.init_db(self.path)
        self.assertNotEqual(first, latest)
        state = db.get_cycle(1, self.path)
        self.assertEqual(state["latest_batch_id"], latest)
        self.assertEqual(state["cursor_mid"], 1000)
        claim = db.claim_cycle(1, self.path)
        db.init_db(self.path)
        self.assertEqual(
            db.get_cycle(1, self.path)["claim_token"], claim["claim_token"]
        )

    def test_two_claims_and_stale_worker_cannot_overwrite(self):
        bids = [
            db.stage_batch(i + 1, rows(first_id=1 + 500 * i), self.path)
            for i in range(3)
        ]
        claim1 = db.claim_batch(bids[0], db_path=self.path)
        self.assertIsNone(db.claim_batch(bids[0], db_path=self.path))
        self.assertIsNotNone(db.claim_batch(bids[1], db_path=self.path))
        self.assertIsNone(db.claim_batch(bids[2], db_path=self.path))
        with db.connection(self.path) as conn:
            conn.execute(
                "UPDATE chat_summary_batch SET started_at=0 WHERE id=?", (bids[0],)
            )
            conn.execute("UPDATE chat_summary_cycle SET claimed_at=0 WHERE group_id=1")
        claim2 = db.claim_batch(bids[0], db_path=self.path)
        self.assertFalse(
            db.finish_batch(bids[0], result(), 1, claim1["token"], self.path)
        )
        self.assertTrue(
            db.finish_batch(bids[0], result(), 1, claim2["token"], self.path)
        )

    def test_expiry_and_retry_limit_remove_payload(self):
        bid = db.stage_batch(1, rows(), self.path)
        with db.connection(self.path) as conn:
            conn.execute(
                "UPDATE chat_summary_batch SET created_at=0 WHERE id=?", (bid,)
            )
        self.assertEqual(db.ready_batch_ids(self.path), [])
        self.assertEqual(db.get_batch(bid, self.path)["status"], "cancelled")
        bid2 = db.stage_batch(1, rows(first_id=501), self.path)
        for attempt in range(3):
            claim = db.claim_batch(bid2, db_path=self.path)
            db.fail_batch(bid2, "ReadTimeout", claim["token"], self.path)
            with db.connection(self.path) as conn:
                conn.execute(
                    "UPDATE chat_summary_batch SET retry_after=0 WHERE id=?", (bid2,)
                )
        self.assertEqual(db.get_batch(bid2, self.path)["status"], "cancelled")
        with db.connection(self.path) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM chat_summary_payload").fetchone()[0],
                0,
            )

    def test_human_definition_survives_seed_and_invalidates_cache(self):
        rev = db.revision(self.path)
        db.query_cache_save("test", result(), self.path)
        db.record_unknown_terms(1, result(), db_path=self.path)
        db.save_definition(
            "待解释",
            "已确认的解释",
            group_id=1,
            qq=12345,
            canonical_name="主名",
            db_path=self.path,
        )
        db.save_definition(
            "pc",
            "更新的解释",
            group_id=605275779,
            qq=389894649,
            canonical_name="新主名",
            db_path=self.path,
        )
        db.init_db(self.path)
        self.assertGreater(db.revision(self.path), rev)
        self.assertIsNone(db.query_cache_get("test", self.path))
        self.assertEqual(db.unknown_page(db_path=self.path)["total"], 0)
        self.assertEqual(db.get_definition("pc", self.path)["definition"], "更新的解释")
        self.assertEqual(
            chat_context.list_people(605275779, self.path)[389894649]["name"], "新主名"
        )


class RollingCycleTests(Fixtures, unittest.IsolatedAsyncioTestCase):
    def seed(self, count=500):
        self.records = rows(1600, start=int(time.time()) - 3000)
        self.insert(self.records[:count])
        if count == 500:
            return self.complete(self.records[:500])

    async def test_generation_notice_is_sent_only_before_new_analysis(self):
        self.seed()
        self.insert(self.records[500:529])
        notify = AsyncMock()

        async def generate(messages, *args, **kwargs):
            notify.assert_awaited_once()
            return result(messages[0]["id"])

        with patch.object(service, "summarize", AsyncMock(side_effect=generate)) as model:
            await service.count_summary(1, db_path=self.path, cache_path=self.cache,
                                        on_generate=notify)
            notify.assert_not_awaited()
            model.assert_not_awaited()
            self.insert(self.records[529:530])
            await service.count_summary(1, db_path=self.path, cache_path=self.cache,
                                        on_generate=notify)
            notify.assert_awaited_once()
            model.assert_awaited_once()
            await service.count_summary(1, db_path=self.path, cache_path=self.cache,
                                        on_generate=notify)
            notify.assert_awaited_once()

    async def test_29_coalesces_30_refreshes_and_restarts_500_count(self):
        self.seed()
        self.insert(self.records[500:529])
        fake = AsyncMock(
            side_effect=lambda msgs, *a, **kw: {
                **result(msgs[0]["id"]),
                "message_count": len(msgs),
            }
        )
        with patch.object(service, "summarize", fake):
            cached = await service.latest_summary(
                1, db_path=self.path, cache_path=self.cache
            )
            self.assertTrue(cached["cache_hit"])
            self.assertEqual(cached["pending_message_count"], 29)
            fake.assert_not_awaited()
            self.insert(self.records[529:530])
            fresh = await service.latest_summary(
                1, db_path=self.path, cache_path=self.cache
            )
            self.assertFalse(fresh["cache_hit"])
            self.assertEqual(fresh["raw_message_count"], 30)
            self.assertEqual(fresh["pending_message_count"], 0)
            cached = await service.latest_summary(
                1, db_path=self.path, cache_path=self.cache
            )
            self.assertTrue(cached["cache_hit"])
            self.assertEqual(fake.await_count, 1)
            self.insert(self.records[530:1029])
            self.assertEqual(
                service.stage_ready(1, db_path=self.path, cache_path=self.cache)[
                    "staged"
                ],
                [],
            )
            self.insert(self.records[1029:1030])
            staged = service.stage_ready(1, db_path=self.path, cache_path=self.cache)
            self.assertEqual(len(staged["staged"]), 1)
            await service.process_batch(
                staged["staged"][0], db_path=self.path, cache_path=self.cache
            )
            self.assertEqual(len(fake.call_args.args[0]), 500)
        state = db.get_cycle(1, self.path)
        self.assertEqual(state["cursor_mid"], self.records[1029]["message_id"])
        self.assertEqual(json.loads(state["result"])["raw_message_count"], 500)

    async def test_messages_arriving_during_request_remain_for_next_cycle(self):
        self.seed()
        self.insert(self.records[500:700])

        async def generate(messages, *a, **kw):
            self.insert(self.records[700:820])
            self.assertEqual(
                service.stage_ready(1, db_path=self.path, cache_path=self.cache)[
                    "staged"
                ],
                [],
            )
            return result(messages[0]["id"])

        with patch.object(service, "summarize", AsyncMock(side_effect=generate)):
            value = await service.latest_summary(
                1, db_path=self.path, cache_path=self.cache
            )
        self.assertEqual(value["pending_message_count"], 120)
        self.assertEqual(
            db.get_cycle(1, self.path)["cursor_mid"], self.records[699]["message_id"]
        )

    async def test_failure_keeps_counter_and_cache(self):
        self.seed()
        self.insert(self.records[500:700])
        with patch.object(
            service, "summarize", AsyncMock(side_effect=RuntimeError("API失败"))
        ):
            with self.assertRaises(RuntimeError):
                await service.latest_summary(
                    1, db_path=self.path, cache_path=self.cache
                )
        state = db.get_cycle(1, self.path)
        self.assertIsNone(state["claim_token"])
        self.assertEqual(state["cursor_mid"], self.records[499]["message_id"])
        self.assertEqual(
            len(service.unconsumed_rows(1, state, cache_path=self.cache)), 200
        )

    async def test_query_consumes_queued_automatic_batch_once(self):
        self.seed()
        self.insert(self.records[500:1000])
        bid = service.stage_ready(1, db_path=self.path, cache_path=self.cache)[
            "staged"
        ][0]
        fake = AsyncMock(return_value=result("501"))
        with patch.object(service, "summarize", fake):
            await service.latest_summary(1, db_path=self.path, cache_path=self.cache)
            await service.process_batch(bid, db_path=self.path, cache_path=self.cache)
        self.assertEqual(fake.await_count, 1)
        self.assertEqual(db.get_batch(bid, self.path)["status"], "complete")
        self.assertEqual(db.get_batch(bid, self.path)["origin"], "query")

    async def test_repeated_queries_share_same_group_request(self):
        self.seed()
        self.insert(self.records[500:700])
        began, finish = asyncio.Event(), asyncio.Event()

        async def generate(messages, *a, **kw):
            began.set()
            await finish.wait()
            return result("501")

        fake = AsyncMock(side_effect=generate)
        with patch.object(service, "summarize", fake):
            first = asyncio.create_task(
                service.latest_summary(1, db_path=self.path, cache_path=self.cache)
            )
            await began.wait()
            reused = await service.latest_summary(
                1, db_path=self.path, cache_path=self.cache
            )
            self.assertTrue(reused["cache_hit"])
            self.assertTrue(reused["refresh_in_progress"])
            finish.set()
            await first
        self.assertEqual(fake.await_count, 1)

    async def test_first_query_handles_one_message_and_context_edits_keep_counter(
        self,
    ):
        self.seed(1)
        fake = AsyncMock(return_value=result())
        with patch.object(service, "summarize", fake):
            value = await service.latest_summary(
                1, db_path=self.path, cache_path=self.cache
            )
            self.assertFalse(value["cache_hit"])
            self.assertEqual(value["raw_message_count"], 1)
            fake.assert_awaited_once()
        fake.reset_mock()
        self.insert(self.records[1:21])
        db.save_definition("新词", "含义", db_path=self.path)
        with patch.object(service, "summarize", fake):
            cached = await service.latest_summary(
                1, db_path=self.path, cache_path=self.cache
            )
        self.assertTrue(cached["context_stale"])
        self.assertEqual(cached["pending_message_count"], 20)
        fake.assert_not_awaited()

    def test_refresh_boundaries_and_empty_queries(self):
        state = {"result": "{}", "updated_at": 1000}
        self.assertFalse(service.query_refresh_due(state, 29, now=1119))
        self.assertTrue(service.query_refresh_due(state, 29, now=1120))
        self.assertTrue(service.query_refresh_due(state, 1, now=1120))
        self.assertTrue(service.query_refresh_due(state, 30, now=1000))
        self.assertFalse(service.query_refresh_due(state, 0, now=99999))
        self.assertTrue(service.query_refresh_due({"result": None}, 1, now=1000))
        self.assertFalse(service.query_refresh_due({"result": None}, 0, now=1000))

    async def test_small_tail_refreshes_after_coalescing_window(self):
        self.seed()
        self.insert(self.records[500:501])
        with db.connection(self.path) as conn:
            conn.execute("UPDATE chat_summary_cycle SET updated_at=? WHERE group_id=1",
                         (int(time.time()) - db.QUERY_COALESCE_SECONDS,))
        with patch.object(service, "summarize", AsyncMock(return_value=result("501"))) as model:
            value = await service.latest_summary(1, db_path=self.path, cache_path=self.cache)
            again = await service.latest_summary(1, db_path=self.path, cache_path=self.cache)
        self.assertEqual(value["new_message_count"], 1)
        self.assertFalse(value["cache_hit"])
        self.assertTrue(again["cache_hit"])
        model.assert_awaited_once()

    async def test_empty_first_query_does_not_call_model(self):
        with patch.object(service, "summarize", AsyncMock()) as model:
            with self.assertRaisesRegex(ValueError, "累计0条"):
                await service.latest_summary(1, db_path=self.path, cache_path=self.cache)
        model.assert_not_awaited()

    async def test_neighboring_query_batches_reanalyze_together_and_count_once(self):
        self.seed(10)

        def generate(messages, *args, **kwargs):
            value = result(messages[0]["id"])
            value["topics"][0]["message_ids"] = [messages[0]["id"], messages[-1]["id"]]
            value["message_count"] = len(messages)
            return value

        with patch.object(service, "summarize", AsyncMock(side_effect=generate)) as model:
            await service.latest_summary(1, db_path=self.path, cache_path=self.cache)
            first_id = db.get_cycle(1, self.path)["latest_batch_id"]
            self.insert(self.records[10:40])
            fresh = await service.latest_summary(1, db_path=self.path, cache_path=self.cache)
            count = await service.count_summary(1, db_path=self.path, cache_path=self.cache)
            self.assertEqual([len(c.args[0]) for c in model.call_args_list], [10, 40])
        self.assertEqual(fresh["raw_message_count"], 40)
        self.assertEqual(fresh["new_message_count"], 30)
        self.assertEqual(fresh["coalesced_from_batch_id"], first_id)
        self.assertEqual(count["raw_message_count"], 40)
        self.assertEqual(count["message_count"], 40)
        self.assertEqual(len(count["topics"]), 1)
        self.assertEqual(count["topics"][0]["message_ids"], ["1", "40"])
        self.assertEqual(len(count["topics"][0]["participants"]), 1)
        self.assertEqual(len(count["batch_ids"]), 1)
        self.assertEqual(db.get_batch(first_id, self.path)["status"], "complete")

    async def test_missing_previous_raw_keeps_batches_separate(self):
        self.seed(10)
        with patch.object(service, "summarize", AsyncMock(return_value=result())):
            await service.latest_summary(1, db_path=self.path, cache_path=self.cache)
        with sqlite3.connect(self.cache) as conn:
            conn.execute("DELETE FROM message WHERE message_id=1")
        self.insert(self.records[10:40])
        with patch.object(service, "summarize", AsyncMock(return_value=result("11"))) as model:
            fresh = await service.latest_summary(1, db_path=self.path, cache_path=self.cache)
        self.assertEqual(len(model.call_args.args[0]), 30)
        self.assertNotIn("coalesced_from_batch_id", fresh)
        self.assertEqual(sum(b["message_count"] for b in db.recent_count_batches(1, 500, self.path)), 40)

    async def test_coalescing_leaves_messages_arriving_during_analysis_for_next_query(self):
        self.seed(10)
        with patch.object(service, "summarize", AsyncMock(return_value=result())):
            await service.latest_summary(1, db_path=self.path, cache_path=self.cache)
        self.insert(self.records[10:40])

        async def generate(messages, *args, **kwargs):
            self.assertEqual(len(messages), 40)
            self.insert(self.records[40:45])
            return result("40")

        with patch.object(service, "summarize", AsyncMock(side_effect=generate)):
            fresh = await service.latest_summary(1, db_path=self.path, cache_path=self.cache)
        self.assertEqual(fresh["pending_message_count"], 5)
        self.assertEqual(fresh["new_message_count"], 30)
        self.assertEqual(db.get_cycle(1, self.path)["cursor_mid"], 40)

    async def test_coalescing_skips_recalled_expired_and_reused_ids(self):
        self.seed(10)
        with patch.object(service, "summarize", AsyncMock(return_value=result())):
            await service.latest_summary(1, db_path=self.path, cache_path=self.cache)
        state = db.get_cycle(1, self.path)
        pending = self.records[10:40]
        with sqlite3.connect(self.cache) as conn:
            conn.execute("UPDATE message SET recalled_at=1 WHERE message_id=1")
        combined, bid = await service.coalesce_query_rows(
            1, state, pending, db_path=self.path, cache_path=self.cache
        )
        self.assertEqual(combined, pending)
        self.assertIsNone(bid)
        with sqlite3.connect(self.cache) as conn:
            conn.execute("UPDATE message SET recalled_at=NULL WHERE message_id=1")
        reused = [dict(pending[0], message_id=1), *pending[1:]]
        combined, bid = await service.coalesce_query_rows(
            1, state, reused, db_path=self.path, cache_path=self.cache
        )
        self.assertEqual(combined, reused)
        self.assertIsNone(bid)
        with db.connection(self.path) as conn:
            conn.execute("UPDATE chat_summary_batch SET start_ts=? WHERE id=?",
                         (int(time.time()) - db.RAW_TTL - 1, state["latest_batch_id"]))
        combined, bid = await service.coalesce_query_rows(
            1, state, pending, db_path=self.path, cache_path=self.cache
        )
        self.assertEqual(combined, pending)
        self.assertIsNone(bid)

    async def test_merge_failure_keeps_prior_batch_and_new_message_counter(self):
        self.seed(10)
        with patch.object(service, "summarize", AsyncMock(return_value=result())):
            await service.latest_summary(1, db_path=self.path, cache_path=self.cache)
        before = db.get_cycle(1, self.path)
        self.insert(self.records[10:40])
        with patch.object(service, "summarize", AsyncMock(side_effect=RuntimeError("失败"))):
            with self.assertRaises(RuntimeError):
                await service.latest_summary(1, db_path=self.path, cache_path=self.cache)
        after = db.get_cycle(1, self.path)
        self.assertEqual(after["result"], before["result"])
        self.assertEqual(after["latest_batch_id"], before["latest_batch_id"])
        self.assertIsNone(after["claim_token"])
        self.assertEqual(len(service.unconsumed_rows(1, after, cache_path=self.cache)), 30)

    async def test_coalescing_respects_batch_size_and_conversation_gap(self):
        for gap, old_count in ((0, 480), (db.QUERY_MERGE_GAP_SECONDS + 1, 10)):
            with self.subTest(gap=gap, old_count=old_count):
                pending = rows(30, first_id=old_count + 1)
                earlier = rows(old_count, start=pending[0]["sent_at"] - old_count - gap)
                token = db.claim_cycle(1, self.path)["claim_token"]
                db.finish_query(1, earlier, result(), db.revision(self.path), token, self.path)
                state = db.get_cycle(1, self.path)
                combined, bid = await service.coalesce_query_rows(
                    1, state, pending, db_path=self.path, cache_path=self.cache
                )
                self.assertEqual(combined, pending)
                self.assertIsNone(bid)

    async def test_reanalysis_preserves_old_version_and_current_counter(self):
        bid = self.seed()
        self.insert(self.records[500:700])
        fake = AsyncMock(return_value=result("501"))
        with patch.object(service, "summarize", fake):
            await service.latest_summary(1, db_path=self.path, cache_path=self.cache)
        state = db.get_cycle(1, self.path)
        db.save_definition("新词", "新的解释", db_path=self.path)
        with patch.object(service, "summarize", AsyncMock(return_value=result())):
            await service.reanalyze_batch(bid, db_path=self.path, cache_path=self.cache)
        after = db.get_cycle(1, self.path)
        self.assertEqual(
            (state["cursor_ts"], state["cursor_mid"], state["latest_batch_id"]),
            (after["cursor_ts"], after["cursor_mid"], after["latest_batch_id"]),
        )
        with db.connection(self.path) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM chat_summary_revision WHERE batch_id=?",
                    (bid,),
                ).fetchone()[0],
                1,
            )


class BatchServiceTests(Fixtures, unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_request_releases_claim_and_keeps_payload_for_retry(self):
        records = rows()
        self.insert(records)
        bid = db.stage_batch(1,records,self.path)
        with patch.object(service,'analyze_rows',AsyncMock(side_effect=asyncio.CancelledError())):
            with self.assertRaises(asyncio.CancelledError):
                await service.process_batch(bid,db_path=self.path,cache_path=self.cache)
        batch = db.get_batch(bid,self.path)
        self.assertEqual(batch['status'],'pending')
        self.assertEqual(batch['attempts'],0)
        self.assertIsNone(batch['claim_token'])
        self.assertIsNone(db.get_cycle(1,self.path)['claim_token'])
        self.assertIsNotNone(db.claim_batch(bid,db_path=self.path))

    async def test_old_worker_cannot_release_new_worker_claim(self):
        bid = db.stage_batch(1,rows(),self.path)
        old = db.claim_batch(bid,db_path=self.path)
        self.assertTrue(db.release_batch(bid,old['token'],self.path))
        new = db.claim_batch(bid,db_path=self.path)
        self.assertFalse(db.release_batch(bid,old['token'],self.path))
        self.assertEqual(db.get_batch(bid,self.path)['claim_token'],new['token'])
        self.assertEqual(db.get_cycle(1,self.path)['claim_token'],new['token'])

    async def test_process_filters_recalls_and_removes_staging(self):
        records = rows()
        self.insert(records)
        bid = db.stage_batch(1, records, self.path)
        with sqlite3.connect(self.cache) as conn:
            conn.execute("UPDATE message SET recalled_at=1 WHERE message_id=1")
        fake = AsyncMock(return_value=result("2"))
        with patch.object(service, "summarize", fake):
            batch = await service.process_batch(
                bid, db_path=self.path, cache_path=self.cache
            )
        self.assertEqual(batch["status"], "complete")
        self.assertEqual(len(fake.call_args.args[0]), 499)
        self.assertEqual(json.loads(batch["result"])["raw_message_count"], 499)

    async def test_batch_and_query_cache_need_no_model_request(self):
        records = rows()
        self.insert(records)
        self.complete(records)
        fake = AsyncMock(side_effect=AssertionError("缓存不应调用模型"))
        with (
            patch.object(service, "summarize", fake),
            patch.object(service, "_complete", fake),
        ):
            first = await service.query_summary(
                1,
                records[0]["sent_at"],
                records[-1]["sent_at"] + 1,
                "测试",
                db_path=self.path,
                cache_path=self.cache,
            )
            second = await service.query_summary(
                1,
                records[0]["sent_at"],
                records[-1]["sent_at"] + 1,
                "测试",
                db_path=self.path,
                cache_path=self.cache,
            )
        self.assertEqual(first["cached_message_count"], 500)
        self.assertTrue(second["cache_hit"])

    async def test_context_edit_reinterprets_summary_without_resending_raw(self):
        records = rows()
        self.insert(records)
        self.complete(records)
        db.save_definition("待解释", "人工定义", db_path=self.path)
        complete = AsyncMock(return_value=(result(), {}))
        with (
            patch.object(
                service,
                "summarize",
                AsyncMock(side_effect=AssertionError("不可重发原文")),
            ),
            patch.object(service, "_complete", complete),
        ):
            await service.query_summary(
                1,
                records[0]["sent_at"],
                records[-1]["sent_at"] + 1,
                "测试",
                db_path=self.path,
                cache_path=self.cache,
            )
        payload = complete.call_args.args[0]
        self.assertIn("partial_summaries", payload)
        self.assertNotIn("messages", payload)
        self.assertIn("人工定义", json.dumps(payload["context"], ensure_ascii=False))

    async def test_partial_chunks_reused_and_query_does_not_assign_auto_batch(self):
        records = rows(501)
        self.insert(records)
        fake = AsyncMock(side_effect=lambda msgs, *a, **kw: result(msgs[0]["id"]))
        with (
            patch.object(service, "summarize", fake),
            patch.object(service, "_complete", AsyncMock(return_value=(result(), {}))),
        ):
            await service.query_summary(
                1,
                records[0]["sent_at"],
                records[499]["sent_at"] + 1,
                "测试",
                db_path=self.path,
                cache_path=self.cache,
            )
            updated = await service.query_summary(
                1,
                records[0]["sent_at"],
                records[-1]["sent_at"] + 1,
                "测试",
                db_path=self.path,
                cache_path=self.cache,
            )
        self.assertEqual([len(call.args[0]) for call in fake.call_args_list], [500, 1])
        self.assertEqual(updated["cached_message_count"], 500)
        self.assertEqual(db.assigned_ids(1, 0, self.path), set())

    async def test_recall_or_window_boundary_invalidates_whole_batch(self):
        records = rows()
        self.insert(records)
        self.complete(records)
        fake = AsyncMock(return_value=result("2"))
        with patch.object(service, "summarize", fake):
            part = await service.query_summary(
                1,
                records[1]["sent_at"],
                records[-1]["sent_at"] + 1,
                "边界",
                db_path=self.path,
                cache_path=self.cache,
            )
            with sqlite3.connect(self.cache) as conn:
                conn.execute("UPDATE message SET recalled_at=1 WHERE message_id=1")
            await service.query_summary(
                1,
                records[0]["sent_at"],
                records[-1]["sent_at"] + 1,
                "撤回",
                db_path=self.path,
                cache_path=self.cache,
            )
        self.assertEqual(part["cached_message_count"], 0)
        self.assertEqual(len(fake.call_args.args[0]), 499)
        self.assertEqual(fake.await_count, 1)  # 同样的剩余499条直接复用片段缓存。


if __name__ == "__main__":
    unittest.main()
