from __future__ import annotations

import argparse
import os
import signal
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "logs"
PID_DIR = LOG_DIR / ".pids"
IS_TTY = sys.stdout.isatty()

RESET = "\033[0m"
BOLD = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"


@dataclass(frozen=True)
class ModuleSpec:
    name: str
    log_file: Path
    pid_file: Path
    command: Sequence[str]
    redirect_output: bool


MODULES = {
    "libot": ModuleSpec(
        name="libot",
        log_file=LOG_DIR / "libot.log",
        pid_file=PID_DIR / "libot.pid",
        command=("nb", "run"),
        redirect_output=True,
    ),
    "spider": ModuleSpec(
        name="spider",
        log_file=LOG_DIR / "spider.log",
        pid_file=PID_DIR / "spider.pid",
        command=(sys.executable, "-m", "src.spider.cron"),
        redirect_output=False,
    ),
}


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _paint(text: str, color: str) -> str:
    if not IS_TTY:
        return text
    return f"{color}{text}{RESET}"


def _banner(text: str) -> str:
    return _paint(text, BOLD + CYAN)


def _ok(text: str) -> str:
    return _paint(text, GREEN)


def _warn(text: str) -> str:
    return _paint(text, YELLOW)


def _bad(text: str) -> str:
    return _paint(text, RED)


def _resolve_command(spec: ModuleSpec) -> list[str]:
    if spec.name == "libot":
        nb_executable = shutil.which("nb")
        if nb_executable is None:
            nb_candidate = Path(sys.executable).resolve().with_name("nb")
            if nb_candidate.exists():
                nb_executable = str(nb_candidate)
        if nb_executable is None:
            raise FileNotFoundError("nb command not found in current virtual environment")
        return [nb_executable, "run"]

    return [sys.executable, "-m", "src.spider.cron"]


def _read_pid(pid_file: Path) -> int | None:
    try:
        pid_text = pid_file.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError:
        return None

    if not pid_text.isdigit():
        return None
    return int(pid_text)


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _cleanup_stale_pidfile(spec: ModuleSpec) -> None:
    pid = _read_pid(spec.pid_file)
    if pid is not None and not _process_alive(pid):
        try:
            spec.pid_file.unlink()
        except FileNotFoundError:
            pass


def _status(spec: ModuleSpec) -> tuple[bool, int | None]:
    _cleanup_stale_pidfile(spec)
    pid = _read_pid(spec.pid_file)
    if pid is None:
        return False, None
    return _process_alive(pid), pid


def _start(spec: ModuleSpec) -> int:
    running, pid = _status(spec)
    if running and pid is not None:
        print(f"{spec.name} is already running (pid={pid})")
        return 1

    _ensure_parent(spec.pid_file)
    _ensure_parent(spec.log_file)

    log_handle = spec.log_file.open("a", encoding="utf-8") if spec.redirect_output else None
    try:
        process = subprocess.Popen(
            _resolve_command(spec),
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log_handle if spec.redirect_output else subprocess.DEVNULL,
            stderr=log_handle if spec.redirect_output else subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        if log_handle is not None:
            log_handle.close()
        print(f"failed to start {spec.name}: {exc}", file=sys.stderr)
        return 1

    spec.pid_file.write_text(str(process.pid), encoding="utf-8")
    print(f"started {spec.name} (pid={process.pid})")
    if log_handle is not None:
        log_handle.close()
    return 0


def _stop(spec: ModuleSpec) -> int:
    pid = _read_pid(spec.pid_file)
    if pid is None:
        print(f"{spec.name} is not running")
        return 0

    if not _process_alive(pid):
        _cleanup_stale_pidfile(spec)
        print(f"{spec.name} is not running")
        return 0

    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except ProcessLookupError:
        _cleanup_stale_pidfile(spec)
        print(f"{spec.name} is not running")
        return 0

    for _ in range(30):
        if not _process_alive(pid):
            break
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except ProcessLookupError:
            break
        time.sleep(1)

    if _process_alive(pid):
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass

    try:
        spec.pid_file.unlink()
    except FileNotFoundError:
        pass

    print(f"stopped {spec.name}")
    return 0


def _restart(spec: ModuleSpec) -> int:
    stop_code = _stop(spec)
    start_code = _start(spec)
    return stop_code or start_code


def _print_status(spec: ModuleSpec) -> int:
    running, pid = _status(spec)
    if running and pid is not None:
        print(f"{_ok('🟢')} {spec.name}: running (pid={pid})")
        return 0
    print(f"{_bad('🔴')} {spec.name}: stopped")
    return 3


def _print_overview() -> int:
    print(_banner("📋 libotctl status overview"))
    exit_code = 0
    for spec in MODULES.values():
        running, pid = _status(spec)
        if running and pid is not None:
            print(f"{_ok('🟢')} {spec.name:<6} pid={pid}")
            continue
        print(f"{_bad('🔴')} {spec.name:<6} stopped")
        exit_code = 3
    return exit_code


def _show_log(spec: ModuleSpec, lines: int) -> int:
    print(_banner(f"📄 following {spec.name} log: {spec.log_file}"))
    _ensure_parent(spec.log_file)

    try:
        if spec.log_file.exists():
            content = spec.log_file.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in content[-lines:]:
                print(line, flush=True)
        else:
            print(_warn(f"waiting for log file: {spec.log_file}"))

        position = spec.log_file.stat().st_size if spec.log_file.exists() else 0
        while True:
            if not spec.log_file.exists():
                time.sleep(0.5)
                continue

            current_size = spec.log_file.stat().st_size
            if current_size < position:
                position = 0

            with spec.log_file.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(position)
                while True:
                    line = handle.readline()
                    if not line:
                        position = handle.tell()
                        break
                    print(line.rstrip("\n"), flush=True)

            time.sleep(0.5)
    except KeyboardInterrupt:
        print(_warn("^C log follow stopped"))
        return 130
    except OSError as exc:
        print(f"failed to read log: {exc}", file=sys.stderr)
        return 1

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="libotctl")
    parser.add_argument("action", choices=("start", "stop", "restart", "status", "log"))
    parser.add_argument("module", nargs="?", choices=tuple(MODULES.keys()))
    parser.add_argument("--lines", type=int, default=100, help="log command output lines")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.lines <= 0:
        print("--lines must be greater than 0", file=sys.stderr)
        return 1

    if args.action == "start":
        if args.module is None:
            parser.error("start requires a module")
        spec = MODULES[args.module]
        return _start(spec)
    if args.action == "stop":
        if args.module is None:
            parser.error("stop requires a module")
        spec = MODULES[args.module]
        return _stop(spec)
    if args.action == "restart":
        if args.module is None:
            parser.error("restart requires a module")
        spec = MODULES[args.module]
        return _restart(spec)
    if args.action == "status":
        if args.module is None:
            return _print_overview()
        spec = MODULES[args.module]
        return _print_status(spec)
    if args.action == "log":
        if args.module is None:
            parser.error("log requires a module")
        spec = MODULES[args.module]
        return _show_log(spec, args.lines)

    parser.error("unsupported action")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
