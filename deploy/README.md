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
