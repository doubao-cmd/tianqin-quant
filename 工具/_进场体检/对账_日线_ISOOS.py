# -*- coding: utf-8 -*-
"""日线体检 · IS/OOS 切窗（2026-11-04）

补齐唯一没跑的周期。**不重写任何信号逻辑**：全部复用 `进场体检.screen_one`。

口径（与 5min/15min 唯一的差别是周期）：
  · 日线（1440min）、HOLD=12 根 = **12 个交易日（约 2.5 周）**
  · 止损 2×ATR(14)、滑点 1 跳、1 手
  · 全样本 2016-01-01 ~ 2025-12-31
  · IS      2016-01-01 ~ 2021-12-31（**部分污染**：SMA 排列 OOS 用过这段）
  · OOS     2022-01-01 ~ 2025-12-31（相对干净，只跑一次）
  · 成交必须整体落在窗内：进场 bar ≥ t_lo 且**出场 bar < t_hi**

判据沿用 5min/15min 的三道关：
  ① 功效：MDE = 2.8×SE（双侧5% + 80% power），SE 由 L0 的 mean/t 反推
  ② 量级：有功效格里 L0 毛 vs 该格往返成本
  ③ 校正：Bonferroni |t| ≥ 3.75（288 格）

用法：<tqsdk venv>\\python.exe 对账_日线_ISOOS.py
产物：对账_日线_ISOOS.md / 体检结果_日线_ISOOS.json
"""
import glob
import json
import math
import os
import sys

import numpy as np
import pandas as pd
from statistics import NormalDist

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import 进场体检 as mj  # noqa: E402

PERIOD = "1440min"
# --index：改用 `数据_指数/`（KQ.i@ 指数连续），消除主连换月 8.7% 拼接跳空。
# 默认关闭 ⇒ 既有产物（主连口径）逐位可复现。
IDX_DIR = os.path.join(_HERE, "数据_指数")
USE_INDEX = "--index" in sys.argv
SUFFIX = "_指数" if USE_INDEX else ""
FULL = (None, None)
IS = ("2016-01-01", "2022-01-01")
OOS = ("2022-01-01", "2026-01-01")
KEYS = ["MA", "M", "RB", "V", "SA", "EB", "JD", "JM", "LH", "P", "RU", "SR"]
MDE_C = 2.8
SYM_CN = {k: mj.cs.get(k)[0] for k in KEYS}


def _fmt(x, nd=2, dash="—"):
    if x is None:
        return dash
    try:
        if isinstance(x, float) and (np.isnan(x) or np.isinf(x)):
            return dash
    except Exception:
        pass
    return f"{x:.{nd}f}"


def grade_of(ratio):
    if ratio is None:
        return "—"
    if ratio < 1.0:
        return "A"
    if ratio < 2.0:
        return "B"
    return "C"


def mde_of(x):
    """MDE = 2.8 × SE，SE 由 L0 的 mean/t 反推；t≈0 时不可算。"""
    if x is None or abs(x.get("L0_t") or 0) < 1e-9:
        return None
    return MDE_C * abs(x["L0_gross"] / x["L0_t"])


def main():
    sigs = list(mj.SIGNALS)
    data, quality = {}, []
    for k in KEYS:
        if USE_INDEX:
            _fs = sorted(glob.glob(os.path.join(IDX_DIR, f"{k}_{PERIOD}_*.csv")))
            p = _fs[-1] if _fs else os.path.join(IDX_DIR, f"{k}_{PERIOD}.csv")
        else:
            p = os.path.join(mj.DATA_DIR, f"{k}_{PERIOD}_2016-01-01_2025-12-31.csv")
        if not os.path.exists(p):
            print(f"[warn] 缺文件 {os.path.basename(p)}", flush=True)
            continue
        df = pd.read_csv(p, encoding="utf-8-sig", parse_dates=["datetime"])
        o, h, l, c = df["open"], df["high"], df["low"], df["close"]
        deg = float(((o == h) & (h == l) & (l == c)).mean())
        quality.append({"key": k, "name_cn": SYM_CN.get(k, k), "n": len(df), "deg": deg,
                        "lo": str(df["datetime"].iloc[0])[:10],
                        "hi": str(df["datetime"].iloc[-1])[:10],
                        "n_is": int(((df["datetime"] >= IS[0]) & (df["datetime"] < IS[1])).sum()),
                        "n_oos": int((df["datetime"] >= OOS[0]).sum())})
        data[k] = df
    print(f"[data] {len(data)} 品种", flush=True)

    rows = []
    for s in sigs:
        for k in KEYS:
            if k not in data:
                continue
            df = data[k]
            rec = {"signal": s, "key": k, "name_cn": SYM_CN.get(k, k)}
            for tag, (lo, hi) in (("full", FULL), ("is", IS), ("oos", OOS)):
                r = mj.screen_one(k, s, df, t_lo=lo, t_hi=hi)
                r["mde"] = mde_of(r)
                r["mde_ok"] = (r["mde"] is not None and r["cost_pts"] > 0
                               and r["mde"] < r["cost_pts"])
                rec[tag] = r
            rows.append(rec)
        print(f"[sig] {s}", flush=True)

    # ---------------- 报告 ----------------
    L = []
    L.append("# 日线体检 · IS/OOS（2026-11-04）\n")
    L.append("> 数据口径：**%s**。" % ("指数连续 KQ.i@（无换月跳空）" if USE_INDEX else "主连 KQ.m@（换月跳空 8.7%）"))
    L.append("> 24 信号 × 12 品种 = 288 格。日线、**HOLD=12 根 = 12 个交易日**、止损 2×ATR(14)、")
    L.append("> 滑点 1 跳、进场 = 信号 bar 的下一根 open。")
    L.append("> **IS 2016-2021 部分污染**（SMA 排列 OOS 用过同一段）；OOS 2022-2025 相对干净。\n")

    L.append("## 一、数据质量\n")
    L.append("| 品种 | 根数 | 区间 | 退化率 | IS 根数 | OOS 根数 |")
    L.append("|---|---:|---|---:|---:|---:|")
    for q in quality:
        L.append(f"| {q['name_cn']}({q['key']}) | {q['n']} | {q['lo']} ~ {q['hi']} | "
                 f"{q['deg']*100:.2f}% | {q['n_is']} | {q['n_oos']} |")
    L.append("")

    # 三窗口汇总
    L.append("## 二、三窗口汇总（288 格）\n")
    L.append("| 窗口 | 有效格 | A 真死 | B 薄利 | C 值得进 | 净>0 | t≥2 正 | 毛/成本 中位 | max |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    stats = {}
    for tag, lab in (("full", "全样本 2016-2025"), ("is", "IS 2016-2021"), ("oos", "OOS 2022-2025")):
        ok = [r[tag] for r in rows if r[tag]["n"] >= 30]
        g = {"A": 0, "B": 0, "C": 0}
        for x in ok:
            g[grade_of(x["ratio"])] += 1
        pos = sum(1 for x in ok if x["L1_net"] > 0)
        sig = sum(1 for x in ok if x["L1_t"] >= 2.0 and x["L1_net"] > 0)
        rr = [x["ratio"] for x in ok if x["ratio"] is not None]
        med = float(np.median(rr)) if rr else None
        mx = float(np.max(rr)) if rr else None
        stats[tag] = {"ok": len(ok), **g, "pos": pos, "sig": sig, "med": med, "max": mx}
        L.append(f"| {lab} | {len(ok)} | {g['A']} | {g['B']} | {g['C']} | {pos} | **{sig}** | "
                 f"{_fmt(med, 3)} | {_fmt(mx, 3)} |")
    L.append("")

    # 三道关
    L.append("## 三、三道关（同 5min/15min 口径）\n")
    L.append("**第一关 · 功效**：MDE = 2.8×SE（SE 由 L0 的 mean/t 反推），有功效 = MDE < 该格成本。\n")
    L.append("**第二关 · 量级**：有功效格里 L0 毛 vs 成本。\n")
    L.append("| 窗口 | 有功效格 | L0 毛中位 | P90 | max | 毛>成本 | 净>0 |")
    L.append("|---|---:|---:|---:|---:|---:|---:|")
    gate = {}
    for tag, lab in (("full", "全样本"), ("is", "IS"), ("oos", "OOS")):
        g = [r[tag] for r in rows if r[tag]["n"] >= 30 and r[tag]["mde_ok"]]
        v = np.array([x["L0_gross"] for x in g], float) if g else np.array([])
        gt = sum(1 for x in g if x["L0_gross"] > x["cost_pts"])
        pn = sum(1 for x in g if x["L1_net"] > 0)
        gate[tag] = {"n": len(g), "gt": gt, "pos": pn,
                     "med": float(np.median(v)) if len(v) else None,
                     "p90": float(np.percentile(v, 90)) if len(v) else None,
                     "max": float(np.max(v)) if len(v) else None}
        L.append(f"| {lab} | {len(g)} | {_fmt(gate[tag]['med'])} | {_fmt(gate[tag]['p90'])} | "
                 f"{_fmt(gate[tag]['max'])} | **{gt}** | {pn} |")
    L.append("")
    thr = NormalDist().inv_cdf(1 - 0.05 / (2 * 288))
    L.append(f"**第三关 · 校正**：288 格 Bonferroni 阈值 **|t| ≥ {thr:.2f}**。\n")
    for tag, lab in (("full", "全样本"), ("is", "IS"), ("oos", "OOS")):
        tmax = max((r[tag]["L1_t"] for r in rows if r[tag]["n"] >= 30), default=float("nan"))
        L.append(f"- {lab}：L1_t 最大 **{tmax:.2f}**，超过 {thr:.2f} 的格数 **0**"
                 if tmax < thr else f"- {lab}：L1_t 最大 **{tmax:.2f}**，**超过阈值**")
    L.append("")

    # IS/OOS 一致
    L.append("## 四、IS 与 OOS 一致性（ratio ≥ 1.0 的格）\n")
    both = [r for r in rows if (r["is"]["ratio"] or -9) >= 1.0 and (r["oos"]["ratio"] or -9) >= 1.0
            and r["is"]["n"] >= 30 and r["oos"]["n"] >= 30]
    L.append(f"两个窗口都 ≥1.0 的格：**{len(both)} / 288**\n")
    if both:
        L.append("| 信号 | 品种 | IS ratio | IS t | OOS ratio | OOS t | 全样本 ratio |")
        L.append("|---|---|---:|---:|---:|---:|---:|")
        for r in sorted(both, key=lambda x: -(x["oos"]["ratio"] or 0))[:25]:
            L.append(f"| {r['signal']} | {r['name_cn']} | {_fmt(r['is']['ratio'])} | "
                     f"{_fmt(r['is']['L1_t'])} | {_fmt(r['oos']['ratio'])} | "
                     f"{_fmt(r['oos']['L1_t'])} | {_fmt(r['full']['ratio'])} |")
        L.append("")

    # 全样本靠前
    L.append("## 五、全样本毛/成本最高的 15 格（诊断，不得择优）\n")
    top = sorted([r for r in rows if r["full"]["n"] >= 30 and r["full"]["ratio"] is not None],
                 key=lambda x: -x["full"]["ratio"])[:15]
    L.append("| 信号 | 品种 | n | ratio | L1净 | L1 t | IS ratio | OOS ratio | 有功效 |")
    L.append("|---|---|---:|---:|---:|---:|---:|---:|---|")
    for r in top:
        L.append(f"| {r['signal']} | {r['name_cn']} | {r['full']['n']} | "
                 f"**{_fmt(r['full']['ratio'])}** | {_fmt(r['full']['L1_net'])} | "
                 f"{_fmt(r['full']['L1_t'])} | {_fmt(r['is']['ratio'], 2)} | "
                 f"{_fmt(r['oos']['ratio'], 2)} | {'✓' if r['full']['mde_ok'] else '×'} |")
    L.append("")

    out_md = os.path.join(_HERE, "对账_日线_ISOOS%s.md" % SUFFIX)
    with open(out_md, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    out_js = os.path.join(_HERE, "体检结果_日线_ISOOS%s.json" % SUFFIX)
    with open(out_js, "w", encoding="utf-8") as f:
        json.dump({"period": PERIOD, "hold": mj.HOLD, "windows": {"full": FULL, "is": IS, "oos": OOS},
                   "quality": quality, "stats": stats, "gate": gate, "rows": rows},
                  f, ensure_ascii=False, indent=1)
    print("WROTE", out_md, flush=True)
    print("WROTE", out_js, flush=True)


if __name__ == "__main__":
    main()
