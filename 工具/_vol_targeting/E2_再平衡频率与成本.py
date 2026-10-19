# -*- coding: utf-8 -*-
"""
E2 · 再平衡频率与成本敏感度（预注册卡 2026-10-15）
====================================================
对象：对 (c) 波动率目标组合（等权组合 + 15% 目标波动杠杆）施加不同再平衡频率。
  - 日频：每个交易日按 t-1 决策杠杆，次日持仓（= E1 的 (c)）
  - 周频：每 5 交易日重决策一次杠杆，其间持有
  - 月频：每 21 交易日重决策一次杠杆，其间持有
成本仅在再平衡日发生：|Δ杠杆| × COST_RT（往返成本占换手名义）。

目的：
  1. 回答障碍 2「再平衡=成本」——频率越低成本越少，但风险漂移越多；
  2. 找「夏普提升被成本吃掉」的临界成本（卡预设失败 #3）。
无未来函数：杠杆 t 时刻值只用 rp[t-WIN:t]（不含 t）。
"""
import csv, math, os
import numpy as np

DATA_DIR = "工具/_进场体检/数据"
QUAL = ["JD", "JM", "M", "MA", "P", "RB", "RU", "SR", "V"]
TARGET = 0.15 / math.sqrt(252)
WIN = 20
LEV_CAP = 3.0
OUT_DIR = "工具/_vol_targeting"


def load(sym):
    p = os.path.join(DATA_DIR, f"{sym}_1440min_2016-01-04_2021-12-31.csv")
    rows = []
    with open(p, encoding="utf-8-sig") as f:
        r = csv.reader(f); hdr = next(r); idx = {c: i for i, c in enumerate(hdr)}
        for row in r:
            if not row or len(row) < 5:
                continue
            rows.append((row[idx["datetime"]], float(row[idx["close"]])))
    return rows


raw = {s: load(s) for s in QUAL}
series = {}
for s in QUAL:
    dts, closes = zip(*raw[s])
    rets = np.array(closes[1:]) / np.array(closes[:-1]) - 1.0
    series[s] = {dt: rr for dt, rr in zip(dts[1:], rets)}
common = sorted(set(series[QUAL[0]]) | set().union(*[set(series[s]) for s in QUAL[1:]]))
# 交集
common = sorted(set(series[QUAL[0]]) & set().union(*[set(series[s]) for s in QUAL[1:]]))
R = np.array([[series[s][dt] for dt in common] for s in QUAL])
dates = np.array(common)
T = R.shape[1]
rp = R.mean(axis=0)
roll = np.full(T, np.nan)
for t in range(T):
    if t >= WIN:
        roll[t] = rp[t - WIN:t].std(ddof=1)
lev_target = np.where(np.isnan(roll), 1.0, np.clip(TARGET / roll, 0, LEV_CAP))


def port_with_freq(freq, cost_rt):
    """freq=1 日 /5 周 /21 月；再平衡日重新设定杠杆，其间持有。返回毛、净序列。"""
    held = np.ones(T)
    last_lev = 1.0
    reb_days = list(range(WIN, T, freq))
    if reb_days[0] != WIN:
        reb_days = [WIN] + reb_days
    for t in reb_days:
        held[t:] = lev_target[t]        # 本次决策自 t 起生效
        last_lev = lev_target[t]
    gross = held * rp
    cost = np.zeros(T)
    prev = 1.0
    for t in reb_days:
        cost[t] = abs(held[t] - prev) * cost_rt
        prev = held[t]
    net = gross - cost
    return gross, net, len(reb_days)


def metrics(rets):
    rets = np.asarray(rets, float)
    n = len(rets)
    eq = np.cumprod(1.0 + rets)
    peak = np.maximum.accumulate(eq)
    mdd = float((eq / peak - 1.0).min())
    vol = rets.std(ddof=1) * math.sqrt(252)
    sharpe = rets.mean() / rets.std(ddof=1) * math.sqrt(252)
    return dict(sharpe=sharpe, ann_vol=vol, mdd=mdd, n=n)


print(f"对齐交易日 {common[0]}~{common[-1]}  T={T}  品种={len(QUAL)}")
print("\n=== E2-1 再平衡频率对照（COST_RT=0.0003 往返）===")
print(f"{'频率':8s} {'再平衡次数':>10s} {'毛夏普':>8s} {'净夏普':>8s} {'毛年化vol':>10s} {'毛MDD':>8s}")
for freq, lbl in [(1, "日频"), (5, "周频"), (21, "月频")]:
    g, n, nb = port_with_freq(freq, 0.0003)
    mg, mn = metrics(g), metrics(n)
    print(f"{lbl:8s} {nb:10d} {mg['sharpe']:8.3f} {mn['sharpe']:8.3f} {mg['ann_vol']*100:9.1f}% {mg['mdd']*100:7.1f}%")

print("\n=== E2-2 日频成本敏感度（找夏普提升被吃光的临界成本）===")
a_sharpe = metrics(rp)["sharpe"]     # (a) 等权毛夏普
print(f"基准 (a) 等权毛夏普 = {a_sharpe:.3f}")
print(f"{'COST_RT(往返)':>16s} {'净夏普':>8s} {'vs(a)增减':>10s} {'判定':>12s}")
for cr in [0.0, 1e-4, 3e-4, 5e-4, 1e-3, 2e-3]:
    g, n, _ = port_with_freq(1, cr)
    cs = metrics(n)["sharpe"]
    delta = cs - a_sharpe
    verdict = "提升被吃光" if cs <= a_sharpe else ("仍优于等权" if delta > 0.05 else "大体持平")
    print(f"{cr:16.4f} {cs:8.3f} {delta:+10.3f} {verdict:>12s}")

# 存档
os.makedirs(OUT_DIR, exist_ok=True)
with open(os.path.join(OUT_DIR, "E2_再平衡频率与成本结果.md"), "w", encoding="utf-8") as f:
    f.write("# E2 再平衡频率与成本敏感度结果（2026-10-15）\n\n")
    f.write(f"样本 QUAL 9 品种日线 {common[0]}~{common[-1]}（IS 层）。(c) vol-targeting，target=15%/√252，WIN=20，clip(0,3)。\n\n")
    f.write("## E2-1 频率对照（COST_RT=0.0003）\n\n")
    f.write("| 频率 | 再平衡次数 | 毛夏普 | 净夏普 | 毛年化vol | 毛MDD |\n")
    for freq, lbl in [(1, "日频"), (5, "周频"), (21, "月频")]:
        g, n, nb = port_with_freq(freq, 0.0003)
        mg, mn = metrics(g), metrics(n)
        f.write(f"| {lbl} | {nb} | {mg['sharpe']:.3f} | {mn['sharpe']:.3f} | {mg['ann_vol']*100:.1f}% | {mg['mdd']*100:.1f}% |\n")
    f.write("\n## E2-2 日频成本敏感度\n\n")
    f.write(f"基准(a)等权毛夏普={a_sharpe:.3f}\n\n")
    f.write("| COST_RT(往返) | 净夏普 | vs(a) | 判定 |\n")
    for cr in [0.0, 1e-4, 3e-4, 5e-4, 1e-3, 2e-3]:
        g, n, _ = port_with_freq(1, cr)
        cs = metrics(n)["sharpe"]
        delta = cs - a_sharpe
        verdict = "提升被吃光" if cs <= a_sharpe else ("仍优于等权" if delta > 0.05 else "大体持平")
        f.write(f"| {cr:.4f} | {cs:.3f} | {delta:+.3f} | {verdict} |\n")
print("\n结果已存档 -> 工具/_vol_targeting/E2_再平衡频率与成本结果.md")
