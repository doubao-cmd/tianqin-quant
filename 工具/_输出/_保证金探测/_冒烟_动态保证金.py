# -*- coding: utf-8 -*-
"""
【冒烟 · 只读】动态保证金接入真实回测后的行为
==========================================================
三个对照（都用 PVC，30 分钟，2025-01-02 ~ 2025-03-31）：
  A  20 万权益，margin_cap_pct=0   —— 关风控（改前口径）
  B  20 万权益，margin_cap_pct=30  —— 开风控（改后默认）→ 与 A 手数应**完全一致**
  C  2 万权益，margin_cap_pct=30   —— 权益缩水 → 上限应收紧、手数被压

不写账本、不开浏览器、不改任何配置。
"""
import sys
import os

_ROOT = os.path.dirname(os.path.abspath(__file__))
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from datetime import date
from lib import engine

START = date(2025, 1, 2)
END = date(2025, 3, 31)
EVAL = date(2025, 1, 9)

BASE = dict(
    symbol_key="V", signal_fn=engine.trend_reverse_signal, n1=5, n2=20, volume=1,
    start=START, end=END, kline_sec=30 * 60,
    eval_start=EVAL, sample_tag="smoke",
    pass_dir=True, ma_filter=60, slip_ticks=1,
    atr_stop_mult=2.0, breakeven_atr=0.0, atr_period=14,
    risk_pct=1.0, risk_hand_min=1, risk_hand_max=8,
    no_open_window_min=30, open_html=False,
)


def run(tag, init_balance, cap):
    print("\n" + "#" * 96)
    print(f"#  {tag}  |  初始资金 {init_balance:,} 元  |  保证金上限 {cap}%")
    print("#" * 96)
    return engine.run_backtest(strategy_name=f"冒烟_{tag}", init_balance=init_balance,
                               margin_cap_pct=cap, **BASE)


if __name__ == "__main__":
    import io
    import contextlib

    outs = {}
    for tag, bal, cap in [("A_关风控20万", 200000, 0),
                          ("B_开风控20万", 200000, 30),
                          ("C_开风控2万", 20000, 30)]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            r = run(tag, bal, cap)
        outs[tag] = (buf.getvalue(), r)

    print("\n" + "=" * 96)
    print("  关键行摘录")
    print("=" * 96)
    for tag, (txt, r) in outs.items():
        print(f"\n----- {tag} -----")
        keys = ("波动率预算", "保证金上限", "目标 ", "本金", "最终", "收益率",
                "最大回撤", "夏普", "笔数")
        for line in txt.splitlines():
            if any(k in line for k in keys):
                print("   " + line.strip()[:170])

    print("\n" + "=" * 96)
    print("  手数一致性核对（A vs B：不触发时必须逐笔一致）")
    print("=" * 96)
    import re

    def hands(txt):
        return re.findall(r"目标 ([+-]?\d+) 手", txt)

    ha, hb, hc = hands(outs["A_关风控20万"][0]), hands(outs["B_开风控20万"][0]), hands(outs["C_开风控2万"][0])
    print(f"  A 关风控 20 万 手数序列（{len(ha)} 条）：{ha[:25]}")
    print(f"  B 开风控 20 万 手数序列（{len(hb)} 条）：{hb[:25]}")
    print(f"  C 开风控 2 万  手数序列（{len(hc)} 条）：{hc[:25]}")
    print(f"\n  A == B ? {ha == hb}   （True = 风控在 20 万下不触发，数字不变）")
    print(f"  B != C ? {hb != hc}   （True = 权益缩水后手数确实被压）")
