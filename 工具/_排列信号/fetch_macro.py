# -*- coding: utf-8 -*-
"""
拉取外盘期货 + 美元汇率，落盘到 _macro/。
数据源：新浪外盘（akshare.futures_foreign_hist）+ 中行/央行美元牌价（akshare.currency_boc_sina）

用法：
    python fetch_macro.py            # 全量刷新
    python fetch_macro.py --check    # 只看本地文件状态，不联网拉数

口径说明（重要）：
    - 外盘的 date 是**美盘交易日**。美盘收盘 ≈ 北京时间次日 02:20（农产品）/ 05:00（金属电子盘）。
      因此国内交易日 T 收盘时，能确定看到的最后一根美盘 = date < T。下游一律用 merge_asof(direction='backward')。
    - 汇率取「中行折算价」，也用 date < T 对齐，不用当日（当日中间价 09:15 才公布，晚于 09:00 开盘）。
"""
import os
import sys
import time
import warnings

import pandas as pd

warnings.filterwarnings("ignore")

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "_macro")

# 代码 -> (说明, 单位)
FOREIGN = {
    "CL": ("WTI 原油", "USD/bbl"),
    "OIL": ("布伦特原油", "USD/bbl"),
    "S": ("CBOT 大豆", "美分/蒲式耳"),
    "SM": ("CBOT 豆粕", "美元/短吨"),
    "BO": ("CBOT 豆油", "美分/磅"),
    "C": ("CBOT 玉米", "美分/蒲式耳"),
    "W": ("CBOT 小麦", "美分/蒲式耳"),
    "CAD": ("LME 铜", "USD/吨"),
    "HG": ("COMEX 铜", "美分/磅"),
    "FCPO": ("马棕油 BMD", "马币/吨"),
    "GC": ("COMEX 黄金", "USD/盎司"),
    "SI": ("COMEX 白银", "USD/盎司"),
    "CT": ("ICE 棉花", "美分/磅"),
}


def fetch_foreign():
    import akshare as ak

    os.makedirs(OUT, exist_ok=True)
    for sym, (name, unit) in FOREIGN.items():
        try:
            d = ak.futures_foreign_hist(symbol=sym)
        except Exception as e:
            print(f"  {sym:4s} FAIL {type(e).__name__}: {str(e)[:50]}")
            continue
        d = d[["date", "open", "high", "low", "close"]].copy()
        d.columns = ["dt", "open", "high", "low", "close"]
        d["dt"] = pd.to_datetime(d["dt"]).dt.strftime("%Y-%m-%d")
        d = d[d["close"] > 0].drop_duplicates("dt").sort_values("dt")
        p = os.path.join(OUT, f"{sym}.csv")
        d.to_csv(p, index=False, encoding="utf-8")
        print(f"  {sym:4s} {name:12s} {len(d):5d} 行  {d.dt.iloc[0]} ~ {d.dt.iloc[-1]}  last={d.close.iloc[-1]}")
        time.sleep(0.3)


def fetch_fx():
    import akshare as ak

    os.makedirs(OUT, exist_ok=True)
    d = ak.currency_boc_sina(symbol="美元", start_date="20150101", end_date="20261231")
    d = d.rename(columns={"日期": "dt", "中行折算价": "usdcny"})[["dt", "usdcny"]]
    d["dt"] = pd.to_datetime(d["dt"]).dt.strftime("%Y-%m-%d")
    # 中行折算价单位是「人民币/100 美元」，如 671.31 => 6.7131
    d["usdcny"] = pd.to_numeric(d["usdcny"], errors="coerce") / 100.0
    d = d.dropna().drop_duplicates("dt").sort_values("dt")
    p = os.path.join(OUT, "USDCNY.csv")
    d.to_csv(p, index=False, encoding="utf-8")
    print(f"  USDCNY      {len(d):5d} 行  {d.dt.iloc[0]} ~ {d.dt.iloc[-1]}  last={d.usdcny.iloc[-1]:.4f}")


def check():
    if not os.path.isdir(OUT):
        print("_macro/ 不存在，先跑一次全量")
        return
    print(f"{'代码':6s}{'行数':>7s}  {'起':10s}  {'止':10s}  最新值")
    for f in sorted(os.listdir(OUT)):
        if not f.endswith(".csv"):
            continue
        d = pd.read_csv(os.path.join(OUT, f))
        col = "usdcny" if "usdcny" in d else "close"
        print(f"{f[:-4]:6s}{len(d):7d}  {d.dt.iloc[0]}  {d.dt.iloc[-1]}  {d[col].iloc[-1]}")


def load(sym, col="close"):
    """读单个外盘序列，返回 DataFrame(index=DatetimeIndex, value)"""
    p = os.path.join(OUT, f"{sym}.csv")
    d = pd.read_csv(p, parse_dates=["dt"])
    return d.set_index("dt")[col].sort_index()


if __name__ == "__main__":
    if "--check" in sys.argv:
        check()
        sys.exit(0)
    print("拉取外盘期货：")
    fetch_foreign()
    print("拉取汇率：")
    fetch_fx()
    print()
    check()
