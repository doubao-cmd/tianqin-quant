# -*- coding: utf-8 -*-
"""
内外盘【比价】是否均值回归
==================================================
前面的回归显示 β_total ≈ 0.45（外盘涨1%，内盘只跟0.45%），说明价差在走阔。
问题：走阔之后会不会补回来？若会 ⇒ 只交易内盘一条腿就能赚（成本可控）。

口径：
  spread = ln(P_inner) - ln(P_outer)   （单位不同不影响变化量）
  z      = (spread - MA60) / SD60      （滚动标准化，避免绝对水平无意义）
  检验1  ：Δspread[t→t+h] ~ z[t]       β<0 且显著 ⇒ 回归
  检验2  ：r_inner[t→t+h] ~ z[t]       （单边可交易口径）
  检验3  ：半衰期（AR(1) on spread）
"""
import os
import numpy as np, pandas as pd

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_cache")

PAIRS = [("CU0", "CAD", "沪铜-LME铜"), ("CU0", "HG", "沪铜-COMEX铜"),
         ("ZN0", "ZSD", "沪锌-LME锌"), ("NI0", "NID", "沪镍-LME镍"),
         ("AL0", "AHD", "沪铝-LME铝"),
         ("SC0", "OIL", "原油-布伦特"), ("SC0", "CL", "原油-NYMEX"),
         ("M0", "S", "豆粕-CBOT大豆"), ("Y0", "BO", "豆油-CBOT豆油"),
         ("P0", "BO", "棕榈-CBOT豆油"), ("P0", "FCPO", "棕榈-马棕"),
         ("CF0", "CT", "棉花-ICE棉"), ("C0", "C", "玉米-CBOT玉米"),
         ("RB0", "CAD", "螺纹-LME铜(安慰剂)")]

COST_BP = {"CU": 5.0, "ZN": 6.0, "NI": 8.0, "AL": 5.0, "SC": 8.0,
           "M": 7.3, "Y": 7.0, "P": 12.0, "CF": 8.0, "C": 5.0, "RB": 6.5}


def ols_nw(y, x, lags=5):
    """OLS + Newey-West 标准误"""
    n = len(y)
    X = np.column_stack([np.ones(n), x])
    XtX = X.T @ X
    b = np.linalg.solve(XtX, X.T @ y)
    e = y - X @ b
    XtXi = np.linalg.inv(XtX)
    S = (X * e[:, None]).T @ (X * e[:, None])
    for L in range(1, lags + 1):
        w = 1 - L / (lags + 1)
        A = (X[L:] * e[L:, None]).T @ (X[:-L] * e[:-L, None])
        S += w * (A + A.T)
    V = XtXi @ S @ XtXi
    se = np.sqrt(np.diag(V))
    return b[1], b[1] / se[1], n


print("=" * 104)
print(f"{'配对':22s} {'样本':>5s} {'半衰期天':>8s} {'z→Δspread(5d)':>14s} {'t':>7s} "
      f"{'做空毛bp':>15s} {'t':>7s} {'成本bp':>7s} {'净bp':>7s}")
print("=" * 104)

rows = []
for isym, osym, label in PAIRS:
    fi = os.path.join(CACHE, f"in_{isym}.csv")
    fo = os.path.join(CACHE, f"out_{osym}.csv")
    if not (os.path.exists(fi) and os.path.exists(fo)):
        print(f"{label:22s} 数据缺失"); continue
    di = pd.read_csv(fi, parse_dates=["date"])[["date", "close"]].sort_values("date")
    do = pd.read_csv(fo, parse_dates=["date"])[["date", "close"]].sort_values("date")
    do["date"] = do["date"] + pd.Timedelta(days=1)          # 外盘 d -> 内盘 d+1（已确诊）
    do = do.drop_duplicates("date", keep="last")
    m = di.merge(do, on="date", suffixes=("_i", "_o")).dropna()
    m = m[(m["close_i"] > 0) & (m["close_o"] > 0)]
    if len(m) < 300:
        print(f"{label:22s} 样本不足 {len(m)}"); continue

    m["li"] = np.log(m["close_i"])
    m["lo"] = np.log(m["close_o"])
    m["sp"] = m["li"] - m["lo"]
    m.loc[m["sp"].diff().abs() > 0.15, "sp"] = np.nan        # 换月/异常
    m = m.dropna(subset=["sp"]).reset_index(drop=True)

    m["z"] = (m["sp"] - m["sp"].rolling(60).mean()) / m["sp"].rolling(60).std()

    H = 5
    m["dsp_fwd"] = m["sp"].shift(-H) - m["sp"]
    m["ri_fwd"] = m["li"].shift(-H) - m["li"]
    s = m.dropna(subset=["z", "dsp_fwd", "ri_fwd"])
    if len(s) < 200:
        print(f"{label:22s} 样本不足 {len(s)}"); continue

    # 半衰期：Δsp = a + b*sp_lag
    sp = m["sp"].values
    d_sp = np.diff(sp)
    b_ar, t_ar, _ = ols_nw(d_sp, sp[:-1])
    hl = -np.log(2) / np.log(1 + b_ar) if -1 < b_ar < 0 else np.nan

    b1, t1, n = ols_nw(s["dsp_fwd"].values, s["z"].values)
    b2, t2, _ = ols_nw(s["ri_fwd"].values, s["z"].values)
    # z=1σ 时的 5 日单边期望收益（bp），扣一次往返成本
    # z=+1σ ⇒ 内盘相对贵 ⇒ 做空内盘。策略期望 = -b2，扣一次往返成本
    gross_bp = -b2 * 1.0 * 1e4
    cost = COST_BP.get(isym[:2], 8.0)
    print(f"{label:22s} {n:>5d} {hl:>8.1f} {b1:>14.4f} {t1:>7.2f} {gross_bp:>15.1f} {t2:>7.2f} "
          f"{cost:>7.1f} {gross_bp-cost:>7.1f}")
    rows.append(dict(label=label, n=n, half_life=hl, beta_dsp=b1, t_dsp=t1,
                     inner_bp=b2 * 1e4, t_inner=t2, cost=cost))

pd.DataFrame(rows).to_csv(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "_比价回归检验_20261102.csv"), index=False, encoding="utf-8-sig")
print("=" * 104)
print("β_dsp<0 ⇒ 价差回归；gross_bp = 做空内盘(z=+1σ)的未来5日期望收益；净=毛-成本")
