# -*- coding: utf-8 -*-
"""中轨趋势持有（08）—— 信号函数单测 + rev 路径未受影响验证。

跑法：python 工具/_中轨趋势持有/单测_信号函数.py
"""
import sys
import os

_ROOT = os.path.dirname(os.path.abspath(__file__))
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)
sys.path.insert(0, _ROOT)

from lib.engine import trend_hold_signal, rev_signal

NAN = float("nan")
_fail = []


def chk(name, got, want):
    ok = (got == want) or (got != got and want != want)
    print(f"  {'✅' if ok else '❌'} {name}: got={got!r} want={want!r}")
    if not ok:
        _fail.append(name)


print("=" * 66)
print("① trend_hold_signal —— 顺势（本策略）")
print("=" * 66)
# 收盘 > 中轨 -> 做多（+1）
chk("收盘>中轨 空仓->多", trend_hold_signal(0, 105.0, 100.0), 1)
chk("收盘>中轨 持空->翻多", trend_hold_signal(-1, 105.0, 100.0), 1)
chk("收盘>中轨 持多->不动", trend_hold_signal(1, 105.0, 100.0), 1)
# 收盘 < 中轨 -> 做空（-1）
chk("收盘<中轨 空仓->空", trend_hold_signal(0, 95.0, 100.0), -1)
chk("收盘<中轨 持多->翻空", trend_hold_signal(1, 95.0, 100.0), -1)
chk("收盘<中轨 持空->不动", trend_hold_signal(-1, 95.0, 100.0), -1)
# 相等 / NaN -> 维持
chk("恰好相等 持多->维持", trend_hold_signal(1, 100.0, 100.0), 1)
chk("恰好相等 持空->维持", trend_hold_signal(-1, 100.0, 100.0), -1)
chk("收盘NaN 持多->维持", trend_hold_signal(1, NAN, 100.0), 1)
chk("均线NaN 持空->维持", trend_hold_signal(-1, 100.0, NAN), -1)
chk("双NaN 空仓->维持0", trend_hold_signal(0, NAN, NAN), 0)

print()
print("=" * 66)
print("② rev_signal —— 逆势（03，必须一字未变）")
print("=" * 66)
chk("收盘<中轨 -> 做多", rev_signal(0, 95.0, 100.0), 1)
chk("收盘>中轨 -> 做空", rev_signal(0, 105.0, 100.0), -1)
chk("恰好相等 -> 维持", rev_signal(-1, 100.0, 100.0), -1)
chk("NaN -> 维持", rev_signal(1, NAN, 100.0), 1)

print()
print("=" * 66)
print("③ 镜像关系（核心断言：两者方向必须完全对调）")
print("=" * 66)
cases = [(0, 105.0, 100.0), (0, 95.0, 100.0), (1, 105.0, 100.0),
         (-1, 95.0, 100.0), (0, 100.0, 100.0), (0, NAN, 100.0)]
allgood = True
for cur, c, m in cases:
    a = trend_hold_signal(cur, c, m)
    b = rev_signal(cur, c, m)
    # 镜像：非维持时方向相反；维持时两者都返回 cur
    if (a != a) or (b != b):
        ok = True
    elif c == m or c != c or m != m:
        ok = (a == cur and b == cur)          # 都维持
    else:
        ok = (a == -b)                        # 方向相反
    print(f"  {'✅' if ok else '❌'} cur={cur:+d} close={c} mid={m} -> "
          f"trendhold={a:+d} rev={b:+d}")
    if not ok:
        allgood = False
        _fail.append(f"mirror cur={cur} c={c} m={m}")

print()
print("=" * 66)
if _fail:
    print(f"❌ {len(_fail)} 项失败：{_fail}")
    sys.exit(1)
print("✅ 全部通过（含镜像关系断言）")
