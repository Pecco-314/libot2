from __future__ import annotations

import binascii
import html
import re
import time
import uuid
from dataclasses import dataclass

import httpx
from Crypto.Cipher import PKCS1_OAEP
from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA

from src.common.bilibili_auth import BilibiliAuth

COOKIE_INFO_URL = (
    "https://passport.bilibili.com/x/passport-login/web/cookie/info"
)
CORRESPOND_URL = "https://www.bilibili.com/correspond/1/{path}"
COOKIE_REFRESH_URL = (
    "https://passport.bilibili.com/x/passport-login/web/cookie/refresh"
)
COOKIE_CONFIRM_URL = (
    "https://passport.bilibili.com/x/passport-login/web/confirm/refresh"
)

AUTH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
}

_CORRESPOND_PUBLIC_KEY = """\
-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDLgd2OAkcGVtoE3ThUREbio0Eg
Uc/prcajMKXvkCKFCWhJYJcLkcM2DKKcSeFpD/j6Boy538YXnR6VhcuUJOhH2x71
nzPjfdTcqMz7djHum0qSZA0AyCBDABUqCrfNgCiJ00Ra7GmRj+YCK1NJEuewlb40
JNrRuoEUXpabUzGB8QIDAQAB
-----END PUBLIC KEY-----"""

_REFRESH_CSRF_PATTERN = re.compile(
    r"""<div[^>]+id=["']1-name["'][^>]*>([^<]+)</div>""",
    re.IGNORECASE,
)


class CookieRefreshError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RefreshedAuth:
    cookies: dict[str, str]
    refresh_token: str


def _response_payload(response: httpx.Response, operation: str) -> dict:
    try:
        payload = response.json()
    except ValueError as exc:
        raise CookieRefreshError(
            f"{operation} returned invalid JSON (HTTP {response.status_code})"
        ) from exc
    if not isinstance(payload, dict):
        raise CookieRefreshError(f"{operation} returned invalid payload")
    return payload


def _ensure_success(response: httpx.Response, operation: str) -> dict:
    if response.status_code != 200:
        raise CookieRefreshError(
            f"{operation} failed with HTTP {response.status_code}"
        )
    payload = _response_payload(response, operation)
    if int(payload.get("code", -1)) != 0:
        message = str(payload.get("message") or payload.get("msg") or "")
        raise CookieRefreshError(
            f"{operation} failed with code {payload.get('code')}: {message}"
        )
    return payload


def _correspond_path() -> str:
    public_key = RSA.import_key(_CORRESPOND_PUBLIC_KEY)
    cipher = PKCS1_OAEP.new(public_key, hashAlgo=SHA256)
    timestamp_ms = round(time.time() * 1000)
    encrypted = cipher.encrypt(f"refresh_{timestamp_ms}".encode("utf-8"))
    return binascii.hexlify(encrypted).decode("ascii")


def _response_cookies(response: httpx.Response) -> dict[str, str]:
    return {
        cookie.name: cookie.value
        for cookie in response.cookies.jar
        if cookie.name
    }


def _scoped_cookies(values: dict[str, str]) -> httpx.Cookies:
    cookies = httpx.Cookies()
    for name, value in values.items():
        if name:
            cookies.set(
                name,
                value,
                domain=".bilibili.com",
                path="/",
            )
    return cookies


async def cookie_needs_refresh(
    auth: BilibiliAuth,
    *,
    client: httpx.AsyncClient | None = None,
) -> bool:
    owns_client = client is None
    request_client = client or httpx.AsyncClient(
        timeout=10.0,
        headers=AUTH_HEADERS,
        cookies=_scoped_cookies(auth.cookies),
        follow_redirects=True,
    )
    if client is not None:
        request_client.cookies.update(_scoped_cookies(auth.cookies))
    try:
        try:
            response = await request_client.get(COOKIE_INFO_URL)
            payload = _ensure_success(response, "cookie info")
            data = payload.get("data")
            if not isinstance(data, dict):
                raise CookieRefreshError("cookie info response has no data")
            return bool(data.get("refresh"))
        except CookieRefreshError:
            raise
        except httpx.HTTPError as exc:
            raise CookieRefreshError(
                f"cookie info request failed: {type(exc).__name__}"
            ) from exc
    finally:
        if owns_client:
            await request_client.aclose()


async def refresh_bilibili_auth(
    auth: BilibiliAuth,
    *,
    client: httpx.AsyncClient | None = None,
) -> RefreshedAuth:
    bili_jct = auth.cookies.get("bili_jct", "")
    if not bili_jct:
        raise CookieRefreshError("COOKIE is missing bili_jct")
    if not auth.refresh_token:
        raise CookieRefreshError("BILI_REFRESH_TOKEN is missing")

    request_cookies = dict(auth.cookies)
    request_cookies["buvid3"] = str(uuid.uuid1())
    owns_client = client is None
    request_client = client or httpx.AsyncClient(
        timeout=15.0,
        headers=AUTH_HEADERS,
        cookies=_scoped_cookies(request_cookies),
        follow_redirects=True,
    )
    if client is not None:
        request_client.cookies.update(_scoped_cookies(request_cookies))

    try:
        try:
            csrf_response = await request_client.get(
                CORRESPOND_URL.format(path=_correspond_path()),
            )
            if csrf_response.status_code != 200:
                raise CookieRefreshError(
                    "refresh csrf request failed with "
                    f"HTTP {csrf_response.status_code}"
                )
            match = _REFRESH_CSRF_PATTERN.search(csrf_response.text)
            if match is None:
                raise CookieRefreshError("refresh csrf was not found")
            refresh_csrf = html.unescape(match.group(1)).strip()

            refresh_response = await request_client.post(
                COOKIE_REFRESH_URL,
                data={
                    "csrf": bili_jct,
                    "refresh_csrf": refresh_csrf,
                    "refresh_token": auth.refresh_token,
                    "source": "main_web",
                },
            )
            refresh_payload = _ensure_success(
                refresh_response,
                "cookie refresh",
            )
            refresh_data = refresh_payload.get("data")
            if not isinstance(refresh_data, dict):
                raise CookieRefreshError(
                    "cookie refresh response has no data"
                )
            new_refresh_token = str(
                refresh_data.get("refresh_token") or ""
            ).strip()
            if not new_refresh_token:
                raise CookieRefreshError(
                    "cookie refresh response has no refresh token"
                )

            new_cookies = dict(auth.cookies)
            new_cookies.update(_response_cookies(refresh_response))
            new_bili_jct = new_cookies.get("bili_jct", "")
            if not new_cookies.get("SESSDATA") or not new_bili_jct:
                raise CookieRefreshError(
                    "cookie refresh response is missing authentication cookies"
                )

            confirm_response = await request_client.post(
                COOKIE_CONFIRM_URL,
                data={
                    "csrf": new_bili_jct,
                    "refresh_token": auth.refresh_token,
                },
            )
            _ensure_success(confirm_response, "cookie refresh confirmation")
            return RefreshedAuth(new_cookies, new_refresh_token)
        except CookieRefreshError:
            raise
        except httpx.HTTPError as exc:
            raise CookieRefreshError(
                f"cookie refresh request failed: {type(exc).__name__}"
            ) from exc
    finally:
        if owns_client:
            await request_client.aclose()


__all__ = [
    "CookieRefreshError",
    "RefreshedAuth",
    "cookie_needs_refresh",
    "refresh_bilibili_auth",
]
