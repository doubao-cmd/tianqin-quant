# -*- coding: utf-8 -*-
"""15min 降频检验 · IS/OOS 切窗对账（2026-11-04）

回答 `交接\15min降频检验_预注册_20261104.md` 的三条判据。
**不重写任何信号逻辑**：全部复用 `进场体检.screen_one`，只是加了日期切窗。

口径（与 5min 完全一致，只有周期不同）：
  · 15min K 线、HOLD=12 根（=3 小时）、止损 2×ATR(14)、滑点 1 跳、1 手
  · 全样本 2021-01-01 ~ 2025-12-31
  · IS      2021-01-01 ~ 2023-12-31（已污染，只作诊断）
  · OOS     2024-01-01 ~ 2025-12-31（干净，只跑一次）
  · 成交必须整体落在窗内：进场 bar ≥ t_lo 且**出场 bar < t_hi**（不跨窗借未来）

用法：<tqsdk venv>\\python.exe 对账_15min_ISOOS.py
产物：对账_15min_ISOOS.md / 体检结果_15min_ISOOS.json
"""
import json
import os
import sys

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import 进场体检 as mj  # noqa: E402

PERIOD = "15min"
FULL = ("2021-01-01", None)
IS = ("2021-01-01", "2024-01-01")
OOS = ("2024-01-01", "2026-01-01")
KEYS = ["MA", "M", "RB", "V", "SA", "EB", "JD", "JM", "LH", "P", "RU", "SR"]
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


def main():
    mj.PERIOD = PERIOD
    sigs = list(mj.SIGNALS)
    data = {}
    quality = []
    for k in KEYS:
        p = os.path.join(mj.DATA_DIR, f"{k}_{PERIOD}_2021-01-01_2025-12-31.csv")
        if not os.path.exists(p):
            print(f"[warn] 缺文件 {p}")
            continue
        df = pd.read_csv(p, encoding="utf-8-sig", parse_dates=["datetime"])
        o, h, l, c = df["open"], df["high"], df["low"], df["close"]
        deg = float(((o == h) & (h == l) & (l == c)).mean())
        quality.append({"key": k, "n": len(df), "deg": deg,
                        "lo": str(df["datetime"].iloc[0]), "hi": str(df["datetime"].iloc[-1]),
                        "n_2021p": int((df["datetime"] >= "2021-01-01").sum()),
                        "n_oos": int((df["datetime"] >= "2024-01-01").sum())})
        data[k] = df

    rows = []
    for s in sigs:
        for k in KEYS:
            if k not in data:
                continue
            df = data[k]
            rec = {"signal": s, "key": k, "sym": SYM_CN[k]}
            for tag, (lo, hi) in (("full", FULL), ("is", IS), ("oos", OOS)):
                r = mj.screen_one(k, s, df, t_lo=lo, t_hi=hi)
                rec[tag] = r
            rows.append(rec)
        print(f"  [{s}] done", flush=True)

    # ---------------- 汇总 ----------------
    def agg(tag):
        rs = [r[tag] for r in rows if r[tag]["n"] > 0]
        r_ok = [r for r in rs if (r.get("ratio") is not None)]
        return {
            "n_cells": len(rs),
            "A": sum(1 for r in r_ok if r["ratio"] < 1.0),
            "B": sum(1 for r in r_ok if 1.0 <= r["ratio"] < 2.0),
            "C": sum(1 for r in r_ok if r["ratio"] >= 2.0),
            "net_pos": sum(1 for r in rs if r["L1_net"] > 0),
            "sig_pos": sum(1 for r in rs if r["L1_t"] >= 2.0),
            "sig_neg": sum(1 for r in rs if r["L1_t"] <= -2.0),
            "max_ratio": max([r["ratio"] for r in r_ok], default=None),
            "max_ratio_cell": max(r_ok, key=lambda r: r["ratio"]) if r_ok else None,
        }

    A_full, A_is, A_oos = agg("full"), agg("is"), agg("oos")

    # ---------------- 5min 同窗对照（毛/成本提升倍数 = 机制验证） ----------------
    # 不读旧的 体检结果_ALL_hold12.json（那个窗口是 2020-10-26 起、含 2 个月预热），
    # 而是**用同一个 screen_one、同一个 2021-2023 窗口**在 5min 上重算一遍 ⇒ 两边严格同窗。
    m5 = {}
    for k in KEYS:
        p5 = os.path.join(mj.DATA_DIR, f"{k}_5min_2021-01-01_2023-12-31.csv")
        if not os.path.exists(p5):
            continue
        d5 = pd.read_csv(p5, encoding="utf-8-sig", parse_dates=["datetime"])
        for s in sigs:
            m5[(s, k)] = mj.screen_one(k, s, d5, t_lo=IS[0], t_hi=IS[1])
        print(f"  [5min同窗] {k} done", flush=True)

    L = []
    L.append("# 15min 降频检验 · IS/OOS 对账（2026-11-04）\n")
    L.append("> 预注册：`交接\\15min降频检验_预注册_20261104.md`（跑之前落盘）")
    L.append("> 复算：`工具\\_进场体检\\对账_15min_ISOOS.py`\n")

    L.append("## 一、数据质量（15min，重拉后）\n")
    L.append("| 品种 | 根数 | 起点 | 终点 | 退化率 | 2021后 | 2024后 |")
    L.append("|---|---:|---|---|---:|---:|---:|")
    for q in quality:
        L.append(f"| {SYM_CN[q['key']]}({q['key']}) | {q['n']} | {q['lo'][:16]} | {q['hi'][:16]} | "
                 f"**{q['deg']*100:.2f}%** | {q['n_2021p']} | {q['n_oos']} |")
    L.append("")

    L.append("## 二、三窗口汇总（24 信号 × 12 品种 = 288 格）\n")
    L.append("| 窗口 | 有效格 | A 真死 | B 薄利 | C 值得进 | 净为正 | t≥2 正 | t≤−2 | 最大 毛/成本 |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for nm, a in (("全样本 2021-2025", A_full), ("IS 2021-2023", A_is), ("OOS 2024-2025", A_oos)):
        mr = a["max_ratio_cell"]
        tag = f"{_fmt(a['max_ratio'],2)}（{mr['signal']}×{mj.cs.get(mr['key'])[0]}）" if mr else "—"
        L.append(f"| {nm} | {a['n_cells']} | {a['A']} | {a['B']} | {a['C']} | "
                 f"{a['net_pos']} | {a['sig_pos']} | {a['sig_neg']} | {tag} |")
    L.append("")

    # ---------------- 预注册判定 ----------------
    def oos_ok(r, need_t=True):
        o = r["oos"]
        if o["n"] < 100 or o.get("ratio") is None or o["ratio"] < 1.0:
            return False
        if not need_t:
            return True
        return o["L1_net"] > 0 and o["L1_t"] >= 2.0

    alive = [r for r in rows if oos_ok(r, True)]
    cand = [r for r in rows if oos_ok(r, False)]

    if alive:
        verdict = "救活了"
    elif cand:
        verdict = "薄利候补"
    else:
        verdict = "15min 也死"

    L.append("## 三、预注册判定（只看 OOS）\n")
    L.append(f"**结论：{verdict}**\n")
    L.append(f"- 满足「n≥100 且 毛/成本≥1.0」的格：**{len(cand)}** 个")
    L.append(f"- 其中同时「净>0 且 t≥2」：**{len(alive)}** 个\n")

    if cand:
        for r in cand:
            r["y2024"] = mj.screen_one(r["key"], r["signal"], data[r["key"]],
                                       t_lo="2024-01-01", t_hi="2025-01-01")
            r["y2025"] = mj.screen_one(r["key"], r["signal"], data[r["key"]],
                                       t_lo="2025-01-01", t_hi="2026-01-01")
        L.append("| 信号 | 品种 | OOS n | OOS 毛/成本 | OOS 净(点) | OOS t | IS 毛/成本 | 2024 毛/成本 | 2025 毛/成本 | 全样本 毛/成本 |")
        L.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        for r in sorted(cand, key=lambda x: -(x["oos"].get("ratio") or 0)):
            o, i, f_ = r["oos"], r["is"], r["full"]
            L.append(f"| {r['signal']} | {r['sym']} | {o['n']} | **{_fmt(o.get('ratio'))}** | "
                     f"{_fmt(o['L1_net'])} | {_fmt(o['L1_t'])} | {_fmt(i.get('ratio'))} | "
                     f"{_fmt(r['y2024'].get('ratio'))} | {_fmt(r['y2025'].get('ratio'))} | {_fmt(f_.get('ratio'))} |")
        L.append("")
        # 跨窗一致（IS 也 ≥1.0）的候选
        cons = [r for r in cand if (r["is"].get("ratio") or 0) >= 1.0]
        L.append(f"其中 **IS 上毛/成本也 ≥1.0** 的只有 **{len(cons)}/{len(cand)}** 个"
                 + ("：" + "、".join(f"`{r['signal']}×{r['sym']}`" for r in cons) if cons else "")
                 + "\n")

    # ---------------- IS/OOS 一致性 ----------------
    both = [r for r in rows if r["is"]["n"] > 0 and r["oos"]["n"] > 0]
    def _g(r, tag):
        return grade_of(r[tag].get("ratio"))
    same = sum(1 for r in both if _g(r, "is") == _g(r, "oos"))
    L.append("## 四、IS 与 OOS 分档一致性\n")
    L.append(f"可比格 **{len(both)}**；IS/OOS 同档 **{same}**（{same/max(1,len(both))*100:.1f}%）\n")
    trans = {}
    for r in both:
        trans[(_g(r, "is"), _g(r, "oos"))] = trans.get((_g(r, "is"), _g(r, "oos")), 0) + 1
    L.append("| IS\\OOS | A | B | C |")
    L.append("|---|---:|---:|---:|")
    for gi in ("A", "B", "C"):
        cells = [str(trans.get((gi, go), 0)) for go in ("A", "B", "C")]
        L.append(f"| **{gi}** | " + " | ".join(cells) + " |")
    L.append("")
    is_b_oos_a = [r for r in both if _g(r, "is") in ("B", "C") and _g(r, "oos") == "A"]
    if is_b_oos_a:
        L.append(f"⚠️ IS 上 B/C 但 OOS 掉回 A 的格：**{len(is_b_oos_a)}** 个（= IS 的「活」不可复现）\n")

    # ---------------- 与 5min 的比值（机制验证） ----------------
    if m5:
        L.append("## 五、与 5min 对照：降频到底有没有把毛/成本推上去（机制验证）\n")
        L.append("> 先验预期：15min 持有时长 ×3 ⇒ 单笔毛位移 ≈ √3 倍、成本不变 ⇒ 毛/成本 ≈ 1.73 倍。")
        L.append("> 检验方式：**同一个 `screen_one`、同一个 2021-01-01~2023-12-31 窗口**，只在 5min / 15min 两套数据上各跑一遍。\n")
        pairs = []
        for r in rows:
            a = m5.get((r["signal"], r["key"]))
            b = r["is"]
            if not a or a.get("ratio") is None or b.get("ratio") is None:
                continue
            if a["n"] < 100 or b["n"] < 100:
                continue
            pairs.append((r["signal"], r["key"], a["ratio"], b["ratio"]))
        if pairs:
            x = np.array([p[2] for p in pairs], float)
            y = np.array([p[3] for p in pairs], float)
            up = int((y > x).sum())
            both = (x > 0) & (y > 0)
            L.append("| 指标 | 值 |")
            L.append("|---|---|")
            L.append(f"| 可比格（两边 n≥100） | **{len(pairs)}** |")
            L.append(f"| 5min 毛/成本 中位 | **{np.median(x):.3f}** |")
            L.append(f"| 15min 毛/成本 中位 | **{np.median(y):.3f}** |")
            L.append(f"| 逐格差值 中位（15min − 5min） | **{np.median(y - x):+.3f}** |")
            L.append(f"| 15min 更好的格 | **{up} / {len(pairs)}** |")
            L.append(f"| 两边都 >0 的格 | {int(both.sum())}，其中 15min 更好 **{int((both & (y > x)).sum())}** |")
            L.append(f"| Spearman 秩相关（排序是否稳定） | {float(np.corrcoef(pd.Series(x).rank(), pd.Series(y).rank())[0, 1]):.3f} |")
            x0 = np.array([m5.get((r["signal"], r["key"]))["L0_gross"] for r in rows
                           if m5.get((r["signal"], r["key"])) and r["is"].get("ratio") is not None
                           and m5[(r["signal"], r["key"])]["n"] >= 100 and r["is"]["n"] >= 100], float)
            y0 = np.array([r["is"]["L0_gross"] for r in rows
                           if m5.get((r["signal"], r["key"])) and r["is"].get("ratio") is not None
                           and m5[(r["signal"], r["key"])]["n"] >= 100 and r["is"]["n"] >= 100], float)
            L.append(f"| **L0 毛（无止损，同持 12 根）中位：5min / 15min** | **{np.median(x0):.3f} / {np.median(y0):.3f}** |")
            L.append(f"| L0 毛 逐格差 中位 | {np.median(y0 - x0):+.3f} |")
            L.append(f"| L0 毛 15min 更大的格 | {int((y0 > x0).sum())} / {len(x0)} |")
            L.append("")
            L.append("**⇒ 先验的 √3 没有兑现**（毛/成本中位差 ≈ 0、一半格子变差）。")
            L.append("拆解：连**不含止损**的 L0 毛都没有按 √3 放大（上表），说明问题不在止损，")
            L.append("而在「15min 的 3 小时持有并没有换来 3 小时的价格位移」——两个候选解释都未拆开：")
            L.append("①进场价用**下一根 bar 的 open**，执行延迟从 5 分钟变 15 分钟；")
            L.append("②这类反转/择时信号的有效窗口本来就短于 3 小时，后 2 小时只加噪声。**两者本次未区分**。\n")

    L.append("## 六、全样本上 毛/成本 ≥ 0.9 的格（诊断用，**不得据此择优**）\n")
    hi = [r for r in rows if r["full"].get("ratio") is not None and r["full"]["ratio"] >= 0.9]
    hi.sort(key=lambda r: -r["full"]["ratio"])
    if hi:
        L.append("| 信号 | 品种 | 全样本 n | 全样本 毛/成本 | IS 毛/成本 | OOS 毛/成本 | 全样本 净 | 全样本 t |")
        L.append("|---|---|---:|---:|---:|---:|---:|---:|")
        for r in hi:
            f_ = r["full"]
            L.append(f"| {r['signal']} | {r['sym']} | {f_['n']} | **{_fmt(f_.get('ratio'))}** | "
                     f"{_fmt(r['is'].get('ratio'))} | {_fmt(r['oos'].get('ratio'))} | "
                     f"{_fmt(f_['L1_net'])} | {_fmt(f_['L1_t'])} |")
    else:
        L.append("（无）")
    L.append("")

    out = "\n".join(L)
    with open(os.path.join(_HERE, "对账_15min_ISOOS.md"), "w", encoding="utf-8") as f:
        f.write(out + "\n")
    with open(os.path.join(_HERE, "体检结果_15min_ISOOS.json"), "w", encoding="utf-8") as f:
        json.dump({"quality": quality,
                   "agg": {"full": {k: v for k, v in A_full.items() if k != "max_ratio_cell"},
                           "is": {k: v for k, v in A_is.items() if k != "max_ratio_cell"},
                           "oos": {k: v for k, v in A_oos.items() if k != "max_ratio_cell"}},
                   "verdict": verdict,
                   "alive": [{"signal": r["signal"], "key": r["key"]} for r in alive],
                   "cand": [{"signal": r["signal"], "key": r["key"], **r["oos"]} for r in cand],
                   "rows": rows}, f, ensure_ascii=False, indent=1)
    print("\n" + out)
    print("[done] 对账_15min_ISOOS.md")


if __name__ == "__main__":
    main()
