import json
import tempfile
import unittest
from functools import wraps
from pathlib import Path

from flask import Flask, abort, session

from src.db import chat_summary as db
from src.db import chat_context
from src.web.chat_context import create_chat_context_blueprint
from src.common.chat_summary import prepare_messages


class ChatContextWebTests(unittest.TestCase):
    def test_manual_person_alias_does_not_require_detected_term_or_existing_person(self):
        form = {"group_id": "1", "qq": "12345", "canonical_name": "行测哥",
                "aliases": "看到我去刷行测，别jb水群了\n刷题哥", "csrf_token": "test"}
        self.assertEqual(self.client.post('/chat-context/people/save', data=form).status_code, 401)
        self.login()
        self.assertEqual(self.client.post('/chat-context/people/save', data={**form, 'csrf_token': ''}).status_code, 403)
        rev = db.revision(self.path)
        response = self.client.post('/chat-context/people/save', data=form)
        self.assertEqual(response.status_code, 303)
        self.assertIn('group=1', response.location)
        person = chat_context.list_people(1, self.path)[12345]
        self.assertEqual(person['name'], '行测哥')
        self.assertIn('刷题哥', person['confirmed_aliases'])
        self.assertIn('看到我去刷行测，别jb水群了', person['confirmed_aliases'])
        self.assertEqual(db.revision(self.path), rev + 1)
        self.assertEqual(chat_context.list_people(2, self.path), {})
        page = self.client.get('/chat-context?view=people&group=1&qq=12345')
        self.assertEqual(page.status_code, 200)
        self.assertIn('修改群友代号', page.get_data(as_text=True))
        self.assertIn('q1.qlogo.cn', page.get_data(as_text=True))
        self.assertIn('行测哥', self.client.get('/chat-context?view=people&group=1&q=12345').get_data(as_text=True))
        self.client.post('/chat-context/people/save', data={**form, 'aliases': '行测达人'})
        rows = [{'message_id': 1, 'user_id': 12345, 'nickname': '新的超长群昵称', 'sent_at': 100,
                 'segments': json.dumps([{'type': 'text', 'text': '最近在刷行测'}])}]
        chat_context.observe_names(1, rows, self.path)
        people = chat_context.list_people(1, self.path)
        self.assertEqual(people[12345]['name'], '行测哥')
        self.assertIn('刷题哥', people[12345]['confirmed_aliases'])
        self.assertIn('行测达人', people[12345]['confirmed_aliases'])
        messages, identities = prepare_messages(rows, people)
        self.assertEqual(messages[0]['name'], '行测哥')
        self.assertIn('刷题哥', identities[messages[0]['user']]['confirmed_aliases'])

    def test_manual_person_validation_and_escaping(self):
        self.login()
        form = {'group_id': '1', 'qq': '12345', 'canonical_name': '正常主名', 'csrf_token': 'test'}
        for bad in ({'qq': '0'}, {'group_id': '-1'}, {'qq': str(2**63)},
                    {'canonical_name': ''}, {'canonical_name': '长' * 101}, {'aliases': '词' * 101}):
            self.assertEqual(self.client.post('/chat-context/people/save', data={**form, **bad}).status_code, 400)
        self.assertEqual(self.client.post('/chat-context/people/save', data={**form, 'canonical_name': '<script>bad</script>'}).status_code, 303)
        page = self.client.get('/chat-context?view=people&group=1').get_data(as_text=True)
        self.assertIn('&lt;script&gt;', page)
        self.assertNotIn('<script>bad</script>', page)

    def test_existing_people_search_and_binding_are_group_scoped(self):
        self.assertEqual(
            self.client.get("/chat-context/people?group=1").status_code, 401
        )
        self.login()
        chat_context.set_aliases(1, 12345, "夕羽乌", ["xyw"], db_path=self.path)
        chat_context.set_aliases(2, 23456, "外群群友", ["xyw"], db_path=self.path)
        response = self.client.get("/chat-context/people?group=1&q=XYW")
        self.assertEqual(response.status_code, 200)
        people = response.json["people"]
        self.assertEqual([person["qq"] for person in people], ["12345"])
        self.assertEqual(people[0]["name"], "夕羽乌")
        self.assertIn("q1.qlogo.cn", people[0]["avatar"])
        form = {
            "term": "幸运乌",
            "definition": "夕羽乌的粉丝牌",
            "group_id": "1",
            "selected_qq": "12345",
            "csrf_token": "test",
        }
        self.assertEqual(
            self.client.post("/chat-context/save", data=form).status_code, 303
        )
        self.assertEqual(db.get_bindings("幸运乌", self.path)[0]["qq"], 12345)
        form["selected_qq"] = "23456"
        self.assertEqual(
            self.client.post("/chat-context/save", data=form).status_code, 400
        )

    def test_pending_sorted_by_message_mentions_and_repeat_filter(self):
        self.login()
        records = [
            {
                "message_id": i,
                "sent_at": 100 + i,
                "segments": json.dumps([{"type": "text", "text": text}]),
            }
            for i, text in enumerate(["高频词", "高频词", "高频词", "单次词"], 1)
        ]
        db.record_unknown_terms(
            1,
            {
                "terms_to_check": [
                    {"term": term, "possible_meaning": "不知道", "message_ids": []}
                    for term in ("单次词", "高频词")
                ]
            },
            db_path=self.path,
            rows=records,
        )
        page = db.unknown_page(db_path=self.path)
        self.assertEqual([r["term"] for r in page["items"]], ["高频词", "单次词"])
        response = self.client.get("/chat-context?min_mentions=2")
        self.assertEqual(response.status_code, 200)
        self.assertIn("高频词", response.get_data(as_text=True))
        self.assertNotIn("单次词", response.get_data(as_text=True))
        self.assertIn("已记录提及 3 条", response.get_data(as_text=True))
        self.assertEqual(db.unknown_page(db_path=self.path, min_mentions=2)["total"], 1)

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "context.db"
        app = Flask(
            __name__,
            template_folder=str(
                Path(__file__).resolve().parents[1] / "src/web/templates"
            ),
        )
        app.secret_key = "test-only"

        def auth(fn):
            @wraps(fn)
            def wrapped(*args, **kwargs):
                if not session.get("logged_in"):
                    abort(401)
                return fn(*args, **kwargs)

            return wrapped

        app.register_blueprint(
            create_chat_context_blueprint(auth, lambda: "test", self.path)
        )
        self.client = app.test_client()
        db.init_db(self.path)

    def tearDown(self):
        self.folder.cleanup()

    def login(self):
        with self.client.session_transaction() as sess:
            sess.update(logged_in=True, csrf_token="test")

    def test_auth_csrf_edit_and_escaping(self):
        self.assertEqual(self.client.get("/chat-context").status_code, 401)
        self.assertEqual(self.client.post("/chat-context/save").status_code, 401)
        self.login()
        payload = {
            "term": "新词",
            "definition": "<script>alert(1)</script>",
            "group_id": "1",
            "qq": "12345",
            "canonical_name": "主名",
        }
        self.assertEqual(
            self.client.post("/chat-context/save", data=payload).status_code, 403
        )
        payload["csrf_token"] = "test"
        self.assertEqual(
            self.client.post("/chat-context/save", data=payload).status_code, 303
        )
        response = self.client.get("/chat-context?view=glossary&term=新词")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"&lt;script&gt;", response.data)
        self.assertNotIn(b"<script>alert", response.data)
        payload["definition"] = "正确解释"
        self.assertEqual(
            self.client.post("/chat-context/save", data=payload).status_code, 303
        )
        self.assertEqual(db.get_definition("新词", self.path)["definition"], "正确解释")
        self.assertEqual(
            self.client.post(
                "/chat-context/save", data={**payload, "qq": "-1"}
            ).status_code,
            400,
        )

    def test_unknown_search_pagination_dismiss_and_resolution(self):
        self.login()
        for i in range(35):
            db.record_unknown_terms(
                1,
                {
                    "terms_to_check": [
                        {
                            "term": f"测试{i:02}",
                            "possible_meaning": "不确定",
                            "message_ids": ["1"],
                        }
                    ]
                },
                db_path=self.path,
            )
        self.assertEqual(db.unknown_page("测试", db_path=self.path)["pages"], 2)
        page = self.client.get("/chat-context?q=测试&page=2")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"2 / 2", page.data)
        self.assertEqual(
            self.client.post(
                "/chat-context/dismiss", data={"term": "测试00", "group_id": "1"}
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/chat-context/dismiss",
                data={"term": "测试00", "group_id": "1", "csrf_token": "test"},
            ).status_code,
            303,
        )
        self.assertEqual(db.get_unknown(1, "测试00", self.path)["status"], "ignored")
        self.assertEqual(
            self.client.get("/chat-context?term=测试01&group=1").status_code, 200
        )
        self.client.post(
            "/chat-context/save",
            data={"term": "测试01", "definition": "确认解释", "csrf_token": "test"},
        )
        self.assertEqual(db.get_unknown(1, "测试01", self.path)["status"], "resolved")
        self.assertEqual(self.client.get("/chat-context?view=batches").status_code, 200)
        self.assertEqual(self.client.get("/chat-context/batches/999").status_code, 404)


if __name__ == "__main__":
    unittest.main()
