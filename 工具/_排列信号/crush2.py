# -*- coding: utf-8 -*-
"""
压榨利润腿的分解诊断 —— 排除「自己被自己解释」的混淆。

问题：P_t = 0.785·M_t + 0.185·Y_t − C_t，P 里含有国内价格本身。
      z(P) 高，可能只是因为国内刚涨过（动量反转），与「进口成本信息」无关。

分解成三条互斥的信号：
    S_dom = 国内复合 20 日变动   （纯价格，正是要规避的东西）
    S_ext = 进口成本 20 日变动   （外生：CBOT + 汇率）
    S_all = 压榨利润 20 日变动   （两者合成）

若只有 S_dom 有效 ⇒ 这是动量反转，不是信息源，收档。
若 S_ext 独立有效 ⇒ 才是真正的新信息。

t 值修正：h 根重叠窗口的自相关，有效 t ≈ t / sqrt(h)。
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from crush import build, fwd, tstat  # noqa: E402


def zscore(s, win=250):
    return (s - s.rolling(win, min_periods=200).mean()) / s.rolling(win, min_periods=200).std()


def report(sub, col, lab):
    v = sub[col].to_numpy()
    t = tstat(v)
    print(f"  {lab:22s} n={len(v):5d}  净{v.mean():+8.3f}  t={t:6.2f}  t_eff={t/np.sqrt(H):5.2f}  胜率{(v>0).mean():5.1%}")


def main():
    global H
    df = build()
    df["dom"] = 0.785 * df["meal"] + 0.185 * df["oil"]
    df["dP"] = df["profit"].diff(20)
    df["dDom"] = df["dom"].diff(20)
    df["dExt"] = df["cost"].diff(20)
    for c in ("dP", "dDom", "dExt"):
        df["z_" + c] = zscore(df[c])

    print("三条信号相关性（20 日变动）:")
    print(df[["z_dP", "z_dDom", "z_dExt"]].corr().round(3).to_string())
    print()

    for H in (10, 20):
        for sym in ("M", "Y"):
            r, atr = fwd(df, sym, H)
            df["r"] = r / atr
            sub = df.dropna(subset=["z_dP", "r"])
            print(f"########## {sym}  持有 {H} 根 ##########")
            # 分档看方向
            for sig, name in (("z_dExt", "进口成本变动"), ("z_dDom", "国内价格变动"), ("z_dP", "压榨利润变动")):
                q = pd.qcut(sub[sig], 3, labels=["低", "中", "高"])
                g = sub.assign(q=q)
                s = "  ".join(f"{k}:{g[g.q == k]['r'].mean():+.3f}" for k in ["低", "中", "高"])
                print(f"  {name:8s} 三分档  {s}")
            print()
            # 极值组（前后 20%）
            for sig, name in (("z_dExt", "进口成本变动"), ("z_dDom", "国内价格变动"), ("z_dP", "压榨利润变动")):
                lo = sub[sub[sig] <= sub[sig].quantile(0.2)]
                hi = sub[sub[sig] >= sub[sig].quantile(0.8)]
                report(lo, "r", f"{name} 低20%")
                report(hi, "r", f"{name} 高20%")
                print(f"     差(高−低) = {hi['r'].mean() - lo['r'].mean():+.3f}")
            print()

            # 双变量：控制国内动量后，进口成本还有没有增量
            print("  ★ 控制国内动量后的进口成本增量（按 z_dDom 三分组，组内再按 z_dExt 分组）:")
            sub2 = sub.assign(qd=pd.qcut(sub["z_dDom"], 3, labels=["国内弱", "国内中", "国内强"]))
            for k, g in sub2.groupby("qd", observed=True):
                ge = g.assign(qe=pd.qcut(g["z_dExt"], 2, labels=["外盘低", "外盘高"]))
                a = ge[ge.qe == "外盘低"]["r"]
                b = ge[ge.qe == "外盘高"]["r"]
                print(f"    {k:5s} 外盘低 {a.mean():+7.3f}(n={len(a):3d})  外盘高 {b.mean():+7.3f}(n={len(b):3d})  差 {b.mean()-a.mean():+7.3f}")
            print()


if __name__ == "__main__":
    main()
