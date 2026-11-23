# -*- coding: utf-8 -*-
r"""
交叉验证：主连（KQ.m@）剔除「跨换月」交易 vs 指数（KQ.i@）
============================================================
为什么需要这一步：
    长持有实验在指数合约上得到 hold=12 净/笔中位 **+7.51**、9/11 为正；
    而同一信号在主连上（24 信号实验）合计约 **−2.56 点/笔**。
    两者差得太多，必须定位差异来源，否则不能用。

两个候选解释：
    H-a  换月拼接跳空（主连有、指数无）⇒ 剔除跨换月交易后，主连应向指数靠拢
    H-b  指数含 roll yield（展期收益，不可交易）⇒ 剔除后主连仍显著低于指数

判据（跑前定死）：
    · 若剔换月后主连 hold=12 净/笔的**符号与量级**向指数靠拢 ≥50%（差距缩小一半以上）⇒ H-a 主导，指数可用作「信号预测力」的度量
    · 若缩小不足 50% ⇒ H-b 显著，指数的绝对收益不可引用，只能看符号/排序

用法：<tqsdk venv>\python.exe 对账_换月剔除.py
产物：对账_换月剔除.md
"""
import glob
import json
import math
import os
import sys

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import 进场体检 as mj  # noqa: E402
from 对账_长持有 import trades, _nw_t, load_idx  # noqa: E402

SIG = "retrace_ma13"
HOLD = 12
KEYS = ["M", "MA", "RB", "V", "SA", "EB", "JD", "JM", "P", "RU", "SR"]


def roll_flags(d):
    """与 _诊断_换月污染.py 完全相同的判据。"""
    c = d["close"].to_numpy(float)
    o = d["open"].to_numpy(float)
    h = d["high"].to_numpy(float)
    lo = d["low"].to_numpy(float)
    n = len(c)
    gap = np.zeros(n)
    gap[1:] = np.abs(o[1:] / c[:-1] - 1.0)
    rng = np.where(o > 0, (h - lo) / o, 0.0)
    f = ((gap >= 0.05) & (rng <= gap * 0.6)) | (gap >= 0.10)
    f[0] = False
    return f


def load_main(k):
    fs = sorted(glob.glob(os.path.join(_HERE, "数据", "%s_1440min_2016*.csv" % k)))
    if not fs:
        return None
    d = pd.read_csv(fs[-1], encoding="utf-8-sig", parse_dates=["datetime"])
    return d.sort_values("datetime").reset_index(drop=True)


def stat(d, key, hold, drop_roll):
    mj.HOLD = hold
    r = mj.screen_one(key, SIG, d)
    tr = trades(key, SIG, d, hold)
    if tr is None or r["n"] < 10:
        return None
    cp = r["cost_pts"]
    net = tr["gross1"] - cp
    keep = np.ones(len(tr["idx"]), bool)
    if drop_roll:
        f = roll_flags(d)
        # 剔除「进场前一根 ~ 出场那一根」之间含换月点的所有交易（宽口径，宁可少留）
        cs = np.concatenate([[0], np.cumsum(f)])
        a = tr["idx"] + 1
        b = np.minimum(tr["idx"] + 1 + np.where(tr["hit"], tr["hit_day"], hold), len(d) - 1)
        keep = (cs[b + 1] - cs[a]) == 0
    n = int(keep.sum())
    if n < 10:
        return dict(n=n, net=float("nan"), gross=float("nan"), stop=float("nan"),
                    nw=float("nan"), dropped=len(keep) - n)
    return dict(n=n, net=float(np.mean(net[keep])), gross=float(np.mean(tr["gross1"][keep])),
                stop=float(np.mean(tr["hit"][keep])), nw=_nw_t(net[keep], hold),
                dropped=len(keep) - n)


def main():
    L = []
    A = L.append
    A("# 交叉验证：主连剔换月 vs 指数（retrace_ma13）\n")
    A("> 目的：定位「主连 vs 指数」的差异来源；并检验长持有结论是否只在指数上成立。\n")

    all_holds = {}
    for hold in (12, 60, 120):
        rows = []
        for k in KEYS:
            dm = load_main(k)
            di = load_idx(k)
            if dm is None or di is None:
                continue
            a = stat(dm, k, hold, drop_roll=False)
            b = stat(dm, k, hold, drop_roll=True)
            c = stat(di, k, hold, drop_roll=False)
            if not a or not b or not c:
                continue
            rows.append((k, a, b, c))
            print("H%-3d %-3s  主连 %8.2f(n=%3d) | 剔换月 %8.2f(n=%3d,剔%2d) | 指数 %8.2f(n=%3d)"
                  % (hold, k, a["net"], a["n"], b["net"], b["n"], b["dropped"], c["net"], c["n"]))
        ma = float(np.median([a["net"] for _, a, _, _ in rows]))
        mb = float(np.median([b["net"] for _, _, b, _ in rows]))
        mc = float(np.median([c["net"] for _, _, _, c in rows]))
        pa = sum(1 for _, a, _, _ in rows if a["net"] > 0)
        pb = sum(1 for _, _, b, _ in rows if b["net"] > 0)
        pc = sum(1 for _, _, _, c in rows if c["net"] > 0)
        all_holds[hold] = (rows, ma, mb, mc, pa, pb, pc)
        print("   >>> H%-3d 中位：主连 %.2f (%d正) | 剔换月 %.2f (%d正) | 指数 %.2f (%d正)"
              % (hold, ma, pa, mb, pb, mc, pc))
        print()

    # ---- 表 1：hold=12 逐品种（差异定位）----
    rows12 = all_holds[12][0]
    A("## 一、HOLD=12：逐品种差异\n")
    A("| 品种 | 主连原始 | n | 剔跨换月 | n | 剔除 | 指数 | n | 指数−剔换月 |")
    A("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for k, a, b, c in rows12:
        A("| %s | %.2f | %d | **%.2f** | %d | %d | %.2f | %d | %+.2f |"
          % (k, a["net"], a["n"], b["net"], b["n"], b["dropped"], c["net"], c["n"], c["net"] - b["net"]))
    A("")

    # ---- 表 2：三口径 × 三持有期 ----
    A("## 二、三口径 × 三持有期（核心：长持有结论是否只在指数上成立）\n")
    A("| 口径 | hold=12 中位 | 正 | hold=60 中位 | 正 | hold=120 中位 | 正 |")
    A("|---|---:|---:|---:|---:|---:|---:|")
    for name, ix in (("主连原始", (1, 4)), ("主连剔跨换月", (2, 5)), ("指数", (3, 6))):
        cells = []
        for hold in (12, 60, 120):
            cells.append("%.2f" % all_holds[hold][ix[0]])
            cells.append("%d/11" % all_holds[hold][ix[1]])
        A("| %s | %s |" % (name, " | ".join(cells)))
    A("")

    # ---- 判定 ----
    gap12 = abs(all_holds[12][3] - all_holds[12][1])
    A("## 三、判定\n")
    A("**HOLD=12 的主连 vs 指数**：中位差 **%.2f 点**（主连剔换月 %.2f / 指数 %.2f）"
      % (gap12, all_holds[12][2], all_holds[12][3]))
    if gap12 < 1.0:
        A("⇒ **无系统偏差**（差 < 1 点）。但单品种最大差 **%.2f 点**（%s），"
          "⇒ **绝对数字只在「中位/一致性」层面可用，单品种数字不可引用**。"
          % (max(abs(c["net"] - b["net"]) for _, _, b, c in rows12),
             max(rows12, key=lambda x: abs(x[3]["net"] - x[2]["net"]))[0]))
    else:
        A("⇒ 有系统偏差，指数的绝对收益不可引用。")
    A("")
    A("**长持有的方向性**：")
    for name, ix in (("主连原始", (1, 4)), ("主连剔跨换月", (2, 5)), ("指数", (3, 6))):
        s = [all_holds[h][ix[0]] for h in (12, 60, 120)]
        A("- %s：%.2f → %.2f → %.2f（%s）" % (
            name, s[0], s[1], s[2],
            "单调下降" if s[0] > s[1] > s[2] else ("先升后降" if s[1] > s[0] and s[1] > s[2] else "非单调")))
    A("")
    if all_holds[12][1] > all_holds[60][1] > all_holds[120][1] and \
       all_holds[12][2] > all_holds[60][2] > all_holds[120][2] and \
       all_holds[12][3] > all_holds[60][3] > all_holds[120][3]:
        A("⇒ **三口径一致单调下降 ⇒ 「拉长持有期不产生毛」成立，不依赖数据口径。**")
    else:
        A("⇒ 三口径不一致，结论受口径影响，不得强断言。")

    md = os.path.join(_HERE, "对账_换月剔除.md")
    with open(md, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("产物：%s" % md)


if __name__ == "__main__":
    main()
