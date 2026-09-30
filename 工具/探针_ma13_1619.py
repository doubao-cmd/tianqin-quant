import os
# -*- coding: utf-8 -*-
"""MA13 死亡窗探针：16-19 年，3 个品种串行跑（禁并发）。只取指标、不写报告。"""
import sys, os, importlib.util
import pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))
while not os.path.exists(os.path.join(ROOT, "config_symbols.py")):
    _up = os.path.dirname(ROOT)
    if _up == ROOT:
        raise RuntimeError("未找到项目根（config_symbols.py）")
    ROOT = _up
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FILES = {
    "焦煤": os.path.join(ROOT, "策略/06_MA13超短线/回测/焦煤/MA13超短线_回测.py"),
    "PVC":  os.path.join(ROOT, "策略/06_MA13超短线/回测/PVC/MA13超短线_回测.py"),
    "豆粕": os.path.join(ROOT, "策略/06_MA13超短线/回测/豆粕/MA13超短线_回测.py"),
}

mods = {}
for name, path in FILES.items():
    spec = importlib.util.spec_from_file_location("ma13_" + name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    mods[name] = m

START, END = pd.Timestamp("2016-01-01"), pd.Timestamp("2019-12-31")
print(f"=== MA13 死亡窗探针 16-19（当前代码：本金保本+减本金利润满40万起等差加仓+保证金约束+硬上限200）===\n", flush=True)

for name in ["焦煤", "PVC", "豆粕"]:
    m = mods[name]
    out, *_ = m.run_backtest(START, END)
    tr = out["total_return"] * 100
    dd = out["max_drawdown"] * 100
    wr = out["win_rate"] * 100
    print(f"{name}({out['symbol']})  收益率 {tr:>10.1f}%  期末权益 {out['final_equity']:>14,.0f}  "
          f"最大回撤 {dd:>6.1f}%  夏普 {out['sharpe']:>6.2f}  胜率 {wr:>5.1f}%  "
          f"笔数 {out['trades']}  爆仓 {out['blown']}", flush=True)

print("\n[done] 探针结束", flush=True)
