# -*- coding: utf-8 -*-
"""
精确配对：外盘 open→close（北京 D 21:00 → D+1 02:20） vs 内盘夜盘段（D 21:00 → D+1 09:00）
这两段在时钟上高度重叠，是「同一条信息」的最干净对照。
若 β 接近 1 ⇒ 外盘动多少我们夜盘就同步跟多少（无免费午餐）；β 明显<1 ⇒ 有溢价缓冲。
"""
import os
import numpy as np, pandas as pd

INNER_DB = r"C:/Users/cc/Desktop/天勤量化/工具/_进场体检/数据"
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_cache")
PAIRS = [("M", "S", "豆粕-CBOT大豆"), ("P", "BO", "棕榈-CBOT豆油"),
         ("MA", "CL", "甲醇-原油"), ("EB", "CL", "苯乙烯-原油"),
         ("V", "CL", "PVC-原油"), ("RB", "CAD", "螺纹-LME铜(安慰剂)")]


def ols(y, x):
    n = len(y)
    X = np.column_stack([np.ones(n), x]); XtX = X.T @ X
    b = np.linalg.solve(XtX, X.T @ y); e = y - X @ b
    s2 = (e @ e) / (n - 2)
    se = np.sqrt(np.diag(np.linalg.inv(XtX) * s2))
    r2 = 1 - (e @ e) / ((y - y.mean()) @ (y - y.mean()))
    return b[1], b[1] / se[1], r2, n


print("=" * 100)
print(f"{'配对':22s} {'样本':>5s} {'外盘日内σ':>9s} {'β(夜盘~外盘日内)':>16s} {'t':>7s} {'R²':>6s} {'相关':>7s}")
print("=" * 100)
for sym, osym, label in PAIRS:
    fp5 = os.path.join(INNER_DB, f"{sym}_5min_2021-01-01_2023-12-31.csv")
    fo = os.path.join(CACHE, f"out_{osym}.csv")
    if not (os.path.exists(fp5) and os.path.exists(fo)):
        print(f"{label:22s} 缺失"); continue
    d = pd.read_csv(fp5, usecols=["datetime", "open", "close"])
    d["dt"] = pd.to_datetime(d["datetime"]) + pd.Timedelta(hours=8)
    d = d.sort_values("dt")
    d["hh"] = d["dt"].dt.hour
    d["cal"] = d["dt"].dt.normalize()
    d["trade_day"] = np.where(d["hh"] >= 18, d["cal"] + pd.Timedelta(days=1), d["cal"])
    db = d[(d["hh"] >= 9) & (d["hh"] < 15)].groupby("trade_day")
    op, cl, cnt = db["open"].first(), db["close"].last(), db.size()
    df = pd.DataFrame({"open_day": op[cnt >= 40], "close_day": cl[cnt >= 40]}).sort_index()
    df["prev_close"] = df["close_day"].shift(1)
    df["night"] = np.log(df["open_day"] / df["prev_close"])
    df.loc[df["night"].abs() > 0.08, "night"] = np.nan

    o = pd.read_csv(fo, parse_dates=["date"]).sort_values("date")
    o["intra"] = np.log(o["close"] / o["open"])       # 外盘 open→close = 北京 21:00→次日02:20
    o.loc[o["intra"].abs() > 0.30, "intra"] = np.nan
    o["date"] = o["date"] + pd.Timedelta(days=1)

    m = df.reset_index().rename(columns={"index": "trade_day"}).merge(
        o[["date", "intra"]], left_on="trade_day", right_on="date", how="inner").dropna()
    if len(m) < 100:
        print(f"{label:22s} 样本不足 {len(m)}"); continue
    b, t, r2, n = ols(m["night"].values, m["intra"].values)
    rho = np.corrcoef(m["night"], m["intra"])[0, 1]
    print(f"{label:22s} {n:>5d} {m['intra'].std()*100:>8.2f}% {b:>16.3f} {t:>7.2f} {r2:>6.3f} {rho:>7.3f}")
print("=" * 100)
print("β≈1 ⇒ 同一时段完全同步（无套利空间）；β<1 ⇒ 内盘溢价/汇率吸收了一部分")
