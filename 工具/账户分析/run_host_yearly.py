# -*- coding: utf-8 -*-
"""
分年份跑「布林带 / 双均线」回测，抽取四层指标（毛收益 / 成本 / 净收益 / 尾部）。

用途：给「盘中量化」准备宿主。只调用 lib.engine 现有回测函数，不改任何策略逻辑。
口径与策略脚本一致（2026-10-13 统一口径）：20 万本金 / 1% 波动率预算 / 手数夹 [1,8] / 1 跳滑点。

用法：
    python run_host_yearly.py            # 默认 5 个核心品种 × 2 策略 × 3 年
    python run_host_yearly.py V MA       # 只跑指定品种
"""
import sys
import os
import io
import re
import json
from datetime import date, timedelta

ROOT = r"C:\Users\cc\Desktop\天勤量化"
sys.path.insert(0, ROOT)

from lib import engine
from lib.strategy_dirs import 策略目录名
import config_account
import config_symbols

INIT_BALANCE = 200000
YEARS = [2024, 2025, 2026]
# 5 个核心品种（布林带与双均线都有的交集）
CORE_VARIETIES = ["V", "MA", "SA", "RB", "M"]

def _year_range(y):
    eval_start = date(y, 1, 1)
    end = date(y, 12, 31) if y < 2026 else date(2026, 11, 4)
    start = eval_start - timedelta(days=7)   # 预热，与策略脚本同口径
    return start, end, eval_start

def _call_boll(sym, start, end, eval_start, tag):
    return engine.run_backtest_boll(
        sym, period=20, k=2.0, volume=1, start=start, end=end, eval_start=eval_start,
        init_balance=INIT_BALANCE, kline_sec=1800, strategy_name="布林带",
        open_html=False, sample_tag=tag, slip_ticks=1, atr_period=14,
        atr_stop_mult=1.0, risk_pct=1.0, risk_hand_min=1, risk_hand_max=8,
        time_stop_bars=20)

def _call_dual(sym, start, end, eval_start, tag):
    return engine.run_backtest(
        sym, engine.trend_reverse_signal, 5, 20, 1, start, end, INIT_BALANCE, 1800,
        strategy_name="双均线", open_html=False, eval_start=eval_start, sample_tag=tag,
        pass_dir=True, ma_filter=60, slip_ticks=1, atr_stop_mult=2.0, breakeven_atr=0.0,
        atr_period=14, risk_pct=1.0, risk_hand_min=1, risk_hand_max=8, no_open_window_min=30)

def _money(s):
    s = s.strip().replace(",", "").replace("¥", "").replace("+", "")
    try:
        return float(s)
    except ValueError:
        return None

def _pct(s):
    s = s.strip().replace("%", "").replace("+", "")
    try:
        return float(s)
    except ValueError:
        return None

def _extract(html_path):
    """从报告 HTML 抽取 结束权益/初始资金/总手续费/最大回撤/收益率/夏普。"""
    with io.open(html_path, encoding="utf-8", errors="replace") as f:
        s = f.read()
    def card(key):
        m = re.search(r'class="k">' + re.escape(key) + r'</div><div class="v[^"]*">([^<]+)</div>', s)
        return m.group(1).strip() if m else None
    bal = _money(card("结束权益"))
    init = _money(card("初始资金"))
    cost = _money(card("总手续费"))
    mdd = _pct(card("最大回撤"))
    ror = _pct(card("收益率"))
    shp = _money(card("夏普率"))
    if bal is None or init is None or cost is None:
        return None
    net = bal - init
    gross = net + cost
    return dict(net=net, cost=cost, gross=gross, mdd=mdd, ror=ror, sharpe=shp,
                final=bal, init=init)

def main():
    varieties = sys.argv[1:] or CORE_VARIETIES
    results = []
    for strategy, fn in [("布林带", _call_boll), ("双均线", _call_dual)]:
        for y in YEARS:
            start, end, eval_start = _year_range(y)
            tag = str(y)
            for sym in varieties:
                name = config_symbols.get(sym)[0]
                print(f"[跑] {strategy} {name} {y}  {start}~{end}", flush=True)
                try:
                    fn(sym, start, end, eval_start, tag)
                    out_dir = os.path.join(ROOT, "回测结果", 策略目录名(strategy), name)
                    html_path = os.path.join(out_dir, f"{name}_{strategy}_30min_{tag}.html")
                    d = _extract(html_path)
                    if d is None:
                        print(f"    [失败] 报告缺字段: {html_path}", flush=True)
                        continue
                    d.update(strategy=strategy, year=y, sym=sym, name=name)
                    results.append(d)
                    print(f"    [ok] 毛 {d['gross']:+,.0f} | 成本 {d['cost']:,.0f} | "
                          f"净 {d['net']:+,.0f} ({d['ror']:.2f}%) | 回撤 {d['mdd']}% | 夏普 {d['sharpe']}",
                          flush=True)
                except Exception as e:
                    print(f"    [异常] {type(e).__name__}: {e}", flush=True)
    # 汇总落 JSON
    out_json = os.path.join(ROOT, "工具", "账户分析", "host_yearly_result.json")
    with io.open(out_json, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n[完成] 共 {len(results)} 条，已写 {out_json}", flush=True)

if __name__ == "__main__":
    main()
