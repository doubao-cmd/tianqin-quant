# -*- coding: utf-8 -*-
"""
R2 · 组合层收益分解  (预注册卡 v1.0 冻结, 2026-10-20 21:20)
================================================================
要回答的唯一问题:
  E1 说「+0.223 毛夏普来自低波期 3x 杠杆」, 但常数杠杆对夏普是恒等变换。
  +0.223 到底来自:  真择时 / 路径(复利)效应 / 分散化-再平衡 / 成本 / 残差

冻结口径 (预注册 §三, 禁止扫描择优):
  QUAL 9 = JD/JM/M/MA/P/RB/RU/SR/V ; IS 2016-01-04~2021-12-31 (唯一干净段)
  TARGET=0.15/sqrt(252), WIN=20, clip(0,3), COST_RT=0.0003 (3bps 往返), seed=20261020
判据只在 IS 判定; 2022-2026 非干净 OOS, 仅描述性 (禁止用于通过/不通过)。
"""
import csv, math, os
import numpy as np

IS_DIR   = "工具/_进场体检/数据"
ROLL_CSV = "工具/_数据/展期_合约日线.csv"
QUAL     = ["JD", "JM", "M", "MA", "P", "RB", "RU", "SR", "V"]
N        = len(QUAL)
TARGET   = 0.15 / math.sqrt(252)
WIN      = 20
LEV_CAP  = 3.0
COST_RT  = 0.0003
SEED     = 20261020
NPERM    = 1000
OUT      = "工具/_r2_decomp/out"
os.makedirs(OUT, exist_ok=True)


def metrics(rets):
    rets = np.asarray(rets, float)
    n = len(rets)
    eq = np.cumprod(1.0 + rets)
    peak = np.maximum.accumulate(eq)
    mdd = float((eq / peak - 1.0).min())
    sd = rets.std(ddof=1)
    vol = sd * math.sqrt(252)
    sharpe = (rets.mean() / sd * math.sqrt(252)) if sd > 0 else float("nan")
    ann = eq[-1] ** (252.0 / n) - 1.0
    return dict(sharpe=sharpe, ann_vol=vol, mdd=mdd, ann_geo=ann, n=n)


# ---------------- 数据 ----------------
def load_is(sym):
    p = os.path.join(IS_DIR, f"{sym}_1440min_2016-01-04_2021-12-31.csv")
    rows = []
    with open(p, encoding="utf-8-sig") as f:
        r = csv.reader(f)
        hdr = next(r)
        idx = {c: i for i, c in enumerate(hdr)}
        ci, cl = idx["datetime"], idx["close"]
        for row in r:
            if not row or len(row) <= max(ci, cl):
                continue
            rows.append((row[ci], float(row[cl])))
    return rows


def load_roll(key):
    out = {}
    with open(ROLL_CSV, encoding="utf-8-sig") as f:
        r = csv.reader(f)
        hdr = next(r)
        idx = {c: i for i, c in enumerate(hdr)}
        ki, di, cl = idx["key"], idx["日期"], idx["close"]
        for row in r:
            if not row or len(row) <= max(ki, di, cl):
                continue
            if row[ki] != key:
                continue
            out[row[di][:10]] = float(row[cl])
    return out


def build(R, dates):
    """给定 (N,T) 日收益 + 日期, 返回六臂毛/净日收益 + 杠杆序列"""
    n, T = R.shape
    rp = R.mean(axis=0)                        # 等权组合日收益 (w=1/N 恒定)
    roll = np.full(T, np.nan)
    for t in range(T):
        if t >= WIN:
            roll[t] = rp[t - WIN:t].std(ddof=1)
    w_lev = np.clip(np.where(np.isnan(roll), 1.0, TARGET / roll), 0.0, LEV_CAP)

    # (a) 等权每日再平衡 (w=1/N 恒定 => 零换手)
    g_a = rp.copy()
    n_a = rp.copy()

    # (a-BH) 等权买入持有 (建仓后权重随价格漂移, 不再平衡)
    w0 = np.full(n, 1.0 / n)
    cum = np.cumprod(1.0 + R, axis=1)
    V = (w0[:, None] * cum).sum(axis=0)
    g_bh = np.empty(T)
    g_bh[0] = V[0] - 1.0
    g_bh[1:] = V[1:] / V[:-1] - 1.0
    n_bh = g_bh.copy()
    n_bh[0] -= COST_RT                          # 一次性建仓成本(往返近似)

    # (a-F) 等权按月再平衡
    months = [d[:7] for d in dates]
    g_F = np.zeros(T)
    turn_F = np.zeros(T)
    i = 0
    while i < T:
        m = months[i]
        j = i
        while j < T and months[j] == m:
            j += 1
        w = np.full(n, 1.0 / n)
        for t in range(i, j):
            g_F[t] = float((w * R[:, t]).sum())
            w = w * (1.0 + R[:, t])
            w = w / w.sum()
        if j < T:
            turn_F[j] = float(np.abs(np.full(n, 1.0 / n) - w).sum())
        i = j
    n_F = g_F - turn_F * COST_RT

    # (c) 波动率目标 (E1 原版)
    g_c = np.zeros(T)
    for t in range(T):
        g_c[t] = rp[t] if t == 0 else w_lev[t - 1] * rp[t]
    cost_c = np.zeros(T)
    cost_c[1:] = np.abs(np.diff(w_lev)) * COST_RT
    n_c = g_c - cost_c

    # (c-Lbar) 常数杠杆 = (c) 已实现平均杠杆
    Lbar = float(w_lev.mean())
    g_L = np.zeros(T)
    for t in range(T):
        g_L[t] = rp[t] if t == 0 else Lbar * rp[t]
    cost_L = np.zeros(T)
    cost_L[0] = abs(Lbar - 1.0) * COST_RT
    n_L = g_L - cost_L

    # (c-shuffle) 杠杆序列置换 1000 次 (seed 固定)
    rng = np.random.default_rng(SEED)
    perm_all = np.array([rng.permutation(w_lev) for _ in range(NPERM)])
    Gs = np.empty((NPERM, T))
    Gs[:, 0] = rp[0]
    Gs[:, 1:] = perm_all[:, :-1] * rp[None, 1:]
    mu = Gs.mean(axis=1)
    sd = Gs.std(axis=1, ddof=1)
    shuf_sharpe = mu / sd * math.sqrt(252)

    return dict(rp=rp, roll=roll, w_lev=w_lev, Lbar=Lbar,
                g_a=g_a, n_a=n_a, g_bh=g_bh, n_bh=n_bh, g_F=g_F, n_F=n_F,
                g_c=g_c, n_c=n_c, cost_c=cost_c,
                g_L=g_L, n_L=n_L, cost_L=cost_L,
                Gs=Gs, shuf_sharpe=shuf_sharpe)


def decomp(R, d):
    """Willenbrock 闭式分解 + H1~H4"""
    n, T = R.shape
    g_i = [metrics(R[i])["ann_geo"] for i in range(n)]
    beta = float(np.mean(g_i))
    g_a_geo = metrics(d["g_a"])["ann_geo"]
    D_actual = g_a_geo - beta
    sig_i = [R[i].std(ddof=1) * math.sqrt(252) for i in range(n)]
    sig_p = d["g_a"].std(ddof=1) * math.sqrt(252)
    D_closed = 0.5 * (float(np.mean([s * s for s in sig_i])) - sig_p * sig_p)
    g_c_geo = metrics(d["g_c"])["ann_geo"]
    g_L_geo = metrics(d["g_L"])["ann_geo"]
    timing = g_c_geo - g_L_geo
    cost_c_ann = float(d["cost_c"].sum() / T * 252)
    g_c_net_geo = metrics(d["n_c"])["ann_geo"]
    resid = g_c_net_geo - (beta + D_actual + timing - cost_c_ann)

    # H1: 常数杠杆不改夏普
    h1 = abs(metrics(d["g_L"])["sharpe"] - metrics(d["g_a"])["sharpe"])
    # H3: D 实测 vs 闭式
    h3 = abs(D_actual - D_closed) / abs(D_closed) if D_closed else float("inf")
    # H2: 实际增益 vs 置换 95 分位
    sh_a = metrics(d["g_a"])["sharpe"]
    actual_gain = metrics(d["g_c"])["sharpe"] - sh_a
    q95 = float(np.quantile(d["shuf_sharpe"] - sh_a, 0.95))
    perm_mean = float((d["shuf_sharpe"] - sh_a).mean())
    actual_pct = float((d["shuf_sharpe"] - sh_a < actual_gain).mean())
    h2_pass = actual_gain > q95
    # H4: 算术 vs 几何
    mc = float(d["g_c"].mean())
    mL = float(d["g_L"].mean())
    mean_rel = abs(mc - mL) / abs(mL) if mL else float("nan")
    geo_diff = g_c_geo - g_L_geo
    if mean_rel < 0.05 and abs(geo_diff) >= 0.005:
        h4 = "路径/复利效应"
    elif mean_rel >= 0.05:
        h4 = "含 Cov(L,r) 成分(真择时)"
    else:
        h4 = "无法分离"

    return dict(beta=beta, D_actual=D_actual, D_closed=D_closed,
                timing=timing, cost=cost_c_ann, resid=resid,
                g_a_geo=g_a_geo, g_c_geo=g_c_geo, g_L_geo=g_L_geo,
                g_c_net_geo=g_c_net_geo, h1=h1, h3=h3,
                actual_gain=actual_gain, q95=q95, perm_mean=perm_mean,
                actual_pct=actual_pct, h2_pass=h2_pass, h4=h4,
                mean_rel=mean_rel, geo_diff=geo_diff)


def cond_sharpe(d):
    mask = ~np.isnan(d["roll"])
    rv, rr = d["roll"][mask], d["rp"][mask]
    qs = np.quantile(rv, [0.2, 0.4, 0.6, 0.8])
    bins = np.digitize(rv, qs)
    rows = []
    for b in range(5):
        sub = rr[bins == b]
        ann_ret = float(np.prod(1.0 + sub) ** (252.0 / len(sub)) - 1.0)
        ann_vol = float(sub.std(ddof=1) * math.sqrt(252))
        sh = float(sub.mean() / sub.std(ddof=1) * math.sqrt(252))
        rows.append(dict(q=b + 1, n=int(len(sub)), ann_ret=ann_ret, ann_vol=ann_vol, sharpe=sh))
    return rows


# ================= IS =================
raw = {s: load_is(s) for s in QUAL}
series = {}
for s in QUAL:
    dts, closes = zip(*raw[s])
    rr = np.array(closes[1:]) / np.array(closes[:-1]) - 1.0
    series[s] = {dt: r for dt, r in zip(dts[1:], rr)}
common = sorted(set().union(*[set(series[s]) for s in QUAL]))
common = [x for x in common if all(x in series[s] for s in QUAL)]
R = np.array([[series[s][x] for x in common] for s in QUAL])
dates = np.array(common)
T = R.shape[1]
print(f"[IS] {common[0]} ~ {common[-1]}  T={T}  N={N}")

d = build(R, dates)
dec = decomp(R, d)
cs = cond_sharpe(d)

# 复算关键量 (核对 E1: a=0.863, c=1.086 毛)
print("\n=== IS 六臂 (毛/净夏普) ===")
for nm, gk, nk in [("a 等权日再平衡", "g_a", "n_a"), ("a-BH 买入持有", "g_bh", "n_bh"),
                   ("a-F 月再平衡", "g_F", "n_F"), ("c vol目标", "g_c", "n_c"),
                   ("c-Lbar 常数杠杆", "g_L", "n_L")]:
    mg, mn = metrics(d[gk]), metrics(d[nk])
    print(f"  {nm:18s} 毛 {mg['sharpe']:6.3f}  净 {mn['sharpe']:6.3f}  毛vol {mg['ann_vol']*100:5.1f}%  净年化 {mn['ann_geo']*100:6.1f}%")
print(f"  c-shuffle(1000次) 夏普 中位 {np.median(d['shuf_sharpe']):6.3f}  均值 {d['shuf_sharpe'].mean():6.3f}")
print(f"  Lbar(平均杠杆) = {d['Lbar']:.4f}")

print("\n=== 分解 (%/年) ===")
print(f"  beta(Sum w*g) {dec['beta']*100:7.3f}   D实测 {dec['D_actual']*100:7.3f}   D闭式 {dec['D_closed']*100:7.3f}")
print(f"  择时 {dec['timing']*100:7.3f}   成本 {dec['cost']*100:7.3f}   残差 {dec['resid']*100:7.3f}")
print(f"  闭合检查: beta+D+timing-cost+resid = {(dec['beta']+dec['D_actual']+dec['timing']-dec['cost']+dec['resid'])*100:.4f}%  vs g_c_net {dec['g_c_net_geo']*100:.4f}%")

print("\n=== 条件夏普 (固定五等分) ===")
for r in cs:
    print(f"  Q{r['q']} n={r['n']:4d}  年化收益 {r['ann_ret']*100:6.2f}%  年化vol {r['ann_vol']*100:5.1f}%  夏普 {r['sharpe']:6.3f}")
q1q5 = cs[0]["sharpe"] - cs[4]["sharpe"]
print(f"  Q1-Q5 夏普差 = {q1q5:+.3f}")

print("\n=== 判据 ===")
print(f"  H1 |Sharpe(c-Lbar)-Sharpe(a)| = {dec['h1']:.5f}  (<0.05 ?)  {'PASS' if dec['h1']<0.05 else 'FAIL'}")
print(f"  H2 实际增益 {dec['actual_gain']:+.4f}  vs 置换95分位 {dec['q95']:+.4f} (均值 {dec['perm_mean']:+.4f}, 实际分位 {dec['actual_pct']*100:.1f}%)  {'PASS' if dec['h2_pass'] else 'FAIL'}")
print(f"  H3 |D实-D闭|/D闭 = {dec['h3']*100:.1f}%  (<40% ?)  {'PASS' if dec['h3']<0.40 else 'FAIL'}")
print(f"  H4 mean_rel={dec['mean_rel']*100:.2f}%  geo_diff={dec['geo_diff']*100:+.3f}%/yr  => {dec['h4']}")

if dec["h1"] >= 0.05:
    conclusion = "有疏漏需修正"
elif dec["h2_pass"] and q1q5 >= 0.3:
    conclusion = "归因成立"
elif dec["h2_pass"] and q1q5 < 0.3:
    conclusion = "无法验证"
else:
    conclusion = "归因不成立"
print(f"\n*** 三态结论: {conclusion} ***")

# ================= OOS 描述性 (展期 7 品种, 仅描述, 不判定) =================
KEYS7 = ["JD", "JM", "M", "P", "RU", "SR", "V"]
roll_maps = {k: load_roll(k) for k in KEYS7}
dts7 = sorted(set().union(*[set(roll_maps[k]) for k in KEYS7]))
dts7 = [x for x in dts7 if x >= "2022-01-01" and all(x in roll_maps[k] for k in KEYS7)]
R7 = np.array([[roll_maps[k][x] for x in dts7] for k in KEYS7])
R7 = R7[:, 1:] / R7[:, :-1] - 1.0
dates7 = np.array(dts7[1:])
d7 = build(R7, dates7)
dec7 = decomp(R7, d7)
print(f"\n[OOS 描述性] 展期7品种 {dates7[0]}~{dates7[-1]} T={len(dates7)} (篮7≠9, 口径B, 非干净OOS)")
for nm, gk in [("a", "g_a"), ("c", "g_c"), ("c-Lbar", "g_L")]:
    print(f"  {nm:8s} 毛夏普 {metrics(d7[gk])['sharpe']:6.3f}")
print(f"  OOS 分解: beta {dec7['beta']*100:.2f} D {dec7['D_actual']*100:.2f} 择时 {dec7['timing']*100:.2f} 成本 {dec7['cost']*100:.2f} 残差 {dec7['resid']*100:.2f} %/yr")

# ================= 落盘 =================
# arms.csv (IS 六臂逐日净值)
arm_defs = [("a", "n_a", "g_a", None), ("a-BH", "n_bh", "g_bh", None), ("a-F", "n_F", "g_F", None),
            ("c", "n_c", "g_c", "w_lev"), ("c-Lbar", "n_L", "g_L", "Lbar"), ("c-shuffle", None, None, "shuf")]
with open(os.path.join(OUT, "arms.csv"), "w", newline="", encoding="utf-8-sig") as f:
    w = csv.writer(f)
    w.writerow(["datetime", "arm", "equity", "lev"])
    # 各臂净值
    eq_map = {}
    for arm, nk, gk, lv in arm_defs:
        if arm == "c-shuffle":
            eq = np.cumprod(1.0 + d["Gs"][0])
            lev = d["Gs"][0] / np.where(d["rp"] == 0, 1, d["rp"])
            lev = np.where(np.abs(d["rp"]) < 1e-12, 1.0, lev)
        else:
            eq = np.cumprod(1.0 + d[nk])
            if lv == "w_lev":
                lev = np.concatenate([[1.0], d["w_lev"][:-1]])
            elif lv == "Lbar":
                lev = np.full(T, d["Lbar"])
            else:
                lev = np.ones(T)
        eq_map[arm] = (eq, lev)
    for t in range(T):
        for arm, _, _, _ in arm_defs:
            eq, lev = eq_map[arm]
            w.writerow([dates[t], arm, f"{eq[t]:.8f}", f"{lev[t]:.6f}"])

# decomp.csv
with open(os.path.join(OUT, "decomp.csv"), "w", newline="", encoding="utf-8-sig") as f:
    w = csv.writer(f)
    w.writerow(["item", "IS_2016_2021", "OOS_2022_desc_only"])
    for k, lbl in [("beta", "Sum_w_g(beta)"), ("D_actual", "D_diversification"),
                   ("timing", "timing"), ("cost", "cost"), ("resid", "residual")]:
        w.writerow([lbl, f"{dec[k]:.6f}", f"{dec7[k]:.6f}"])
    w.writerow(["D_closed", f"{dec['D_closed']:.6f}", f"{dec7['D_closed']:.6f}"])
    w.writerow(["g_net_geo", f"{dec['g_c_net_geo']:.6f}", f"{dec7['g_c_net_geo']:.6f}"])

# cond_sharpe.csv
with open(os.path.join(OUT, "cond_sharpe.csv"), "w", newline="", encoding="utf-8-sig") as f:
    w = csv.writer(f)
    w.writerow(["quantile", "n", "ann_ret", "ann_vol", "sharpe"])
    for r in cs:
        w.writerow([f"Q{r['q']}", r["n"], f"{r['ann_ret']:.6f}", f"{r['ann_vol']:.6f}", f"{r['sharpe']:.6f}"])

# shuffle.csv
with open(os.path.join(OUT, "shuffle.csv"), "w", newline="", encoding="utf-8-sig") as f:
    w = csv.writer(f)
    w.writerow(["idx", "gain"])
    gs = d["shuf_sharpe"] - metrics(d["g_a"])["sharpe"]
    for i, gv in enumerate(gs):
        w.writerow([i, f"{gv:.6f}"])
    w.writerow(["ACTUAL", f"{dec['actual_gain']:.6f}"])
    w.writerow(["MEAN", f"{dec['perm_mean']:.6f}"])
    w.writerow(["Q95", f"{dec['q95']:.6f}"])
    w.writerow(["ACTUAL_PCT", f"{dec['actual_pct']:.6f}"])
    w.writerow(["SEED", SEED])

# yearly.csv
yrs = np.array([x[:4] for x in dates])
shuf_year_med = {}
for y in sorted(set(yrs)):
    m = yrs == y
    sub = d["Gs"][:, m]
    mu = sub.mean(axis=1); sd = sub.std(axis=1, ddof=1)
    shuf_year_med[y] = float(np.median(mu / sd * math.sqrt(252)))
with open(os.path.join(OUT, "yearly.csv"), "w", newline="", encoding="utf-8-sig") as f:
    w = csv.writer(f)
    w.writerow(["year", "a", "a-BH", "a-F", "c", "c-Lbar", "c-shuffle_median", "inc_c_minus_a"])
    for y in sorted(set(yrs)):
        m = yrs == y
        def sh(k):
            return metrics(d[k][m])["sharpe"]
        w.writerow([y, f"{sh('g_a'):.4f}", f"{sh('g_bh'):.4f}", f"{sh('g_F'):.4f}",
                    f"{sh('g_c'):.4f}", f"{sh('g_L'):.4f}", f"{shuf_year_med[y]:.4f}",
                    f"{sh('g_c')-sh('g_a'):+.4f}"])

# 摘要 json 供写报告引用
summary = {
    "IS_window": [common[0], common[-1]], "T": int(T), "N": int(N),
    "Lbar": d["Lbar"], "conclusion": conclusion,
    "arms": {nm: {"sharpe_gross": metrics(d[gk])["sharpe"], "sharpe_net": metrics(d[nk])["sharpe"],
                  "ann_vol_gross": metrics(d[gk])["ann_vol"], "ann_geo_net": metrics(d[nk])["ann_geo"]}
             for nm, gk, nk in [("a", "g_a", "n_a"), ("a-BH", "g_bh", "n_bh"), ("a-F", "g_F", "n_F"),
                                ("c", "g_c", "n_c"), ("c-Lbar", "g_L", "n_L")]},
    "shuffle": {"median": float(np.median(d["shuf_sharpe"])), "mean": float(d["shuf_sharpe"].mean())},
    "decomp": {k: float(dec[k]) for k in ["beta", "D_actual", "D_closed", "timing", "cost", "resid", "g_c_net_geo"]},
    "H1": dec["h1"], "H2": {"actual_gain": dec["actual_gain"], "q95": dec["q95"],
                            "perm_mean": dec["perm_mean"], "actual_pct": dec["actual_pct"], "pass": bool(dec["h2_pass"])},
    "H3": dec["h3"], "H4": {"mean_rel": dec["mean_rel"], "geo_diff": dec["geo_diff"], "verdict": dec["h4"]},
    "cond_sharpe": cs, "Q1_minus_Q5": float(q1q5),
    "OOS_desc": {"window": [dates7[0], dates7[-1]], "T": int(len(dates7)),
                 "sharpe": {nm: float(metrics(d7[gk])["sharpe"]) for nm, gk in [("a", "g_a"), ("c", "g_c"), ("c-Lbar", "g_L")]},
                 "decomp": {k: float(dec7[k]) for k in ["beta", "D_actual", "D_closed", "timing", "cost", "resid"]}},
    "yearly": {y: {k: float(metrics(d[k][yrs == y])["sharpe"]) for k in ["g_a", "g_c", "g_L"]} for y in sorted(set(yrs))},
    "shuf_year_med": shuf_year_med,
}
import json
with open(os.path.join(OUT, "_summary.json"), "w", encoding="utf-8") as f:
    json.dump(summary, f, ensure_ascii=False, indent=1)

print("\n已落盘 ->", OUT, " (arms/decomp/cond_sharpe/shuffle/yearly/_summary.json)")
