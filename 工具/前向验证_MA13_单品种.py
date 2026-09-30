# -*- coding: utf-8 -*-
"""
MA13 超短线 · 单品种前向验证跑批（2024-2025 / OOS）
用法：python 前向验证_MA13_单品种.py <品种中文名>
例：  python 前向验证_MA13_单品种.py 豆粕
说明：
  - 逐个 import 各品种目录下的 MA13超短线_回测.py（不改动原脚本）
  - 注入前向窗 2024-01-01~2025-12-31，sample_mode="OOS"
  - 结果落盘 回测结果/06_MA13超短线/<品种>/ 前向窗*.html|txt|json
  - 每个品种单独进程（tqsdk 禁并发、每次新建会话最干净）
"""
import sys, os, json, importlib.util
import pandas as pd

BASE = r"C:\Users\cc\Desktop\天勤量化"

def main():
    if len(sys.argv) < 2:
        print("用法: python 前向验证_MA13_单品种.py <品种中文名>", flush=True)
        sys.exit(2)
    variety = sys.argv[1]
    mod_dir = os.path.join(BASE, "策略", "06_MA13超短线", "回测", variety)
    mod_path = os.path.join(mod_dir, "MA13超短线_回测.py")
    if not os.path.isfile(mod_path):
        print(f"[错误] 找不到 {mod_path}", flush=True)
        sys.exit(1)

    spec = importlib.util.spec_from_file_location(f"ma13_fwd_{variety}", mod_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    START = pd.Timestamp("2024-01-01")
    END   = pd.Timestamp("2025-12-31")
    print(f"[{variety}] 前向窗 {START.date()} ~ {END.date()} 开始...", flush=True)
    out, eq_series, dt_series, trades_rec, kline_history = mod.run_backtest(start=START, end=END)
    html = mod.build_html(out, eq_series, dt_series, trades_rec, kline_history, sample_mode="OOS")

    res_dir = os.path.join(BASE, "回测结果", "06_MA13超短线", variety)
    os.makedirs(res_dir, exist_ok=True)
    base_name = f"{mod.name_cn}_MA13超短线_5min_前向窗{out['start']}_{out['end']}"
    with open(os.path.join(res_dir, base_name + ".html"), "w", encoding="utf-8") as f:
        f.write(html)
    with open(os.path.join(res_dir, base_name + "_参数.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    with open(os.path.join(res_dir, base_name + ".txt"), "w", encoding="utf-8") as f:
        f.write(f"# {mod.name_cn} MA13 超短线 逐笔成交 前向窗{out['start']}~{out['end']}\n")
        f.write(f"# 笔数={out['trades']} 胜率={out['win_rate']*100:.1f}% 夏普={out['sharpe']:.2f} "
                f"收益率={out['total_return']*100:.1f}% 最大回撤={out['max_drawdown']*100:.1f}%\n")
        f.write("方向,开仓时间,平仓时间,开仓价,平仓价,手数,盈亏(元),每手(点),离场原因\n")
        for t in trades_rec:
            f.write(f"{t['dir']},{t['entry_dt']},{t['exit_dt']},{t['entry']},{t['exit']},"
                    f"{t['hand']},{t['pnl']:.0f},{t['pnl_per_hand']},{t['reason']}\n")

    print(f"[done {variety}] 收益率={out['total_return']*100:.1f}% 夏普={out['sharpe']:.2f} "
          f"回撤={out['max_drawdown']*100:.1f}% 笔数={out['trades']} -> {base_name}.html", flush=True)

if __name__ == "__main__":
    main()
