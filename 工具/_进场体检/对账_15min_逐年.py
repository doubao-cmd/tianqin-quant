# -*- coding: utf-8 -*-
"""15min 逐年拆解（诊断用，不得据此择优）

口径与 对账_15min_ISOOS.py 完全一致：复用 进场体检.screen_one()，只切窗口，不改逻辑。
窗口：按自然年 2021 / 2022 / 2023 / 2024 / 2025，每段 [y-01-01, (y+1)-01-01)。
指标与信号仍在全量序列上计算（不因切窗丢预热），只筛成交。

★ 纪律：本表是**诊断**，用于看「同一条格子在时间上稳不稳」。
   逐年挑最好年份 = 事后择优，禁止。
"""
import json
import os
import sys

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import 进场体检 as mj  # noqa: E402

YEARS = [2021, 2022, 2023, 2024, 2025]
SYMS = ["MA", "P", "JM", "EB", "M", "RB", "V", "SA", "JD", "LH", "RU", "SR"]
SIGS = list(mj.SIGNALS.keys())
# 冒烟测试用：EP_YEARLY_SYMS=MA EP_YEARLY_SIGS=boll_rsi
if os.environ.get("EP_YEARLY_SYMS"):
    SYMS = [x.strip() for x in os.environ["EP_YEARLY_SYMS"].split(",") if x.strip()]
if os.environ.get("EP_YEARLY_SIGS"):
    SIGS = [x.strip() for x in os.environ["EP_YEARLY_SIGS"].split(",") if x.strip()]


def _fmt(x, n=2):
    if x is None:
        return "—"
    try:
        v = float(x)
    except Exception:
        return "—"
    if not np.isfinite(v):
        return "—"
    return f"{v:.{n}f}"


def main():
    rows = []
    for k in SYMS:
        p = os.path.join(_HERE, "数据", f"{k}_15min_2021-01-01_2025-12-31.csv")
        if not os.path.exists(p):
            print(f"[skip] {k} 无数据", flush=True)
            continue
        df = pd.read_csv(p, encoding="utf-8-sig", parse_dates=["datetime"])
        print(f"[read] {k} {len(df)}", flush=True)
        for s in SIGS:
            base = mj.screen_one(k, s, df)  # 全样本，用于排序展示
            rec = {"signal": s, "key": k, "name_cn": mj.cs.get(k)[0],
                   "full_ratio": base.get("ratio"), "full_n": base.get("n"),
                   "yearly": {}}
            for y in YEARS:
                r = mj.screen_one(k, s, df, t_lo=f"{y}-01-01", t_hi=f"{y + 1}-01-01")
                rec["yearly"][str(y)] = r
            rows.append(rec)
        print(f"[done] {k}", flush=True)

    # ---------------- 汇总 ----------------
    L = []
    L.append("# 15min 逐年拆解（诊断用，**不得据此择优**）\n")
    L.append("> 口径同 `对账_15min_ISOOS.py`：24 信号 × 12 品种，HOLD=12 根（=3 小时），")
    L.append("> 止损 2×ATR(14)，滑点 1 跳，进场 = 信号 bar 的下一根 open。")
    L.append("> 窗口按自然年切，`[y-01-01, (y+1)-01-01)`；指标仍在全量序列上算，只筛成交。\n")

    L.append("## 一、逐年汇总（288 格 / 年）\n")
    L.append("| 年份 | 有效格 n≥30 | A 真死 | B 薄利 | C 值得进 | ratio 中位 | 正净期望 | t≥2 正 |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    stat = {}
    for y in YEARS:
        ok, g = [], {"A": 0, "B": 0, "C": 0}
        pos_net = 0
        pos_sig = 0
        for rec in rows:
            r = rec["yearly"][str(y)]
            if r["n"] < 30:
                continue
            ok.append(r)
            g[mj.grade(r)] += 1
            if r["L1_net"] > 0:
                pos_net += 1
            if r["L1_t"] >= 2.0 and r["L1_net"] > 0:
                pos_sig += 1
        rr = [x["ratio"] for x in ok if x.get("ratio") is not None]
        stat[y] = {"ok": len(ok), "A": g["A"], "B": g["B"], "C": g["C"],
                   "med": float(np.median(rr)) if rr else None,
                   "pos_net": pos_net, "pos_sig": pos_sig}
        L.append(f"| {y} | {len(ok)} | {g['A']} | {g['B']} | {g['C']} | "
                 f"{_fmt(stat[y]['med'], 3)} | {pos_net} | **{pos_sig}** |")
    L.append("")

    # ---------------- 跨年一致 ----------------
    L.append("## 二、跨年一致性：ratio ≥ 1.0 出现在几年\n")
    L.append("| 出现年数 | 格数 |")
    L.append("|---:|---:|")
    dist = {}
    for rec in rows:
        cnt = 0
        for y in YEARS:
            r = rec["yearly"][str(y)]
            if r["n"] >= 30 and (r.get("ratio") or -9) >= 1.0:
                cnt += 1
        dist[cnt] = dist.get(cnt, 0) + 1
    for c in sorted(dist, reverse=True):
        L.append(f"| {c} | {dist[c]} |")
    L.append("")

    # 5 年全过的格子逐条列出
    five = []
    for rec in rows:
        cnt, det = 0, []
        for y in YEARS:
            r = rec["yearly"][str(y)]
            if r["n"] >= 30 and (r.get("ratio") or -9) >= 1.0:
                cnt += 1
                det.append(f"{y}:{_fmt(r['ratio'])}")
        if cnt >= 4:
            five.append((cnt, rec, det))
    if five:
        L.append("### ratio ≥ 1.0 达到 4 年及以上的格\n")
        L.append("| 信号 | 品种 | 年数 | 逐年 ratio | 全样本 ratio | 逐年净(点) | 逐年 n |")
        L.append("|---|---|---:|---|---:|---|---|")
        for cnt, rec, det in sorted(five, key=lambda x: -x[0]):
            nets = " / ".join(_fmt(rec["yearly"][str(y)]["L1_net"], 2) for y in YEARS)
            ns = " / ".join(str(rec["yearly"][str(y)]["n"]) for y in YEARS)
            L.append(f"| {rec['signal']} | {rec['name_cn']} | **{cnt}** | {' · '.join(det)} | "
                     f"{_fmt(rec['full_ratio'])} | {nets} | {ns} |")
        L.append("")
    else:
        L.append("**没有任何一格在 4 年以上同时 ratio ≥ 1.0。**\n")

    # ---------------- 全样本靠前的格子逐年明细 ----------------
    L.append("## 三、全样本 ratio 最高的 20 格：逐年明细（诊断）\n")
    top = sorted([r for r in rows if r.get("full_ratio") is not None],
                 key=lambda x: -x["full_ratio"])[:20]
    L.append("| 信号 | 品种 | 全样本 | n | " + " | ".join(str(y) for y in YEARS) + " |")
    L.append("|---|---|---:|---:|" + "---:|" * len(YEARS))
    for rec in top:
        cells = []
        for y in YEARS:
            r = rec["yearly"][str(y)]
            if r["n"] < 30:
                cells.append("n<30")
            else:
                cells.append(f"{_fmt(r['ratio'])} ({r['n']})")
        L.append(f"| {rec['signal']} | {rec['name_cn']} | **{_fmt(rec['full_ratio'])}** | "
                 f"{rec['full_n']} | " + " | ".join(cells) + " |")
    L.append("")
    L.append("> 括号内为当年笔数。ratio ≥ 2.0 的格子标粗无意义——**逐年挑年份是事后择优**。")

    out_md = os.path.join(_HERE, "对账_15min_逐年.md")
    with open(out_md, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    out_json = os.path.join(_HERE, "体检结果_15min_逐年.json")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({"years": YEARS, "syms": SYMS, "sigs": SIGS,
                   "stat": {str(k): v for k, v in stat.items()},
                   "rows": rows}, f, ensure_ascii=False, indent=1)
    print("WROTE", out_md, flush=True)
    print("WROTE", out_json, flush=True)


if __name__ == "__main__":
    main()
