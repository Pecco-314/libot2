import eventlet
eventlet.monkey_patch()

import os
import sys
import subprocess
from collections import deque
from flask import Flask, render_template, request, send_file
from flask_socketio import SocketIO
from pathlib import Path

from src.common.utils import ROOT, load_env_file

load_env_file()

# 配置路径
NAPCAT_PATH = Path(os.environ.get("NAPCAT_PATH"))
NAPCAT_LOG_FILE = ROOT / "logs" / "napcat.log"
QRCODE_PATH = NAPCAT_PATH / "opt/QQ/resources/app/app_launcher/napcat/cache/qrcode.png"
RESTART_COMMAND = f"{ROOT}/libotctl restart napcat"

app = Flask(__name__, 
            template_folder=str(ROOT / "src/web/templates"),
            static_folder=str(ROOT / "src/web/static"))
socketio = SocketIO(app, cors_allowed_origins="*")

def get_history(n=20):
    if not NAPCAT_LOG_FILE.exists(): return []
    try:
        with open(NAPCAT_LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
            return list(deque(f, n))
    except: return []

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/qrcode')
def qrcode_page():
    return render_template('qrcode.html')

@app.route('/get_qrcode_img')
def get_qrcode_img():
    if QRCODE_PATH.exists():
        return send_file(QRCODE_PATH, mimetype='image/png', download_name='qrcode.png', max_age=0)
    return "二维码文件暂未生成", 404

@socketio.on('connect')
def handle_connect():
    history = get_history(20)
    if history:
        socketio.emit('log_message', {'data': "".join(history)}, room=request.sid)

def tail_logs():
    NAPCAT_LOG_FILE.parent.mkdir(exist_ok=True)
    if not NAPCAT_LOG_FILE.exists(): NAPCAT_LOG_FILE.touch()
    with open(NAPCAT_LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
        f.seek(0, 2)
        while True:
            line = f.readline()
            if line: socketio.emit('log_message', {'data': line})
            socketio.sleep(0.1)

@socketio.on('restart_service')
def handle_restart():
    try:
        subprocess.run(RESTART_COMMAND, shell=True, check=True)
        socketio.emit('log_message', {'data': '\n\033[1;32m[System] 服务已成功重启，日志已重定向...\033[0m\n'})
    except Exception as e:
        socketio.emit('log_message', {'data': f'\n\033[1;31m[Error] 重启失败: {str(e)}\033[0m\n'})

if __name__ == '__main__':
    socketio.start_background_task(tail_logs)
    socketio.run(app, host='0.0.0.0', port=5000)