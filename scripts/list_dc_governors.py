"""List 2026 Douchong streamers with their accumulated governor count.

``guard_3`` is a monthly count of 总督 purchases/renewals.  This script sums
that count across the requested months and orders every listed streamer by
the same summed revenue used by the /斗虫 yearly query.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
SCOPE_URLS = {
    "vr": ("https://vr.qianqiuzy.cn",),
    "psp": ("https://psp.qianqiuzy.cn",),
    "all": ("https://vr.qianqiuzy.cn", "https://psp.qianqiuzy.cn"),
}


def months_for_year(year: int) -> list[str]:
    now = datetime.now()
    end_month = 12 if year < now.year else now.month
    return [f"{year}{month:02d}" for month in range(1, end_month + 1)]


def revenue(anchor: dict[str, Any]) -> float:
    return sum(float(anchor.get(key) or 0) for key in ("gift", "guard", "super_chat"))


def short_name(name: str) -> str:
    """Use the Chinese/Japanese display-name prefix for a compact text list."""
    prefix = re.split(r"[_A-Za-z]", name, maxsplit=1)[0].rstrip("_- ")
    return prefix or name


async def fetch_month(month: str, scope: str) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(trust_env=False, timeout=30) as client:
        responses = await asyncio.gather(
            *(client.get(f"{base}/gift/by_month", params={"month": month}) for base in SCOPE_URLS[scope])
        )
    by_room: dict[int, dict[str, Any]] = {}
    for response in responses:
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError(f"{month} 斗虫接口响应不是数组")
        for anchor in payload:
            if not isinstance(anchor, dict) or anchor.get("month") != month:
                continue
            room_id = int(anchor.get("room_id") or 0)
            if not room_id:
                continue
            previous = by_room.get(room_id)
            if previous is None or revenue(anchor) > revenue(previous):
                by_room[room_id] = anchor
    return list(by_room.values())


async def load_month(month: str, scope: str, cache_dir: Path, refresh: bool) -> list[dict[str, Any]]:
    path = cache_dir / f"{scope}_{month}.json"
    if path.exists() and not refresh:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            return payload
    payload = await fetch_month(month, scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


async def build(year: int, scope: str, cache_dir: Path, refresh: bool) -> list[dict[str, Any]]:
    rows: dict[int, dict[str, Any]] = {}
    for month in months_for_year(year):
        for anchor in await load_month(month, scope, cache_dir, refresh):
            room_id = int(anchor["room_id"])
            item = rows.setdefault(room_id, {
                "room_id": room_id,
                "anchor_name": str(anchor.get("anchor_name") or room_id),
                "total_revenue": 0.0,
                "governor_count": 0,
            })
            item["anchor_name"] = str(anchor.get("anchor_name") or item["anchor_name"])
            item["total_revenue"] += revenue(anchor)
            item["governor_count"] += int(anchor.get("guard_3") or 0)
    result = sorted(rows.values(), key=lambda row: (-row["total_revenue"], row["room_id"]))
    for rank, row in enumerate(result, 1):
        row["rank"] = rank
    return result


def write_outputs(rows: list[dict[str, Any]], output_dir: Path, year: int) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / f"douchong_{year}_governors.csv"
    md_path = output_dir / f"douchong_{year}_governors.md"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=("rank", "anchor_name", "room_id", "governor_count", "total_revenue"))
        writer.writeheader()
        writer.writerows(rows)
    lines = [
        f"# {year} 年斗虫主播总督数",
        "",
        "按全年斗虫总营收排序。总督数为各月 `guard_3` 累加，即全年总督开通/续费次数。",
        "",
        "|排名|主播|房间号|总督数|全年斗虫营收|",
        "|---:|---|---:|---:|---:|",
        *(f"|{row['rank']}|{row['anchor_name']}|{row['room_id']}|{row['governor_count']}|¥{row['total_revenue']:,.2f}|" for row in rows),
        "",
    ]
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return csv_path, md_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int, default=datetime.now().year)
    parser.add_argument("--scope", choices=tuple(SCOPE_URLS), default="all")
    parser.add_argument("--refresh", action="store_true", help="重新请求全部月份的斗虫 API")
    parser.add_argument("--cache-dir", type=Path, default=ROOT / "data/cache/dc")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data/reports")
    parser.add_argument("--plain", action="store_true", help="仅输出‘主播 数量’纯文本")
    args = parser.parse_args()
    rows = asyncio.run(build(args.year, args.scope, args.cache_dir, args.refresh))
    if args.plain:
        print("\n".join(f"{short_name(row['anchor_name'])} {row['governor_count']}" for row in rows))
        return
    csv_path, md_path = write_outputs(rows, args.output_dir, args.year)
    print(f"streamers={len(rows)} governors={sum(row['governor_count'] for row in rows)}")
    print(csv_path)
    print(md_path)


if __name__ == "__main__":
    main()
