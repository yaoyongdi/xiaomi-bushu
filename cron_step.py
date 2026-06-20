#!/usr/bin/env python3
# cron_step.py — 宝塔计划任务：触发所有已启用账号的刷步
# 用法: python3 /www/wwwroot/bs.adi0618.com/cron_step.py
# 宝塔 cron: 0 8,10,12,14,16,18,22 * * *

import sqlite3, time, json, random, os, urllib.request, sys
from datetime import datetime

# ===== 配置 =====
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'step_sign.db')
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN') or open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', '.github_token')).read().strip()
REPO = 'yaoyongdi/xiaomi-bushu'
WORKFLOW = 'web-dispatch.yml'
BRANCH = 'master'

# ===== 日志 =====
def log(msg):
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{now}] {msg}"
    print(line)
    sys.stdout.flush()


def trigger_github(phone, password, step):
    """触发 GitHub Actions workflow dispatch"""
    url = f"https://api.github.com/repos/{REPO}/actions/workflows/{WORKFLOW}/dispatches"
    payload = json.dumps({
        "ref": BRANCH,
        "inputs": {
            "phone": phone,
            "password": password,
            "step": str(step),
            "min_step": "18000",
            "max_step": "25000",
        }
    }).encode('utf-8')

    req = urllib.request.Request(url, data=payload, method='POST')
    req.add_header('Authorization', f'Bearer {GITHUB_TOKEN}')
    req.add_header('Accept', 'application/vnd.github.v3+json')
    req.add_header('Content-Type', 'application/json')
    req.add_header('User-Agent', 'Baota-Cron/1.0')

    try:
        resp = urllib.request.urlopen(req, timeout=15)
        return resp.status == 204, f"[自动] HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')[:300]
        return False, f"[自动] HTTP {e.code}: {body}"
    except Exception as e:
        return False, f"[自动] {e}"


def get_last_success_step(account_id):
    """获取历史上一次成功步数"""
    conn = sqlite3.connect(DB)
    row = conn.execute(
        "SELECT step FROM step_logs WHERE mi_account_id = ? AND status = 'success' "
        "ORDER BY id DESC LIMIT 1",
        (account_id,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


def get_progressive_step(account_id, min_step, max_step):
    """递进式步数：基于历史上一次成功步数 +500~2000"""
    last = get_last_success_step(account_id)
    if last and last > 0:
        new_step = last + random.randint(500, 2000)
        return min(new_step, max_step)
    return random.randint(min_step, max(min_step + 3000, max_step // 2))


def add_step_log(user_id, account_id, phone, step, status, message):
    """写入日志到数据库"""
    conn = sqlite3.connect(DB)
    conn.execute(
        "INSERT INTO step_logs (user_id, mi_account_id, account_phone, step, status, message) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, account_id, phone, step, status, message)
    )
    conn.execute(
        "UPDATE mi_accounts SET last_run_at = datetime('now'), last_result = ? WHERE id = ?",
        (status, account_id)
    )
    conn.commit()
    conn.close()


def main():
    log("========== 宝塔计划任务：自动刷步开始 ==========")

    if not os.path.exists(DB):
        log(f"❌ 数据库不存在: {DB}")
        return

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT a.*, u.username FROM mi_accounts a "
        "JOIN web_users u ON a.user_id = u.id "
        "WHERE a.enabled = 1 AND u.enabled = 1"
    ).fetchall()
    conn.close()

    accounts = [dict(r) for r in rows]
    log(f"共 {len(accounts)} 个已启用账号")

    if len(accounts) == 0:
        log("无已启用账号，退出")
        return

    for i, acc in enumerate(accounts):
        phone = acc['phone']
        min_step = acc.get('min_step', 18000)
        max_step = acc.get('max_step', 25000)
        log(f"[{i+1}/{len(accounts)}] 处理: {phone} (用户: {acc['username']})")

        # 检查是否已达最大步数
        last_step = get_last_success_step(acc['id'])
        if last_step is not None and last_step >= max_step:
            log(f"  ⏭️ 已达最大步数 {max_step} (上次: {last_step})，跳过自动任务")
            add_step_log(
                acc['user_id'], acc['id'], phone,
                last_step, 'skipped',
                f'[自动] 已达最大步数 {max_step}，跳过'
            )
            continue

        # 递进式步数
        step = get_progressive_step(acc['id'], min_step, max_step)
        log(f"  目标步数: {step} (范围: {min_step}-{max_step})")

        # 触发 GitHub Actions
        ok, msg = trigger_github(phone, acc['password'], step)
        status = 'pending' if ok else 'failed'
        log(f"  结果: {'✅ 已提交' if ok else '❌ 失败'} — {msg}")

        # 写入日志
        add_step_log(acc['user_id'], acc['id'], phone, step, status, msg)

        # 账号间延迟
        if i < len(accounts) - 1:
            delay = random.uniform(3, 6)
            log(f"  等待 {delay:.1f}s ...")
            time.sleep(delay)

    log("========== 自动刷步任务完成 ==========")


if __name__ == '__main__':
    main()
