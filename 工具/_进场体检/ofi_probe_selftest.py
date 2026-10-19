# -*- coding: utf-8 -*-
# ofi_probe_selftest.py — 合成数据单元自测（不连网、不依赖快期 auth）。
# 验证：① OFI 聚合正确（主动买→正、主动卖→负）② _screen L0/L1 数学正确
#       ③ 无未来函数（bar i 的 ofi 只依赖桶内 tick，截断重算一致）
import importlib.util, types
import numpy as np

_HERE = "C:/Users/cc/Desktop/天勤量化/工具/_进场体检"
spec = importlib.util.spec_from_file_location(
    "ofi_probe", f"{_HERE}/ofi_probe.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

# ---- 构造合成 tick：6 根 5min 桶，前 3 桶全主动买、后 3 桶全主动卖 ----
def make_ticks(side_sign):
    ticks = []
    for bi in range(30):
        px = 300 + bi * 10
        for ti in range(5):
            dt = (bi * 300 + ti) * 1_000_000_000
            lp = float(px + ti)
            a1, b1 = lp + 1.0, lp - 1.0
            v = float(bi * 5 + ti + 1)
            ticks.append((dt, lp, a1, b1, v))
    if side_sign < 0:   # 翻转为主动卖：让 last 落在 bid 侧
        ticks = [(dt, lp, lp + 1.0, lp - 1.0, v) for (dt, lp, _, _, v) in ticks]
        # 把 last 压到 <= bid：构造 lp <= b1 的情形
        ticks = []
        for bi in range(30):
            px = 300 + bi * 10
            for ti in range(5):
                dt = (bi * 300 + ti) * 1_000_000_000
                lp = float(px + ti)
                a1, b1 = lp + 2.0, lp + 1.0   # bid 高于 last ⇒ last<=bid ⇒ 主动卖
                v = float(bi * 5 + ti + 1)
                ticks.append((dt, lp, a1, b1, v))
    return ticks

bars_buy = m._tick_to_bars(make_ticks(+1))
bars_sell = m._tick_to_bars(make_ticks(-1))

print("OFI(全买):", np.round(bars_buy["ofi"], 1))
print("OFI(全卖):", np.round(bars_sell["ofi"], 1))
assert np.all(bars_buy["ofi"] > 0), "全买场景 OFI 应全正"
assert np.all(bars_sell["ofi"] < 0), "全卖场景 OFI 应全负"

# ---- 无未来函数：前 3 桶截断重算应与全量前 3 桶一致 ----
trunc = m._tick_to_bars(make_ticks(+1)[:15])   # 前 3 桶共 15 tick
assert np.allclose(trunc["ofi"], bars_buy["ofi"][:3]), "截断重算不一致 ⇒ 有未来函数"
print("无未来函数检验: ✅（截断重算与前 3 桶一致）")

# 验证 make_ticks(-1) 也维持 30 桶规模
bars_sell = m._tick_to_bars(make_ticks(-1))
assert len(bars_sell["ofi"]) == 30, "sell 场景应为 30 桶"

# ---- _screen 数学：patch 成本模型，验证 L0/L1 方向正确 ----
m.cs = types.SimpleNamespace(get=lambda k: ("TEST", "KQ.m@TEST.x", 10, 1))
m.cost_points = lambda key: 2.0 * (m.SLIP * 1.0 + 0.0 / 10.0)   # 往返 2 点
r_buy = m._screen("TEST", bars_buy)
r_sell = m._screen("TEST", bars_sell)
print("buy 场景 L0毛=%.3f L1毛=%.3f L1净=%.3f 毛/成本=%.2f 胜率=%.2f" % (
    r_buy["L0_gross"], r_buy["L1_gross"], r_buy["L1_net"], r_buy["ratio"], r_buy["L1_winrate"]))
print("sell场景 L0毛=%.3f L1毛=%.3f L1净=%.3f 毛/成本=%.2f 胜率=%.2f" % (
    r_sell["L0_gross"], r_sell["L1_gross"], r_sell["L1_net"], r_sell["ratio"], r_sell["L1_winrate"]))
# 全买场景价格单调上升→做多应盈利(L1净>0)；全卖场景→做空上升市应亏损(L1净<0)
assert r_buy["L1_net"] > 0, "全买+上涨应盈利"
assert r_sell["L1_net"] < 0, "全卖+上涨应亏损"
print("\n[selftest PASS] OFI 聚合 / L0-L1 方向 / 无未来函数 全部通过")
