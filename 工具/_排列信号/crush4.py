# -*- coding: utf-8 -*-
"""
决定性对照：进口成本腿到底是「新信息」，还是「国内价格换了个壳」？

每个自变量都配一个同形状的纯价格基准：
    压榨利润 level z   vs   国内复合价格 level z
    进口成本 20日变动 z vs   国内价格 20日变动 z
若两者预测力相同 ⇒ 没有新信息，只是价格的重新包装。
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from crush import build, load_domestic, fwd  # noqa: E402


def zs(s, win=250):
    return (s - s.rolling(win, min_periods=200).mean()) / s.rolling(win, min_periods=200).std()


def q5q1(df, sig, rcol, tag):
    row = []
    for name, lo, hi in (("16-20", "2016-01-01", "2020-12-31"),
                         ("21-26", "2021-01-01", "2026-12-31")):
        s = df.loc[(df.index >= lo) & (df.index <= hi)].dropna(subset=[sig, rcol])
        if len(s) < 200:
            row.append("  不足")
            continue
        q = pd.qcut(s[sig], 5, labels=False)
        g = s.assign(q=q)
        a = g[g.q == 0][rcol].to_numpy()
        b = g[g.q == 4][rcol].to_numpy()
        diff = b.mean() - a.mean()
        se = np.sqrt(b.var(ddof=1) / len(b) + a.var(ddof=1) / len(a))
        row.append(f"{diff:+6.3f}(t={diff/se:+5.2f})")
    print(f"  {tag:28s} {row[0]:>18s}  {row[1]:>18s}")


def main():
    df = build()
    df["dom"] = 0.785 * df["meal"] + 0.185 * df["oil"]
    df["z_level_price"] = zs(df["dom"])
    df["dDom"] = df["dom"].diff(20)
    df["z_dDom"] = zs(df["dDom"])
    df["dCost"] = df["cost"].diff(20)
    df["z_dCost"] = zs(df["dCost"])

    for sym in ("M", "Y"):
        r, atr = fwd(df, sym, 20)
        df["r"] = r / atr
        d = load_domestic(sym)["close"]
        d.index = pd.to_datetime(d.index).normalize()
        df["z_self"] = zs(d.reindex(df.index))
        print(f"===== {sym}  持有 20 根  Q5−Q1（分段） =====")
        q5q1(df, "z", "r", "压榨利润 level z")
        q5q1(df, "z_level_price", "r", "★ 国内复合价 level z（基准）")
        q5q1(df, "z_self", "r", "★ 该品种自身 level z（基准）")
        q5q1(df, "z_dCost", "r", "进口成本 20日变动 z")
        q5q1(df, "z_dDom", "r", "★ 国内价 20日变动 z（基准）")
        print()

    # 铜
    cu = load_domestic("CU")["close"]
    cu.index = pd.to_datetime(cu.index).normalize()
    import fetch_macro as F
    lme = F.load("CAD").rename("lme")
    fx = F.load("USDCNY", "usdcny").rename("fx")
    ext = pd.concat([lme, fx], axis=1)
    ext.index = pd.to_datetime(ext.index).normalize()
    mg = pd.merge_asof(pd.DataFrame({"dt": cu.index}), ext.reset_index().rename(columns={"index": "dt"}),
                       on="dt", direction="backward", allow_exact_matches=False)
    mg.index = cu.index
    dc = pd.DataFrame({"cu": cu, "lme": mg["lme"].values, "fx": mg["fx"].values}).dropna()
    dc["pl"] = (dc.cu - (dc.lme + 60) * dc.fx * 1.13) / ((dc.lme + 60) * dc.fx * 1.13)
    dc["z_pl"] = zs(dc["pl"])
    dc["z_cu"] = zs(dc["cu"])
    dc["dpl"] = dc["pl"].diff(20)
    dc["z_dpl"] = zs(dc["dpl"])
    dc["dcu"] = dc["cu"].diff(20)
    dc["z_dcu"] = zs(dc["dcu"])
    r, atr = fwd(dc, "CU", 20)
    dc["r"] = r / atr
    print("===== CU  持有 20 根  Q5−Q1（分段） =====")
    q5q1(dc, "z_pl", "r", "进口盈亏 level z")
    q5q1(dc, "z_cu", "r", "★ 沪铜自身 level z（基准）")
    q5q1(dc, "z_dpl", "r", "进口盈亏 20日变动 z")
    q5q1(dc, "z_dcu", "r", "★ 沪铜 20日变动 z（基准）")


if __name__ == "__main__":
    main()
