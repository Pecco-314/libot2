from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from src.common.utils import ROOT

logger = logging.getLogger("bilibili.auth")

DEFAULT_STATE_PATH = ROOT / "data" / "bilibili_auth.json"
DEFAULT_ENV_PATHS = (ROOT / ".env", ROOT / ".env.prod")
STATE_VERSION = 1


def parse_cookie_header(value: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for pair in value.split(";"):
        if "=" not in pair:
            continue
        key, cookie_value = pair.split("=", 1)
        key = key.strip()
        if key:
            cookies[key] = cookie_value.strip()
    return cookies


def _read_env_values(paths: tuple[Path, ...]) -> dict[str, str]:
    values: dict[str, str] = {}
    for path in paths:
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key and key not in values:
                values[key] = value.strip().strip('"').strip("'")
    return values


def _bootstrap_fingerprint(cookie_header: str, refresh_token: str) -> str:
    payload = f"{cookie_header}\0{refresh_token}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class BilibiliAuth:
    cookies: dict[str, str]
    refresh_token: str
    revision: int
    bootstrap_fingerprint: str

    @property
    def has_sessdata(self) -> bool:
        return bool(self.cookies.get("SESSDATA"))


class BilibiliAuthStore:
    def __init__(
        self,
        state_path: Path = DEFAULT_STATE_PATH,
        env_paths: tuple[Path, ...] = DEFAULT_ENV_PATHS,
    ) -> None:
        self.state_path = state_path
        self.env_paths = env_paths
        self._lock = threading.RLock()
        self._cached_state: BilibiliAuth | None = None
        self._cached_mtime_ns: int | None = None

    def _read_bootstrap(self) -> tuple[dict[str, str], str, str]:
        values = _read_env_values(self.env_paths)
        cookie_header = values.get("COOKIE", "") or os.getenv("COOKIE", "")
        refresh_token = (
            values.get("BILI_REFRESH_TOKEN", "")
            or os.getenv("BILI_REFRESH_TOKEN", "")
        )
        cookies = parse_cookie_header(cookie_header)
        if not refresh_token:
            refresh_token = cookies.get("ac_time_value", "")
        return (
            cookies,
            refresh_token,
            _bootstrap_fingerprint(cookie_header, refresh_token),
        )

    def _read_state_file(self) -> BilibiliAuth | None:
        try:
            stat = self.state_path.stat()
        except FileNotFoundError:
            return None

        if (
            self._cached_state is not None
            and self._cached_mtime_ns == stat.st_mtime_ns
        ):
            return self._cached_state

        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        if int(payload.get("version", 0)) != STATE_VERSION:
            raise ValueError("unsupported Bilibili auth state version")

        raw_cookies = payload.get("cookies")
        if not isinstance(raw_cookies, dict):
            raise ValueError("invalid Bilibili auth cookies")

        cookies = {
            str(key): str(value)
            for key, value in raw_cookies.items()
            if str(key) and value is not None
        }
        state = BilibiliAuth(
            cookies=cookies,
            refresh_token=str(payload.get("refresh_token") or ""),
            revision=int(payload.get("revision") or 0),
            bootstrap_fingerprint=str(
                payload.get("bootstrap_fingerprint") or ""
            ),
        )
        self._cached_state = state
        self._cached_mtime_ns = stat.st_mtime_ns
        return state

    def _write_state(
        self,
        cookies: dict[str, str],
        refresh_token: str,
        bootstrap_fingerprint: str,
    ) -> BilibiliAuth:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        revision = time.time_ns()
        payload = {
            "version": STATE_VERSION,
            "revision": revision,
            "updated_at": int(time.time()),
            "bootstrap_fingerprint": bootstrap_fingerprint,
            "cookies": cookies,
            "refresh_token": refresh_token,
        }

        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.state_path.name}.",
            suffix=".tmp",
            dir=self.state_path.parent,
        )
        temp_path = Path(temp_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.state_path)
            os.chmod(self.state_path, 0o600)
        finally:
            temp_path.unlink(missing_ok=True)

        state = BilibiliAuth(
            cookies=dict(cookies),
            refresh_token=refresh_token,
            revision=revision,
            bootstrap_fingerprint=bootstrap_fingerprint,
        )
        self._cached_state = state
        self._cached_mtime_ns = self.state_path.stat().st_mtime_ns
        return state

    def load(self) -> BilibiliAuth:
        with self._lock:
            bootstrap_cookies, bootstrap_token, bootstrap_fingerprint = (
                self._read_bootstrap()
            )
            try:
                state = self._read_state_file()
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                logger.error("Bilibili auth state is invalid: %s", exc)
                state = None

            if state is None:
                if not bootstrap_cookies and not bootstrap_token:
                    return BilibiliAuth({}, "", 0, bootstrap_fingerprint)
                return self._write_state(
                    bootstrap_cookies,
                    bootstrap_token,
                    bootstrap_fingerprint,
                )

            if (
                bootstrap_cookies
                and state.bootstrap_fingerprint != bootstrap_fingerprint
            ):
                logger.info(
                    "Detected updated Bilibili auth bootstrap; migrating state"
                )
                return self._write_state(
                    bootstrap_cookies,
                    bootstrap_token,
                    bootstrap_fingerprint,
                )

            return BilibiliAuth(
                cookies=dict(state.cookies),
                refresh_token=state.refresh_token,
                revision=state.revision,
                bootstrap_fingerprint=state.bootstrap_fingerprint,
            )

    def save_refreshed(
        self,
        previous: BilibiliAuth,
        cookies: dict[str, str],
        refresh_token: str,
    ) -> BilibiliAuth:
        with self._lock:
            return self._write_state(
                dict(cookies),
                refresh_token,
                previous.bootstrap_fingerprint,
            )


AUTH_STORE = BilibiliAuthStore()


def get_bilibili_auth() -> BilibiliAuth:
    return AUTH_STORE.load()


def save_refreshed_bilibili_auth(
    previous: BilibiliAuth,
    cookies: dict[str, str],
    refresh_token: str,
) -> BilibiliAuth:
    return AUTH_STORE.save_refreshed(previous, cookies, refresh_token)


def build_bilibili_cookies() -> dict[str, str]:
    return dict(get_bilibili_auth().cookies)


__all__ = [
    "AUTH_STORE",
    "BilibiliAuth",
    "BilibiliAuthStore",
    "build_bilibili_cookies",
    "get_bilibili_auth",
    "parse_cookie_header",
    "save_refreshed_bilibili_auth",
]
