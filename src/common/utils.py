from __future__ import annotations

import os
import logging
import smtplib
import unicodedata
from email.mime.text import MIMEText
from email.header import Header
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


def send_notification_email(subject: str, content: str) -> None:
    load_env_file()
    sender = os.environ.get("EMAIL_SENDER")
    receiver = os.environ.get("EMAIL_RECEIVER")
    auth_code = os.environ.get("EMAIL_AUTH_CODE")
    smtp_server = os.environ.get("SMTP_SERVER")
    smtp_port = os.environ.get("SMTP_PORT")

    # 构造邮件内容
    message = MIMEText(content, "plain", "utf-8")
    message["From"] = Header(sender)
    message["To"] = Header(receiver)
    message["Subject"] = Header(subject, "utf-8")

    with smtplib.SMTP_SSL(smtp_server, smtp_port) as server:
        server.login(sender, auth_code)
        server.sendmail(sender, [receiver], message.as_string())


def truncate_name(name: str, max_len: int = 18) -> str:
    current_width = 0
    truncated_str = ""
    
    # 预留出省略号 "..." 的宽度
    limit = max_len - 3
    
    # 首先检查总宽度，如果没超限直接返回
    total_width = sum(2 if unicodedata.east_asian_width(c) in 'WFA' else 1 for c in name)
    if total_width <= max_len:
        return name

    for char in name:
        # 判断当前字符宽度
        width = 2 if unicodedata.east_asian_width(char) in 'WFA' else 1
        
        if current_width + width <= limit:
            truncated_str += char
            current_width += width
        else:
            break
            
    return truncated_str + "..."