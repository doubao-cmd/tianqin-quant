# -*- coding: utf-8 -*-
"""
路线1 最后一搏：日频扩到 2016-2025（展期合约日线）
逻辑：5min/15min/30min/60min/日频(3年) 全死，但日频效应量 βσ 是 5min 的 20 倍，
      唯一可能是"样本不够"。用十年日线把样本从 640 天扩到 2400 天。
数据：工具/_数据/展期_合约日线.csv（9 品种，每日多合约）
      主力 = 每日 volume 最大合约；换月日收益置 NaN（避免换月跳空冒充信号）
配对：M->JD / M->LH / M->P（展期日线里没有 RB、SA，另三对做不了）
判据：同前 P1 |t|>3.5、P2 后半段同号、P3 每笔扣成本>0、P4 正向>反向
"""
import os
import sys
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)
import config_symbols as cs

F = os.path.join(ROOT, "工具", "_数据", "展期_合约日线.csv")
PAIRS = [("M", "JD", "豆粕->鸡蛋"), ("M", "LH", "豆粕->生猪"), ("M", "P", "豆粕->棕榈")]
NW_LAG = 10

d = pd.read_csv(F, encoding="utf-8-sig")
d["日期"] = pd.to_datetime(d["日期"])
d = d.dropna(subset=["close", "volume"])
d = d[d["volume"] > 0]

# 每日主力 = volume 最大
idx = d.groupby(["key", "日期"])["volume"].idxmax()
main = d.loc[idx].sort_values(["key", "日期"]).copy()
main["prev_sym"] = main.groupby("key")["sym"].shift(1)
main["switch"] = main["sym"] != main["prev_sym"]
main["r"] = np.log(main["close"] / main.groupby("key")["close"].shift(1))
main.loc[main["switch"], "r"] = np.nan
main.loc[main["r"].abs() > 0.15, "r"] = np.nan      # 极端值再兜一层

px = main.pivot(index="日期", columns="key", values="r")
print("=== 数据 ===")
for k in ["M", "JD", "LH", "P"]:
    s = px[k].dropna()
    print(f"  {k:3s} {len(s):>5} 天  {s.index.min().date()} ~ {s.index.max().date()}  σ={s.std()*100:.2f}%/日")
print()


def ols_t(y, x, y1):
    n = len(y)
    X = np.column_stack([np.ones(n), x, y1])
    XtX = X.T @ X
    beta = np.linalg.solve(XtX, X.T @ y)
    e = y - X @ beta
    Xe = X * e[:, None]
    S = Xe.T @ Xe
    L = min(NW_LAG, n // 10)
    for l in range(1, L + 1):
        w = 1.0 - l / (L + 1.0)
        G = Xe[l:].T @ Xe[:-l]
        S += w * (G + G.T)
    cov = np.linalg.inv(XtX) @ S @ np.linalg.inv(XtX)
    return beta[1], beta[1] / np.sqrt(cov[1, 1])


def cost_bp(sym):
    s = px[sym].dropna()
    p = None
    for key_file in [sym]:
        pass
    # 用 5min 数据的中位价近似（更稳）
    f = os.path.join(ROOT, "工具", "_进场体检", "数据", f"{sym}_5min_2021-01-01_2023-12-31.csv")
    p = pd.read_csv(f, usecols=["close"])["close"].median()
    _, _, mult, tick = cs.SYMBOLS[sym]
    return (tick * mult + cs.comm_per_hand(sym, p)) / (p * mult) * 1e4


print("=" * 100)
print("A. 全样本（2016-2025，LH 从 2021 起）逐 lag")
print("=" * 100)
for a, b, label in PAIRS:
    sub = px[[a, b]].dropna()
    print(f"\n【{a}->{b}】{label}  n={len(sub)}  {sub.index.min().date()}~{sub.index.max().date()}")
    n = len(sub)
    y = sub[b].to_numpy()
    y1 = np.concatenate([[np.nan], y[:-1]])
    best = None
    for k in range(1, 6):
        x = np.concatenate([[np.nan] * k, sub[a].to_numpy()[:-k]])
        m = ~(np.isnan(x) | np.isnan(y1) | np.isnan(y))
        beta, t = ols_t(y[m], x[m], y1[m])
        pnl = np.sign(x[m][:-1]) * y[m][1:] * 1e4
        c = cost_bp(b)
        print(f"   lag={k}  n={m.sum():>5}  t={t:>6.2f}  βσ={beta*x[m].std()*1e4:>6.2f}bp  "
              f"每笔毛={pnl.mean():>7.2f}bp  往返成本={2*c:>5.2f}bp  净={pnl.mean()-2*c:>7.2f}bp")
        if best is None or abs(t) > abs(best[1]):
            best = (k, t, beta)
    print(f"   => 最佳 lag={best[0]}  t={best[1]:.2f}")

print()
print("=" * 100)
print("B. 分段稳定性：前 5 年 vs 后 5 年（用各自最佳 lag）")
print("=" * 100)
print(f"{'配对':<9}{'lag':>4}{'前半n':>7}{'前半t':>8}{'后半n':>7}{'后半t':>8}{'同号':>6}")
for a, b, label in PAIRS:
    sub = px[[a, b]].dropna()
    y = sub[b].to_numpy()
    y1 = np.concatenate([[np.nan], y[:-1]])
    best_k, best_t = None, 0
    for k in range(1, 6):
        x = np.concatenate([[np.nan] * k, sub[a].to_numpy()[:-k]])
        m = ~(np.isnan(x) | np.isnan(y1) | np.isnan(y))
        _, t = ols_t(y[m], x[m], y1[m])
        if abs(t) > abs(best_t):
            best_k, best_t = k, t
    mid = len(sub) // 2
    out = []
    for part in (sub.iloc[:mid], sub.iloc[mid:]):
        yy = part[b].to_numpy()
        y1p = np.concatenate([[np.nan], yy[:-1]])
        xp = np.concatenate([[np.nan] * best_k, part[a].to_numpy()[:-best_k]])
        mp = ~(np.isnan(xp) | np.isnan(y1p) | np.isnan(yy))
        _, tp = ols_t(yy[mp], xp[mp], y1p[mp])
        out.append((mp.sum(), tp))
    same = np.sign(out[0][1]) == np.sign(out[1][1])
    print(f"{a}->{b:<6}{best_k:>4}{out[0][0]:>7}{out[0][1]:>8.2f}{out[1][0]:>7}{out[1][1]:>8.2f}"
          f"{str(same):>6}")

print()
print("=" * 100)
print("C. 分年 t 值（看是否只在某一年成立）")
print("=" * 100)
for a, b, label in PAIRS:
    sub = px[[a, b]].dropna()
    y_all = sub[b].to_numpy()
    y1_all = np.concatenate([[np.nan], y_all[:-1]])
    best_k, best_t = None, 0
    for k in range(1, 6):
        x = np.concatenate([[np.nan] * k, sub[a].to_numpy()[:-k]])
        m = ~(np.isnan(x) | np.isnan(y1_all) | np.isnan(y_all))
        _, t = ols_t(y_all[m], x[m], y1_all[m])
        if abs(t) > abs(best_t):
            best_k, best_t = k, t
    print(f"\n{a}->{b}  lag={best_k}")
    for yr, g in sub.groupby(sub.index.year):
        if len(g) < 150:
            continue
        yy = g[b].to_numpy()
        y1p = np.concatenate([[np.nan], yy[:-1]])
        xp = np.concatenate([[np.nan] * best_k, g[a].to_numpy()[:-best_k]])
        mp = ~(np.isnan(xp) | np.isnan(y1p) | np.isnan(yy))
        if mp.sum() < 150:
            continue
        bb, tt = ols_t(yy[mp], xp[mp], y1p[mp])
        print(f"   {yr}  n={mp.sum():>4}  t={tt:>6.2f}  βσ={bb*xp[mp].std()*1e4:>6.1f}bp")
