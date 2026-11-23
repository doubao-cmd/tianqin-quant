# -*- coding: utf-8 -*-
"""预注册判据 1 的分段检验 + 铜进口盈亏第二腿。"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from crush import build, load_domestic, fwd, tstat  # noqa: E402
import fetch_macro as F  # noqa: E402


def seg(df, sig, rcol, h, tag):
    print(f"--- {tag}  持有{h}根 ---")
    for name, lo, hi in (("2016-2020", "2016-01-01", "2020-12-31"),
                         ("2021-2026", "2021-01-01", "2026-12-31"),
                         ("全样本", "2016-01-01", "2026-12-31")):
        s = df.loc[(df.index >= lo) & (df.index <= hi)].dropna(subset=[sig, rcol])
        if len(s) < 200:
            print(f"  {name} 样本不足 {len(s)}")
            continue
        q = pd.qcut(s[sig], 5, labels=["Q1低", "Q2", "Q3", "Q4", "Q5高"])
        g = s.assign(q=q)
        v = {k: g[g.q == k][rcol].to_numpy() for k in ["Q1低", "Q5高"]}
        diff = v["Q5高"].mean() - v["Q1低"].mean()
        n = len(v["Q5高"]) + len(v["Q1低"])
        se = np.sqrt(v["Q5高"].var(ddof=1) / len(v["Q5高"]) + v["Q1低"].var(ddof=1) / len(v["Q1低"]))
        print(f"  {name}  Q1 {v['Q1低'].mean():+7.3f}  Q5 {v['Q5高'].mean():+7.3f}  "
              f"差 {diff:+7.3f}  t={diff/se:5.2f}  n={n}")
    print()


def copper():
    cu = load_domestic("CU")["close"].rename("cu")
    cu.index = pd.to_datetime(cu.index).normalize()
    lme = F.load("CAD").rename("lme")
    fx = F.load("USDCNY", "usdcny").rename("fx")
    ext = pd.concat([lme, fx], axis=1).sort_index()
    ext.index = pd.to_datetime(ext.index).normalize()
    left = pd.DataFrame({"dt": cu.index})
    mg = pd.merge_asof(left, ext.reset_index().rename(columns={"index": "dt"}),
                       on="dt", direction="backward", allow_exact_matches=False)
    mg.index = cu.index
    d = pd.DataFrame({"cu": cu, "lme": mg["lme"].values, "fx": mg["fx"].values}).dropna()
    # 进口成本（含 13% 增值税），升水常数 60 USD/t
    d["imp_cost"] = (d.lme + 60) * d.fx * 1.13
    d["imp_pl"] = d.cu - d.imp_cost            # 进口盈亏 元/吨
    d["pl_pct"] = d.imp_pl / d.imp_cost        # 相对比例
    for c in ("pl_pct",):
        d["z_" + c] = (d[c] - d[c].rolling(250, min_periods=200).mean()) / d[c].rolling(250, min_periods=200).std()
    d["d_pct"] = d["pl_pct"].diff(20)
    d["z_d_pct"] = (d["d_pct"] - d["d_pct"].rolling(250, min_periods=200).mean()) / d["d_pct"].rolling(250, min_periods=200).std()
    print(f"=== 铜进口盈亏 样本 {len(d)}  {d.index[0].date()} ~ {d.index[-1].date()}")
    print(f"  进口盈亏 均值 {d.imp_pl.mean():.0f} 元/吨  中位 {d.imp_pl.median():.0f}  "
          f"p5 {d.imp_pl.quantile(.05):.0f}  p95 {d.imp_pl.quantile(.95):.0f}")
    print(f"  沪伦比 均值 {(d.cu/(d.lme*d.fx)).mean():.3f}")
    return d


def main():
    df = build()
    for h in (20,):
        for sym in ("M", "Y"):
            r, atr = fwd(df, sym, h)
            df[f"r{sym}"] = r / atr
            # 变化口径
            df["dP"] = df["profit"].diff(20)
            df["z_dP"] = (df.dP - df.dP.rolling(250, min_periods=200).mean()) / df.dP.rolling(250, min_periods=200).std()
        df["dExt"] = df["cost"].diff(20)
        df["z_dExt"] = (df.dExt - df.dExt.rolling(250, min_periods=200).mean()) / df.dExt.rolling(250, min_periods=200).std()
    seg(df, "z", "rM", 20, "M / 压榨利润 level z（预注册）")
    seg(df, "z", "rY", 20, "Y / 压榨利润 level z（预注册）")
    seg(df, "z_dP", "rM", 20, "M / 压榨利润 20日变动 z")
    seg(df, "z_dP", "rY", 20, "Y / 压榨利润 20日变动 z")
    seg(df, "z_dExt", "rY", 20, "Y / 进口成本 20日变动 z")

    d = copper()
    r, atr = fwd(d, "CU", 20)
    d["r"] = r / atr
    seg(d, "z_pl_pct", "r", 20, "CU / 进口盈亏 level z")
    seg(d, "z_d_pct", "r", 20, "CU / 进口盈亏 20日变动 z")


if __name__ == "__main__":
    main()
