# -*- coding: utf-8 -*-
"""
config_account.py 模板 —— 复制本文件为 config_account.py 后填入真实账号。
config_account.py 已被 .gitignore 排除，真实账号密码永不进入 git 仓库。
注册入口：https://account.shinnytech.com/
"""

# ---------- 主账号：回测 / 研发专用 ----------
USER = "在这里填主账号"
PASSWORD = "在这里填主账号密码"

# ---------- 模拟盘账号：模拟盘进程专用 ----------
LIVE_USER = "在这里填模拟盘账号"
LIVE_PASSWORD = "在这里填模拟盘账号密码"


def check():
    if not USER or not PASSWORD or "填" in USER:
        raise SystemExit("还没填主账号：请编辑 config_account.py")
    return USER, PASSWORD


def check_live():
    if not LIVE_USER or not LIVE_PASSWORD or "填" in LIVE_USER:
        raise SystemExit("还没填模拟盘账号：请编辑 config_account.py")
    return LIVE_USER, LIVE_PASSWORD
