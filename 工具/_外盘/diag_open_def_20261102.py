# -*- coding: utf-8 -*-
"""
验证：新浪内盘日线的 open 到底是「夜盘21:00开盘」还是「日盘09:00开盘」
方法：用 _进场体检 的 5min 数据自己聚合，与新浪日线同日期对比
"""
import os, sys
import numpy as np, pandas as pd

INNER_DB = r"C:/Users/cc/Desktop/天勤量化/工具/_进场体检/数据"
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_cache")

for sym in ["RB", "M", "P"]:
    fp5 = os.path.join(INNER_DB, f"{sym}_5min_2021-01-01_2023-12-31.csv")
    if not os.path.exists(fp5):
        print(f"{sym}: 无 5min 数据"); continue
    d = pd.read_csv(fp5, usecols=["datetime", "open", "high", "low", "close"])
    d["dt"] = pd.to_datetime(d["datetime"]) + pd.Timedelta(hours=8)   # UTC -> 北京
    d = d.sort_values("dt").reset_index(drop=True)
    # 交易日归属：>=18:00 的 bar 归到「下一个日历日」对应的交易日（近似）
    d["cal"] = d["dt"].dt.normalize()
    d["hh"] = d["dt"].dt.hour
    d["trade_day"] = np.where(d["hh"] >= 18, d["cal"] + pd.Timedelta(days=1), d["cal"])

    # 每个交易日：夜盘首根 open / 日盘首根 open / 收盘
    g = d.groupby("trade_day")
    night = d[d["hh"] >= 18].groupby("trade_day").agg(open_night=("open", "first"))
    day = d[(d["hh"] >= 9) & (d["hh"] < 15)].groupby("trade_day").agg(
        open_day=("open", "first"), close_day=("close", "last"))
    agg = night.join(day, how="left").dropna()

    # 新浪日线
    pf = os.path.join(CACHE, f"in_{sym}0.csv")
    if not os.path.exists(pf):
        print(f"{sym}: 无内盘日线缓存"); continue
    dl = pd.read_csv(pf, parse_dates=["date"]).set_index("date")

    j = agg.join(dl[["open", "close"]], how="inner")
    j = j[(j.index >= "2021-01-04") & (j.index <= "2023-12-29")]
    if len(j) < 50:
        print(f"{sym}: 交集样本不足 {len(j)}"); continue

    # 差异（相对日盘开盘价）
    d_night = ((j["open_night"] - j["open"]) / j["open"]).abs().median()
    d_day = ((j["open_day"] - j["open"]) / j["open"]).abs().median()
    d_close = ((j["close_day"] - j["close"]) / j["close"]).abs().median()
    eq_n = (np.isclose(j["open_night"], j["open"], rtol=1e-4)).mean()
    eq_d = (np.isclose(j["open_day"], j["open"], rtol=1e-4)).mean()

    print(f"\n【{sym}】交集 {len(j)} 个交易日（2021-2023）")
    print(f"  新浪open vs 夜盘首根open   中位相对差 {d_night*100:6.3f}%   完全相等占比 {eq_n*100:5.1f}%")
    print(f"  新浪open vs 日盘09:00 open 中位相对差 {d_day*100:6.3f}%   完全相等占比 {eq_d*100:5.1f}%")
    print(f"  新浪close vs 日盘15:00收   中位相对差 {d_close*100:6.3f}%")
    verdict = "夜盘开盘" if d_night < d_day else "日盘开盘"
    print(f"  ⇒ 判定：新浪日线 open ≈ {verdict}")
    # 抽 3 天肉眼核对
    print("  抽样：")
    for dt in j.index[:3]:
        r = j.loc[dt]
        print(f"    {dt.date()}  新浪open={r['open']:.1f}  夜盘首={r['open_night']:.1f}  "
              f"日盘09:00={r['open_day']:.1f}  新浪close={r['close']:.1f}  日盘收={r['close_day']:.1f}")
