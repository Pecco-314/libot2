from __future__ import annotations

import os
from pathlib import Path


def load_env_file() -> None:
    project_root = Path(__file__).resolve().parents[2]
    for env_name in (".env", ".env.prod"):
        env_path = project_root / env_name
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
