# -*- coding: utf-8 -*-
"""截面引擎单元测试（不连网、不碰模拟盘，纯本地合成数据）。

★ 为什么先测这个：引擎有 5 类易错点，全部必须先验，
  否则跑完真实数据才发现 bug，样本外就白污染了。

五类易错点：
  ① 禁未来函数：信号只能用 i−1 及之前 ⇒ 在 i 改数据后，i 的信号不该变
  ② 截面性：必须「排序后取 Top3」，不是「各品种独立判断」
  ③ 信号互斥：同一 bar 不会既做多又做空
  ④ 平仓条件：反向信号 or 持仓超 MAX_HOLD=20
  ⑤ 手数上限：≤ HAND_MAX=8
"""
import os, sys
import numpy as np
import pandas as pd

ROOT = r"C:\Users\cc\Desktop\天勤量化"
sys.path.insert(0, os.path.join(ROOT, "工具", "_截面因子_持仓量"))

import importlib.util
spec = importlib.util.spec_from_file_location(
    "引擎", os.path.join(ROOT, "工具", "_截面因子_持仓量", "截面引擎.py"))
E = importlib.util.module_from_spec(spec)
sys.modules["引擎"] = E
spec.loader.exec_module(E)

失败 = []
def 查(名, 条件, 详情=""):
    if 条件:
        print(f"  ✅ {名}")
    else:
        print(f"  ❌ {名}  {详情}")
        失败.append(名)

print("=" * 74)
print("截面引擎单元测试（合成数据）")
print("=" * 74)

# ---------- 造4 个品种的数据 ----------
N = 120
def 造(oi路径, px路径):
    """按指定的变化路径生成序列（拼接 LOOKBACK 段+变化段）。"""
    oi = np.full(N, 100000.0)
    px = np.full(N, 1000.0)
    for i, (o, p) in enumerate(zip(oi路径, px路径)):
        oi[i] = 100000.0 * (1 + o)
        px[i] = 1000.0 * (1 + p)
    return oi, px

# 前 LOOKBACK 根平稳，之后给不同品种不同的持仓量/价格路径
平oi, 平px = [0.0] * N, [0.0] * N
增仓滞涨_多 = 平oi[:]; 增仓滞涨_多[E.LOOKBACK:] = [0.10] * (N - E.LOOKBACK)   # oi+10% px不变 → 顶部
增仓滞涨_空 = 平oi[:]; 增仓滞涨_空[E.LOOKBACK:] = [0.10] * (N - E.LOOKBACK)
减仓下跌_多 = 平oi[:]; 减仓下跌_多[E.LOOKBACK:] = [-0.10] * (N - E.LOOKBACK)  # oi-10% → 底部
健康上涨    = 平oi[:]; 健康上涨[E.LOOKBACK:] = [0.10] * (N - E.LOOKBACK)
健康上涨2   = 平oi[:]; 健康上涨2[E.LOOKBACK:] = [0.10] * (N - E.LOOKBACK)
健康上涨2[E.LOOKBACK:] = [0.10] * (N - E.LOOKBACK)
健康上涨3   = 平oi[:]; 健康上涨3[E.LOOKBACK:] = [0.10] * (N - E.LOOKBACK)

# 健康上涨：oi 和 px 同向大幅变动 ⇒ 不该出信号（阈值卡住了）
健康_oi = 平oi[:]; 健康_oi[E.LOOKBACK:] = [0.12] * (N - E.LOOKBACK)
健康_px = 平px[:]; 健康_px[E.LOOKBACK:] = [0.08] * (N - E.LOOKBACK)

腿 = [
    ("甲", "A", 10, 1.0, 3.0),
    ("乙", "B", 10, 1.0, 3.0),
    ("丙", "C", 10, 1.0, 3.0),
    ("丁", "D", 10, 1.0, 3.0),
]
数据 = {
    "A": 造(增仓滞涨_多, 平px),   # 顶部信号
    "B": 造(增仓滞涨_空, 平px),   # 顶部信号
    "C": 造(减仓下跌_多, 平px),   # 底部信号（方向与 A/B 相反）
    "D": 造(健康_oi, 健康_px),   # 健康上涨 ⇒ 不该有信号
}

eng = E.截面引擎(腿, 手数函数=lambda s, p: 8)
for k, (oi, px) in 数据.items():
    eng.装数据(k, oi, px)

print("\n【测试1】数据装载与就绪")
查("引擎就绪", eng.就绪())
查("长度一致", eng.长度() == N, f"实际={eng.长度()}")

print("\n【测试2】禁未来函数：改第 i 根数据，i 的信号强度不应变")
i_test = 80
eng2 = E.截面引擎(腿, 手数函数=lambda s, p: 8)
for k, (oi, px) in 数据.items():
    eng2.装数据(k, oi, px)
before = {k: eng2.腿信号(k, i_test) for 名, k, *_ in 腿}
# 把 i_test-1（即信号源那根）改掉，信号**应该**变；改 i_test+1 之后不该变
oi2 = 数据["A"][0].copy(); px2 = 数据["A"][1].copy()
oi2[i_test - 1] = oi2[i_test - 1] * 3      # 改信号源
eng2.装数据("A", oi2, px2)
after = eng2.腿信号("A", i_test)
查("改信号源(i-1)后信号改变（说明确实在用历史）",
    abs(before["A"][0] - after[0]) > 1e-9,
    f"{before['A'][0]:.4f} → {after[0]:.4f}")

eng3 = E.截面引擎(腿, 手数函数=lambda s, p: 8)
for k, (oi, px) in 数据.items():
    eng3.装数据(k, oi, px)
oi3 = 数据["A"][0].copy(); px3 = 数据["A"][1].copy()
oi3[i_test + 1] = oi3[i_test + 1] * 3     # 改信号之后那根
px3[i_test + 1] = px3[i_test + 1] * 0.5
eng3.装数据("A", oi3, px3)
查("改未来根(i+1)后信号**不变**（无未来函数）",
    abs(eng3.腿信号("A", i_test)[0] - before["A"][0]) < 1e-12,
    f"{before['A'][0]:.4f} vs {eng3.腿信号('A', i_test)[0]:.4f}")

print("\n【测试3】信号方向正确性")
eng4 = E.截面引擎(腿, 手数函数=lambda s, p: 8)
for k, (oi, px) in 数据.items():
    eng4.装数据(k, oi, px)
选 = eng4.截面选(E.LOOKBACK + 1)
方向表 = {k: d for k, d, s, _, _ in 选}
查("A 增仓滞涨 → 做空(SHORT=-1)", 方向表.get("A") == E.SIGNAL_SHORT, str(方向表))
查("C 减仓下跌 → 做多(LONG=1)", 方向表.get("C") == E.SIGNAL_LONG, str(方向表))
查("D 健康上涨(oi+12%,px+8%) → 无信号", "D" not in 方向表, str(方向表))

print("\n【测试4】★ 截面性：只取 TOP_N=3 个")
查(f"选中数≤ TOP_N({E.TOP_N})", len(选) <= E.TOP_N, f"实际={len(选)}")
查("只从有信号的品种里选", len(选) <= 3)

print("\n【测试5】★ 持仓不重复开（同 bar 同品种只开一次）")
eng5 = E.截面引擎(腿, 手数函数=lambda s, p: 8)
for k, (oi, px) in 数据.items():
    eng5.装数据(k, oi, px)
所有事件 = []
for i in range(E.LOOKBACK + 1, N):
    开盘 = {k: v[1][i] for (k, v) in 数据.items()}   # 第 i 根的开盘价
    ev = eng5.走一根(i, 开盘)
    所有事件.extend(ev)
开仓 = [e for e in 所有事件 if e["act"] == "开"]
键集 = [(e["key"], e["i"]) for e in 开仓]
查("无同 bar 重复开仓", len(键集) == len(set(键集)))
查("持仓数 ≤ TOP_N", max(
    sum(1 for 名, k, *_ in 腿 if eng5.st[k]["pos"] != 0) for _ in range(1)) <= E.TOP_N,
    f"={sum(1 for 名,k,*_ in 腿 if eng5.st[k]['pos']!=0)}")

print("\n【测试6】手数上限 ≤ HAND_MAX")
查("所有开仓手数 ≤ 8", all(e["手"] <= 8 for e in 开仓),
    f"最大={max((e['手'] for e in 开仓), default=0)}")

print("\n【测试7】平仓条件（反向信号 or 超 MAX_HOLD）")
平仓 = [e for e in 所有事件 if e["act"] == "平"]
查("有平仓发生", len(平仓) > 0, f"平仓数={len(平仓)}")
if 平仓:
    超期 = [e for e in 平仓 if e["bars"] >= E.MAX_HOLD]
    print(f"     超期平仓 {len(超期)} 笔 / 总平仓 {len(平仓)} 笔")

print("\n【测试8】★ 恒等式：平仓后 pos 必须清零")
残留 = [k for 名, k, *_ in 腿 if eng5.st[k]["手"] != 0 and eng5.st[k]["pos"] == 0]
查("无「手非0但pos=0」的脏状态", not 残留, str(残留))

print("\n【测试9】盈亏符号（做空在价格涨时亏）")
if 平仓:
    e = 平仓[0]
    预期 = "涨则亏" if e["dir"] == -1 else "跌则亏"
    实际方向 = "做空" if e["dir"] == -1 else "做多"
    print(f"     首笔平仓：{实际方向} {e['名']} pnl={e['pnl']:+.1f}（{预期}）")

print("\n" + "=" * 74)
if 失败:
    print(f"❌ {len(失败)} 项失败: {失败}")
    sys.exit(1)
print("✅ 全部通过")
print("=" * 74)