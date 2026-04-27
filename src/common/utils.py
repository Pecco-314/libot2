from __future__ import annotations

import os
import logging
from pathlib import Path
from logging.handlers import RotatingFileHandler

ROOT = Path(__file__).resolve().parents[2]

def load_env_file() -> None:
    for env_name in (".env", ".env.prod"):
        env_path = ROOT / env_name
        if not env_path.exists():
            continue

        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            if not key or key in os.environ:
                continue

            os.environ[key] = value.strip().strip('"').strip("'")


def init_logger(name: str) -> logging.Logger:
    log_path = ROOT / "logs"
    log_path.mkdir(exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    handler = RotatingFileHandler(
        filename=log_path / f"{name}.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger