# -*- coding: utf-8 -*-
"""
路线1：多周期 lead-lag 检验
周期：5min(已有) / 15min(现成) / 30min / 60min / 日频
     30/60min 由 5min 按"连续段内每 n 根"重采样，尾组不足 n/2 丢弃
     日频 = 每日最后一根 5min 的 close
口径：r = log(C_t/C_{t-1})，仅相邻 bar（gap <= n+30min）才算，跨日置 NaN
      |r| > 2.5% 剔除
检验：r_B[t] = a + b*r_A[t-k] + c*r_B[t-1] + e，NW(HAC, L=12)
安慰剂：置换 A 的收益 200 次，取 max|t| 零分布
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
SYMS = ["JM", "RB", "M", "JD", "LH", "P", "SA", "V", "MA", "RU", "SR", "EB"]
PAIRS = [("JM", "RB"), ("M", "JD"), ("M", "LH"), ("M", "P"), ("SA", "V"), ("RB", "RU")]
NW_LAG = 12
JUMP_CAP = 0.025
rng = np.random.default_rng(20261102)


def load_close(sym, minutes):
    if minutes == 15:
        f = os.path.join(DATA, f"{sym}_15min_2021-01-01_2023-12-31.csv")
        d = pd.read_csv(f, usecols=["datetime", "close"])
        d["dt"] = pd.to_datetime(d["datetime"]) + pd.Timedelta(hours=8)
        return d.set_index("dt")["close"].astype(float).sort_index()
    f = os.path.join(DATA, f"{sym}_5min_2021-01-01_2023-12-31.csv")
    d = pd.read_csv(f, usecols=["datetime", "close"])
    d["dt"] = pd.to_datetime(d["datetime"]) + pd.Timedelta(hours=8)
    s = d.set_index("dt")["close"].astype(float).sort_index()
    if minutes == 5:
        return s
    if minutes == 1440:
        return s.groupby(s.index.normalize()).last()
    n = minutes // 5
    df = pd.DataFrame({"close": s.to_numpy(), "dt": s.index})
    newblk = df["dt"].diff() != pd.Timedelta(minutes=5)
    df["blk"] = newblk.cumsum()
    df["pos"] = df.groupby("blk").cumcount()
    df["grp"] = df["blk"] * 10000 + df["pos"] // n
    agg = df.groupby("grp").agg(close=("close", "last"), dt=("dt", "last"), size=("close", "size"))
    agg = agg[agg["size"] >= max(1, n // 2)]
    return agg.set_index("dt")["close"].sort_index()


def make_returns(minutes):
    """返回宽表 r（index=bar 时刻）。"""
    out = {}
    for sym in SYMS:
        s = load_close(sym, minutes)
        if minutes == 1440:
            r = np.log(s / s.shift(1))
        else:
            gap = pd.Series(s.index, index=s.index).diff().dt.total_seconds() / 60
            r = np.log(s / s.shift(1))
            r = r.where(gap <= minutes + 30)
        r = r.where(r.abs() <= JUMP_CAP)
        out[sym] = r
    return pd.DataFrame(out)


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


def scan(ra, rb, lags):
    """全 lag 扫描，返回 [(lag, t, beta_std_bp)]。"""
    n = len(rb)
    y1 = np.concatenate([[np.nan], rb[:-1]])
    res = []
    for k in lags:
        if k >= n - 100:
            continue
        x = np.concatenate([[np.nan] * k, ra[:-k]])
        m = ~(np.isnan(x) | np.isnan(y1) | np.isnan(rb))
        if m.sum() < 300:
            continue
        b, t = ols_t(rb[m], x[m], y1[m])
        res.append((k, t, b * x[m].std() * 1e4))
    return res


CONF = {5: 12, 15: 8, 30: 8, 60: 6, 1440: 5}
LABEL = {5: "5min", 15: "15min", 30: "30min", 60: "60min", 1440: "日频"}

summary = []
for minutes in [15, 30, 60, 1440]:
    R = make_returns(minutes)
    R = R.loc["2021-01-01":"2023-12-29"]
    lags = list(range(1, CONF[minutes] + 1))
    print("=" * 104)
    print(f"周期 {LABEL[minutes]}   样本 {len(R)} 根   {R.index.min()} ~ {R.index.max()}   lag=1..{lags[-1]}")
    print("=" * 104)
    print(f"{'配对':<9}{'n':>7}{'最佳lag':>8}{'t':>7}{'βσbp':>8}"
          f"{'零分布p95':>10}{'置换p':>8}  判定")
    for a, b in PAIRS:
        sub = R[[a, b]].dropna()
        if len(sub) < 400:
            print(f"{a}->{b:<6} 样本不足 {len(sub)}")
            continue
        ra, rb = sub[a].to_numpy(), sub[b].to_numpy()
        res = scan(ra, rb, lags)
        if not res:
            continue
        k_best, t_best, bstd = max(res, key=lambda x: abs(x[1]))
        mx = []
        for _ in range(200):
            perm = ra.copy()
            rng.shuffle(perm)
            rr = scan(perm, rb, lags)
            if rr:
                mx.append(max(abs(x[1]) for x in rr))
        mx = np.array(mx)
        p95 = np.quantile(mx, 0.95)
        pv = float((mx >= abs(t_best)).mean())
        ok = "★存活" if (abs(t_best) > max(3.5, p95) and pv < 0.05) else "死"
        print(f"{a}->{b:<6}{len(sub):>7}{k_best:>8}{t_best:>7.2f}{bstd:>8.2f}"
              f"{p95:>10.2f}{pv:>8.3f}  {ok}")
        summary.append({"freq": LABEL[minutes], "pair": f"{a}->{b}", "n": len(sub),
                        "k": k_best, "t": round(t_best, 2), "beta_std_bp": round(bstd, 2),
                        "p95": round(p95, 2), "p": round(pv, 3), "alive": ok})
    print()

print("=" * 104)
print("汇总：每个周期里 |t| 最大的三个配对")
print("=" * 104)
s = pd.DataFrame(summary)
if len(s):
    for f in ["15min", "30min", "60min", "日频"]:
        x = s[s["freq"] == f].copy()
        if len(x) == 0:
            continue
        x["abst"] = x["t"].abs()
        print(f"\n[{f}]")
        print(x.nlargest(3, "abst")[["pair", "k", "t", "beta_std_bp", "p95", "p"]].to_string(index=False))
