from __future__ import annotations

import argparse
import os
import signal
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Sequence
from src.common.utils import load_env_file

load_env_file()

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "logs"
DATA_DIR = ROOT / "data"
PID_DIR = DATA_DIR / ".pids"
IS_TTY = sys.stdout.isatty()
VENV_BIN = ROOT / ".venv" / "bin"
NAPCAT_PATH = Path(os.environ.get("NAPCAT_PATH"))
BOT_QQ = os.environ.get("BOT_QQ")
NAPCAT_CMD = f"stdbuf -oL -eL xvfb-run -a opt/QQ/qq --no-sandbox -q {BOT_QQ}"

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
    identity_markers: Sequence[str]
    redirect_output: bool
    cwd: Path | None = None


MODULES = {
    "libot": ModuleSpec(
        name="libot",
        log_file=LOG_DIR / "libot.log",
        pid_file=PID_DIR / "libot.pid",
        command=("nb", "run"),
        identity_markers=("/libot2/.venv/bin/nb", "run"),
        redirect_output=True,
    ),
    "spider": ModuleSpec(
        name="spider",
        log_file=LOG_DIR / "spider.log",
        pid_file=PID_DIR / "spider.pid",
        command=("python", "-m", "src.spider.cron"),
        identity_markers=("-m", "src.spider.cron"),
        redirect_output=False,
    ),
    "monitor": ModuleSpec(
        name="monitor",
        log_file=LOG_DIR / "monitor.log",
        pid_file=PID_DIR / "monitor.pid",
        command=("python", "-m", "src.monitor.monitor"),
        identity_markers=("-m", "src.monitor.monitor"),
        redirect_output=True,
    ),
    "web": ModuleSpec(
        name="web",
        log_file=LOG_DIR / "web.log",
        pid_file=PID_DIR / "web.pid",
        command=("python", "-m", "src.web.app"),
        identity_markers=("-m", "src.web.app"),
        redirect_output=True,
    ),
    "napcat": ModuleSpec(
        name="napcat",
        log_file=LOG_DIR / "napcat.log",
        pid_file=PID_DIR / "napcat.pid",
        command=("bash", "-c", NAPCAT_CMD),
        identity_markers=("xvfb-run", "opt/QQ/qq", str(BOT_QQ)),
        redirect_output=True,
        cwd=NAPCAT_PATH,
    ),
    "lyrics": ModuleSpec(
        name="lyrics",
        log_file=LOG_DIR / "lyrics.log",
        pid_file=PID_DIR / "lyrics.pid",
        command=("python", "app.py"),
        identity_markers=("python", "app.py"),
        redirect_output=True,
        cwd=ROOT / "third_party" / "LrcApi",
    ),
    "capture": ModuleSpec(
        name="capture",
        log_file=LOG_DIR / "capture.log",
        pid_file=PID_DIR / "capture.pid",
        command=("python", "-m", "src.capture.capture"),
        identity_markers=("-m", "src.capture.capture"),
        redirect_output=False,
    ),
}


class ProcessState(Enum):
    ALIVE = "alive"
    DEAD = "dead"
    UNKNOWN = "unknown"


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
    cmd = list(spec.command)
    if not cmd:
        return cmd

    # 处理 nb 命令
    if cmd[0] == "nb":
        nb_executable = shutil.which("nb")
        if nb_executable is None:
            nb_candidate = VENV_BIN / "nb"
            if nb_candidate.exists():
                nb_executable = str(nb_candidate)
        if nb_executable is None:
            nb_candidate = Path(sys.executable).resolve().with_name("nb")
            if nb_candidate.exists():
                nb_executable = str(nb_candidate)
        if nb_executable is None:
            raise FileNotFoundError("nb command not found in current virtual environment")
        cmd[0] = nb_executable
        return cmd

    # 处理 python 命令
    if cmd[0] == "python":
        python_executable = str(VENV_BIN / "python") if (VENV_BIN / "python").exists() else sys.executable
        cmd[0] = python_executable
        return cmd

    return cmd


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


def _pid_visibility_limited() -> bool:
    try:
        init_name = Path("/proc/1/comm").read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return init_name in {"bwrap", "unshare"}


def _process_state(pid: int) -> ProcessState:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        if _pid_visibility_limited():
            return ProcessState.UNKNOWN
        return ProcessState.DEAD
    except PermissionError:
        return ProcessState.ALIVE
    return ProcessState.ALIVE


def _read_process_cmdline(pid: int) -> tuple[str, ...] | None:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (FileNotFoundError, ProcessLookupError):
        return None
    except OSError:
        return ()
    return tuple(
        part.decode("utf-8", errors="replace")
        for part in raw.split(b"\0")
        if part
    )


def _read_process_cwd(pid: int) -> Path | None:
    try:
        return Path(os.readlink(f"/proc/{pid}/cwd")).resolve()
    except (FileNotFoundError, ProcessLookupError):
        return None
    except OSError:
        return Path()


def _managed_process_problem(spec: ModuleSpec, pid: int) -> str | None:
    cmdline = _read_process_cmdline(pid)
    if cmdline is None:
        return "process disappeared"
    if not cmdline:
        return "cannot read process command line"
    command_text = "\0".join(cmdline)
    missing_markers = [
        marker for marker in spec.identity_markers if marker not in command_text
    ]
    if missing_markers:
        return f"command line does not match ({', '.join(missing_markers)})"

    expected_cwd = (spec.cwd or ROOT).resolve()
    actual_cwd = _read_process_cwd(pid)
    if actual_cwd is None:
        return "process disappeared"
    if actual_cwd == Path():
        return "cannot read process working directory"
    if actual_cwd != expected_cwd:
        return f"working directory is {actual_cwd}, expected {expected_cwd}"

    try:
        process_group = os.getpgid(pid)
    except ProcessLookupError:
        return "process disappeared"
    except PermissionError:
        return "cannot read process group"
    if process_group != pid:
        return f"process group {process_group} is not led by pid {pid}"
    return None


def _cleanup_stale_pidfile(spec: ModuleSpec) -> ProcessState:
    pid = _read_pid(spec.pid_file)
    if pid is None:
        return ProcessState.DEAD
    state = _process_state(pid)
    if state is ProcessState.DEAD:
        try:
            spec.pid_file.unlink()
        except FileNotFoundError:
            pass
    return state


def _status(spec: ModuleSpec) -> tuple[ProcessState, int | None]:
    state = _cleanup_stale_pidfile(spec)
    pid = _read_pid(spec.pid_file)
    if pid is None:
        return ProcessState.DEAD, None
    return state, pid


def _start(spec: ModuleSpec) -> int:
    state, pid = _status(spec)
    if state is ProcessState.UNKNOWN and pid is not None:
        print(
            f"refusing to start {spec.name}: pid={pid} is outside the visible "
            "PID namespace; run libotctl on the host",
            file=sys.stderr,
        )
        return 1
    if state is ProcessState.ALIVE and pid is not None:
        problem = _managed_process_problem(spec, pid)
        if problem is not None:
            print(
                f"refusing to start {spec.name}: pid file is unsafe: {problem}",
                file=sys.stderr,
            )
            return 1
        print(f"{spec.name} is already running (pid={pid})")
        return 1

    _ensure_parent(spec.pid_file)
    _ensure_parent(spec.log_file)

    log_handle = spec.log_file.open("a", encoding="utf-8") if spec.redirect_output else None

    work_dir = spec.cwd if spec.cwd else ROOT
    child_env = {
        key: value
        for key, value in os.environ.items()
        if not key.lower().endswith("_proxy")
    }
    if spec.name == "napcat":
        child_env = os.environ.copy()
        child_env["NO_PROXY"] = "*"
        child_env["no_proxy"] = "*"

    try:
        process = subprocess.Popen(
            _resolve_command(spec),
            cwd=work_dir,
            stdin=subprocess.DEVNULL,
            stdout=log_handle if spec.redirect_output else subprocess.DEVNULL,
            stderr=log_handle if spec.redirect_output else subprocess.DEVNULL,
            env=child_env,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        if log_handle is not None:
            log_handle.close()
        print(f"failed to start {spec.name}: {exc}", file=sys.stderr)
        return 1

    spec.pid_file.write_text(str(process.pid), encoding="utf-8")
    print(f"started {spec.name} (pid={process.pid}) at {work_dir}")
    if log_handle is not None:
        log_handle.close()
    return 0


def _stop(spec: ModuleSpec) -> int:
    pid = _read_pid(spec.pid_file)
    if pid is None:
        print(f"{spec.name} is not running")
        return 0

    state = _process_state(pid)
    if state is ProcessState.UNKNOWN:
        print(
            f"refusing to stop {spec.name}: pid={pid} is outside the visible "
            "PID namespace; PID file preserved",
            file=sys.stderr,
        )
        return 1
    if state is ProcessState.DEAD:
        _cleanup_stale_pidfile(spec)
        print(f"{spec.name} is not running")
        return 0

    problem = _managed_process_problem(spec, pid)
    if problem is not None:
        print(
            f"refusing to stop {spec.name}: pid file is unsafe: {problem}; "
            "PID file preserved",
            file=sys.stderr,
        )
        return 1

    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        _cleanup_stale_pidfile(spec)
        print(f"{spec.name} is not running")
        return 0

    for _ in range(30):
        state = _process_state(pid)
        if state is not ProcessState.ALIVE:
            break
        time.sleep(1)

    if state is ProcessState.ALIVE:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        for _ in range(5):
            state = _process_state(pid)
            if state is not ProcessState.ALIVE:
                break
            time.sleep(0.2)

    if state is not ProcessState.DEAD:
        print(
            f"failed to confirm that {spec.name} pid={pid} stopped; "
            "PID file preserved",
            file=sys.stderr,
        )
        return 1

    try:
        spec.pid_file.unlink()
    except FileNotFoundError:
        pass

    print(f"stopped {spec.name}")
    return 0


def _restart(spec: ModuleSpec) -> int:
    stop_code = _stop(spec)
    if stop_code != 0:
        return stop_code
    return _start(spec)


def _print_status(spec: ModuleSpec) -> int:
    state, pid = _status(spec)
    if state is ProcessState.ALIVE and pid is not None:
        print(f"{_ok('🟢')} {spec.name}: running (pid={pid})")
        return 0
    if state is ProcessState.UNKNOWN and pid is not None:
        print(
            f"{_warn('🟡')} {spec.name}: unknown "
            f"(pid={pid} is outside the visible PID namespace)"
        )
        return 4
    print(f"{_bad('🔴')} {spec.name}: stopped")
    return 3


def _print_overview() -> int:
    print(_banner("📋 libotctl status overview"))
    exit_code = 0
    for spec in MODULES.values():
        state, pid = _status(spec)
        if state is ProcessState.ALIVE and pid is not None:
            print(f"{_ok('🟢')} {spec.name:<6} pid={pid}")
            continue
        if state is ProcessState.UNKNOWN and pid is not None:
            print(f"{_warn('🟡')} {spec.name:<6} pid={pid} visibility unknown")
            exit_code = max(exit_code, 4)
            continue
        print(f"{_bad('🔴')} {spec.name:<6} stopped")
        exit_code = max(exit_code, 3)
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
