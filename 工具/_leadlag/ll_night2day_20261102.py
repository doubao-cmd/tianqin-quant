# -*- coding: utf-8 -*-
"""
路线2：夜盘 -> 日盘 跨时段传导

分段（关键：必须拆开，否则不可交易的部分会污染结论）
  r_night[t] = log(前一日22:55收盘 / 前一日14:55收盘)   夜盘收益（外盘信息）
  r_gap[t]   = log(当日09:00开盘 / 前一日22:55收盘)     开盘跳空（不可交易，只能接受）
  r_intra[t] = log(当日14:55收盘 / 当日09:00开盘)       日内（唯一可交易段）
  r_full[t]  = r_gap + r_intra

检验：r_intra[t] = a + b*r_night[t] + c*r_intra[t-1] + e
      b>0 => 夜盘信息在日盘延续（动量）
      b<0 => 夜盘过度反应，日盘回吐（反转）
成本：往返 3.6~8.5bp，日内σ 约 80~120bp，门槛只有 5~10%
样本：2021-2023，10 个有夜盘的品种（JD/LH 无夜盘，排除）
"""
import os
import sys
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)
import config_symbols as cs

DATA = os.path.join(ROOT, "工具", "_进场体检", "数据")
SYMS = ["JM", "RB", "M", "P", "SA", "V", "MA", "RU", "SR", "EB"]
NW_LAG = 5
rng = np.random.default_rng(20261102)


def load(sym):
    f = os.path.join(DATA, f"{sym}_5min_2021-01-01_2023-12-31.csv")
    d = pd.read_csv(f, usecols=["datetime", "open", "close"])
    d["dt"] = pd.to_datetime(d["datetime"]) + pd.Timedelta(hours=8)
    d = d.drop_duplicates("dt").sort_values("dt")
    d["date"] = d["dt"].dt.normalize()
    hm = d["dt"].dt.strftime("%H:%M")
    o = d[hm == "09:00"].set_index("date")["open"].astype(float)
    c = d[hm == "14:55"].set_index("date")["close"].astype(float)
    n = d[hm == "22:55"].set_index("date")["close"].astype(float)
    df = pd.DataFrame({"dopen": o, "dclose": c, "nclose": n}).sort_index()
    df = df.dropna(subset=["nclose", "dopen", "dclose"])
    df["p_n"] = df["nclose"].shift(1)
    df["p_c"] = df["dclose"].shift(1)
    df = df.dropna(subset=["p_n", "p_c"])
    df["r_night"] = np.log(df["p_n"] / df["p_c"])
    df["r_gap"] = np.log(df["dopen"] / df["p_n"])
    df["r_intra"] = np.log(df["dclose"] / df["dopen"])
    df["r_full"] = np.log(df["dclose"] / df["p_n"])
    for c2 in ["r_night", "r_gap", "r_intra", "r_full"]:
        df.loc[df[c2].abs() > 0.12, c2] = np.nan
    return df.dropna(subset=["r_night", "r_gap", "r_intra", "r_full"])


def ols_t(y, cols):
    X = np.column_stack([np.ones(len(y))] + cols)
    XtX = X.T @ X
    beta = np.linalg.solve(XtX, X.T @ y)
    e = y - X @ beta
    Xe = X * e[:, None]
    S = Xe.T @ Xe
    L = min(NW_LAG, len(y) // 10)
    for l in range(1, L + 1):
        w = 1.0 - l / (L + 1.0)
        G = Xe[l:].T @ Xe[:-l]
        S += w * (G + G.T)
    cov = np.linalg.inv(XtX) @ S @ np.linalg.inv(XtX)
    return beta, beta / np.sqrt(np.diag(cov))


def cost1(sym, px):
    _, _, mult, tick = cs.SYMBOLS[sym]
    return (tick * mult + cs.comm_per_hand(sym, px)) / (px * mult) * 1e4


D = {s: load(s) for s in SYMS}
PX = {}
for s in SYMS:
    PX[s] = pd.read_csv(os.path.join(DATA, f"{s}_5min_2021-01-01_2023-12-31.csv"),
                        usecols=["close"])["close"].median()

print("=" * 108)
print("A. 全样本：夜盘 -> 次日三段（2021-2023）")
print("=" * 108)
print(f"{'品种':<5}{'n':>6}{'夜盘σ':>8}{'日内σ':>8}"
      f"{'β→gap':>8}{'t':>7}{'β→intra':>9}{'t':>7}{'β→full':>8}{'t':>7}"
      f"{'每笔净bp':>9}{'胜率':>6}")
rows = []
for s in SYMS:
    d = D[s]
    y = d["r_intra"].to_numpy()
    x = d["r_night"].to_numpy()
    y1 = np.concatenate([[np.nan], y[:-1]])
    m = ~(np.isnan(x) | np.isnan(y) | np.isnan(y1))
    bb, tt = ols_t(y[m], [x[m], y1[m]])
    b_intra, t_intra = bb[1], tt[1]
    g = d["r_gap"].to_numpy()
    m2 = ~(np.isnan(x) | np.isnan(g))
    bb, tt = ols_t(g[m2], [x[m2]])
    b_gap, t_gap = bb[1], tt[1]
    f = d["r_full"].to_numpy()
    m3 = ~(np.isnan(x) | np.isnan(f))
    bb, tt = ols_t(f[m3], [x[m3]])
    b_full, t_full = bb[1], tt[1]
    # 扣成本：sign(r_night) 开在 09:00，平在 15:00
    pos = np.sign(x[m])
    gross = pos[:-1] * y[m][1:] * 1e4
    c = 2 * cost1(s, PX[s])
    net = gross.mean() - c
    rows.append({"sym": s, "n": int(m.sum()), "night_sd": d["r_night"].std() * 1e4,
                 "intra_sd": d["r_intra"].std() * 1e4,
                 "b_gap": b_gap, "t_gap": t_gap, "b_intra": b_intra, "t_intra": t_intra,
                 "b_full": b_full, "t_full": t_full, "net": net,
                 "win": (gross > 0).mean() * 100, "cost": c})
    r = rows[-1]
    print(f"{s:<5}{r['n']:>6}{r['night_sd']:>8.0f}{r['intra_sd']:>8.0f}"
          f"{r['b_gap']*1e4:>8.2f}{r['t_gap']:>7.2f}{r['b_intra']*1e4:>9.2f}{r['t_intra']:>7.2f}"
          f"{r['b_full']*1e4:>8.2f}{r['t_full']:>7.2f}{r['net']:>9.2f}{r['win']:>6.1f}")

print()
print("=" * 108)
print("B. 分年稳定性：r_intra ~ r_night 的 t 值")
print("=" * 108)
print(f"{'品种':<5}" + "".join(f"{y:>9}" for y in [2021, 2022, 2023]) + "   同号  P2")
for s in SYMS:
    d = D[s]
    ts = []
    for yr in (2021, 2022, 2023):
        sub = d[d.index.year == yr]
        y = sub["r_intra"].to_numpy()
        x = sub["r_night"].to_numpy()
        y1 = np.concatenate([[np.nan], y[:-1]])
        m = ~(np.isnan(x) | np.isnan(y) | np.isnan(y1))
        if m.sum() < 100:
            ts.append(np.nan)
            continue
        _, tv = ols_t(y[m], [x[m], y1[m]])
        ts.append(tv[1])
    good = sum(1 for t in ts if not np.isnan(t) and np.sign(t) == np.sign(ts[0]))
    print(f"{s:<5}" + "".join(f"{t:>9.2f}" for t in ts) +
          f"   {good}/3  {'过' if good >= 2 else '不过'}")

print()
print("=" * 108)
print("C. 分层：按夜盘收益 5 组，看次日日内平均收益（bp）")
print("=" * 108)
print(f"{'品种':<5}{'Q1(夜跌)':>10}{'Q2':>8}{'Q3':>8}{'Q4':>8}{'Q5(夜涨)':>10}{'Q5-Q1':>9}{'单调?':>7}")
for s in SYMS:
    d = D[s]
    q = pd.qcut(d["r_night"], 5, labels=False, duplicates="drop")
    g = d.groupby(q)["r_intra"].mean() * 1e4
    if len(g) < 5:
        continue
    diff = g.iloc[-1] - g.iloc[0]
    mono = "向上" if (g.diff().dropna() > 0).sum() >= 3 else ("向下" if (g.diff().dropna() < 0).sum() >= 3 else "无")
    print(f"{s:<5}" + "".join(f"{v:>10.1f}" if i in (0, 4) else f"{v:>8.1f}"
                              for i, v in enumerate(g.values)) + f"{diff:>9.1f}{mono:>7}")

print()
print("=" * 108)
print("D. 安慰剂：置换 r_night 500 次，看 |t| 零分布（r_intra 为被解释变量）")
print("=" * 108)
print(f"{'品种':<5}{'观测t':>8}{'零p95':>8}{'p':>7}  判定")
for s in SYMS:
    d = D[s]
    y = d["r_intra"].to_numpy()
    x = d["r_night"].to_numpy()
    y1 = np.concatenate([[np.nan], y[:-1]])
    m = ~(np.isnan(x) | np.isnan(y) | np.isnan(y1))
    _, tv0 = ols_t(y[m], [x[m], y1[m]])
    t_obs = tv0[1]
    mx = []
    for _ in range(500):
        xp = x.copy()
        rng.shuffle(xp)
        mm = ~(np.isnan(xp) | np.isnan(y) | np.isnan(y1))
        _, tv2 = ols_t(y[mm], [xp[mm], y1[mm]])
        mx.append(abs(tv2[1]))
    mx = np.array(mx)
    p95 = np.quantile(mx, 0.95)
    p = float((mx >= abs(t_obs)).mean())
    ok = "★" if (abs(t_obs) > max(3.2, p95) and p < 0.05) else "死"
    print(f"{s:<5}{t_obs:>8.2f}{p95:>8.2f}{p:>7.3f}  {ok}")

print()
print("=" * 108)
print("E. 汇总：判据（P1 |t|>3.2, P2 分年≥2/3同号, P3 扣成本净>0, P4 安慰剂p<0.05）")
print("=" * 108)
tbl = pd.DataFrame(rows)
tbl["P1"] = tbl["t_intra"].abs() > 3.2
tbl["P3"] = tbl["net"] > 0
print(tbl[["sym", "t_intra", "net", "P1", "P3"]].to_string(index=False))
