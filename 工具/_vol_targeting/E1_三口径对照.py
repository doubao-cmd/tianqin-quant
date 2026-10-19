# -*- coding: utf-8 -*-
"""
E1 · 三口径对照（预注册卡 2026-10-15）
=====================================
目的：隔离「夏普提升来自哪个变量」。
  (a) 等权基线        w_i = 1/N（每日恒定，零换手）
  (b) 风险平价        w_i ∝ 1/σ_i（横截面波动差异贡献），日再平衡
  (c) 波动率目标      = (a) 组合施加 15% 目标波动杠杆（时序波动贡献），日再平衡
  (d) 组合版(信息项)  = (b) 组合再施加同一杠杆（a+b+c 叠加，仅作备注不进入单调判据）

口径严格对齐 `讨论/波动率_四件粗略.py` 的 (c)：
  roll[t] = rp[t-20:t].std()（不含 t，无未来函数）
  w_lev[t-1] 应用于 rp[t]；target=0.15/√252；clip(0,3)

成本模型（E2 正式测，此处给净夏普防误导）：
  每再平衡成本 = 换手率 × COST_RT；COST_RT=0.0003（3bps 往返，零售保守假设）
  换手率_t = Σ|w_i,t − w_i,t-1|

样本：QUAL 9 品种 日线 2016-01-04~2021-12-31（主测窗口，IS 层）。
"""
import csv, math, os
import numpy as np

DATA_DIR = "工具/_进场体检/数据"
QUAL = ["JD", "JM", "M", "MA", "P", "RB", "RU", "SR", "V"]   # 主测品种表
TARGET = 0.15 / math.sqrt(252)
WIN = 20
LEV_CAP = 3.0
COST_RT = 0.0003          # 往返成本占换手名义的假设
OUT_DIR = "工具/_vol_targeting"


def load(sym):
    p = os.path.join(DATA_DIR, f"{sym}_1440min_2016-01-04_2021-12-31.csv")
    if not os.path.exists(p):
        return None
    rows = []
    with open(p, encoding="utf-8-sig") as f:
        r = csv.reader(f); hdr = next(r)
        idx = {c: i for i, c in enumerate(hdr)}
        for row in r:
            if not row or len(row) < 5:
                continue
            rows.append((row[idx["datetime"]], float(row[idx["close"]])))
    return rows


raw = {s: load(s) for s in QUAL}
# 对齐共同交易日
series = {}
for s in QUAL:
    dts, closes = zip(*raw[s])
    rets = np.array(closes[1:]) / np.array(closes[:-1]) - 1.0
    series[s] = {dt: rr for dt, rr in zip(dts[1:], rets)}
common = sorted(set(series[QUAL[0]]) & set().union(*[set(series[s]) for s in QUAL[1:]]))
R = np.array([[series[s][dt] for dt in common] for s in QUAL])   # (9, T)
dates = np.array(common)
T = R.shape[1]
print(f"对齐交易日: {common[0]} ~ {common[-1]}  T={T}  品种={len(QUAL)}")

# ---- 权重 ----
# (a) 等权
w_a = np.full((len(QUAL), T), 1.0 / len(QUAL))

# (b) 风险平价：w_i ∝ 1/σ_i，σ 用滚动 20 日（t-20:t-1）
# 稳健化：σ 地板（当帧横截面中位数 50%）+ 单股权重上限 40%，避免低波动窗口全仓单押（朴素逆波动陷阱）
MAXW = 0.40
w_b = np.zeros((len(QUAL), T))
for t in range(T):
    if t < WIN:
        w_b[:, t] = 1.0 / len(QUAL)
        continue
    sig = np.array([R[i, t - WIN:t].std(ddof=1) for i in range(len(QUAL))])
    floor = 0.5 * np.median(sig)
    sig = np.maximum(sig, floor)
    w = 1.0 / sig
    w /= w.sum()
    w = np.minimum(w, MAXW)
    w /= w.sum()
    w_b[:, t] = w

# (c) 波动率目标杠杆：施加到等权组合 rp
rp = R.mean(axis=0)
roll = np.full(T, np.nan)
for t in range(T):
    if t >= WIN:
        roll[t] = rp[t - WIN:t].std(ddof=1)
w_lev = np.clip(np.where(np.isnan(roll), 1.0, TARGET / roll), 0, LEV_CAP)


def port_ret(W, lev=None):
    """给定权重矩阵 W (N,T) 与可选杠杆序列 lev (T,)，返回净/毛日收益。"""
    gross = (W * R).sum(axis=0)           # 横截面加权毛收益
    if lev is not None:
        # 杠杆施加在组合层面：第 t 日收益用 t-1 决定的杠杆
        g = np.zeros(T)
        for t in range(T):
            if t == 0:
                g[t] = gross[t]          # 首日用 1x
            else:
                g[t] = lev[t - 1] * gross[t]
        gross = g
    # 换手率（含杠杆变化）
    turn = np.zeros(T)
    for t in range(1, T):
        if lev is None:
            turn[t] = np.abs(W[:, t] - W[:, t - 1]).sum()
        else:
            turn[t] = np.abs(W[:, t] - W[:, t - 1]).sum() + abs(lev[t] - lev[t - 1])
    cost = np.zeros(T)
    cost[1:] = turn[1:] * COST_RT
    net = gross - cost
    return gross, net, turn


def metrics(rets):
    rets = np.asarray(rets, float)
    n = len(rets)
    eq = np.cumprod(1.0 + rets)
    peak = np.maximum.accumulate(eq)
    mdd = float((eq / peak - 1.0).min())
    vol = rets.std(ddof=1) * math.sqrt(252)
    sharpe = rets.mean() / rets.std(ddof=1) * math.sqrt(252)
    ann = eq[-1] ** (252.0 / n) - 1.0
    return dict(sharpe=sharpe, ann_vol=vol, mdd=mdd, ann_geo=ann, n=n)


def yearly_sharpe(rets, dates):
    rets = np.asarray(rets, float)
    yrs = [d[:4] for d in dates]
    out = {}
    for y in sorted(set(yrs)):
        m = np.array([y == yy for yy in yrs])
        if m.sum() > 30:
            out[y] = metrics(rets[m])["sharpe"]
    return out


# 计算各口径
ga, na, ta = port_ret(w_a)                      # (a)
gb, nb, tb = port_ret(w_b)                      # (b)
gc_gross = np.zeros(T); gc_net = np.zeros(T)
for t in range(T):
    base = rp[t] if t == 0 else w_lev[t - 1] * rp[t]
    gc_gross[t] = base
gc_cost = np.zeros(T)
gc_cost[1:] = np.abs(np.diff(w_lev)) * COST_RT
gc_net = gc_gross - gc_cost                     # (c)

# (d) 组合版：风险平价组合再施加同一杠杆
gd_gross = np.zeros(T); gd_net = np.zeros(T)
rp_b = (w_b * R).sum(axis=0)
for t in range(T):
    base = rp_b[t] if t == 0 else w_lev[t - 1] * rp_b[t]
    gd_gross[t] = base
gd_net = gd_gross - np.abs(np.diff(np.concatenate([[1.0], w_lev]))) * COST_RT


def show(name, g, n):
    mg, mn = metrics(g), metrics(n)
    print(f"  {name:12s} 毛夏普 {mg['sharpe']:6.3f}  净夏普 {mn['sharpe']:6.3f}"
          f"  毛年化vol {mg['ann_vol']*100:5.1f}%  毛MDD {mg['mdd']*100:6.1f}%  净年化 {mn['ann_geo']*100:6.1f}%")


print("\n=== E1 结果（QUAL 9，2016-2021，IS 层）===")
show("(a) 等权", ga, na)
show("(b) 风险平价", gb, nb)
show("(c) 波动率目标", gc_gross, gc_net)
show("(d) 风险平价+vol目标", gd_gross, gd_net)

# 单调性判据：毛夏普 c >= b >= a ?
ma, mb, mc = metrics(ga)["sharpe"], metrics(gb)["sharpe"], metrics(gc_gross)["sharpe"]
print(f"\n  毛夏普单调 (c≥b≥a): c={mc:.3f} b={mb:.3f} a={ma:.3f} "
      f"=> {'YES' if mc >= mb >= ma else 'NO'}")

# 逐年 (c)-(a) 毛夏普增量，检验「方向稳定」
ys_a = yearly_sharpe(ga, dates)
ys_c = yearly_sharpe(gc_gross, dates)
inc = {y: ys_c[y] - ys_a[y] for y in ys_a}
pos = sum(1 for v in inc.values() if v >= 0)
neg_bad = [y for y, v in inc.items() if v < -0.05]
print("\n  逐年 (c)−(a) 毛夏普增量：")
for y in sorted(inc):
    print(f"    {y}: {inc[y]:+.3f}")
print(f"  增量≥0 年份 {pos}/{len(inc)}  反向翻车年(<−0.05): {neg_bad or '无'}")
stable = (pos >= 5 and not neg_bad)
print(f"  「方向稳定」判据（≥5/6 年非负且无翻车年）: {'PASS' if stable else 'FAIL'}")

# 净夏普下 (c) 是否仍优于 (a)
mn_a, mn_c = metrics(na)["sharpe"], metrics(gc_net)["sharpe"]
print(f"\n  净夏普对比 (含 3bps 往返成本): (a)={mn_a:.3f}  (c)={mn_c:.3f} "
      f"=> {'成本吃掉全部提升' if mn_c <= mn_a else '成本下仍保留部分提升'}")

# 存档
os.makedirs(OUT_DIR, exist_ok=True)
with open(os.path.join(OUT_DIR, "E1_三口径对照结果.md"), "w", encoding="utf-8") as f:
    f.write("# E1 三口径对照结果（2026-10-15）\n\n")
    f.write(f"样本：QUAL 9 品种日线 {common[0]}~{common[-1]}（IS 层）。target=15%/√252，WIN=20，clip(0,3)，COST_RT=0.0003。\n\n")
    f.write("| 口径 | 毛夏普 | 净夏普 | 毛年化vol | 毛MDD | 净年化 |\n")
    for nm, g, n in [("(a)等权", ga, na), ("(b)风险平价", gb, nb),
                     ("(c)波动率目标", gc_gross, gc_net), ("(d)风险平价+vol目标", gd_gross, gd_net)]:
        mg, mn = metrics(g), metrics(n)
        f.write(f"| {nm} | {mg['sharpe']:.3f} | {mn['sharpe']:.3f} | {mg['ann_vol']*100:.1f}% | {mg['mdd']*100:.1f}% | {mn['ann_geo']*100:.1f}% |\n")
    f.write(f"\n毛夏普单调(c≥b≥a): {'YES' if mc>=mb>=ma else 'NO'}\n")
    f.write(f"逐年(c)-(a)增量≥0年: {pos}/{len(inc)}；翻车年: {neg_bad or '无'} → 方向稳定: {'PASS' if stable else 'FAIL'}\n")
    f.write(f"净夏普(a)={mn_a:.3f} vs (c)={mn_c:.3f} → {'成本吃掉全部提升' if mn_c<=mn_a else '成本下保留部分提升'}\n")
print("\n结果已存档 -> 工具/_vol_targeting/E1_三口径对照结果.md")
