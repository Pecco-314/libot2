"""登录后维护群聊术语和成员称呼；网页操作不调用模型。"""

from __future__ import annotations

import hmac
import json
import math
import re
import sqlite3
from datetime import datetime

from flask import Blueprint, abort, redirect, render_template, request, session, url_for

from src.common.chat_summary import TZ
from src.db import chat_context
from src.db import chat_summary as db
from src.common.qq_avatars import avatar_url


def create_chat_context_blueprint(
    login_required, csrf_token, db_path=db.DB_PATH, cache_path=None
):
    bp = Blueprint("chat_context", __name__)

    def check_csrf():
        expected = str(session.get("csrf_token") or "")
        supplied = request.form.get("csrf_token", "")
        if not expected or not hmac.compare_digest(expected, supplied):
            abort(403)

    @bp.app_template_filter("chat_time")
    def chat_time(timestamp):
        return (
            datetime.fromtimestamp(timestamp, TZ).strftime("%m-%d %H:%M")
            if timestamp
            else "—"
        )

    def evidence(item):
        info = json.loads(item["evidence"])
        ids = {str(mid) for mid in info.get("message_ids", [])}
        # 原文只读取现有24小时缓存，过期后仍保留批次和消息ID用于追溯。
        try:
            rows = chat_context.list_messages(
                item["group_id"],
                item["first_seen"] - 86400,
                int(datetime.now(TZ).timestamp()) + 1,
                cache_path,
            )
        except (OSError, sqlite3.OperationalError):
            rows = []
        result = []
        for row in rows:
            if str(row["message_id"]) not in ids:
                continue
            text = "".join(
                str(s.get("text", ""))
                for s in json.loads(row["segments"] or "[]")
                if s["type"] == "text"
            )
            result.append(
                {
                    "id": row["message_id"],
                    "name": row["nickname"],
                    "text": text,
                    "time": row["sent_at"],
                }
            )
        return {"rows": result, "batch_id": info.get("batch_id"), "ids": sorted(ids)}

    @bp.get("/chat-context")
    @login_required
    def page():
        db.init_db(db_path)
        view = request.args.get("view", "pending")
        if view not in {"pending", "glossary", "batches", "people"}:
            view = "pending"
        query = request.args.get("q", "").strip()[:200]
        page_number = max(1, request.args.get("page", 1, type=int))
        min_mentions = max(
            1, min(1000000, request.args.get("min_mentions", 1, type=int))
        )
        term = request.args.get("term", "").strip()[:100]
        group_id = request.args.get("group", 605275779, type=int)
        edit = db.get_definition(term, db_path) if term else None
        unknown = db.get_unknown(group_id, term, db_path) if term else None
        bindings = db.get_bindings(term, db_path) if term else []
        person_qq = request.args.get("qq", "").strip()
        person_edit = None
        if view == "people":
            people = chat_context.search_people(group_id, query, db_path, limit=100000)
            total = len(people)
            pages = max(1, math.ceil(total / 30))
            page_number = min(page_number, pages)
            pagination = {"items": people[(page_number - 1) * 30 : page_number * 30],
                          "total": total, "pages": pages, "page": page_number}
            if person_qq.isascii() and person_qq.isdigit():
                person_edit = chat_context.list_people(group_id, db_path).get(int(person_qq))
        else:
            pagination = (
                db.glossary_page(query, page_number, db_path)
                if view == "glossary"
                else db.unknown_page(query, page_number, db_path, min_mentions=min_mentions)
            )
        return render_template(
            "chat_context.html",
            view=view,
            query=query,
            pagination=pagination,
            min_mentions=min_mentions,
            batches=db.recent_batches(db_path),
            edit=edit,
            term=term,
            group_id=group_id,
            person_edit=person_edit,
            person_qq=person_qq,
            person_avatar_url=avatar_url,
            bindings=bindings,
            unknown=unknown,
            evidence=evidence(unknown) if unknown else None,
            revision=db.revision(db_path),
            csrf_token=csrf_token(),
            saved=request.args.get("saved") == "1",
            error=None,
        )

    @bp.post("/chat-context/save")
    @login_required
    def save():
        check_csrf()
        db.init_db(db_path)
        term = request.form.get("term", "").strip()
        try:
            qq_text = request.form.get("qq", "").strip()
            group_text = request.form.get("group_id", "").strip()
            canonical_name = request.form.get("canonical_name", "").strip()
            selected = request.form.get("selected_qq", "").strip()
            if selected:
                if not selected.isdigit() or not group_text.isdigit():
                    raise ValueError("请选择有效的本群成员")
                person = chat_context.list_people(int(group_text), db_path).get(
                    int(selected)
                )
                if not person:
                    raise ValueError("该群友不在当前群的已有记录中")
                qq_text = selected
                canonical_name = canonical_name or person["name"]
            if qq_text and (not qq_text.isdigit() or not group_text.isdigit()):
                raise ValueError("群号和QQ号需为正整数")
            db.save_definition(
                term,
                request.form.get("definition", ""),
                keep_original="keep_original" in request.form,
                needs_context="needs_context" in request.form,
                qq=int(qq_text) if qq_text else None,
                group_id=int(group_text) if qq_text else None,
                canonical_name=canonical_name,
                db_path=db_path,
            )
        except ValueError as exc:
            return str(exc), 400, {"Content-Type": "text/plain; charset=utf-8"}
        return redirect(
            url_for("chat_context.page", view="glossary", term=term,
                    group=int(group_text) if group_text.isdigit() else 605275779, saved="1"),
            code=303,
        )

    @bp.post("/chat-context/people/save")
    @login_required
    def save_person():
        check_csrf()
        db.init_db(db_path)
        try:
            group = request.form.get("group_id", "").strip()
            qq = request.form.get("qq", "").strip()
            if not re.fullmatch(r"[0-9]{1,19}", group) or not re.fullmatch(r"[0-9]{1,19}", qq):
                raise ValueError("群号和QQ号需为正整数")
            db.save_person(
                int(group), int(qq), request.form.get("canonical_name", ""),
                re.split(r"[\r\n]+", request.form.get("aliases", "")), db_path,
            )
        except ValueError as exc:
            return str(exc), 400, {"Content-Type": "text/plain; charset=utf-8"}
        return redirect(url_for("chat_context.page", view="people", group=int(group),
                                qq=qq, saved="1"), code=303)

    @bp.get("/chat-context/people")
    @login_required
    def people():
        db.init_db(db_path)
        group_id = request.args.get("group", 605275779, type=int)
        if not group_id or group_id <= 0:
            abort(400)
        matches = chat_context.search_people(
            group_id, request.args.get("q", "")[:100], db_path
        )
        return {
            "people": [
                {
                    "qq": person["qq"],
                    "name": person["name"],
                    "aliases": person["aliases"][:8],
                    "avatar": avatar_url(person["qq"]),
                }
                for person in matches
            ]
        }

    @bp.post("/chat-context/dismiss")
    @login_required
    def dismiss():
        check_csrf()
        db.init_db(db_path)
        group_id = request.form.get("group_id", type=int)
        term = request.form.get("term", "").strip()[:100]
        if not group_id or not term:
            abort(400)
        db.dismiss_term(group_id, term, db_path)
        return redirect(url_for("chat_context.page"), code=303)

    @bp.get("/chat-context/batches/<int:batch_id>")
    @login_required
    def batch_page(batch_id):
        db.init_db(db_path)
        batch = db.get_batch(batch_id, db_path)
        if not batch:
            abort(404)
        result = json.loads(batch["result"]) if batch["result"] else None
        return render_template("chat_summary_batch.html", batch=batch, result=result)

    return bp
