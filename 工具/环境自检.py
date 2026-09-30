# -*- coding: utf-8 -*-
"""
==========================================================
环境自检（先跑通这个，再往下走）
==========================================================
检查四件事：
  1. tqsdk 装好没、版本多少
  2. 账号能不能登录
  3. 五个品种代码在天勤里对不对（顺便看最新价）
  4. 快期模拟账户（TqKq）能不能用、有多少虚拟资金

运行：双击  工具/环境自检.bat
"""
import sys
import os

# 把项目根目录（天勤量化/）加到搜索路径
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config_account
import config_symbols
from tqsdk import TqApi, TqAuth, TqKq
from datetime import datetime, timedelta

print("=" * 62)
print(" 天勤量化 环境自检")
print("=" * 62)

# ---------- 1. 检查 tqsdk ----------
try:
    import tqsdk
    print(f"[1/4] tqsdk 版本：{tqsdk.__version__}   Python {sys.version.split()[0]}")
except Exception as e:
    print(f"[1/4] tqsdk 没装好：{e}")
    print("      解决：pip install tqsdk -i https://pypi.tuna.tsinghua.edu.cn/simple")
    raise SystemExit(1)

user, pwd = config_account.check()

# ---------- 2. 登录 ----------
print("[2/4] 正在登录信易服务器（正常 5~30 秒，请别关窗口）...", flush=True)
try:
    api = TqApi(TqKq(), auth=TqAuth(user, pwd))
    api.wait_update()
    print("      登录成功")
except Exception as e:
    print(f"      登录失败：{e}")
    print("      常见原因：邮箱没激活 / 密码错 / 用户名没填全")
    raise SystemExit(1)

# ---------- 3. 验证五个品种 ----------
print("[3/4] 检查五个品种代码（休市日也能查，约 10 秒）...", flush=True)
print(f"      {'品种':<8}{'主连代码':<18}{'当前主力':<16}{'最新价':>10}  {'一跳':>6}")
print("      " + "-" * 62)
for key in ["MA", "M", "RB", "V", "SA"]:
    name, main_code, mult, tick = config_symbols.SYMBOLS[key]
    try:
        q = api.get_quote(main_code)
        api.wait_update(deadline=datetime.now() + timedelta(seconds=8))
        last = q.last_price
        und = str(q.underlying_symbol) if q.underlying_symbol else "未知"
        price_str = f"{last:>10.2f}" if last == last else f"{'nan':>10}"
        print(f"      {name:<8}{main_code:<18}{und:<16}{price_str}  {tick * mult:>5}元")
    except Exception as e:
        print(f"      {name:<8}{main_code:<18} 取行情失败：{e}")
print("      (如果某个品种显示 nan 或报错，说明代码写法不对，告诉我)")

# ---------- 4. 模拟账户 ----------
acc = api.get_account()
print("[4/4] 快期模拟账户：")
print(f"      可用资金：{acc.available:,.2f} 元")
print(f"      动态权益：{acc.balance:,.2f} 元")
print("      这个账户和「快期 APP」是互通的，手机上也能看到持仓")

api.close()

print("=" * 62)
print(" 全部通过！")
print(" 下一步：双击根目录「跑回测.bat」调出主菜单，")
print("         或直接进 策略/<编号>_<名>/回测/<品种>/ 双击 回测.bat")
print("（2026-09-27：池化结构已收档 —— 「策略池/ 品种池/ 组合/」都不在了，")
print("  现在只有 策略/ 一个工作区，里面 4 个编号目录 × 12 个品种。）")
print("=" * 62)
