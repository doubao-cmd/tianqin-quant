# -*- coding: utf-8 -*-
"""
横扫汇总 —— 从「进场体检 --all」的结果里挖掘跨信号的**结构性规律**
==============================================================================
读 `体检结果_ALL.json`（17 信号 × 12 品种），回答四个问题：

  ① **方差分解**：毛/成本的差异，主要来自「信号不同」还是「品种不同」？
     —— 这是本脚本最有价值的一问。若品种占主导 ⇒ 信号选择几乎无关紧要，
        真正决定生死的是「品种的成本/波动结构」。
  ② **单变量解释力**：频率 / 止损率 / 成本点数 / L0毛 与 毛/成本 的相关性。
  ③ **止损是帮忙还是帮倒忙**：L1 毛（进场+止损）相对 L0 毛（纯进场）的变化。
  ④ **边际均值**：信号侧、品种侧各自的平均毛/成本（⚠️ 只作描述，不作择优）。

⚠️ 纪律
  · 跨 17 个信号比较 = 17 次比较 ⇒ 本脚本**只做结构性描述**，
    **绝不挑「最好的信号/品种」当发现**（规则 2 禁区）。
  · 窗口 2021-2023 已污染 ⇒ 结论只能用于「判死 / 定性」，不能当 edge 证据。

用法
----
  python 横扫汇总.py                    # 读默认的 体检结果_ALL.json
  python 横扫汇总.py --json xxx.json
"""
import os
import json
import argparse

import numpy as np
import pandas as pd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "体检结果_ALL.json"))
    a = ap.parse_args()

    with open(a.json, encoding="utf-8") as f:
        d = json.load(f)

    # ---- 组装长表
    recs = []
    for sig, blk in d.items():
        for r in blk.get("rows", []):
            if r.get("n", 0) < 30:
                continue
            recs.append({
                "信号": sig, "品种": r["name_cn"], "key": r["key"],
                "笔数": r["n"], "笔日": r.get("per_day", np.nan),
                "成本": r.get("cost_pts", np.nan),
                "L0毛": r.get("L0_gross", np.nan),
                "L1毛": r.get("L1_gross", np.nan),
                "毛成本": r.get("ratio", np.nan),
                "止损率": r.get("stop_share", np.nan),
            })
    df = pd.DataFrame(recs).dropna(subset=["毛成本"])

    L = []
    L.append("=" * 96)
    L.append("横扫汇总  ｜  17 信号 × 12 品种（已污染窗口，只作结构性描述，**禁止择优**）")
    L.append("=" * 96)
    L.append(f"  有效格数 {len(df)}（剔除了 n<30 的格）")

    # ---------- ① 方差分解 ----------
    mat = df.pivot_table(index="信号", columns="品种", values="毛成本", aggfunc="mean")
    mat = mat.dropna()
    vals = mat.to_numpy(float)
    grand = vals.mean()
    ss_tot = ((vals - grand) ** 2).sum()
    ss_sig = vals.shape[1] * ((vals.mean(axis=1) - grand) ** 2).sum()
    ss_sym = vals.shape[0] * ((vals.mean(axis=0) - grand) ** 2).sum()
    ss_res = max(ss_tot - ss_sig - ss_sym, 0.0)
    L.append("")
    L.append("【① 方差分解】毛/成本的差异从哪来？（双因素无重复分解）")
    L.append(f"  矩阵 {vals.shape[0]} 信号 × {vals.shape[1]} 品种   总方差 SS={ss_tot:.3f}")
    for nm, ss in [("**信号**", ss_sig), ("**品种**", ss_sym), ("交互/残差", ss_res)]:
        L.append(f"    {nm:<10} SS={ss:>9.3f}   占比 {100*ss/ss_tot:>6.1f}%")
    dom = "品种" if ss_sym > ss_sig else "信号"
    L.append(f"  ★ 主导项 = **{dom}**（{100*max(ss_sym,ss_sig)/ss_tot:.1f}%）"
             f" ⇒ 毛/成本主要由「{dom}」决定，而不是「{'信号' if dom=='品种' else '品种'}」")

    # ---------- ② 单变量相关 ----------
    L.append("")
    L.append("【② 单变量解释力】与「毛成本」的相关系数（跨全部有效格）")
    for col in ["笔日", "止损率", "成本", "L0毛", "L1毛"]:
        s = df[[col, "毛成本"]].dropna()
        if len(s) > 5 and s[col].std() > 0:
            L.append(f"    {col:<6} corr = {s[col].corr(s['毛成本']):>+7.3f}   (n={len(s)})")
    # 品种层面：平均止损率 vs 平均毛成本
    g = df.groupby("品种")[["毛成本", "止损率", "成本"]].mean()
    if len(g) > 3:
        L.append(f"    品种层面：止损率 vs 毛成本 corr = "
                 f"{g['止损率'].corr(g['毛成本']):>+7.3f}")
        L.append(f"    品种层面：成本(点) vs 毛成本 corr = "
                 f"{g['成本'].corr(g['毛成本']):>+7.3f}")

    # ---------- ③ L1 vs L0 ----------
    L.append("")
    L.append("【③ 止损（L1）相对纯进场（L0）是帮忙还是帮倒忙？")
    d2 = df[["信号", "品种", "L0毛", "L1毛"]].dropna()
    delta = d2["L1毛"] - d2["L0毛"]
    L.append(f"    L1毛 − L0毛：均值 {delta.mean():+.3f} 点，中位 {delta.median():+.3f}，"
             f"为正的格子 {100*(delta>0).mean():.1f}%")
    L.append(f"    ⇒ 止损{'**平均在改善**' if delta.mean()>0 else '**平均在恶化**'}毛利"
             f"（{'截断亏损有效' if delta.mean()>0 else '被止损扫掉的赢单多于避免的亏损'}）")

    # ---------- ④ 边际均值 ----------
    L.append("")
    L.append("【④ 边际均值（只作描述，⛔ 禁止据此择优）】")
    L.append("  ── 按品种（跨 17 个信号平均）──")
    by_sym = df.groupby("品种")[["毛成本", "止损率", "成本"]].mean().sort_values("毛成本")
    for nm, row in by_sym.iterrows():
        L.append(f"    {nm:<6} 平均毛成本 {row['毛成本']:>+6.3f}   平均止损率 "
                 f"{row['止损率']*100:>5.1f}%   成本 {row['成本']:>6.2f}")
    L.append("  ── 按信号（跨 12 品种平均）──")
    by_sig = df.groupby("信号")[["毛成本", "笔日"]].mean().sort_values("毛成本")
    for nm, row in by_sig.iterrows():
        L.append(f"    {nm:<16} 平均毛成本 {row['毛成本']:>+6.3f}   频率 "
                 f"{row['笔日']:>6.2f} 笔/日")

    L.append("")
    L.append("⚠️ 17 次比较 ⇒ 以上排序**不得**用于挑「最优信号/品种」；窗口已污染，只能定性。")
    txt = "\n".join(L)
    print(txt)
    out = os.path.join(os.path.dirname(a.json), "横扫汇总.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(f"# 横扫汇总（17 信号 × 12 品种）\n\n```\n{txt}\n```\n")
    print(f"\n[done] {out}")


if __name__ == "__main__":
    main()
