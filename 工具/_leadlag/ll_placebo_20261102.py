# -*- coding: utf-8 -*-
"""
路线1 收尾：三件事
  A. 安慰剂：把 A 的收益整体随机置换，重跑 6 配对 x 12 lag，看"最大 |t|"的零分布
     —— 判定观察到的 2.69 是信号还是噪声
  B. 事件触发：只在 |r_A| > 90%/95% 分位时开仓，持有 1 根（真实可交易形态）
  C. 日频：用 5min 合成日收盘收益，看传导是否在日频存在（lag 1-3 天）
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
R = pd.read_parquet(os.path.join(HERE, "_r_wide_5min.parquet"))
R.index = pd.to_datetime(R.index)

PAIRS = [("JM", "RB"), ("M", "JD"), ("M", "LH"), ("M", "P"), ("SA", "V"), ("RB", "RU")]
LAGS = list(range(1, 13))
NW_LAG = 12
rng = np.random.default_rng(20261102)


def ols_t(y, x, y1):
    """r_B = a + b*x + c*r_B(-1)，返回 b 的 NW t 值。"""
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


def prep(a, b):
    """返回对齐后的 (x 原始序列, y, y1) 数组（未 shift）。"""
    sub = R[[a, b]].dropna()
    return sub[a].to_numpy(), sub[b].to_numpy()


def max_abs_t(ra, rb, lags=LAGS):
    """在一组 lag 上取最大 |t|。"""
    n = len(rb)
    y1 = np.concatenate([[np.nan], rb[:-1]])
    out = []
    for k in lags:
        x = np.concatenate([[np.nan] * k, ra[:-k]])
        m = ~(np.isnan(x) | np.isnan(y1) | np.isnan(rb))
        if m.sum() < 500:
            continue
        _, t = ols_t(rb[m], x[m], y1[m])
        out.append((k, t))
    return out


# ---------- A. 安慰剂 ----------
print("=" * 100)
print("A. 安慰剂检验：置换 A 的收益 500 次，看每个配对的 max|t| 零分布")
print("   若观测值落在零分布的 95% 分位以内 => 该配对的最佳 lag 是噪声")
print("=" * 100)
N_PLACEBO = 500
pl_rows = []
for a, b in PAIRS:
    ra, rb = prep(a, b)
    obs = max_abs_t(ra, rb)
    k_obs, t_obs = max(obs, key=lambda kv: abs(kv[1]))
    mx = []
    for _ in range(N_PLACEBO):
        perm = ra.copy()
        rng.shuffle(perm)
        res = max_abs_t(perm, rb)
        mx.append(max(abs(t) for _, t in res))
    mx = np.array(mx)
    p95 = np.quantile(mx, 0.95)
    pval = float((mx >= abs(t_obs)).mean())
    pl_rows.append({
        "pair": f"{a}->{b}", "k": k_obs, "t_obs": round(t_obs, 2),
        "placebo_p95": round(p95, 2), "placebo_max": round(mx.max(), 2),
        "p_value": round(pval, 3),
        "结论": "噪声" if pval > 0.05 else "非噪声",
    })
print(pd.DataFrame(pl_rows).to_string(index=False))
print()

# ---------- B. 事件触发 ----------
print("=" * 100)
print("B. 事件触发：只在 |r_A[t-k]| > 分位阈值时开仓，持有 1 根")
print("=" * 100)
PRICE, COST1 = {}, {}
for sym in R.columns:
    p = pd.read_csv(os.path.join(DATA, f"{sym}_5min_2021-01-01_2023-12-31.csv"),
                    usecols=["close"])["close"].median()
    PRICE[sym] = p
    _, _, mult, tick = cs.SYMBOLS[sym]
    COST1[sym] = (tick * mult + cs.comm_per_hand(sym, p)) / (p * mult) * 1e4

BEST = {("JM", "RB"): 2, ("M", "JD"): 4, ("M", "LH"): 4,
        ("M", "P"): 8, ("SA", "V"): 1, ("RB", "RU"): 7}
print(f"{'配对':<9}{'lag':>4}{'阈值':>7}{'笔数':>7}{'每笔bp':>9}{'t':>7}{'胜率':>7}{'成本/笔':>8}  净每笔>成本?")
for a, b in PAIRS:
    k = BEST[(a, b)]
    ra, rb = prep(a, b)
    x = np.concatenate([[np.nan] * k, ra[:-k]])
    n = len(rb)
    for q in (0.90, 0.95):
        m = ~np.isnan(x)
        thr = np.quantile(np.abs(x[m]), q)
        sel = m & (np.abs(x) >= thr)
        # 当前 bar 开仓 -> 下一根收益
        idx = np.where(sel)[0]
        idx = idx[idx + 1 < n]
        fwd = rb[idx + 1]
        pos = np.sign(x[idx])
        pnl = pos * fwd * 1e4
        net = pnl - COST1[b]
        # 一元 t（每笔独立近似）
        tv = net.mean() / (net.std(ddof=1) / np.sqrt(len(net))) if len(net) > 30 else np.nan
        print(f"{a}->{b:<6}{k:>4}{q*100:>6.0f}%{len(net):>7}{net.mean():>9.2f}"
              f"{tv:>7.2f}{(net>0).mean()*100:>7.1f}{COST1[b]:>8.2f}  "
              f"{'是' if net.mean() > COST1[b] else '否'}")
print()

# ---------- C. 日频 ----------
print("=" * 100)
print("C. 日频：5min 合成日收盘（每日 14:55 bar 的 close），lag 1-3 交易日")
print("=" * 100)
# 每日最后一个 bar（北京时间 14:55 开始）
tmp = R.copy()
tmp["day"] = tmp.index.normalize()
last = tmp.groupby("day").tail(1).set_index("day")
# 只保留完整交易日（当天有 >= 40 根）
cnt = tmp.groupby("day").size()
last = last[cnt >= 40]
dayR = np.log(last / last.shift(1)).dropna(how="all")
print(f"交易日数 {len(dayR)}  区间 {dayR.index.min().date()} ~ {dayR.index.max().date()}")
print()
print(f"{'配对':<9}{'lag':>4}{'n':>6}{'t':>7}{'βσbp':>8}{'每笔bp':>8}{'胜率':>7}")
for a, b in PAIRS:
    sub = dayR[[a, b]].dropna()
    if len(sub) < 200:
        print(f"{a}->{b:<6} 日频样本不足 {len(sub)}")
        continue
    for k in (1, 2, 3):
        x = sub[a].shift(k)
        y = sub[b]
        y1 = sub[b].shift(1)
        m = x.notna() & y.notna() & y1.notna()
        if m.sum() < 200:
            continue
        beta, t = ols_t(y[m].to_numpy(), x[m].to_numpy(), y1[m].to_numpy())
        pos = np.sign(x[m].to_numpy())
        pnl = pos[:-1] * y[m].to_numpy()[1:] * 1e4
        print(f"{a}->{b:<6}{k:>4}{m.sum():>6}{t:>7.2f}{beta*x[m].std()*1e4:>8.2f}"
              f"{pnl.mean():>8.2f}{(pnl>0).mean()*100:>7.1f}")
print()
print("注：日频每笔成本参考 5min 表（同样的 COST1），一次往返约 3.6~8.5bp")
