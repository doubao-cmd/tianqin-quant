# -*- coding: utf-8 -*-
"""
分年份跑「06 MA13 超短线」回测，抽取四层指标（毛收益 / 成本 / 净收益 / 尾部）。

用途：给「盘中量化」准备宿主，验证 06 的「整段毛 +47,962」是否也随年份翻转。
只加载 MA13 自包含脚本并调用其 run_backtest(start, end)，不改任何策略逻辑。

★ 口径对齐布林带/双均线（run_host_yearly.py）：
    20 万本金 / 1% 风险 / 手数夹 [1,8] / **1 跳滑点**。
    MA13 脚本默认「同步模拟盘实测滑点≈0 跳」（SYNC_SLIP_LIVE=True），
    这里覆盖为 1 跳（SYNC_SLIP_LIVE=False → SLIP=1），其余执行层开关保持默认
    （止损基准 close[i-1]、尾盘守卫、时间止损成交价——这些是 06 策略特征，不动）。

用法：
    python run_host_yearly_ma13.py            # 默认 5 核心品种 × 3 年
    python run_host_yearly_ma13.py V MA       # 只跑指定品种
"""
import sys
import os
import io
import json
import importlib.util
from datetime import date, timedelta

import pandas as pd

ROOT = r"C:\Users\cc\Desktop\天勤量化"
sys.path.insert(0, ROOT)

import config_symbols  # noqa: E402

MA13_PY = os.path.join(ROOT, "策略", "06_MA13超短线", "回测", "_多年", "MA13超短线_回测.py")

INIT = 200000.0
YEARS = [2024, 2025, 2026]
CORE_VARIETIES = ["V", "MA", "SA", "RB", "M"]


def _year_range(y):
    eval_start = date(y, 1, 1)
    end = date(y, 12, 31) if y < 2026 else date(2026, 11, 4)
    return pd.Timestamp(eval_start - timedelta(days=7)), pd.Timestamp(end)


def _load_module():
    spec = importlib.util.spec_from_file_location("ma13_bt", MA13_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # ★ 对齐布林带/双均线口径：1 跳滑点（关闭"同步模拟盘实测≈0 跳"）
    mod.SYNC_SLIP_LIVE = False
    mod.SLIP = 1.0
    return mod


def main():
    varieties = sys.argv[1:] or CORE_VARIETIES
    mod = _load_module()
    results = []
    for y in YEARS:
        start, end = _year_range(y)
        for sym in varieties:
            name_cn, main_code, MULT, TICK = config_symbols.get(sym)
            COMM = float(config_symbols.COST[sym])
            # 覆盖模块级品种相关变量（run_backtest 内部读这些全局量）
            mod.SYMBOL_KEY = sym
            mod.name_cn = name_cn
            mod.main_code = main_code
            mod.MULT = MULT
            mod.TICK = TICK
            mod.COMM = COMM
            print(f"[跑] MA13 {name_cn} {y}  {start.date()}~{end.date()}", flush=True)
            try:
                out, _eq, _dt, trades, _kline = mod.run_backtest(start=start, end=end)
                net = out["final_equity"] - INIT
                comm = float(out["commission"])
                # 滑点总额 = SLIP * TICK * MULT * 2 * Σhand（开平各一次）
                slip_total = sum(mod.SLIP * TICK * MULT * 2.0 * t["hand"] for t in trades)
                cost = comm + slip_total
                gross = net + cost
                d = dict(net=round(net, 2), cost=round(cost, 2), gross=round(gross, 2),
                         mdd=round(float(out["max_drawdown"]) * 100, 2),
                         ror=round(float(out["total_return"]) * 100, 4),
                         sharpe=float(out["sharpe"]), final=float(out["final_equity"]),
                         init=INIT, strategy="MA13", year=y, sym=sym, name=name_cn,
                         n_trades=int(out["trades"]))
                results.append(d)
                print(f"    [ok] 毛 {gross:+,.0f} | 成本 {cost:,.0f} | "
                      f"净 {net:+,.0f} ({d['ror']:.2f}%) | 回撤 {d['mdd']}% | "
                      f"夏普 {d['sharpe']} | 笔数 {d['n_trades']}", flush=True)
            except Exception as e:
                print(f"    [异常] {type(e).__name__}: {e}", flush=True)
    out_json = os.path.join(ROOT, "工具", "账户分析", "host_yearly_result_ma13.json")
    with io.open(out_json, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n[完成] 共 {len(results)} 条，已写 {out_json}", flush=True)


if __name__ == "__main__":
    main()
