# -*- coding: utf-8 -*-
"""
布林带 2016-2019 探针（独立进程，每品种一个，tqsdk 禁并发）
==========================================================
用法: python 探针验证_布林带_2016_2019.py <品种中文名>

直接复用 07_布林带 回测脚本里的全部参数（import 模块读常量，不跑其 __main__），
仅把回测窗注入为 2016-01-01 ~ 2019-12-31，调用同一套 engine.run_backtest_boll。

结果落盘：回测结果/03_布林带/<品种>/<品种>_布林带_30min_probe1619.{html,txt,json}
（sample_tag="probe1619"，与 in/out 互不覆盖）
open_html=False：不弹浏览器，落盘即可（符合用户交付习惯）。
"""
import os
import sys
import importlib.util
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
while not os.path.exists(os.path.join(ROOT, "config_symbols.py")):
    _up = os.path.dirname(ROOT)
    if _up == ROOT:
        raise RuntimeError("未找到项目根（config_symbols.py）")
    ROOT = _up
sys.path.insert(0, ROOT)
from lib import engine  # noqa: E402

VNAME = sys.argv[1]
SCRIPT = os.path.join(ROOT, "策略/07_布林带/回测", VNAME, "布林带_回测.py")
if not os.path.isfile(SCRIPT):
    print(f"[错误] 找不到脚本: {SCRIPT}")
    sys.exit(2)

# 仅加载模块、读取参数常量（__main__ 守卫使其不会自动回测）
spec = importlib.util.spec_from_file_location(f"boll_{VNAME}", SCRIPT)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

START = date(2016, 1, 1) - timedelta(days=7)   # 预热：前移 1 周（与 07 脚本同口径）
END = date(2019, 12, 31)
EVAL = date(2016, 1, 1)

print(f"[探针-定稿] 布林带 {VNAME} | 2016-2019 | START={START} END={END} eval={EVAL} | 定稿口径: 本金20万 risk_pct=1.0% 手数[1,10] ATR浮动 | 信号=BOLL(20,2)/1×ATR止损/30min/时间止损20根(同07)")
engine.run_backtest_boll(
    m.SYMBOL_KEY, period=m.BOLL_PERIOD, k=m.BOLL_K, volume=m.VOLUME,
    start=START, end=END, eval_start=EVAL,
    init_balance=200000, kline_sec=m.KLINE_SEC,
    strategy_name="布林带", open_html=False, sample_tag="probe1619",
    slip_ticks=1,
    atr_period=m.ATR_PERIOD,
    atr_stop_mult=m.ATR_STOP_MULT,
    risk_pct=1.0,
    risk_hand_min=1,
    risk_hand_max=10,
    time_stop_bars=m.TIME_STOP_BARS,
)
print(f"[done] {VNAME}")
