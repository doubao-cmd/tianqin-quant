# -*- coding: utf-8 -*-
"""
路线1：产业链 lead-lag 先验检验（不回测）

预注册口径（跑之前写死）：
  收益   r[t] = log(close[t]/close[t-1])，仅 5min 连续 bar；跨时段置 NaN
  异常   |r| > 2.5% 剔除（换月/极端）
  回归   r_B[t] = a + b*r_A[t-k] + c*r_B[t-1] + e，NW(HAC, L=12)
         c 项必须有，否则 B 的自相关会被误读成 A 的领先性
  交易   pos = sign(r_A[t-k])，持有 1 根；只在换手时计成本
  成本   单边 = (TICK*MULT + comm) / (price*MULT)
  窗口   探索 2021-01-01~2022-12-31 | 确认 2023-01-01~2023-12-29
  判据   P1 探索段最佳 lag 的 NW |t| > 3.5（Bonferroni 110 检验）
         P2 2023 同 lag 同号且 |t| > 2
         P3 sign 扣成本后每笔期望 > 0 且换手 >= 200
         P4 正向 |t| > 反向 |t| + 1
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

PAIRS = [
    ("JM", "RB", "焦煤->螺纹(炉料->成材)", 1),
    ("M",  "JD", "豆粕->鸡蛋(饲料成本)",   1),
    ("M",  "LH", "豆粕->生猪(饲料成本)",   1),
    ("M",  "P",  "豆粕-棕榈(油脂油料)",    1),
    ("SA", "V",  "纯碱->PVC(氯碱)",        1),
    ("RB", "RU", "螺纹->橡胶(无产业链对照)", 0),
]
WINDOWS = {
    "探索21-22": ("2021-01-01", "2022-12-31"),
    "确认2023":  ("2023-01-01", "2023-12-29"),
}
LAGS = list(range(1, 13))
NW_LAG = 12

# 价格（中位）与成本
PRICE, COST1 = {}, {}
for sym in R.columns:
    p = pd.read_csv(os.path.join(DATA, f"{sym}_5min_2021-01-01_2023-12-31.csv"),
                    usecols=["close"])["close"].median()
    PRICE[sym] = p
    _, _, mult, tick = cs.SYMBOLS[sym]
    COST1[sym] = (tick * mult + cs.comm_per_hand(sym, p)) / (p * mult) * 1e4  # bp 单边


def ols_nw(y, X, L=NW_LAG):
    n, k = X.shape
    XtX = X.T @ X
    beta = np.linalg.solve(XtX, X.T @ y)
    e = y - X @ beta
    Xe = X * e[:, None]
    S = Xe.T @ Xe
    for l in range(1, L + 1):
        w = 1.0 - l / (L + 1.0)
        G = Xe[l:].T @ Xe[:-l]
        S += w * (G + G.T)
    cov = np.linalg.inv(XtX) @ S @ np.linalg.inv(XtX)
    se = np.sqrt(np.diag(cov))
    return beta, beta / se


def analyze(df, a, b, lag):
    """回归 + sign 策略。"""
    x = df[a].shift(lag)
    y = df[b]
    y1 = df[b].shift(1)
    m = x.notna() & y.notna() & y1.notna()
    if m.sum() < 500:
        return None
    xv = x[m].to_numpy()
    yv = y[m].to_numpy()
    X = np.column_stack([np.ones(len(xv)), xv, y1[m].to_numpy()])
    beta, tv = ols_nw(yv, X)

    pos = np.sign(xv)
    pnl = np.zeros(len(yv))
    pnl[1:] = pos[:-1] * yv[1:]
    turn = np.abs(np.diff(np.concatenate([[0.0], pos])))
    cost = turn * COST1[b] / 1e4
    net = pnl - cost
    ntr = int((turn > 0).sum())
    return {
        "n": int(m.sum()), "beta": beta[1], "t": tv[1], "t_ctrl": tv[2],
        "beta_std_bp": beta[1] * xv.std() * 1e4,
        "gross_bp": pnl.sum() * 1e4, "cost_bp": cost.sum() * 1e4,
        "net_bp": net.sum() * 1e4, "trades": ntr,
        "per_trade_bp": net.sum() / ntr * 1e4 if ntr else np.nan,
        "winrate": float((net[turn > 0] > 0).mean() * 100) if ntr else np.nan,
    }


def run(a, b, win):
    s, e = WINDOWS[win]
    sub = R[[a, b]].loc[s:e].dropna()
    out = {}
    for k in LAGS:
        r = analyze(sub, a, b, k)
        if r:
            out[k] = r
    return out, (pd.Timestamp(e) - pd.Timestamp(s)).days / 365.25


print("=" * 108)
print("PART 1  探索段 2021-2022：每配对全部 lag（t 值 / 扣成本每笔 bp / 换手次数）")
print("=" * 108)
best = {}
for a, b, label, prior in PAIRS:
    res, yrs = run(a, b, "探索21-22")
    if not res:
        print(f"{a}->{b} 样本不足")
        continue
    k_best = max(res, key=lambda k: abs(res[k]["t"]))
    best[(a, b)] = k_best
    print(f"\n【{a}->{b}】{label}   成本 {COST1[b]:.2f}bp/边  换手计费")
    for k in LAGS:
        r = res[k]
        flag = " <== |t|最大" if k == k_best else ""
        print(f"   lag={k:>2}  t={r['t']:>6.2f}  βσ={r['beta_std_bp']:>6.2f}bp  "
              f"毛利={r['gross_bp']:>8.0f}bp  成本={r['cost_bp']:>7.0f}bp  "
              f"净={r['net_bp']:>8.0f}bp  每笔={r['per_trade_bp']:>7.2f}bp  "
              f"换手={r['trades']:>5}{flag}")

print()
print("=" * 108)
print("PART 2  确认段 2023：只跑 PART1 选中的 lag（严格样本外）")
print("=" * 108)
print(f"{'配对':<10}{'lag':>4}{'n':>8}{'t':>8}{'βσbp':>8}{'净bp':>9}{'每笔bp':>9}{'换手':>7}{'胜率':>7}  判定")
confirm = {}
for a, b, label, prior in PAIRS:
    k = best.get((a, b))
    if k is None:
        continue
    res, yrs = run(a, b, "确认2023")
    if k not in res:
        print(f"{a}->{b:<4}  lag={k} 无有效样本")
        continue
    r = res[k]
    confirm[(a, b)] = r
    ok2 = (np.sign(r["t"]) == np.sign(res[k]["t"])) and abs(r["t"]) > 2
    print(f"{a}->{b:<10}{k:>4}{r['n']:>8}{r['t']:>8.2f}{r['beta_std_bp']:>8.2f}"
          f"{r['net_bp']:>9.0f}{r['per_trade_bp']:>9.2f}{r['trades']:>7}{r['winrate']:>7.1f}  "
          f"{'P2过' if ok2 else 'P2不过'}")

print()
print("=" * 108)
print("PART 3  方向性对照：同 lag 反向回归（B->A）的 t 值")
print("=" * 108)
print(f"{'配对':<10}{'lag':>4}{'正t':>8}{'反t':>8}{'正-反':>8}  P4")
for a, b, label, prior in PAIRS:
    k = best.get((a, b))
    if k is None:
        continue
    s, e = WINDOWS["探索21-22"]
    sub = R[[a, b]].loc[s:e].dropna()
    fwd = analyze(sub, a, b, k)
    rev = analyze(sub, b, a, k)
    if fwd and rev:
        d = abs(fwd["t"]) - abs(rev["t"])
        print(f"{a}->{b:<10}{k:>4}{fwd['t']:>8.2f}{rev['t']:>8.2f}{d:>8.2f}  "
              f"{'过' if d > 1 else '不过'}")

print()
print("=" * 108)
print("PART 4  判据汇总（四条全过才存活）")
print("=" * 108)
for a, b, label, prior in PAIRS:
    k = best.get((a, b))
    if k is None:
        continue
    res, _ = run(a, b, "探索21-22")
    r0 = res[k]
    c = confirm.get((a, b))
    P1 = abs(r0["t"]) > 3.5
    P2 = bool(c and np.sign(c["t"]) == np.sign(r0["t"]) and abs(c["t"]) > 2)
    P3 = bool(c and c["per_trade_bp"] > 0 and c["trades"] >= 200)
    s, e = WINDOWS["探索21-22"]
    sub = R[[a, b]].loc[s:e].dropna()
    fwd = analyze(sub, a, b, k); rev = analyze(sub, b, a, k)
    P4 = bool(fwd and rev and abs(fwd["t"]) - abs(rev["t"]) > 1)
    print(f"{a}->{b:<6} lag={k:<3} P1(t>3.5)={str(P1):<5} P2(确认同号)={str(P2):<5} "
          f"P3(净每笔>0)={str(P3):<5} P4(正向>反向)={str(P4):<5}  => "
          f"{'★存活' if all([P1,P2,P3,P4]) else '判死'}")

print()
print("=" * 108)
print("成本 / 波动 对照（衡量需要多大的效应量才有意义）")
print("=" * 108)
for sym in sorted({p[1] for p in PAIRS}):
    sig = R[sym].std() * 1e4
    print(f"  {sym:3s} 5minσ={sig:>5.1f}bp  往返成本={2*COST1[sym]:>6.2f}bp  "
          f"成本/σ={2*COST1[sym]/sig:>5.2f}  盈亏平衡需每笔捕获 >{2*COST1[sym]:.1f}bp")
