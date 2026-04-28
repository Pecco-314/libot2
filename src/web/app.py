from gevent import monkey
monkey.patch_all()

import os
import subprocess
from pathlib import Path
from functools import wraps
from flask import Flask, render_template, request, session, redirect, url_for, jsonify, send_file
from flask_socketio import SocketIO
from werkzeug.security import generate_password_hash, check_password_hash

from src.common.utils import ROOT, load_env_file, init_logger
from src.db.state import init_state_db, get_state, set_state
from src.common.log_parser import LogStreamParser, parse_log_iterable

load_env_file()

logger = init_logger("web")

# 配置路径
NAPCAT_PATH = Path(os.environ.get("NAPCAT_PATH"))
LOGS_DIR = ROOT / "logs"
QRCODE_PATH = NAPCAT_PATH / "opt/QQ/resources/app/app_launcher/napcat/cache/qrcode.png"
RESTART_COMMAND = f"{ROOT}/libotctl restart napcat"

app = Flask(__name__, 
            template_folder=str(ROOT / "src/web/templates"),
            static_folder=str(ROOT / "src/web/static"))

@app.after_request
def log_access(response):
    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    logger.info(f"API Access: {ip} - {request.method} {request.path} - {response.status_code}")
    return response

app.secret_key = os.environ.get("FLASK_SECRET_KEY")
if not app.secret_key:
    raise ValueError("FLASK_SECRET_KEY 未设置")

socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent')

init_state_db()
if get_state("web_password") is None:
    initial_password = os.environ.get("FLASK_INITIAL_PASSWORD")
    set_state("web_password", generate_password_hash(initial_password))

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            # 如果是 API 请求，返回 JSON 而不是重定向，防止前端解析 HTML 出错
            if request.path.startswith('/api/'):
                return jsonify({"error": "unauthorized"}), 401
            return redirect(url_for('login_page', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/login', methods=['GET', 'POST'])
def login_page():
    if request.method == 'POST':
        if check_password_hash(get_state("web_password"), request.form.get('password', '')):
            session.permanent = True
            session['logged_in'] = True
            logger.info(f"用户已登录: {request.headers.get('X-Forwarded-For', request.remote_addr)}")
            return redirect(request.args.get('next') or url_for('index'))
        return render_template('login.html', error="密码错误")
    return render_template('login.html')

@app.route('/')
@login_required
def index():
    log_files = sorted([f.name for f in LOGS_DIR.glob("*.log") if f.is_file()])
    return render_template('index.html', log_files=log_files)

@app.route('/qrcode')
def qrcode_page():
    return render_template('qrcode.html')

@app.route('/get_qrcode_img')
def get_qrcode_img():
    if QRCODE_PATH.exists():
        return send_file(QRCODE_PATH, mimetype='image/png', download_name='qrcode.png', max_age=0)
    return "二维码文件暂未生成", 404

@app.route('/api/logs/<filename>')
@login_required
def api_get_history(filename):
    file_path = LOGS_DIR / filename
    if ".." in filename or not file_path.exists(): 
        return jsonify([])
    
    # 接收前端传来的偏移量和每次加载的数量
    offset = int(request.args.get('offset', 0))
    limit = int(request.args.get('limit', 100))
    
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            # 读取全部行进行解析，确保多行日志不会在截断处被破坏
            lines = f.readlines()
            
        parsed_logs = list(parse_log_iterable(lines))
        total_logs = len(parsed_logs)
        
        # 如果请求的偏移量已经超过了总日志数，说明到头了
        if total_logs == 0 or offset >= total_logs:
            return jsonify([])
        
        # 计算切片的起始和结束索引（倒序往前推算）
        start_idx = max(0, total_logs - offset - limit)
        end_idx = total_logs - offset
        
        return jsonify(parsed_logs[start_idx:end_idx])
    except Exception as e:
        logger.error(f"读取日志历史失败: {e}")
        return jsonify([])

@socketio.on('restart_service')
def handle_restart(data):
    if not session.get('logged_in'): return
    
    # 获取前端传来的服务名
    service_name = data.get('service')

    if service_name == 'web':
        logger.warning(f"检测到非法请求：尝试从 API 重启 web 服务。")
        socketio.emit('system_msg', {'data': '操作被拒绝：Web 服务不支持在线重启。'})
        return

    # 严格的安全校验：只允许字母、数字、短横线或下划线
    if not service_name.replace('_', '').replace('-', '').isalnum():
        logger.warning(f"拦截到非法的重启服务名: {service_name}")
        return
        
    command = f"{ROOT}/libotctl restart {service_name}"
    logger.info(f"执行动态重启指令: {command}")
    
    try:
        subprocess.run(command, shell=True, check=True)
    except Exception as e:
        logger.error(f"重启 {service_name} 失败: {e}")

def tail_single_file(file_path):
    parser = LogStreamParser()
    if not file_path.exists(): 
        file_path.touch()
        
    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        f.seek(0, 2)
        partial_line = ""
        while True:
            line = f.readline()
            if line:
                partial_line += line
                if partial_line.endswith('\n'):
                    parsed = parser.feed(partial_line)
                    partial_line = ""
                    if parsed:
                        parsed['filename'] = file_path.name
                        socketio.emit('log_event', parsed)
            else:
                if not partial_line:
                    parsed = parser.flush()
                    if parsed:
                        parsed['filename'] = file_path.name
                        socketio.emit('log_event', parsed)
                socketio.sleep(0.1)

def start_tail_tasks():
    LOGS_DIR.mkdir(exist_ok=True)
    # 为每一个日志文件启动一个独立的监听协程
    for log_file in LOGS_DIR.glob("*.log"):
        socketio.start_background_task(tail_single_file, log_file)

@app.route('/logout')
def logout():
    session.clear()
    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    logger.info(f"用户已登出: {ip}")
    return redirect(url_for('login_page'))

@app.route('/api/change_password', methods=['POST'])
@login_required
def change_password():
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "msg": "无效的请求数据"}), 400
        
    new_pwd = data.get("new_password")
    confirm_pwd = data.get("confirm_password")
    
    if not new_pwd or new_pwd != confirm_pwd:
        return jsonify({"success": False, "msg": "密码为空或两次输入不一致"}), 400
    
    set_state("web_password", generate_password_hash(new_pwd))
    logger.info("用户修改了 Web 密码")
    return jsonify({"success": True, "msg": "密码已成功修改"})

if __name__ == '__main__':
    socketio.start_background_task(start_tail_tasks)
    logger.info("Web 服务已启动")
    socketio.run(app, host='0.0.0.0', port=5000)