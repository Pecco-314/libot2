"""监督外部服务并轮转原始输出；归档不自动删除，退出时回收子进程组。"""
from __future__ import annotations

import argparse
import fcntl
import gzip
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import sys
import time


class RotatingOutput:
    def __init__(self, path: Path, max_bytes: int):
        if max_bytes <= 0:
            raise ValueError('max_bytes must be positive')
        self.path = path
        self.max_bytes = max_bytes
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = path.with_name(path.name + '.rotate.lock').open('a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.lock.close()
            raise
        self.file = path.open('ab', buffering=0)
        self.size = path.stat().st_size
        if self.size >= max_bytes:
            self.rotate()

    def rotate(self):
        self.file.close()
        stamp = time.strftime('%Y%m%d-%H%M%S') + f'-{time.time_ns()}'
        archive = self.path.with_name(self.path.name + '.' + stamp)
        self.path.rename(archive)
        self.file = self.path.open('ab', buffering=0)
        self.size = 0
        temporary = archive.with_name(archive.name + '.gz.tmp')
        try:
            with archive.open('rb') as source, temporary.open('xb') as target:
                with gzip.GzipFile(fileobj=target, mode='wb') as compressed:
                    shutil.copyfileobj(source, compressed)
                target.flush()
                os.fsync(target.fileno())
            temporary.rename(archive.with_name(archive.name + '.gz'))
            archive.unlink()
        except OSError:
            # 压缩失败时原始归档仍完整保留，继续接收输出。
            temporary.unlink(missing_ok=True)

    def write(self, data: bytes):
        while data:
            if self.size >= self.max_bytes:
                self.rotate()
            length = min(len(data), self.max_bytes - self.size)
            if length < len(data):
                newline = data.rfind(b'\n', 0, length)
                if newline >= 0:
                    length = newline + 1
            written = self.file.write(data[:length])
            if not written:
                raise OSError('log write made no progress')
            self.size += written
            data = data[written:]
            if data and self.size + len(data) > self.max_bytes and data[:1] != b'\n':
                self.rotate()

    def close(self):
        self.file.close()
        self.lock.close()


def supervise(command: list[str], path: Path, max_bytes: int) -> int:
    output = RotatingOutput(path, max_bytes)
    child = None
    stopping = False
    handlers = {}

    def request_stop(signum, frame):
        nonlocal stopping
        stopping = True

    def send(sig):
        if child is not None:
            try:
                os.killpg(child.pid, sig)
            except ProcessLookupError:
                pass

    try:
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            handlers[sig] = signal.signal(sig, request_stop)
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, start_new_session=True)
        assert child.stdout is not None
        os.set_blocking(child.stdout.fileno(), False)
        termination_at = None
        exited_at = None
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            eof = False
            while True:
                now = time.monotonic()
                if child.poll() is not None and exited_at is None:
                    exited_at = now
                    stopping = True
                if stopping and termination_at is None:
                    send(signal.SIGTERM)
                    termination_at = now
                if termination_at is not None and now - termination_at > 8:
                    send(signal.SIGKILL)
                if eof and child.poll() is not None:
                    break
                if exited_at is not None and now - exited_at > 10:
                    break
                for key, _ in selector.select(.2):
                    data = os.read(key.fd, 65536)
                    if data:
                        output.write(data)
                    else:
                        selector.unregister(key.fileobj)
                        eof = True
        returncode = child.wait()
        return returncode if returncode >= 0 else 128 - returncode
    finally:
        send(signal.SIGTERM)
        if child is not None:
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                send(signal.SIGKILL)
                child.wait()
            send(signal.SIGKILL)
            if child.stdout is not None:
                child.stdout.close()
        output.close()
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--log', type=Path, required=True)
    parser.add_argument('--max-bytes', type=int, default=20 * 1024 * 1024)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('missing child command')
    return supervise(command, args.log, args.max_bytes)


if __name__ == '__main__':
    sys.exit(main())
