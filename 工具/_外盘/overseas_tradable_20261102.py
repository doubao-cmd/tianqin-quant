# -*- coding: utf-8 -*-
"""
外盘 → 内盘【可交易段】检验
==================================================
已确诊（diag_open_def）：新浪内盘日线 open = 夜盘21:00开盘，close = 日盘15:00收盘。
所以日线算出的 "intra" = 夜盘+日盘全程，其中夜盘段(21:00→次日02:30)与外盘同步，不可交易。

本脚本用 5min 真实数据切三段：
  night[d] = ln( open_09:00[d] / close_15:00[d-1] )   前日收盘→今晨开盘，含夜盘（外盘欧美时段在此）
  day[d]   = ln( close_15:00[d] / open_09:00[d] )     09:00→15:00，**唯一可交易段**
  自变量   = 外盘 d-1 的日收益（其欧美时段落在 night[d] 内）

判据：day 段的 β 若显著且 β×σ_外盘 > 往返成本 ⇒ 有可交易残留；若 ≈0 ⇒ 信息在开盘前定价完。
"""
import os
import numpy as np, pandas as pd

INNER_DB = r"C:/Users/cc/Desktop/天勤量化/工具/_进场体检/数据"
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_cache")

PAIRS = [("M", "S", "豆粕-CBOT大豆"), ("M", "SM", "豆粕-CBOT豆粕"),
         ("P", "BO", "棕榈-CBOT豆油"), ("P", "FCPO", "棕榈-马棕"),
         ("SR", "SB", "白糖-ICE原糖"), ("MA", "CL", "甲醇-原油"),
         ("EB", "CL", "苯乙烯-原油"), ("V", "CL", "PVC-原油"),
         ("RB", "CAD", "螺纹-LME铜(安慰剂)")]

# 往返成本(bp)：1跳滑点×2边 + 佣金×2边，价格用样本中位
COST_BP = {"M": 7.3, "P": 12.0, "SR": 6.0, "MA": 10.0, "EB": 12.0, "V": 9.0, "RB": 6.5}


def ols(y, x):
    n = len(y)
    X = np.column_stack([np.ones(n), x])
    XtX = X.T @ X
    b = np.linalg.solve(XtX, X.T @ y)
    e = y - X @ b
    s2 = (e @ e) / (n - 2)
    se = np.sqrt(np.diag(np.linalg.inv(XtX) * s2))
    r2 = 1 - (e @ e) / ((y - y.mean()) @ (y - y.mean()))
    return b[1], b[1] / se[1], r2, n


print("=" * 108)
print(f"{'配对':22s} {'样本':>5s} {'外盘σ':>7s} {'β_night':>8s} {'t':>7s} {'β_day':>8s} {'t':>7s} "
      f"{'R²day':>6s} {'day毛利bp':>9s} {'成本bp':>7s} {'净bp':>7s}")
print("=" * 108)

rows = []
for sym, osym, label in PAIRS:
    fp5 = os.path.join(INNER_DB, f"{sym}_5min_2021-01-01_2023-12-31.csv")
    fo = os.path.join(CACHE, f"out_{osym}.csv")
    if not (os.path.exists(fp5) and os.path.exists(fo)):
        print(f"{label:22s} 数据缺失")
        continue

    d = pd.read_csv(fp5, usecols=["datetime", "open", "close"])
    d["dt"] = pd.to_datetime(d["datetime"]) + pd.Timedelta(hours=8)
    d = d.sort_values("dt").reset_index(drop=True)
    d["cal"] = d["dt"].dt.normalize()
    d["hh"] = d["dt"].dt.hour
    d["mm"] = d["dt"].dt.minute
    d["trade_day"] = np.where(d["hh"] >= 18, d["cal"] + pd.Timedelta(days=1), d["cal"])

    day_bars = d[(d["hh"] >= 9) & (d["hh"] < 15)]
    g = day_bars.groupby("trade_day")
    open_day = g["open"].first()
    close_day = g["close"].last()
    # 日盘 bar 数量校验：正常 09:00-14:55 共 54 根（含 10:15/11:30/13:30 边界）
    cnt = g.size()
    open_day = open_day[cnt >= 40]
    close_day = close_day[cnt >= 40]

    df = pd.DataFrame({"open_day": open_day, "close_day": close_day}).sort_index()
    df["prev_close"] = df["close_day"].shift(1)
    df["night"] = np.log(df["open_day"] / df["prev_close"])
    df["day"] = np.log(df["close_day"] / df["open_day"])
    df.loc[df["night"].abs() > 0.08, ["night", "day"]] = np.nan   # 换月/异常

    o = pd.read_csv(fo, parse_dates=["date"]).sort_values("date")
    o["oret"] = np.log(o["close"]).diff()
    o.loc[o["oret"].abs() > 0.30, "oret"] = np.nan
    o["date"] = o["date"] + pd.Timedelta(days=1)      # 外盘 d-1 -> 内盘 d

    m = df.reset_index().rename(columns={"index": "trade_day"}).merge(
        o[["date", "oret"]], left_on="trade_day", right_on="date", how="inner")
    s = m.dropna(subset=["oret", "night", "day"])
    if len(s) < 100:
        print(f"{label:22s} 样本不足 {len(s)}")
        continue

    b_n, t_n, _, _ = ols(s["night"].values, s["oret"].values)
    b_d, t_d, r2d, n = ols(s["day"].values, s["oret"].values)
    sig_o = s["oret"].std()
    # day 段毛利：外盘动 1σ 时，日内策略的平均持仓收益 ≈ β_day × σ_外盘 × E|z| (≈0.8)
    gross_bp = b_d * sig_o * 0.798 * 1e4
    cost = COST_BP.get(sym, 10.0)
    print(f"{label:22s} {n:>5d} {sig_o*100:>6.2f}% {b_n:>8.3f} {t_n:>7.2f} {b_d:>8.3f} {t_d:>7.2f} "
          f"{r2d:>6.3f} {gross_bp:>9.2f} {cost:>7.1f} {gross_bp-cost:>7.2f}")
    rows.append(dict(label=label, sym=sym, n=n, beta_night=b_n, t_night=t_n,
                     beta_day=b_d, t_day=t_d, r2_day=r2d,
                     gross_bp=gross_bp, cost_bp=cost))

pd.DataFrame(rows).to_csv(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "_可交易段检验_20261102.csv"), index=False, encoding="utf-8-sig")
print("=" * 108)
print("β_night ≈ 夜盘段（外盘同步段）定价比例；β_day = 可交易段残留；净bp = 毛利 - 往返成本")
