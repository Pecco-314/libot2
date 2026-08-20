from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any
from typing import TypeVar
from urllib.parse import quote

import httpx


NBNHHSH_API_BASE = "https://lab.magiconch.com/api/nbnhhsh"
NBNHHSH_API_URL = f"{NBNHHSH_API_BASE}/guess"
NBNHHSH_HEADERS = {
    "User-Agent": "LiBot/1.0",
    "Origin": "https://lab.magiconch.com",
    "Referer": "https://lab.magiconch.com/nbnhhsh/",
}
NBNHHSH_COOKIES = httpx.Cookies()

T = TypeVar("T")


class NbnhhshError(RuntimeError):
    pass


def _parse_results(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise NbnhhshError("unexpected response type")

    results: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue

        approved_expansions: list[str] = []
        pending_expansions: list[str] = []
        seen: set[str] = set()
        for field, target in (
            ("trans", approved_expansions),
            ("inputting", pending_expansions),
        ):
            values = item.get(field)
            if not isinstance(values, list):
                continue
            for value in values:
                text = str(value).strip()
                key = text.casefold()
                if text and key not in seen:
                    seen.add(key)
                    target.append(text)
        results.append(
            {
                "name": name,
                "expansions": approved_expansions + pending_expansions,
                "approved_expansions": approved_expansions,
                "pending_expansions": pending_expansions,
            }
        )
    return results


async def guess_abbreviation(text: str) -> list[dict[str, Any]]:
    query = text.strip()
    if not query:
        return []

    return await _run_with_hard_timeout(_request_abbreviation(query))


async def submit_translation(abbreviation: str, expansion: str) -> None:
    name = abbreviation.strip().casefold()
    text = " ".join(expansion.strip().split())
    if not name or not text:
        raise ValueError("abbreviation and expansion must not be empty")
    await _run_with_hard_timeout(_request_translation(name, text))


async def _run_with_hard_timeout(
    coroutine: Coroutine[Any, Any, T],
) -> T:
    task = asyncio.create_task(coroutine)
    done, _ = await asyncio.wait({task}, timeout=8.0)
    if not done:
        task.cancel()
        task.add_done_callback(_consume_background_task)
        raise NbnhhshError("request timed out")
    try:
        return task.result()
    except asyncio.CancelledError:
        raise
    except NbnhhshError:
        raise
    except Exception as exc:
        raise NbnhhshError(str(exc)) from exc


def _consume_background_task(task: asyncio.Task[Any]) -> None:
    try:
        task.result()
    except (asyncio.CancelledError, Exception):
        pass


async def _request_abbreviation(query: str) -> list[dict[str, Any]]:
    last_error: Exception | None = None
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(8.0),
        trust_env=False,
        headers=NBNHHSH_HEADERS,
        cookies=NBNHHSH_COOKIES,
    ) as client:
        for attempt in range(2):
            try:
                response = await client.post(NBNHHSH_API_URL, json={"text": query})
                response.raise_for_status()
                NBNHHSH_COOKIES.update(response.cookies)
                return _parse_results(response.json())
            except asyncio.CancelledError:
                raise
            except (
                httpx.HTTPError,
                ValueError,
                NbnhhshError,
            ) as exc:
                last_error = exc
                if attempt == 0:
                    await asyncio.sleep(0.4)

    raise NbnhhshError(str(last_error or "request failed"))


async def _request_translation(abbreviation: str, expansion: str) -> None:
    url = f"{NBNHHSH_API_BASE}/translation/{quote(abbreviation, safe='')}"
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(8.0),
            trust_env=False,
            headers=NBNHHSH_HEADERS,
            cookies=NBNHHSH_COOKIES,
        ) as client:
            response = await client.post(url, json={"text": expansion})
            response.raise_for_status()
            NBNHHSH_COOKIES.update(response.cookies)
    except asyncio.CancelledError:
        raise
    except httpx.HTTPError as exc:
        raise NbnhhshError(str(exc)) from exc


__all__ = ["NbnhhshError", "guess_abbreviation", "submit_translation"]
