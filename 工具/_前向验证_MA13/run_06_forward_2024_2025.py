# -*- coding: utf-8 -*-
"""
MA13 超短线 · 前向验证运行器（隔离）
====================================
只读复用 策略/06_MA13超短线/回测/甲醇/MA13超短线_回测.py 的策略逻辑与报告函数，
仅注入前向窗口 2024-01-01~2025-12-31，**不改动任何策略参数**。

纪律：
  - 参数与开发窗（2021-2023）完全一致（未调参）；
  - 只看一次，本窗跑完即作废，绝不据此改参重跑；
  - 串行、禁并发（单次单品种）。

判定线（见 预注册卡_MA13前向验证.md）：MDES≈1.39（K=1, T=2年）。
  前向夏普 ≥ 1.39 且为正 ⇒ PASS（有 edge 证据）；否则 FAIL。
"""
import os
import sys
import json
import pandas as pd

# ---- 项目根 ----
_ROOT = os.path.dirname(os.path.abspath(__file__))
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# ---- 导入 06 策略模块（不执行其 __main__）----
_BT_DIR = os.path.join(_ROOT, "策略", "06_MA13超短线", "回测", "甲醇")
sys.path.insert(0, _BT_DIR)
import MA13超短线_回测 as M

FWD_START = pd.Timestamp("2024-01-01")
FWD_END = pd.Timestamp("2025-12-31")
MDES = 1.386  # 1.96 / sqrt(2.0)


def main():
    # 仅注入窗口；所有策略常量（MA_N/SLOPE_N/ATR_N/...）沿用模块默认，未改
    out, eq_series, dt_series, trades_rec, kline_history = M.run_backtest(
        start=FWD_START, end=FWD_END
    )
    print(json.dumps({k: v for k, v in out.items() if k != "cost_assumption"},
                     ensure_ascii=False, indent=2), flush=True)

    res_dir = os.path.join(_ROOT, "回测结果", "06_MA13超短线", "甲醇")
    os.makedirs(res_dir, exist_ok=True)
    base = f"{out['symbol_cn']}_MA13超短线_5min_前向窗{out['start']}_{out['end']}"
    html_path = os.path.join(res_dir, base + ".html")
    txt_path = os.path.join(res_dir, base + ".txt")
    json_path = os.path.join(res_dir, base + "_参数.json")

    # sample_mode="OOS" ⇒ 报告标注为「前向验证窗（未看）」，判定线提示 MDES≈1.39
    html = M.build_html(out, eq_series, dt_series, trades_rec, kline_history,
                        sample_mode="OOS")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(f"# 甲醇 MA13 超短线 逐笔成交 前向窗{out['start']}~{out['end']}\n")
        f.write(f"# 笔数={out['trades']} 胜率={out['win_rate']*100:.1f}% 夏普={out['sharpe']:.2f} "
                f"收益率={out['total_return']*100:.1f}% 最大回撤={out['max_drawdown']*100:.1f}%\n")
        f.write("方向,开仓时间,平仓时间,开仓价,平仓价,手数,盈亏(元),每手(点),离场原因\n")
        for t in trades_rec:
            f.write(f"{t['dir']},{t['entry_dt']},{t['exit_dt']},{t['entry']},{t['exit']},"
                    f"{t['hand']},{t['pnl']:.0f},{t['pnl_per_hand']},{t['reason']}\n")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    # ---- 事前判定 ----
    sharpe = out["sharpe"]
    passed = (sharpe >= MDES) and (sharpe > 0)
    verdict = "PASS（有 edge 证据）" if passed else "FAIL（无 edge / 样本内运气）"
    print(f"\n[判定] 前向夏普={sharpe:.2f}  MDES≈{MDES:.2f}  =>  {verdict}", flush=True)
    print(f"[done] 报告：{html_path}", flush=True)
    print(f"[done] 逐笔：{txt_path}", flush=True)
    print(f"[done] 参数：{json_path}", flush=True)


if __name__ == "__main__":
    main()
