# -*- coding: utf-8 -*-
"""
路线1 最终修正：控制同期收益后，再看 lead-lag

问题：JM-RB 同期 corr=0.61，原始回归测到的"领先性"可能只是共同因子在两品种间的
      不同步反应（流动性差异），不是产业链传导。
修正：回归加同期项
      r_B[t] = a + b*r_A[t-k] + c*r_B[t-1] + d*r_A[t] + e
      b 现在是"剔除同期共同变动后，A 的滞后信息对 B 的增量解释"。
      若 b 显著而 d 也显著 => 同期同涨同跌 + 滞后残留（可能是真传导）
      若 b 不显著而 d 显著 => 纯共同因子，无传导
数据：5min(2021-2023) + 日频(2016-2025, 展期日线)
"""
import os
import sys
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)

NW_LAG = 12
PAIRS = [("JM", "RB"), ("M", "JD"), ("M", "LH"), ("M", "P"), ("SA", "V"), ("RB", "RU")]


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


print("=" * 100)
print("A. 5min（2021-2023）：加同期控制 r_A[t] 后，各 lag 的 t 值")
print("=" * 100)
R = pd.read_parquet(os.path.join(HERE, "_r_wide_5min.parquet"))
R.index = pd.to_datetime(R.index)
print(f"{'配对':<9}{'同期corr':>9}{'lag':>4}{'t(滞后)':>10}{'t(同期)':>10}{'β滞后bp':>10}")
for a, b in PAIRS:
    sub = R[[a, b]].dropna()
    ra, rb = sub[a].to_numpy(), sub[b].to_numpy()
    corr = np.corrcoef(ra, rb)[0, 1]
    n = len(rb)
    y1 = np.concatenate([[np.nan], rb[:-1]])
    rows = []
    for k in range(1, 13):
        x = np.concatenate([[np.nan] * k, ra[:-k]])
        m = ~(np.isnan(x) | np.isnan(y1) | np.isnan(rb) | np.isnan(ra))
        beta, tv = ols_t(rb[m], [x[m], y1[m], ra[m]])
        rows.append((k, tv[1], tv[3], beta[1] * x[m].std() * 1e4))
    kbest = max(rows, key=lambda r: abs(r[1]))
    print(f"{a}->{b:<6}{corr:>9.3f}{kbest[0]:>4}{kbest[1]:>10.2f}{kbest[2]:>10.2f}{kbest[3]:>10.2f}")

print()
print("=" * 100)
print("B. 日频十年（展期日线）：加同期控制")
print("=" * 100)
d = pd.read_csv(os.path.join(ROOT, "工具", "_数据", "展期_合约日线.csv"), encoding="utf-8-sig")
d["日期"] = pd.to_datetime(d["日期"])
d = d.dropna(subset=["close", "volume"])
d = d[d["volume"] > 0]
idx = d.groupby(["key", "日期"])["volume"].idxmax()
main = d.loc[idx].sort_values(["key", "日期"]).copy()
main["prev_sym"] = main.groupby("key")["sym"].shift(1)
main["switch"] = main["sym"] != main["prev_sym"]
main["r"] = np.log(main["close"] / main.groupby("key")["close"].shift(1))
main.loc[main["switch"], "r"] = np.nan
main.loc[main["r"].abs() > 0.15, "r"] = np.nan
px = main.pivot(index="日期", columns="key", values="r")

print(f"{'配对':<9}{'同期corr':>9}{'lag':>4}{'t(滞后)':>10}{'t(同期)':>10}{'β滞后bp':>10}{'n':>7}")
for a, b in [("M", "JD"), ("M", "LH"), ("M", "P")]:
    sub = px[[a, b]].dropna()
    ra, rb = sub[a].to_numpy(), sub[b].to_numpy()
    corr = np.corrcoef(ra, rb)[0, 1]
    y1 = np.concatenate([[np.nan], rb[:-1]])
    rows = []
    for k in range(1, 6):
        x = np.concatenate([[np.nan] * k, ra[:-k]])
        m = ~(np.isnan(x) | np.isnan(y1) | np.isnan(rb) | np.isnan(ra))
        beta, tv = ols_t(rb[m], [x[m], y1[m], ra[m]])
        rows.append((k, tv[1], tv[3], beta[1] * x[m].std() * 1e4, m.sum()))
    kbest = max(rows, key=lambda r: abs(r[1]))
    print(f"{a}->{b:<6}{corr:>9.3f}{kbest[0]:>4}{kbest[1]:>10.2f}{kbest[2]:>10.2f}"
          f"{kbest[3]:>10.2f}{kbest[4]:>7}")

print()
print("=" * 100)
print("C. 对照：不加同期控制时的 t(滞后)（同 lag，便于比 |t| 掉多少）")
print("=" * 100)
print(f"{'配对':<9}{'lag':>4}{'控制前t':>10}{'控制后t':>10}{'衰减%':>8}")
for a, b in PAIRS:
    sub = R[[a, b]].dropna()
    ra, rb = sub[a].to_numpy(), sub[b].to_numpy()
    y1 = np.concatenate([[np.nan], rb[:-1]])
    rows = []
    for k in range(1, 13):
        x = np.concatenate([[np.nan] * k, ra[:-k]])
        m = ~(np.isnan(x) | np.isnan(y1) | np.isnan(rb) | np.isnan(ra))
        b0, t0 = ols_t(rb[m], [x[m], y1[m]])
        b1, t1 = ols_t(rb[m], [x[m], y1[m], ra[m]])
        rows.append((k, t0[1], t1[1]))
    kbest = max(rows, key=lambda r: abs(r[1]))
    decay = (1 - abs(kbest[2]) / abs(kbest[1])) * 100 if kbest[1] else np.nan
    print(f"{a}->{b:<6}{kbest[0]:>4}{kbest[1]:>10.2f}{kbest[2]:>10.2f}{decay:>8.0f}%")
