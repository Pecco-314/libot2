#!/usr/bin/env python3
"""Backfill Mit3uri dynamics, localize images, and rebuild activity cleanly."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.common.activity_assets import (  # noqa: E402
    ActivityAssetLocalizer,
    DEFAULT_ASSET_DIR,
)
from src.common.bilibili_auth import build_bilibili_cookies  # noqa: E402
from src.db.activity import init_activity_db  # noqa: E402


UID = 2030198123
ROOM_ID = 1967216004
DEFAULT_DB = ROOT / "data" / "libot.db"
DEFAULT_RAW = ROOT / "data" / "activity_history" / str(UID) / "items.json"
DEFAULT_RECOVERED_ASSETS = (
    ROOT / "data" / "activity_history" / str(UID) / "recovered_assets.json"
)
DEFAULT_BACKUP_DIR = ROOT / "data" / "backups"
SPACE_FEED_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
FEATURES = (
    "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote,"
    "forwardListHidden,decorationCard,commentsNewVersion,"
    "onlyfansAssetsV2,ugcDelete,onlyfansQaCard,avatarAutoTheme,"
    "sunflowerStyle,cardsEnhance,eva3CardOpus,eva3CardVideo,"
    "eva3CardComment,eva3CardUser"
)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": f"https://space.bilibili.com/{UID}/dynamic",
    "Origin": "https://space.bilibili.com",
    "Accept": "application/json, text/plain, */*",
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uid", type=int, default=UID)
    parser.add_argument("--room-id", type=int, default=ROOM_ID)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--raw-output", type=Path, default=DEFAULT_RAW)
    parser.add_argument(
        "--recovered-assets",
        type=Path,
        default=DEFAULT_RECOVERED_ASSETS,
        help="CDN 原图失效时，从历史成图恢复的资源清单",
    )
    parser.add_argument("--asset-dir", type=Path, default=DEFAULT_ASSET_DIR)
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    parser.add_argument(
        "--since",
        default="2025-03-01T00:00:00+08:00",
        help="只导入该时间之后的动态（ISO 8601）",
    )
    parser.add_argument("--request-interval", type=float, default=1.0)
    parser.add_argument(
        "--risk-control-wait",
        type=float,
        default=180.0,
        help="遇到 Bilibili -352 风控时的首次等待秒数（后续线性增加）",
    )
    parser.add_argument(
        "--risk-control-retries",
        type=int,
        default=3,
        help="单页遇到 Bilibili -352 时的重试次数",
    )
    parser.add_argument(
        "--restart-fetch",
        action="store_true",
        help="忽略未完成的抓取检查点，从第一页重新抓取",
    )
    parser.add_argument("--download-concurrency", type=int, default=4)
    parser.add_argument("--max-pages", type=int, default=200)
    parser.add_argument(
        "--fetch-only",
        action="store_true",
        help="只抓取原始动态 JSON，不下载图片或修改数据库",
    )
    parser.add_argument(
        "--import-only",
        action="store_true",
        help="使用已有原始 JSON 下载图片并整理数据库",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="完成抓取和本地化，但不整理数据库",
    )
    return parser.parse_args()


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _read_json_dict(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _author(item: dict[str, Any]) -> dict[str, Any]:
    return (
        (item.get("modules") or {}).get("module_author") or {}
        if isinstance(item, dict)
        else {}
    )


def _item_timestamp(item: dict[str, Any]) -> int:
    try:
        return int(_author(item).get("pub_ts") or 0)
    except (TypeError, ValueError):
        return 0


def _item_uid(item: dict[str, Any]) -> int:
    try:
        return int(_author(item).get("mid") or 0)
    except (TypeError, ValueError):
        return 0


async def _request_page(
    client: httpx.AsyncClient,
    *,
    uid: int,
    offset: str,
    risk_control_wait: float,
    risk_control_retries: int,
) -> dict[str, Any]:
    params = {
        "host_mid": uid,
        "features": FEATURES,
    }
    if offset:
        params["offset"] = offset
    last_error: Exception | None = None
    transient_attempt = 0
    risk_control_attempt = 0
    while True:
        try:
            response = await client.get(SPACE_FEED_URL, params=params)
            response.raise_for_status()
            payload = response.json()
            code = int(payload.get("code", -1))
            if code == -352:
                if risk_control_attempt >= risk_control_retries:
                    raise RuntimeError("Bilibili code=-352: risk control")
                risk_control_attempt += 1
                delay = max(0.0, risk_control_wait) * risk_control_attempt
                print(
                    "Bilibili risk control (-352), "
                    f"waiting {delay:.0f}s before retry "
                    f"({risk_control_attempt}/{risk_control_retries})",
                    flush=True,
                )
                await asyncio.sleep(delay)
                continue
            if code != 0:
                raise RuntimeError(
                    f"Bilibili code={code}: "
                    f"{payload.get('message') or payload.get('msg')}"
                )
            data = payload.get("data")
            if not isinstance(data, dict):
                raise RuntimeError("Bilibili response has no data object")
            return data
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_error = exc
            if "Bilibili code=-352" in str(exc):
                break
            if transient_attempt >= 5:
                break
            await asyncio.sleep(min(30.0, 2.0**transient_attempt))
            transient_attempt += 1
    raise RuntimeError(f"dynamic request failed: {last_error}")


async def fetch_all_items(
    *,
    uid: int,
    output: Path,
    request_interval: float,
    max_pages: int,
    risk_control_wait: float,
    risk_control_retries: int,
    restart_fetch: bool,
) -> dict[str, Any]:
    cookies = build_bilibili_cookies()
    items_by_id: dict[str, dict[str, Any]] = {}
    offsets_seen: set[str] = set()
    offset = ""
    first_page = 1
    complete = False

    if output.exists() and not restart_fetch:
        checkpoint = json.loads(output.read_text(encoding="utf-8"))
        if int(checkpoint.get("uid") or 0) != uid:
            raise ValueError(f"{output} UID does not match {uid}")
        checkpoint_items = checkpoint.get("items")
        if not isinstance(checkpoint_items, list):
            raise ValueError(f"{output} checkpoint has no items list")
        if checkpoint.get("complete"):
            print(
                f"using complete checkpoint: pages={checkpoint.get('pages')} "
                f"items={len(checkpoint_items)}",
                flush=True,
            )
            return checkpoint
        offset = str(checkpoint.get("next_offset") or "")
        if not offset:
            raise ValueError(f"{output} incomplete checkpoint has no next_offset")
        for item in checkpoint_items:
            if not isinstance(item, dict):
                continue
            activity_id = str(item.get("id_str") or "")
            if activity_id and _item_uid(item) == uid:
                items_by_id[activity_id] = item
        first_page = int(checkpoint.get("pages") or 0) + 1
        print(
            f"resuming checkpoint at page={first_page} "
            f"unique_target_items={len(items_by_id)}",
            flush=True,
        )

    async with httpx.AsyncClient(
        timeout=30.0,
        follow_redirects=True,
        headers=HEADERS,
        cookies=cookies,
        trust_env=False,
    ) as client:
        for page in range(first_page, max_pages + 1):
            data = await _request_page(
                client,
                uid=uid,
                offset=offset,
                risk_control_wait=risk_control_wait,
                risk_control_retries=risk_control_retries,
            )
            page_items = data.get("items") or []
            if not isinstance(page_items, list):
                raise RuntimeError(f"page {page}: items is not a list")
            for item in page_items:
                if not isinstance(item, dict):
                    continue
                activity_id = str(item.get("id_str") or "")
                if activity_id and _item_uid(item) == uid:
                    items_by_id[activity_id] = item

            next_offset = str(data.get("offset") or "")
            has_more = bool(data.get("has_more"))
            checkpoint = {
                "uid": uid,
                "fetched_at": datetime.now().astimezone().isoformat(),
                "complete": not has_more,
                "pages": page,
                "next_offset": next_offset,
                "items": sorted(
                    items_by_id.values(),
                    key=lambda item: (
                        _item_timestamp(item),
                        str(item.get("id_str") or ""),
                    ),
                ),
            }
            _write_json_atomic(output, checkpoint)
            print(
                f"page={page} page_items={len(page_items)} "
                f"unique_target_items={len(items_by_id)} has_more={has_more}",
                flush=True,
            )
            if not has_more:
                complete = True
                break
            if not next_offset or next_offset == offset or next_offset in offsets_seen:
                raise RuntimeError(
                    f"pagination offset loop at page {page}: {next_offset!r}"
                )
            offsets_seen.add(next_offset)
            offset = next_offset
            await asyncio.sleep(max(0.0, request_interval))

    if not complete:
        raise RuntimeError(f"dynamic pagination exceeded {max_pages} pages")
    payload = json.loads(output.read_text(encoding="utf-8"))
    if not payload.get("complete"):
        raise RuntimeError("raw checkpoint is incomplete")
    return payload


def load_raw_payload(path: Path, uid: int) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if int(payload.get("uid") or 0) != uid:
        raise ValueError(f"{path} UID does not match {uid}")
    if not payload.get("complete"):
        raise ValueError(f"{path} is an incomplete checkpoint")
    if not isinstance(payload.get("items"), list):
        raise ValueError(f"{path} has no items list")
    return payload


def load_recovered_assets(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    assets = payload.get("assets")
    if not isinstance(assets, list):
        raise ValueError(f"{path} has no assets list")
    result: list[dict[str, Any]] = []
    for asset in assets:
        if not isinstance(asset, dict):
            raise ValueError(f"{path} contains a non-object asset")
        required = {
            "remote_url",
            "local_path",
            "content_sha256",
            "size_bytes",
        }
        if not required.issubset(asset):
            raise ValueError(f"{path} contains an incomplete asset entry")
        result.append(asset)
    return result


def _load_existing_rows(db_path: Path) -> list[dict[str, Any]]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in conn.execute(
                """
                SELECT id, activity_id, room_id, uid, uname, timestamp,
                       item, dy_type_str, item_remote, assets_localized,
                       created_at
                FROM activity
                ORDER BY timestamp, activity_id
                """
            )
        ]


def _load_existing_assets(db_path: Path) -> list[dict[str, Any]]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in conn.execute(
                """
                SELECT remote_url, local_path, content_sha256,
                       content_type, size_bytes
                FROM activity_asset
                """
            )
        ]


def _merge_rows(
    *,
    fetched_items: list[dict[str, Any]],
    existing_rows: list[dict[str, Any]],
    uid: int,
    room_id: int,
    since_timestamp: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    existing_target = {
        str(row["activity_id"]): row
        for row in existing_rows
        if int(row["uid"]) == uid and int(row["timestamp"]) >= since_timestamp
    }
    fetched = {
        str(item.get("id_str") or ""): item
        for item in fetched_items
        if isinstance(item, dict)
        and str(item.get("id_str") or "")
        and _item_uid(item) == uid
        and _item_timestamp(item) >= since_timestamp
    }

    result: list[dict[str, Any]] = []
    for activity_id in sorted(
        set(existing_target) | set(fetched),
        key=lambda value: (
            _item_timestamp(fetched[value])
            if value in fetched
            else int(existing_target[value]["timestamp"]),
            value,
        ),
    ):
        old = existing_target.get(activity_id)
        item = fetched.get(activity_id)
        if item is not None:
            author = _author(item)
            timestamp = _item_timestamp(item)
            result.append(
                {
                    "activity_id": activity_id,
                    "room_id": room_id,
                    "uid": uid,
                    "uname": str(author.get("name") or "三理Mit3uri"),
                    "timestamp": timestamp,
                    "item_remote_dict": item,
                    "dy_type_str": str(item.get("type") or ""),
                    "created_at": (old or {}).get("created_at"),
                }
            )
            continue

        assert old is not None
        remote_item = _read_json_dict(old.get("item_remote"))
        if remote_item is None:
            remote_item = _read_json_dict(old.get("item"))
        result.append(
            {
                "activity_id": activity_id,
                "room_id": room_id,
                "uid": uid,
                "uname": str(old.get("uname") or "三理Mit3uri"),
                "timestamp": int(old["timestamp"]),
                "item_remote_dict": remote_item,
                "dy_type_str": str(old.get("dy_type_str") or ""),
                "created_at": old.get("created_at"),
            }
        )

    return result, {
        "existing_total": len(existing_rows),
        "existing_target": len(existing_target),
        "removed_non_target_or_pre_debut": len(existing_rows) - len(existing_target),
        "fetched_since_debut": len(fetched),
        "preserved_not_in_api": len(set(existing_target) - set(fetched)),
        "merged_total": len(result),
    }


async def _localize_rows(
    rows: list[dict[str, Any]],
    *,
    asset_dir: Path,
    concurrency: int,
    known_assets: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    result: list[dict[str, Any]] = []
    all_assets: list[dict[str, Any]] = []
    incomplete = 0
    async with ActivityAssetLocalizer(
        asset_dir,
        concurrency=concurrency,
    ) as localizer:
        seeded = localizer.seed_cache(known_assets)
        if known_assets:
            print(
                f"seeded_asset_cache={seeded}/{len(known_assets)}",
                flush=True,
            )
        for index, row in enumerate(rows, 1):
            assets_by_url: dict[str, dict[str, Any]] = {}
            item_remote = row["item_remote_dict"]
            if item_remote is not None:
                item_local, item_assets, item_complete = await localizer.localize(
                    item_remote
                )
                assets_by_url.update(
                    {str(asset["remote_url"]): asset for asset in item_assets}
                )
            else:
                item_local, item_complete = None, True

            fully_localized = item_complete
            if not fully_localized:
                incomplete += 1
            localized_row = {
                **row,
                "item_local_dict": item_local,
                "assets_localized": int(fully_localized),
            }
            result.append(localized_row)
            for asset in assets_by_url.values():
                all_assets.append({"activity_id": row["activity_id"], **asset})
            if index % 20 == 0 or index == len(rows):
                print(
                    f"localized={index}/{len(rows)} "
                    f"asset_links={len(all_assets)} incomplete={incomplete}",
                    flush=True,
                )
    return result, all_assets, incomplete


def _backup_activity(
    db_path: Path,
    backup_dir: Path,
    rows: list[dict[str, Any]],
) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    path = backup_dir / f"activity-before-history-{stamp}.json"
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        assets = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM activity_asset ORDER BY activity_id, remote_url"
            )
        ]
        state = conn.execute(
            "SELECT value FROM state WHERE key = 'last_activity_id'"
        ).fetchone()
    _write_json_atomic(
        path,
        {
            "backed_up_at": datetime.now().astimezone().isoformat(),
            "db_path": str(db_path),
            "last_activity_id": state[0] if state else None,
            "activity": rows,
            "activity_asset": assets,
        },
    )
    return path


def _import_rows(
    db_path: Path,
    rows: list[dict[str, Any]],
    assets: list[dict[str, Any]],
    expected_activity_ids: set[str],
) -> None:
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("BEGIN IMMEDIATE")
        try:
            current_activity_ids = {
                str(row[0])
                for row in conn.execute("SELECT activity_id FROM activity")
            }
            if current_activity_ids != expected_activity_ids:
                added = len(current_activity_ids - expected_activity_ids)
                removed = len(expected_activity_ids - current_activity_ids)
                raise RuntimeError(
                    "activity changed during import preparation; "
                    f"added={added} removed={removed}"
                )
            conn.execute("DELETE FROM activity_asset")
            conn.execute("DELETE FROM activity")
            conn.execute("DELETE FROM sqlite_sequence WHERE name = 'activity'")
            conn.executemany(
                """
                INSERT INTO activity (
                    id, activity_id, room_id, uid, uname, timestamp,
                    item, dy_type_str, item_remote, assets_localized, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        index,
                        row["activity_id"],
                        row["room_id"],
                        row["uid"],
                        row["uname"],
                        row["timestamp"],
                        (
                            json.dumps(
                                row["item_local_dict"],
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                            if row["item_local_dict"] is not None
                            else None
                        ),
                        row["dy_type_str"],
                        (
                            json.dumps(
                                row["item_remote_dict"],
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                            if row["item_remote_dict"] is not None
                            else None
                        ),
                        row["assets_localized"],
                        row["created_at"],
                    )
                    for index, row in enumerate(rows, 1)
                ],
            )
            conn.executemany(
                """
                INSERT INTO activity_asset (
                    activity_id, remote_url, local_path, content_sha256,
                    content_type, size_bytes
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        asset["activity_id"],
                        asset["remote_url"],
                        asset["local_path"],
                        asset["content_sha256"],
                        asset.get("content_type") or "",
                        asset["size_bytes"],
                    )
                    for asset in assets
                ],
            )
            max_id = len(rows)
            conn.execute(
                """
                INSERT INTO state(key, value)
                VALUES ('last_activity_id', ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (str(max_id),),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


async def async_main(args: argparse.Namespace) -> int:
    if args.fetch_only and args.import_only:
        raise ValueError("--fetch-only and --import-only are mutually exclusive")
    since_timestamp = int(datetime.fromisoformat(args.since).timestamp())
    raw_path = args.raw_output.resolve()
    if args.import_only:
        raw_payload = load_raw_payload(raw_path, args.uid)
    else:
        raw_payload = await fetch_all_items(
            uid=args.uid,
            output=raw_path,
            request_interval=args.request_interval,
            max_pages=args.max_pages,
            risk_control_wait=args.risk_control_wait,
            risk_control_retries=args.risk_control_retries,
            restart_fetch=args.restart_fetch,
        )
    if args.fetch_only:
        print(
            json.dumps(
                {
                    "raw_output": str(raw_path),
                    "items": len(raw_payload["items"]),
                    "complete": raw_payload["complete"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    db_path = args.db.resolve()
    init_activity_db(db_path)
    existing_rows = _load_existing_rows(db_path)
    existing_assets = _load_existing_assets(db_path)
    recovered_assets = load_recovered_assets(args.recovered_assets.resolve())
    merged_rows, merge_stats = _merge_rows(
        fetched_items=raw_payload["items"],
        existing_rows=existing_rows,
        uid=args.uid,
        room_id=args.room_id,
        since_timestamp=since_timestamp,
    )
    localized_rows, assets, incomplete = await _localize_rows(
        merged_rows,
        asset_dir=args.asset_dir.resolve(),
        concurrency=args.download_concurrency,
        known_assets=[*existing_assets, *recovered_assets],
    )
    backup_path: Path | None = None
    if not args.dry_run:
        backup_path = _backup_activity(
            db_path,
            args.backup_dir.resolve(),
            existing_rows,
        )
        _import_rows(
            db_path,
            localized_rows,
            assets,
            {str(row["activity_id"]) for row in existing_rows},
        )

    print(
        json.dumps(
            {
                **merge_stats,
                "asset_links": len(assets),
                "unique_asset_files": len(
                    {str(asset["content_sha256"]) for asset in assets}
                ),
                "incomplete_asset_rows": incomplete,
                "recovered_asset_entries": len(recovered_assets),
                "raw_output": str(raw_path),
                "backup": str(backup_path) if backup_path else None,
                "database_updated": not args.dry_run,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def main() -> int:
    args = _parse_args()
    started = time.monotonic()
    result = asyncio.run(async_main(args))
    print(f"elapsed_seconds={time.monotonic() - started:.1f}")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
