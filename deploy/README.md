# Web ingress and Paste

Nginx listens on port 5000 and forwards to the libot web process on
127.0.0.1:15000. Configuration: `nginx-libot.conf`, installed at
`/etc/nginx/sites-available/libot` and linked into `sites-enabled`.

Paste lives at `/paste/` and uses the existing libot login/password.
It supports text, drag-and-drop, clipboard image paste (Ctrl+V / Cmd+V),
multi-file selection with local previews and sequential uploads, safe JPEG image previews,
authenticated downloads and permanent deletion. Copy works over HTTP
using a clipboard fallback. Limits: 100,000 characters per note,
100 MiB per file, 5 GiB of original files and 10,000 entries total.
Preview files and SQLite overhead are additional disk usage.

Persistent storage is `data/paste/` (SQLite metadata, original files and
JPEG previews). Back up the whole directory while the web process is
stopped for a consistent copy. Deletions are permanent unless backed up.

Restart only the web process with `.venv/bin/python libotctl restart web`.
After changing Nginx configuration, install it, run `sudo nginx -t`,
then `sudo systemctl reload nginx`. Nginx's upload limit is 101 MiB to
allow multipart overhead. Flask also limits requests to 101 MiB.

# Independent availability alerts

`libot-watchdog.service` is a system-level service running as `pecco`,
independent of the bot process and login sessions. Install the unit in
`/etc/systemd/system/`, run `systemctl daemon-reload`, then enable it with
`systemctl enable --now libot-watchdog.service`.

Every 30 seconds it checks the timestamp of the bot's successful OneBot
online check in `logs/libot.log` and its first rotated file. Three minutes
without that heartbeat triggers email; continuing outages get hourly
reminders and recovery gets one notification. Missing logs also count as
an outage. Email errors are retried on the next check. SMTP has a 20-second
socket timeout. State lives in `data/service_watchdog.json` and survives
watchdog restarts. Log format or timezone changes must preserve this reader;
the unit uses Asia/Shanghai to match the bot's log timestamps.

The watchdog never stops or restarts business processes and never modifies
their PID files. The bot's internal NapCat checks remain a separate alert
path, so a NapCat-only outage can produce both notifications. This local
watchdog cannot report a whole-server or network outage while it is ongoing;
that requires an external monitoring host.

Read-only check: `.venv/bin/python -m src.service_watchdog --check`.
Service logs: `journalctl -u libot-watchdog.service`.
