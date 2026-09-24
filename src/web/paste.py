"""Private, authenticated paste storage independent of the bot database."""
import hmac
import secrets
import sqlite3
from io import BytesIO
from contextlib import closing
from pathlib import Path

from flask import Blueprint, abort, jsonify, redirect, render_template, request, send_file, session, url_for
from PIL import Image, UnidentifiedImageError


def create_paste_blueprint(data_dir, login_required):
    root = Path(data_dir)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    db_path = root / 'paste.sqlite3'
    with closing(sqlite3.connect(db_path)) as db:
        db.execute('CREATE TABLE IF NOT EXISTS items (id TEXT PRIMARY KEY, text TEXT NOT NULL, name TEXT NOT NULL, size INTEGER NOT NULL, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
        db.commit()
    bp = Blueprint('paste', __name__, url_prefix='/paste')

    @bp.record_once
    def configure(state):
        state.app.config['MAX_CONTENT_LENGTH'] = 101 * 1024 * 1024

    @bp.before_request
    def protect():
        if request.method == 'POST':
            if not session.get('logged_in'):
                abort(401)
            if not session.get('csrf_token') or not hmac.compare_digest(session['csrf_token'], request.form.get('csrf_token', '')):
                abort(403)

    @bp.after_request
    def private_response(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        return response

    @bp.errorhandler(413)
    def too_large(error):
        return '上传过大：单次最多 100 MiB，文字最多 100,000 字符。', 413

    @bp.route('/', methods=['GET', 'POST'])
    @login_required
    def index():
        session.setdefault('csrf_token', secrets.token_urlsafe(32))
        if request.method == 'POST':
            content = request.form.get('text', '').strip()
            upload = request.files.get('file')
            name = (upload.filename or '').replace('\\', '/').split('/')[-1][:255] if upload else ''
            if len(content) > 100000:
                abort(413)
            if not content and not name:
                abort(400, '请填写文字或选择文件')
            ident = secrets.token_hex(16)
            path = root / ident
            try:
                if name:
                    upload.save(path)
                size = path.stat().st_size if name else 0
                if size > 100 * 1024 * 1024:
                    abort(413)
                with closing(sqlite3.connect(db_path, timeout=30)) as db:
                    db.execute('BEGIN IMMEDIATE')
                    used, count = db.execute('SELECT COALESCE(SUM(size),0), COUNT(*) FROM items').fetchone()
                    if used + size > 5 * 1024**3 or count >= 10000:
                        abort(413, '存储已满，请先删除旧内容')
                    db.execute('INSERT INTO items(id,text,name,size) VALUES (?,?,?,?)', (ident, content, name, size))
                    db.commit()
            except BaseException:
                path.unlink(missing_ok=True)
                raise
            if name:
                try:
                    with Image.open(path) as img:
                        img.thumbnail((960, 960))
                        img.convert('RGB').save(root / (ident + '.jpg'), 'JPEG')
                except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
                    pass
            if request.accept_mimetypes.best == 'application/json':
                return jsonify(id=ident), 201
            return redirect(url_for('paste.index'), code=303)
        page = max(1, request.args.get('page', 1, type=int))
        with closing(sqlite3.connect(db_path)) as db:
            db.row_factory = sqlite3.Row
            items = db.execute('SELECT * FROM items ORDER BY created DESC, rowid DESC LIMIT 21 OFFSET ?', ((page - 1) * 20,)).fetchall()
        return render_template('paste.html', items=items[:20], page=page, more=len(items) > 20,
                               previews={item['id'] for item in items if (root / (item['id'] + '.jpg')).exists()})

    def find(ident):
        with closing(sqlite3.connect(db_path)) as db:
            db.row_factory = sqlite3.Row
            item = db.execute('SELECT * FROM items WHERE id=?', (ident,)).fetchone()
        if item is None:
            abort(404)
        return item

    @bp.get('/<ident>/download')
    @login_required
    def download(ident):
        item = find(ident)
        if not item['name']:
            abort(404)
        return send_file(root / item['id'], as_attachment=True, download_name=item['name'], mimetype='application/octet-stream')

    @bp.get('/<ident>/image')
    @login_required
    def image(ident):
        item = find(ident)
        if not item['name']:
            abort(404)
        path = root / item['id']
        # Detect actual raster format, never serve arbitrary uploads as inline HTML/SVG.
        try:
            with Image.open(path) as img:
                mime = {'PNG': 'image/png', 'JPEG': 'image/jpeg',
                        'GIF': 'image/gif', 'WEBP': 'image/webp'}.get(img.format)
                if mime:
                    return send_file(path, mimetype=mime, as_attachment=False,
                                     download_name=item['name'])
                # Convert other decodable formats at full resolution for Safari.
                output = BytesIO()
                img.convert('RGBA').save(output, format='PNG')
                output.seek(0)
                return send_file(output, mimetype='image/png', as_attachment=False,
                                 download_name='image.png')
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
            abort(415, '此文件无法作为图片打开，请下载原文件')

    @bp.get('/<ident>/preview')
    @login_required
    def preview(ident):
        item = find(ident)
        path = root / (item['id'] + '.jpg')
        if not path.exists():
            abort(404)
        return send_file(path, mimetype='image/jpeg')

    @bp.post('/<ident>/delete')
    @login_required
    def delete(ident):
        item = find(ident)
        with closing(sqlite3.connect(db_path)) as db:
            db.execute('DELETE FROM items WHERE id=?', (ident,))
            db.commit()
        (root / item['id']).unlink(missing_ok=True)
        (root / (item['id'] + '.jpg')).unlink(missing_ok=True)
        return redirect(url_for('paste.index'), code=303)

    return bp
