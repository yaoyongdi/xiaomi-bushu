"""
步数助手 - Flask Web 应用
多用户刷步数系统 + 卡密管理
"""
import os
import sys
import json
import time
import random
import logging
from functools import wraps
from datetime import datetime, timezone, timedelta

from flask import Flask, request, jsonify, render_template, session, redirect, url_for
from apscheduler.schedulers.background import BackgroundScheduler
import pytz

from database import init_db, create_user, authenticate_user, get_user_by_id
from database import add_mi_account, get_user_mi_accounts, update_mi_account, delete_mi_account
from database import get_all_enabled_mi_accounts, add_step_log, update_step_log, get_user_logs, get_all_logs
from database import generate_card_keys, get_card_keys, get_all_users, toggle_user_status, get_stats
from database import get_system_config, set_system_config, update_admin_credentials, delete_user, delete_card_key, clear_all_logs
from github_runner import init_runner, run_single_github, check_run_status

# ===== 初始化 =====
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'step-sign-secret-key-change-me')
app.config['JSON_AS_ASCII'] = False

# GitHub Runner 配置
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN') or (open('/www/wwwroot/bs.adi0618.com/data/gh_token.txt').read().strip() if os.path.exists('/www/wwwroot/bs.adi0618.com/data/gh_token.txt') else None)
GITHUB_OWNER = 'yaoyongdi'
GITHUB_REPO = 'xiaomi-bushu'
init_runner(GITHUB_TOKEN, GITHUB_OWNER, GITHUB_REPO)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(BASE_DIR, 'data', 'app.log'), encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)


def get_progressive_step(account_id: int, min_step: int, max_step: int) -> int:
    """递进式步数：今日首次从min_step附近开始，之后每次+500~2000，不超过max_step"""
    import sqlite3 as _sqlite
    import random as _random
    db_path = os.path.join(BASE_DIR, 'data', 'step_sign.db')
    if os.path.exists(db_path):
        conn = _sqlite.connect(db_path)
        # 查历史上一次成功步数（不限今天，保证递进）
        row = conn.execute(
            "SELECT step FROM step_logs WHERE mi_account_id = ? AND status = 'success' ORDER BY id DESC LIMIT 1",
            (account_id,)
        ).fetchone()
        conn.close()
        if row and row[0] > 0:
            new_step = row[0] + _random.randint(500, 2000)
            if new_step > max_step:
                new_step = max_step
            return new_step
    # 首次或无从数据库，随机起步
    return _random.randint(min_step, max(min_step + 3000, max_step // 2))

os.makedirs(os.path.join(BASE_DIR, 'data'), exist_ok=True)
init_db()

# ===== 定时任务 =====
# 已迁移至系统 cron (宝塔计划任务) → cron_step.py
# APScheduler 已停用，避免与系统 cron 重复触发
# TZ = pytz.timezone('Asia/Shanghai')
# scheduler = BackgroundScheduler(timezone=TZ)
# scheduler.add_job(daily_step_task, 'cron', hour='8,10,12,14,16,18,22', minute='0', id='daily_step')
# scheduler.start()

# ===== 鉴权装饰器 =====

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('user_id'):
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({'success': False, 'message': '请先登录'}), 401
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('user_id'):
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({'success': False, 'message': '请先登录'}), 401
            return redirect(url_for('index'))
        if not session.get('is_admin'):
            return jsonify({'success': False, 'message': '需要管理员权限'}), 403
        return f(*args, **kwargs)
    return decorated


# ===== 页面路由 =====

@app.route('/')
def index():
    if session.get('user_id'):
        if session.get('is_admin'):
            return redirect(url_for('admin_page'))
        return redirect(url_for('dashboard'))
    return render_template('login.html')


@app.route('/register')
def register_page():
    return render_template('register.html')


@app.route('/dashboard')
@login_required
def dashboard():
    if session.get('is_admin'):
        return redirect(url_for('admin_page'))
    return render_template('dashboard.html')


@app.route('/admin')
@admin_required
def admin_page():
    return render_template('admin.html')


# ===== 认证 API =====

@app.route('/api/auth/register', methods=['POST'])
def api_register():
    data = request.get_json()
    username = (data.get('username') or '').strip()
    password = (data.get('password') or '').strip()
    card_key = (data.get('card_key') or '').strip()

    if not username or not password:
        return jsonify({'success': False, 'message': '用户名和密码不能为空'})
    if len(username) < 3:
        return jsonify({'success': False, 'message': '用户名至少3个字符'})
    if len(password) < 6:
        return jsonify({'success': False, 'message': '密码至少6位'})

    result = create_user(username, password, card_key if card_key else None)
    return jsonify(result)


@app.route('/api/auth/login', methods=['POST'])
def api_login():
    data = request.get_json()
    username = (data.get('username') or '').strip()
    password = (data.get('password') or '').strip()

    result = authenticate_user(username, password)
    if result['success']:
        session['user_id'] = result['user']['id']
        session['username'] = result['user']['username']
        session['is_admin'] = result['user']['is_admin']
    return jsonify(result)


@app.route('/api/auth/logout', methods=['POST'])
def api_logout():
    session.clear()
    return jsonify({'success': True})


@app.route('/api/auth/me')
def api_me():
    if not session.get('user_id'):
        return jsonify({'success': False})
    return jsonify({
        'success': True,
        'user': {
            'id': session['user_id'],
            'username': session['username'],
            'is_admin': session['is_admin'],
        }
    })


# ===== 小米账号管理 API =====

@app.route('/api/accounts', methods=['GET'])
@login_required
def api_accounts():
    accounts = get_user_mi_accounts(session['user_id'])
    return jsonify({'success': True, 'accounts': accounts})


@app.route('/api/accounts', methods=['POST'])
@login_required
def api_add_account():
    data = request.get_json()
    phone = (data.get('phone') or data.get('account') or '').strip()
    password = (data.get('password') or '').strip()
    min_step = int(data.get('min_step', 18000))
    max_step = int(data.get('max_step', 25000))

    if not phone or not password:
        return jsonify({'success': False, 'message': '账号和密码不能为空'})
    if min_step < 1000:
        return jsonify({'success': False, 'message': '最小步数不能少于1000'})
    if max_step > 99999:
        return jsonify({'success': False, 'message': '最大步数不能超过99999'})
    if min_step > max_step:
        return jsonify({'success': False, 'message': '最小步数不能大于最大步数'})

    result = add_mi_account(session['user_id'], phone, password, min_step, max_step)
    return jsonify(result)


@app.route('/api/accounts/<int:account_id>', methods=['PUT'])
@login_required
def api_update_account(account_id):
    data = request.get_json()
    result = update_mi_account(account_id, session['user_id'], **data)
    return jsonify(result)


@app.route('/api/accounts/<int:account_id>', methods=['DELETE'])
@login_required
def api_delete_account(account_id):
    result = delete_mi_account(account_id, session['user_id'])
    return jsonify(result)


# ===== 刷步执行 API =====

@app.route('/api/step/run/<int:account_id>', methods=['POST'])
@login_required
def api_run_single_account(account_id):
    """手动刷步 - 默认递进式，可传 mode=custom + step=具体步数"""
    accounts = get_user_mi_accounts(session['user_id'])
    target = next((a for a in accounts if a['id'] == account_id), None)
    if not target:
        return jsonify({'success': False, 'message': '账号不存在'})

    data = request.get_json(silent=True) or {}
    mode = data.get('mode', 'progressive')  # progressive | custom | random

    if mode == 'custom':
        step = int(data.get('step', 0))
        if step < 1000:
            return jsonify({'success': False, 'message': '步数至少1000'})
        result = run_single_github(target['phone'], target['password'],
                                   target['min_step'], target['max_step'], step=step)
    elif mode == 'progressive':
        step = get_progressive_step(target['id'], target['min_step'], target['max_step'])
        result = run_single_github(target['phone'], target['password'],
                                   target['min_step'], target['max_step'], step=step)
    else:
        # 默认递进模式
        step = get_progressive_step(target['id'], target['min_step'], target['max_step'])
        result = run_single_github(target['phone'], target['password'],
                                   target['min_step'], target['max_step'], step=step)

    if result['success']:
        mode_label = {
            'custom': '[手动/自定义]',
            'progressive': '[手动/递进]',
            'random': '[手动/随机]',
        }.get(mode, '[手动/递进]')
        log_id = add_step_log(
            session['user_id'], target['id'], target['phone'],
            step if mode != 'random' else 0, 'success',
            f"{mode_label} 已提交到 GitHub (Run #{result['run_id']})",
        )
        result['log_id'] = log_id
        result['step'] = step if mode != 'random' else 0
    return jsonify(result)


@app.route('/api/step/result/<int:log_id>', methods=['POST'])
@login_required
def api_update_step_result(log_id):
    """更新日志：前端轮询到结果后回写步数和状态"""
    data = request.get_json() or {}
    update_step_log(
        log_id,
        data.get('step', 0),
        data.get('status', 'failed'),
        data.get('message', ''),
    )
    return jsonify({'success': True})


@app.route('/api/step/status/<run_id>', methods=['GET'])
@login_required
def api_check_run_status(run_id):
    """查询 GitHub Actions 执行状态"""
    return jsonify(check_run_status(run_id))


@app.route('/api/logs/clear', methods=['POST'])
@login_required
def api_clear_my_logs():
    """清空当前用户的日志"""
    import sqlite3
    db_path = os.path.join(BASE_DIR, 'data', 'step_sign.db')
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM step_logs WHERE user_id = ?", (session['user_id'],))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'message': '日志已清空'})


@app.route('/api/admin/logs/clear', methods=['POST'])
@admin_required
def api_admin_clear_logs():
    """管理员清空所有用户的日志"""
    clear_all_logs()
    return jsonify({'success': True, 'message': '所有日志已清空'})


@app.route('/api/step/run-all', methods=['POST'])
@login_required
def api_run_all_accounts():
    """手动批量执行所有账号刷步 - 通过 GitHub Actions"""
    accounts = get_user_mi_accounts(session['user_id'])
    enabled = [a for a in accounts if a['enabled']]

    if not enabled:
        return jsonify({'success': False, 'message': '没有已启用的账号'})

    results = []
    for i, acc in enumerate(enabled):
        if i > 0:
            time.sleep(3)  # GitHub API 间隔
        step = get_progressive_step(acc['id'], acc['min_step'], acc['max_step'])
        result = run_single_github(acc['phone'], acc['password'], acc['min_step'], acc['max_step'], step=step)
        add_step_log(
            session['user_id'], acc['id'], acc['phone'],
            step, 'success' if result['success'] else 'failed',
            f"[手动/递进] 已提交到 GitHub (Run #{result.get('run_id', 'unknown')})",
        )
        results.append({
            'phone': acc['phone'],
            **result,
        })

    success_count = sum(1 for r in results if r['success'])
    return jsonify({
        'success': True,
        'total': len(results),
        'success_count': success_count,
        'results': results,
    })


# ===== 日志 API =====

@app.route('/api/logs', methods=['GET'])
@login_required
def api_logs():
    limit = request.args.get('limit', 50, type=int)
    logs = get_user_logs(session['user_id'], limit)
    return jsonify({'success': True, 'logs': logs})


# ===== 管理端 API =====

@app.route('/api/admin/cards', methods=['GET'])
@admin_required
def api_admin_cards():
    page = request.args.get('page', 1, type=int)
    result = get_card_keys(page)
    return jsonify(result)


@app.route('/api/admin/cards/generate', methods=['POST'])
@admin_required
def api_admin_generate_cards():
    data = request.get_json()
    count = data.get('count', 10)
    if count < 1 or count > 100:
        return jsonify({'success': False, 'message': '数量范围 1-100'})
    result = generate_card_keys(count)
    return jsonify(result)


@app.route('/api/admin/users', methods=['GET'])
@admin_required
def api_admin_users():
    users = get_all_users()
    # 给每个用户附上账号数量
    from database import get_db
    conn = get_db()
    cursor = conn.cursor()
    for u in users:
        cursor.execute("SELECT COUNT(*) FROM mi_accounts WHERE user_id = ?", (u['id'],))
        u['account_count'] = cursor.fetchone()[0]
    conn.close()
    return jsonify({'success': True, 'users': users})


@app.route('/api/admin/users/<int:user_id>', methods=['GET', 'PUT'])
@admin_required
def api_admin_user_detail(user_id):
    from database import get_user_detail, admin_update_user, admin_upsert_mi_account
    if request.method == 'GET':
        user = get_user_detail(user_id)
        if not user:
            return jsonify({'success': False, 'message': '用户不存在'})
        return jsonify({'success': True, 'user': user})
    # PUT
    data = request.get_json()
    username = (data.get('username') or '').strip() or None
    password = (data.get('password') or '').strip() or None
    enabled = data.get('enabled')
    if enabled is not None:
        enabled = bool(enabled)
    # 更新用户信息
    result = admin_update_user(user_id, username=username, password=password, enabled=enabled)
    if not result['success']:
        return jsonify(result)
    # 更新小米账号
    mi_data = data.get('mi_account')
    if mi_data:
        mi_phone = (mi_data.get('phone') or '').strip()
        mi_password = (mi_data.get('password') or '').strip()
        mi_min = mi_data.get('min_step')
        mi_max = mi_data.get('max_step')
        if mi_phone and mi_password and mi_min and mi_max:
            admin_upsert_mi_account(user_id, mi_phone, mi_password, int(mi_min), int(mi_max))
    return jsonify({'success': True, 'message': '用户信息已更新'})


@app.route('/api/admin/users/<int:user_id>/toggle', methods=['POST'])
@admin_required
def api_admin_toggle_user(user_id):
    data = request.get_json()
    result = toggle_user_status(user_id, data.get('enabled', True))
    return jsonify(result)


@app.route('/api/admin/users/<int:user_id>', methods=['DELETE'])
@admin_required
def api_admin_delete_user(user_id):
    result = delete_user(user_id)
    return jsonify(result)


@app.route('/api/admin/cards/<int:card_id>', methods=['DELETE'])
@admin_required
def api_admin_delete_card(card_id):
    result = delete_card_key(card_id)
    return jsonify(result)


@app.route('/api/admin/stats', methods=['GET'])
@admin_required
def api_admin_stats():
    return jsonify({'success': True, 'stats': get_stats()})


@app.route('/api/admin/logs', methods=['GET'])
@admin_required
def api_admin_logs():
    page = request.args.get('page', 1, type=int)
    return jsonify(get_all_logs(page))


@app.route('/api/admin/config', methods=['GET'])
@admin_required
def api_admin_get_config():
    reg_mode = get_system_config('open_registration') or 'card_only'
    return jsonify({'success': True, 'registration_mode': reg_mode})


@app.route('/api/admin/config', methods=['PUT'])
@admin_required
def api_admin_set_config():
    data = request.get_json()
    mode = data.get('registration_mode', 'card_only')
    if mode not in ('open', 'card_only'):
        return jsonify({'success': False, 'message': '无效模式'})
    set_system_config('open_registration', mode)
    return jsonify({'success': True, 'message': '设置已保存'})


@app.route('/api/admin/credentials', methods=['PUT'])
@admin_required
def api_admin_update_credentials():
    """修改管理员账号密码"""
    data = request.get_json()
    new_username = data.get('username', '').strip()
    new_password = data.get('password', '').strip()
    if not new_username or not new_password:
        return jsonify({'success': False, 'message': '用户名和密码不能为空'})
    if len(new_password) < 6:
        return jsonify({'success': False, 'message': '密码至少6位'})
    result = update_admin_credentials(new_username, new_password)
    return jsonify(result)


# ===== 健康检查 =====

@app.route('/api/health')
def api_health():
    return jsonify({'status': 'ok', 'time': datetime.now().isoformat()})


# ===== 错误处理 =====

@app.errorhandler(404)
def not_found(e):
    if request.path.startswith('/api/'):
        return jsonify({'success': False, 'message': '接口不存在'}), 404
    return render_template('404.html'), 404


@app.errorhandler(500)
def server_error(e):
    if request.path.startswith('/api/'):
        return jsonify({'success': False, 'message': '服务器内部错误'}), 500
    return render_template('500.html'), 500


# ===== 启动 =====

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 3458))
    debug = os.environ.get('DEBUG', 'true').lower() == 'true'
    logger.info(f"步数助手启动: http://0.0.0.0:{port}")
    app.run(host='0.0.0.0', port=port, debug=debug)

