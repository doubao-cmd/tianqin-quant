# -*- coding: utf-8 -*-
"""
【自测 · 只读】动态保证金 + 保证金上限 的纯函数行为
==========================================================
验证 2026-09-28 的新增逻辑，不改任何配置、不连网。
"""
import sys
import os

_ROOT = os.path.dirname(os.path.abspath(__file__))
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config_symbols
from lib import engine as E

PASS, FAIL = [], []


def ck(name, got, want, tol=1e-6):
    ok = (abs(got - want) <= tol) if isinstance(want, (int, float)) and isinstance(got, (int, float)) \
        else (got == want)
    (PASS if ok else FAIL).append(name)
    print(f"  [{'OK ' if ok else '!! '}] {name}  got={got!r}  want={want!r}")


print("=" * 88)
print("  1. 保证金率表")
print("=" * 88)
ck("MARGIN_RATE 覆盖 12 品种", len(config_symbols.MARGIN_RATE), 12)
ck("margin_rate('V')", config_symbols.margin_rate("V"), 5.1)
ck("margin_rate('v') 小写也认", config_symbols.margin_rate("v"), 5.1)
ck("margin_rate('不存在的')", config_symbols.margin_rate("XX"), 0.0)

print()
print("=" * 88)
print("  2. 每手保证金 = 价格 × 乘数 × 费率")
print("=" * 88)
# PVC 4482 元/吨，乘数 5，费率 5.1%  -> 4482*5*0.051 = 1142.91
ck("PVC @4482", E._margin_per_hand_dynamic(4482, 5, 5.1), 4482 * 5 * 0.051, 1e-6)
# 生猪 10680 元/吨，乘数 16，费率 16.1% -> 27511.68
ck("生猪 @10680", E._margin_per_hand_dynamic(10680, 16, 16.1), 10680 * 16 * 0.161, 1e-6)
ck("价格 0 -> 0", E._margin_per_hand_dynamic(0, 5, 5.1), 0.0)
ck("费率 0 -> 0", E._margin_per_hand_dynamic(4482, 5, 0), 0.0)
ck("NaN 价格 -> 0", E._margin_per_hand_dynamic(float("nan"), 5, 5.1), 0.0)
ck("None 价格 -> 0", E._margin_per_hand_dynamic(None, 5, 5.1), 0.0)

print()
print("=" * 88)
print("  3. _margin_rate_of：None 自动查表 / 数值强制 / 0 关闭")
print("=" * 88)
ck("None + V -> 5.1", E._margin_rate_of(None, "V"), 5.1)
ck("None + LH -> 16.1", E._margin_rate_of(None, "LH"), 16.1)
ck("显式 7.5 -> 7.5", E._margin_rate_of(7.5, "V"), 7.5)
ck("显式 0 -> 关闭", E._margin_rate_of(0, "V"), 0.0)
ck("None + 未配品种 -> 0", E._margin_rate_of(None, "ZZZ"), 0.0)

print()
print("=" * 88)
print("  4. _risk_budget_hand：手数夹逼")
print("=" * 88)
# 场景：20 万权益、目标风险 1%（2000 元）、2×ATR×乘数 = 1000 元/手 -> 算 2 手
mk = dict(atr_val=10.0, stop_mult=2.0, mult=5.0)   # 每手止损 2*10*5 = 100 元
h = E._risk_budget_hand(200000, 1.0, **mk)          # 2000/100 = 20 手
ck("无上限时=20（受 hand_max 8 夹）", h, 8)
h = E._risk_budget_hand(200000, 1.0, **mk, hand_max=0)
ck("hand_max=0（不夹）时=20", h, 20)

# 保证金上限：PVC 1142.91 元/手，权益 20 万 × 30% = 6 万 -> 上限 52 手（不夹）
h = E._risk_budget_hand(200000, 1.0, **mk, hand_max=0,
                        margin_per_hand=1142.9, margin_cap_pct=30.0)
ck("保证金上限 52 手，不触发", h, 20)

# ★ 关键：权益缩水到 3 万 -> 上限 = 30000*0.3/1142.9 = 7 手 -> 夹到 7
h = E._risk_budget_hand(200000, 1.0, **mk, hand_max=0,
                        margin_per_hand=1142.9, margin_cap_pct=30.0,
                        margin_equity=30000)
ck("权益缩水到 3 万 -> 夹到 7 手", h, 7)

# 生猪 27512 元/手，20 万权益 -> 上限 2 手 -> 从 20 夹到 2
h = E._risk_budget_hand(200000, 1.0, **mk, hand_max=0,
                        margin_per_hand=27511.7, margin_cap_pct=30.0,
                        margin_equity=200000)
ck("生猪 20 万权益 -> 夹到 2 手", h, 2)

# 极端：权益只剩 1 万，生猪 上限 = 10000*0.3/27511.7 = 0 手 -> 仍返回 hand_min（老语义）
h = E._risk_budget_hand(200000, 1.0, **mk, hand_max=0, hand_min=1,
                        margin_per_hand=27511.7, margin_cap_pct=30.0,
                        margin_equity=10000)
ck("上限算成 0 手时仍返回 hand_min=1（老语义，未改）", h, 1)

# margin_equity=None 时必须与老行为逐字一致
a = E._risk_budget_hand(200000, 1.0, **mk, margin_per_hand=1142.9, margin_cap_pct=30.0)
b = E._risk_budget_hand(200000, 1.0, **mk, margin_per_hand=1142.9, margin_cap_pct=30.0,
                        margin_equity=200000)
ck("margin_equity=None 等价于传同一权益", a, b)

# 老调用（不传任何保证金参数）行为不变
ck("完全不传保证金参数 -> 仍 8 手", E._risk_budget_hand(200000, 1.0, **mk), 8)

print()
print("=" * 88)
print("  5. _margin_cap_text 披露文案")
print("=" * 88)
t = E._margin_cap_text(30.0, 1142.9, 200000, 8)
print(f"  正常：{t}")
ck("含『保证金上限 30%』", "保证金上限 30%" in t, True)
t2 = E._margin_cap_text(30.0, 27511.7, 200000, 5)
print(f"  超限：{t2}")
ck("超限时有告警", "已超上限" in t2, True)
ck("参数缺失时返回空串", E._margin_cap_text(0, 0, 0, 1), "")

print()
print("=" * 88)
print(f"  通过 {len(PASS)} / 失败 {len(FAIL)}")
if FAIL:
    for f in FAIL:
        print(f"    !! {f}")
print("=" * 88)
