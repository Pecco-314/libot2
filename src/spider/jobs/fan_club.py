from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import logging
import math
import random
import time
from datetime import date, datetime, time as datetime_time
from pathlib import Path
from typing import Any, Sequence

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.common.bilibili_auth import build_bilibili_cookies
from src.db.fan_club import (
    begin_snapshot,
    completed_target_uids,
    create_or_resume_run,
    finish_run,
    init_fan_club_db,
    list_targets,
    save_complete_snapshot,
    save_failed_snapshot,
    sync_targets,
    run_status,
)
from src.db.sqlite import DEFAULT_DB_PATH


logger = logging.getLogger("spider.jobs.fan_club")

FAN_MEMBERS_URL = (
    "https://api.live.bilibili.com/xlive/general-interface/v1/rank/"
    "getFansMembersRank"
)
TARGETS_PATH = Path(__file__).resolve().parents[1] / "fan_club_targets.json"
PAGE_SIZE = 30
RISK_CODES = {-352, -412, -509}
TRANSIENT_CODES = {-500, -504}


class BilibiliApiError(RuntimeError):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"Bilibili code={code}: {message}")
        self.code = code


def load_targets(path: Path = TARGETS_PATH) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("fan-club target config must be a JSON list")
    return [dict(row) for row in payload]


class SlowFanClubClient:
    def __init__(
        self,
        *,
        interval: float = 1.0,
        jitter: float = 0.3,
        risk_pause: float = 900.0,
        retries: int = 4,
    ) -> None:
        self.interval = max(0.0, interval)
        self.jitter = max(0.0, jitter)
        self.risk_pause = max(1.0, risk_pause)
        self.retries = max(1, retries)
        self.request_count = 0
        self._next_request_at = 0.0
        self._client = httpx.AsyncClient(
            trust_env=False,
            cookies=build_bilibili_cookies(),
            timeout=httpx.Timeout(45.0),
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Origin": "https://live.bilibili.com",
                "Referer": "https://live.bilibili.com/",
            },
        )

    async def __aenter__(self) -> "SlowFanClubClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._client.aclose()

    async def _throttle(self) -> None:
        remaining = self._next_request_at - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(remaining)
        self._next_request_at = (
            time.monotonic()
            + self.interval
            + random.uniform(0.0, self.jitter)
        )

    async def fetch_page(
        self,
        streamer_uid: int,
        page: int,
        snapshot_ts: int,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            await self._throttle()
            self.request_count += 1
            try:
                response = await self._client.get(
                    FAN_MEMBERS_URL,
                    params={
                        "ruid": streamer_uid,
                        "page": page,
                        "page_size": PAGE_SIZE,
                        "rank_type": 0,
                        "ts": snapshot_ts,
                    },
                )
                if response.status_code in {412, 429}:
                    logger.warning(
                        "fan-club risk HTTP=%d pause=%.0fs",
                        response.status_code,
                        self.risk_pause,
                    )
                    await asyncio.sleep(self.risk_pause)
                    continue
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("response is not a JSON object")
                code = int(payload.get("code", -1))
                message = str(payload.get("message") or payload.get("msg") or "")
                if code in RISK_CODES:
                    logger.warning(
                        "fan-club risk code=%d pause=%.0fs", code, self.risk_pause
                    )
                    await asyncio.sleep(self.risk_pause)
                    continue
                if code in TRANSIENT_CODES and attempt < self.retries:
                    logger.warning(
                        "fan-club transient code=%d retry=%d/%d",
                        code,
                        attempt,
                        self.retries,
                    )
                    await asyncio.sleep(min(30.0, 3.0 * attempt))
                    continue
                if code != 0:
                    raise BilibiliApiError(code, message)
                return payload
            except BilibiliApiError:
                raise
            except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt < self.retries:
                    await asyncio.sleep(min(30.0, 3.0 * attempt))
        raise RuntimeError(f"request failed after {self.retries} attempts: {last_error}")


def _normalize_member(item: dict[str, Any], streamer_uid: int) -> dict[str, Any]:
    member_uid = int(item.get("uid") or 0)
    target_id = int(item.get("target_id") or 0)
    uname = str(item.get("name") or "").strip()
    level = int(item.get("level") or 0)
    user_rank = int(item.get("user_rank") or 0)
    if target_id != streamer_uid:
        raise ValueError(f"member target_id mismatch: {target_id} != {streamer_uid}")
    if member_uid <= 0 or not uname or level <= 0 or user_rank <= 0:
        raise ValueError(
            f"invalid member uid={member_uid} name={uname!r} "
            f"level={level} rank={user_rank}"
        )
    return {
        "uid": member_uid,
        "uname": uname,
        "level": level,
        "guard_level": int(item.get("guard_level") or 0),
        "user_rank": user_rank,
        "score": int(item.get("score") or 0),
    }


async def collect_one_target(
    client: SlowFanClubClient,
    *,
    run_id: int,
    target: dict[str, Any],
    db_path: Path,
) -> dict[str, int]:
    streamer_uid = int(target["uid"])
    name = str(target["full_name"])
    snapshot_ts = int(time.time() * 1000)
    snapshot_id = begin_snapshot(
        run_id, streamer_uid, snapshot_ts, db_path=db_path
    )
    request_start = client.request_count
    reported_count: int | None = None
    expected_pages: int | None = None
    fetched_pages = 0
    members_by_uid: dict[int, dict[str, Any]] = {}
    observed_counts: set[int] = set()

    try:
        first_payload = await client.fetch_page(streamer_uid, 1, snapshot_ts)
        first_data = first_payload.get("data") or {}
        if not isinstance(first_data, dict):
            raise ValueError("response data is not an object")
        reported_count = int(first_data.get("num") or 0)
        observed_counts.add(reported_count)
        medal_status = int(first_data.get("medal_status") or 0)
        if reported_count > 0 and not medal_status:
            raise ValueError("fan club reports members but medal_status is false")
        expected_pages = math.ceil(reported_count / PAGE_SIZE) if reported_count else 0

        async def consume_page(page: int, data: dict[str, Any]) -> None:
            nonlocal fetched_pages
            items = data.get("item") or []
            if not isinstance(items, list):
                raise ValueError(f"page {page} item is not a list")
            fetched_pages += 1
            for raw_item in items:
                if not isinstance(raw_item, dict):
                    raise ValueError(f"page {page} contains a non-object member")
                member = _normalize_member(raw_item, streamer_uid)
                member_uid = int(member["uid"])
                # 排名是实时变动的，即使固定 ts，跨页时也可能发生一位成员
                # 加入/离开并使边界成员重复。以 UID 合并当天实际观察到的集合。
                members_by_uid[member_uid] = member

            if page == 1 or page % 25 == 0 or page == expected_pages:
                logger.info(
                    "fan-club target=%s uid=%d page=%d/%d members=%d",
                    name,
                    streamer_uid,
                    page,
                    expected_pages,
                    len(members_by_uid),
                )

        if expected_pages:
            await consume_page(1, first_data)
        page = 2
        while page <= expected_pages:
            payload = await client.fetch_page(streamer_uid, page, snapshot_ts)
            data = payload.get("data") or {}
            if not isinstance(data, dict):
                raise ValueError(f"page {page} data is not an object")
            page_reported = int(data.get("num") or 0)
            observed_counts.add(page_reported)
            reported_count = page_reported
            expected_pages = max(
                expected_pages,
                math.ceil(page_reported / PAGE_SIZE) if page_reported else 0,
            )
            await consume_page(page, data)
            page += 1

        assert reported_count is not None
        largest_reported = max(observed_counts)
        # 接口不是原子快照，慢速翻页期间会有人加入/退出。只扫一遍并按
        # UID去重；只有差异异常大（超过10%）才认为返回内容损坏。
        tolerance = max(30, math.ceil(largest_reported * 0.10))
        if abs(len(members_by_uid) - reported_count) > tolerance:
            raise ValueError(
                f"member count differs too much: unique={len(members_by_uid)} "
                f"last_reported={reported_count} tolerance={tolerance}"
            )

        if len(members_by_uid) != reported_count:
            logger.warning(
                "fan-club target=%s non-atomic snapshot members=%d "
                "last_reported=%d observed_range=%d..%d",
                name,
                len(members_by_uid),
                reported_count,
                min(observed_counts),
                largest_reported,
            )

        members = sorted(
            members_by_uid.values(),
            key=lambda row: (int(row["user_rank"]), int(row["uid"])),
        )
        save_complete_snapshot(
            snapshot_id,
            reported_count=reported_count,
            expected_pages=expected_pages,
            fetched_pages=fetched_pages,
            request_count=client.request_count - request_start,
            members=members,
            observed_at=snapshot_ts // 1000,
            db_path=db_path,
        )
        return {"member_count": len(members), "page_count": expected_pages}
    except asyncio.CancelledError:
        save_failed_snapshot(
            snapshot_id,
            reported_count=reported_count,
            expected_pages=expected_pages,
            fetched_pages=fetched_pages,
            request_count=client.request_count - request_start,
            error="collection cancelled",
            db_path=db_path,
        )
        raise
    except Exception as exc:
        save_failed_snapshot(
            snapshot_id,
            reported_count=reported_count,
            expected_pages=expected_pages,
            fetched_pages=fetched_pages,
            request_count=client.request_count - request_start,
            error=str(exc),
            db_path=db_path,
        )
        raise


async def _collect_daily_fan_clubs_unlocked(
    *,
    snapshot_date: str | None = None,
    interval: float = 0.75,
    jitter: float = 0.15,
    risk_pause: float = 900.0,
    retries: int = 4,
    db_path: Path = DEFAULT_DB_PATH,
    only_uids: set[int] | None = None,
) -> dict[str, Any]:
    day = snapshot_date or date.today().isoformat()
    init_fan_club_db(db_path)
    sync_targets(load_targets(), db_path)
    run = create_or_resume_run(day, db_path)
    completed = completed_target_uids(int(run["id"]), db_path)
    targets = [
        target
        for target in list_targets(db_path)
        if int(target["uid"]) not in completed
        and (only_uids is None or int(target["uid"]) in only_uids)
    ]
    logger.info(
        "fan-club daily begin date=%s targets=%d already_complete=%d",
        day,
        len(targets),
        len(completed),
    )

    failures: list[str] = []
    async with SlowFanClubClient(
        interval=interval,
        jitter=jitter,
        risk_pause=risk_pause,
        retries=retries,
    ) as client:
        for index, target in enumerate(targets, start=1):
            uid = int(target["uid"])
            name = str(target["full_name"])
            try:
                result = await collect_one_target(
                    client,
                    run_id=int(run["id"]),
                    target=target,
                    db_path=db_path,
                )
                logger.info(
                    "fan-club [%d/%d] complete target=%s uid=%d members=%d pages=%d",
                    index,
                    len(targets),
                    name,
                    uid,
                    result["member_count"],
                    result["page_count"],
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures.append(f"{name}({uid}): {exc}")
                logger.exception(
                    "fan-club [%d/%d] failed target=%s uid=%d",
                    index,
                    len(targets),
                    name,
                    uid,
                )

        summary = finish_run(
            int(run["id"]),
            request_count=client.request_count,
            error="\n".join(failures) or None,
            db_path=db_path,
        )
        result = {
            "date": day,
            "run_id": int(run["id"]),
            "request_count": int(summary.get("request_count", client.request_count)),
            **summary,
        }
    logger.info("fan-club daily finished: %s", result)
    return result


async def collect_daily_fan_clubs(
    *,
    snapshot_date: str | None = None,
    interval: float = 0.75,
    jitter: float = 0.15,
    risk_pause: float = 900.0,
    retries: int = 4,
    db_path: Path = DEFAULT_DB_PATH,
    only_uids: set[int] | None = None,
) -> dict[str, Any]:
    lock_path = db_path.parent / ".fan_club_collection.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another fan-club collection is already running") from exc
        try:
            return await _collect_daily_fan_clubs_unlocked(
                snapshot_date=snapshot_date,
                interval=interval,
                jitter=jitter,
                risk_pause=risk_pause,
                retries=retries,
                db_path=db_path,
                only_uids=only_uids,
            )
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


async def run_scheduled_collection() -> None:
    try:
        await collect_daily_fan_clubs()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("fan-club scheduled collection failed")


def register_jobs(scheduler: AsyncIOScheduler) -> None:
    init_fan_club_db()
    sync_targets(load_targets())
    scheduler.add_job(
        run_scheduled_collection,
        CronTrigger(hour=6, minute=7, second=0),
        id="fan_club_daily",
        name="fan_club_daily",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    # 18:07只补抓当天失败项；已成功的主播会被断点逻辑直接跳过。
    scheduler.add_job(
        run_scheduled_collection,
        CronTrigger(hour=18, minute=7, second=0),
        id="fan_club_daily_retry",
        name="fan_club_daily_retry",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    # spider若在06:07之后才启动，补上当天遗漏或中断的任务。
    now = datetime.now()
    today_status = run_status(now.date().isoformat())
    if now.time() >= datetime_time(hour=6, minute=7) and (
        today_status is None or today_status["status"] != "complete"
    ):
        scheduler.add_job(
            run_scheduled_collection,
            id="fan_club_startup_catchup",
            name="fan_club_startup_catchup",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect daily fan-club snapshots")
    parser.add_argument("--run-now", action="store_true")
    parser.add_argument("--date", dest="snapshot_date")
    parser.add_argument("--interval", type=float, default=0.75)
    parser.add_argument("--jitter", type=float, default=0.15)
    parser.add_argument("--risk-pause", type=float, default=900.0)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--only-uid", type=int, action="append", default=[])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if not args.run_now:
        raise SystemExit("pass --run-now to start a collection")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    result = asyncio.run(
        collect_daily_fan_clubs(
            snapshot_date=args.snapshot_date,
            interval=args.interval,
            jitter=args.jitter,
            risk_pause=args.risk_pause,
            retries=args.retries,
            db_path=args.db,
            only_uids=set(args.only_uid) or None,
        )
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["failure_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
