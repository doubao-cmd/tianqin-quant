# -*- coding: utf-8 -*-
"""
MA13 超短线 · 剩余 7 品种 2026 探针前向（探索性，非期末考）
用法：python 探针验证_MA13_2026.py <品种>
说明：
  - 逐个 import 各品种目录下的 MA13超短线_回测.py（不改动原脚本）
  - 注入探针窗 2026-01-01~2026-09-26，sample_mode="OOS" 生成报告后做探针标签替换
  - 结果落盘 回测结果/06_MA13超短线/<品种>/探针窗2026...{html,json,txt}
  - 每个品种单独进程（tqsdk 禁并发、每次新建会话最干净）
  - ⚠️ 本探针刻意避开 2024-2025（期末考），仅作方向性观察，不套用 MDES 判定线
"""
import sys, os, json, importlib.util
import pandas as pd

BASE = r"C:\Users\cc\Desktop\天勤量化"
PROBE_START = pd.Timestamp("2026-01-01")
PROBE_END   = pd.Timestamp("2026-09-26")

def relabel(html: str) -> str:
    # 先替换长串特定文本（避免被下面的泛替换破坏）
    html = html.replace("前向验证窗（未看 / OOS）", "探针窗（2026 年至今，探索性前向、非期末考）")
    html = html.replace("前向验证窗（未看/OOS）", "探针窗（2026 年至今，探索性前向、非期末考）")
    html = html.replace(
        "一段我们从未看过、未调过参的干净窗上的回测，仅跑一次、跑完即作废。",
        "一段探索性前向窗，用于观察 MA13 在 2026 年是否仍有效；期末考（2024-2025）保留至最终拍板。")
    html = html.replace(
        "判定线 <b>MDES ≈ 1.39</b>（K=1，T=2 年）：夏普 ≥ 1.39 且为正 ⇒ <b>有 edge 证据</b>；否则样本内（2021-2023）结果作废，绝不回头调参。",
        "本探针不套用 MDES 判定线（那是期末考专用）；仅作方向性观察。")
    # 最后泛替换残留的 window_tag「前向窗」
    html = html.replace("前向窗", "探针窗2026")
    return html

def main():
    if len(sys.argv) < 2:
        print("用法: python 探针验证_MA13_2026.py <品种中文名>", flush=True)
        sys.exit(2)
    variety = sys.argv[1]
    mod_dir = os.path.join(BASE, "策略", "06_MA13超短线", "回测", variety)
    mod_path = os.path.join(mod_dir, "MA13超短线_回测.py")
    if not os.path.isfile(mod_path):
        print(f"[错误] 找不到 {mod_path}", flush=True)
        sys.exit(1)

    spec = importlib.util.spec_from_file_location(f"ma13_probe_{variety}", mod_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    print(f"[{variety}] 探针窗 {PROBE_START.date()} ~ {PROBE_END.date()} 开始...", flush=True)
    out, eq_series, dt_series, trades_rec, kline_history = mod.run_backtest(start=PROBE_START, end=PROBE_END)
    html = relabel(mod.build_html(out, eq_series, dt_series, trades_rec, kline_history, sample_mode="OOS"))

    res_dir = os.path.join(BASE, "回测结果", "06_MA13超短线", variety)
    os.makedirs(res_dir, exist_ok=True)
    base_name = f"{mod.name_cn}_MA13超短线_5min_探针窗2026-01-01_2026-09-26"
    with open(os.path.join(res_dir, base_name + ".html"), "w", encoding="utf-8") as f:
        f.write(html)
    with open(os.path.join(res_dir, base_name + "_参数.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    with open(os.path.join(res_dir, base_name + ".txt"), "w", encoding="utf-8") as f:
        f.write(f"# {mod.name_cn} MA13 超短线 探针窗2026(探索性前向,非期末考)\n")
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
