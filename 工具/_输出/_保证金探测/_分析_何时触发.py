# -*- coding: utf-8 -*-
"""
【只读分析】保证金上限 30% 在什么情况下才真的拦得住？
==========================================================
用实探测到的 2026-03 真实价格 + config_symbols 的乘数/费率，纯计算。
不连网、不写盘（除打印）。

背景：手数预算 = 固定 20 万 × 1% ÷ (2×ATR×乘数)   —— 用固定基数
      保证金上限 = 当前权益 × 30% ÷ (价格×乘数×费率) —— 用当前权益
两者会在「相对低波动 + 权益缩水」时交叉，这条风控在那之前**从不触发**。
本脚本把交叉点算出来，好知道它到底是不是形同虚设。
"""
import sys
import os

_ROOT = os.path.dirname(os.path.abspath(__file__))
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config_symbols as C

# 实探测到的 2026-03 真实价格（回测 2026-03-02 起点时的具体合约）
PX = {
    "EB": 7628, "JD": 3267, "JM": 1098, "LH": 11485, "M": 2824, "MA": 2228,
    "P": 8818, "RB": 3090, "RU": 17175, "SA": 1202, "SR": 5324, "V": 4834,
}
BALANCES = [200000, 100000, 50000, 20000]
CAP = 30.0
RISK_PCT = 1.0
RISK_BASE = 200000      # 算手数用的固定基数


def main():
    print("=" * 118)
    print("  每手保证金（= 价格 × 乘数 × 费率）与「保证金上限手数」")
    print(f"  上限 = 权益 × {CAP:g}% ÷ 每手保证金     ｜  手数预算基数固定 = {RISK_BASE:,} 元")
    print("=" * 118)

    hdr = f"{'品种':<8}{'价格':>9}{'乘数':>6}{'费率':>7}{'每手保证金':>12}"
    for b in BALANCES:
        hdr += f"{'上限@' + f'{b // 10000}万':>11}"
    hdr += f"{'每日1手风险':>12}{'触发阈值ATR':>12}"
    print(hdr)
    print("-" * 118)

    for key, (cn, main, mult, tick) in sorted(C.SYMBOLS.items(),
                                              key=lambda kv: kv[0]):
        px = PX.get(key)
        rate = C.margin_rate(key)
        if not px or rate <= 0:
            continue
        mph = px * mult * rate / 100.0
        row = f"{cn:<8}{px:>9}{mult:>6}{rate:>6.1f}%{mph:>12,.0f}"
        for b in BALANCES:
            row += f"{int(b * CAP / 100.0 / mph):>11}"
        # 触发阈值：预算手数 > 上限 ⇔ ATR < 价格×费率/(60×固定基数/20万)
        # 手数预算 = RISK_BASE×1%/(2×ATR×mult)；上限 = E×30%/(px×mult×rate)
        # 触发 ⇔ RISK_BASE×0.01/(2×ATR×mult) > E×0.30/(px×mult×rate)
        #      ⇔ ATR < RISK_BASE×0.01×px×rate/(0.6×E)
        thr = RISK_BASE * 0.01 * px * rate / (0.6 * BALANCES[0])
        # 每日1手风险：假设 ATR≈价格×1.5%（粗估），仅供量级感
        atr_typ = px * 0.015
        r1 = 2 * atr_typ * mult
        row += f"{r1:>12,.0f}{thr:>12.1f}"
        print(row)

    print("-" * 118)
    print("读法：")
    print("  · 「上限@20万」这一列 > 8（= HAND_MAX）的品种，在 20 万权益下**永不触发**。")
    print("  · 触发阈值ATR = 该品种 ATR 掉到这个数以下、且权益仍是 20 万时，上限才会压手数。")
    print("    把「阈值ATR」和该品种的真实 ATR 比一下：真实 ATR 远大于阈值 ⇒ 平时不触发。")
    print("  · 真正会让它生效的是**权益缩水**：上限按当前权益线性缩小，而预算基数是固定的。")
    print("    例：权益掉到 5 万，上限手数变成上表 1/4；预算手数却一动不动。")
    print("=" * 118)


if __name__ == "__main__":
    main()
