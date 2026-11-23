# -*- coding: utf-8 -*-
"""用户版「三根 K 线」突破 · 15min 一次性体检（2026-11-18）

规则（用户 2026-11-18 22:20 原话转写，跑前写死，事后不追认）：
  · 周期 15min 主连
  · 前 2 根（t-2、t-1）的 high/low 构成轨道
  · 第 3 根（t）盘中触及上轨 → 多；触及下轨 → 空；区间内不触发
  · 第 4 根（t+1）开盘价成交
  · 止损 2×ATR(14)，基准 = 信号根 ATR；时间止损 = 持有满 15 根按开盘价平
  · 1 手（不做波动率手数，用户指定）
  · 成本：1 跳滑点/边 + 交易所手续费（与全项目同口径）

两种执行口径都跑（这是「1 手」的两种合理解释，不是调参）：
  A 每笔独立 1 手     —— 允许多笔同时在场（体检工具原生口径，可比）
  B 同时只持 1 手     —— 持仓期间的新信号直接丢弃（单仓贪心）

样本（2026-11-18 22:23 用户指定）：
  品种   V / MA / JD / RB / P 五个
  全样本 2023-01-01 ~ 数据末尾（2026-11-04）
  IS     2023-01-01 ~ 2024-12-31（已被 288 格的旧 IS/OOS 覆盖过，**只作诊断**）
  OOS    2025-01-01 ~ 2026-11-04（2025 段已污染、2026 段相对干净；仍只能判死不能判生）

用法：.venv_tq\\Scripts\\python.exe 体检_breakout2_15min.py
产物：体检_breakout2_15min.md / 体检_breakout2_15min.json
"""
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

PERIOD = "15min"
HOLD = 15            # 时间止损（根）
STOP = 2.0           # 2×ATR14
# ★ 用户 2026-11-18 22:23 指定：样本内 2023-2024、样本外 2025-2026、只跑 5 品种
W_FULL = ("2023-01-01", None)
W_IS = ("2023-01-01", "2025-01-01")
W_OOS = ("2025-01-01", None)
KEYS = ["V", "MA", "JD", "RB", "P"]


def load_merged(key):
    """2024 前用 2021-2025 文件，2024 起用 2024-2026 文件（后者更新到 2026-11）。
    按 datetime 去重升序，保证 ATR/信号在拼接处连续。"""
    p1 = os.path.join(mj.DATA_DIR, f"{key}_{PERIOD}_2021-01-01_2025-12-31.csv")
    p2 = os.path.join(mj.DATA_DIR, f"{key}_{PERIOD}_2024-01-01_2026-11-04.csv")
    if not os.path.exists(p1):
        return None, "缺 2021-2025 文件"
    d1 = pd.read_csv(p1, encoding="utf-8-sig", parse_dates=["datetime"])
    if not os.path.exists(p2):
        return d1, "缺 2024-2026 文件（OOS 到 2025-12-31 为止）"
    d2 = pd.read_csv(p2, encoding="utf-8-sig", parse_dates=["datetime"])
    head = d1[d1["datetime"] < pd.Timestamp("2024-01-01")]
    df = pd.concat([head, d2], ignore_index=True)
    df = df.drop_duplicates(subset="datetime").sort_values("datetime").reset_index(drop=True)
    return df, ""


def _t(x):
    x = np.asarray(x, float)
    if len(x) < 2:
        return 0.0
    sd = float(np.std(x, ddof=1))
    return float(np.mean(x) / (sd / math.sqrt(len(x)))) if sd > 1e-12 else 0.0


def screen_single(key, sig_fn, df, t_lo=None, t_hi=None, stop=STOP, hold=HOLD):
    """单仓贪心版：持仓期间的新信号一律丢弃。其余口径与 mj.screen_one 逐字一致。"""
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    N = len(c)
    atr = mj._atr(h, l, c, mj.ATR_N)
    ma = mj._sma(c, mj.MA_N)
    sig = sig_fn(o, h, l, c, atr, ma)
    bad = np.isnan(atr) | np.isnan(ma)
    sig = np.where(bad, 0.0, np.nan_to_num(sig, nan=0.0))
    cp = mj.cost_points(key)
    dtn = df["datetime"].to_numpy()

    trades, skipped = [], 0
    busy_until = -1
    for i in range(0, N - hold - 1):
        s = sig[i]
        if s == 0:
            continue
        if i + 1 < busy_until:          # 仓位未释放 ⇒ 该信号被丢弃
            skipped += 1
            continue
        if t_lo is not None and dtn[i] < np.datetime64(pd.Timestamp(t_lo)):
            continue
        if t_hi is not None and dtn[i + 1 + hold] >= np.datetime64(pd.Timestamp(t_hi)):
            continue
        e = i + 1
        entry = o[e]
        st = entry - s * stop * atr[i]
        exit_px, exit_i, hit = o[e + hold], e + hold, False
        for jj in range(e, e + hold):
            if s > 0:
                if l[jj] <= st:
                    exit_px, exit_i, hit = min(st, o[jj]), jj, True
                    break
            else:
                if h[jj] >= st:
                    exit_px, exit_i, hit = max(st, o[jj]), jj, True
                    break
        gross = (exit_px - entry) * s
        trades.append({"i": i, "gross": gross, "net": gross - cp, "hit": hit,
                       "bars": exit_i - e})
        busy_until = exit_i + 1

    if not trades:
        return {"n": 0, "skipped": skipped}
    g = np.array([t["gross"] for t in trades])
    n_ = np.array([t["net"] for t in trades])
    mult = mj.cs.get(key)[2]
    days = df["datetime"].dt.date.nunique()
    return {
        "n": len(trades), "skipped": skipped, "days": int(days),
        "per_day": round(len(trades) / days, 2),
        "cost_pts": round(cp, 3),
        "L1_gross": round(float(g.mean()), 3), "L1_net": round(float(n_.mean()), 3),
        "L1_t": round(_t(n_), 2), "winrate": round(float((n_ > 0).mean()), 4),
        "stop_share": round(float(np.mean([t["hit"] for t in trades])), 4),
        "bars_med": float(np.median([t["bars"] for t in trades])),
        "ratio": round(float(g.mean() / cp), 3) if cp > 0 else None,
        "net_yuan_per_lot": round(float(n_.mean()) * mult, 0),
        "total_yuan_1lot": round(float(n_.sum()) * mult, 0),
    }


def fmt(x, nd=2, dash="—"):
    if x is None:
        return dash
    if isinstance(x, float) and (np.isnan(x) or np.isinf(x)):
        return dash
    return f"{x:.{nd}f}"


def main():
    mj.PERIOD = PERIOD
    mj.HOLD = HOLD
    sig_fn = mj.SIGNALS["breakout_2"][1]
    sig_fn_c = mj.SIGNALS["breakout_2c"][1]

    data, quality = {}, []
    for k in KEYS:
        df, warn = load_merged(k)
        if df is None:
            print(f"[warn] {k}: {warn}")
            continue
        if warn:
            print(f"[warn] {k}: {warn}")
        o, h, l, c = df["open"], df["high"], df["low"], df["close"]
        deg = float(((o == h) & (h == l) & (l == c)).mean())
        quality.append({"key": k, "n": len(df), "deg": deg,
                        "lo": str(df["datetime"].iloc[0])[:16],
                        "hi": str(df["datetime"].iloc[-1])[:16]})
        data[k] = df

    rows = []
    for k in KEYS:
        if k not in data:
            continue
        df = data[k]
        rec = {"key": k, "sym": mj.cs.get(k)[0], "mult": mj.cs.get(k)[2]}
        for tag, (lo, hi) in (("full", W_FULL), ("is", W_IS), ("oos", W_OOS)):
            rec[f"multi_{tag}"] = mj.screen_one(k, "breakout_2", df, t_lo=lo, t_hi=hi)
            rec[f"single_{tag}"] = screen_single(k, sig_fn, df, t_lo=lo, t_hi=hi)
            rec[f"close_{tag}"] = mj.screen_one(k, "breakout_2c", df, t_lo=lo, t_hi=hi)
        rows.append(rec)
        print(f"  [{rec['sym']}] done", flush=True)

    L = []
    L.append("# 用户版「三根 K 线」突破 · 15min 一次性体检（2026-11-18）\n")
    L.append("> 样本按用户 22:23 指定：5 品种（V/MA/JD/RB/P）、IS 2023-2024、OOS 2025-2026")
    L.append("> 规则：前 2 根定轨道 → 第 3 根盘中触及 → 第 4 根开盘进 → 2×ATR(14) 止损 → 持有满 15 根平 → 1 手")
    L.append("> 脚本：`工具\\_进场体检\\体检_breakout2_15min.py`（信号 `breakout_2` 已注册进 `进场体检.py`）\n")

    L.append("## 〇、数据质量\n")
    L.append("| 品种 | 根数 | 起点 | 终点 | 退化率 |")
    L.append("|---|---:|---|---|---:|")
    for q in quality:
        L.append(f"| {mj.cs.get(q['key'])[0]}({q['key']}) | {q['n']} | {q['lo']} | {q['hi']} | {q['deg']*100:.2f}% |")
    L.append("")

    for tag, nm in (("full", "全样本 2023-2026"), ("is", "IS 2023-2024"), ("oos", "OOS 2025-2026")):
        L.append(f"## 一-{tag}、口径 A：每笔独立 1 手（{nm}）\n")
        L.append("| 品种 | 笔数 | 笔/日 | 成本(点) | 毛(点) | **净(点)** | 毛/成本 | 胜率 | 止损占比 | t | 1手净(元/笔) | 1手累计(元) |")
        L.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        nets = []
        for r in rows:
            x = r[f"multi_{tag}"]
            if x["n"] == 0:
                L.append(f"| {r['sym']} | 0 | — | — | — | — | — | — | — | — | — | — |")
                continue
            nets.append(x["L1_net"] * r["mult"])
            L.append(f"| {r['sym']} | {x['n']} | {x['per_day']:.2f} | {x['cost_pts']:.2f} | "
                     f"{x['L1_gross']:+.3f} | **{x['L1_net']:+.3f}** | {fmt(x['ratio'])} | "
                     f"{x['L1_winrate']*100:.1f}% | {x['stop_share']*100:.1f}% | "
                     f"{x['L1_t']:.2f} | {x['L1_net']*r['mult']:+,.0f} | "
                     f"{x['L1_net']*x['n']*r['mult']:+,.0f} |")
        L.append("")
        L.append(f"- 5 品种净(点)/笔 **中位 {fmt(float(np.median([r[f'multi_{tag}']['L1_net'] for r in rows if r[f'multi_{tag}']['n']>0])),3)}**；"
                 f"净为正 **{sum(1 for r in rows if r[f'multi_{tag}']['n']>0 and r[f'multi_{tag}']['L1_net']>0)}/{len(rows)}**")
        L.append("")

    L.append("## 二、口径 B：同时只持 1 手（信号被丢弃的比例）\n")
    L.append("| 品种 | 全样本笔数(A) | 全样本笔数(B) | 丢弃率 | B 净(点)/笔 | B 毛/成本 | B t | 1手合计(元) |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for r in rows:
        a, b = r["multi_full"], r["single_full"]
        if a["n"] == 0 or b.get("n", 0) == 0:
            continue
        dr = b["skipped"] / max(1, a["n"] + b["skipped"])
        L.append(f"| {r['sym']} | {a['n']} | {b['n']} | {dr*100:.0f}% | **{b['L1_net']:+.3f}** | "
                 f"{fmt(b['ratio'])} | {b['L1_t']:.2f} | {b['total_yuan_1lot']:,.0f} |")
    L.append("")

    L.append("## 三、敏感性：收盘确认 vs 盘中触及（仅对照，**不得择优**）\n")
    L.append("| 品种 | 触及·全样本净 | 触及·OOS净 | 收盘·全样本净 | 收盘·OOS净 | 触及笔数 | 收盘笔数 |")
    L.append("|---|---:|---:|---:|---:|---:|---:|")
    for r in rows:
        a, c_ = r["multi_full"], r["close_full"]
        L.append(f"| {r['sym']} | {a['L1_net']:+.3f} | {r['multi_oos']['L1_net']:+.3f} | "
                 f"{c_['L1_net']:+.3f} | {r['close_oos']['L1_net']:+.3f} | {a['n']} | {c_['n']} |")
    L.append("")

    # ---- 汇总判定 ----
    def agg(prefix, tag):
        rs = [r[f"{prefix}_{tag}"] for r in rows if r[f"{prefix}_{tag}"]["n"] > 0]
        if not rs:
            return {}
        return {
            "cells": len(rs),
            "net_pos": sum(1 for x in rs if x["L1_net"] > 0),
            "ratio_ge1": sum(1 for x in rs if (x.get("ratio") or -9) >= 1.0),
            "ratio_ge2": sum(1 for x in rs if (x.get("ratio") or -9) >= 2.0),
            "net_med": float(np.median([x["L1_net"] for x in rs])),
            "ratio_med": float(np.median([x["ratio"] for x in rs if x.get("ratio") is not None])),
            "t_pos_sig": sum(1 for x in rs if x["L1_t"] >= 2.0),
            "t_neg_sig": sum(1 for x in rs if x["L1_t"] <= -2.0),
            "cost_sum": sum(x["cost_pts"] * x["n"] * r["mult"] for r, x in
                            [(r, r[f"{prefix}_{tag}"]) for r in rows] if x["n"] > 0),
            "yuan_sum": sum(x["L1_net"] * x["n"] * r["mult"] for r, x in
                            [(r, r[f"{prefix}_{tag}"]) for r in rows] if x["n"] > 0),
            "n_total": sum(x["n"] for x in rs),
        }

    A = {tag: agg("multi", tag) for tag in ("full", "is", "oos")}
    Bs = {tag: agg("single", tag) for tag in ("full", "is", "oos")}

    L.append("## 四、汇总判定\n")
    L.append("| 口径 | 窗口 | 有效品种 | 总笔数 | 净>0 | 毛/成本≥1 | ≥2 | 净(点)/笔 中位 | 毛/成本 中位 | t≥2 | t≤−2 | **总成本(元)** | **总净盈亏(元)** |")
    L.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for lbl, D in (("A 每笔独立", A), ("B 单仓", Bs)):
        for tag, nm in (("full", "全样本"), ("is", "IS"), ("oos", "OOS")):
            a = D[tag]
            if not a:
                continue
            L.append(f"| {lbl} | {nm} | {a['cells']} | {a['n_total']:,} | {a['net_pos']} | {a['ratio_ge1']} | {a['ratio_ge2']} | "
                     f"{a['net_med']:+.3f} | {a['ratio_med']:+.3f} | {a['t_pos_sig']} | {a['t_neg_sig']} | "
                     f"{a['cost_sum']:,.0f} | **{a['yuan_sum']:,.0f}** |")
    L.append("")
    L.append("> 「总净盈亏」= Σ(每笔净点数 × 笔数 × 乘数)，1 手、5 品种、全窗口累计。**不是年化，不是复利**。")
    L.append("")

    oos_a = A["oos"]
    if oos_a.get("ratio_ge2", 0) >= 3 and oos_a.get("net_pos", 0) >= 4:
        vd = "PASS 形式（值得换干净样本再验）——但 15min 无干净样本，实际不可执行"
    elif oos_a.get("ratio_ge1", 0) > 0:
        vd = "FAIL / 薄利候补（OOS 有格 ≥1.0 但远不到 2.0 门槛）"
    else:
        vd = "FAIL 判死（OOS 全部毛/成本 < 1.0）"
    L.append(f"**预注册式判定（按体检工具既有门槛 `毛/成本 ≥ 2.0` 且过半品种）：{vd}**\n")
    L.append("### 关键读数：毛/成本 ≈ 0 意味着什么\n")
    L.append("| 窗口 | 毛/成本 中位 | 含义 |")
    L.append("|---|---:|---|")
    for tag, nm in (("full", "全样本 2023-2026"), ("is", "IS 2023-2024"), ("oos", "OOS 2025-2026")):
        a = A[tag]
        L.append(f"| {nm} | **{a['ratio_med']:+.3f}** | 毛利只有往返成本的 {a['ratio_med']*100:.0f}% |")
    L.append("")
    L.append("⇒ **止损、止盈、时间止损怎么设都不改变结论。** 它们只重新分配盈亏形状，")
    L.append("期望由毛决定；毛 ≈ 0 时，任何出场方案的净期望都 ≈ −成本。")
    L.append("本项目已于 `01_判死与已试清单` §1.2 #10/#11 用实测钉死过这一条：")
    L.append("毛 ≤ 0 的宿主上测出场 = 成本混杂，测不出方法优劣。\n")
    L.append("⚠️ **本报告只能判死，不能判生。** 15min 2023-2025 已被 288 格 × 5 年 + 逐年拆解全部跑过（2023-2024 落在旧 IS+OOS、2025 落在旧 OOS），")
    L.append("不存在干净样本外；任何「正」都不构成 edge 证据，只说明值得换数据源重验。\n")

    out = "\n".join(L)
    with open(os.path.join(_HERE, "体检_breakout2_15min.md"), "w", encoding="utf-8") as f:
        f.write(out + "\n")
    with open(os.path.join(_HERE, "体检_breakout2_15min.json"), "w", encoding="utf-8") as f:
        json.dump({"quality": quality, "verdict": vd, "agg_A": A, "agg_B": Bs,
                   "rows": rows}, f, ensure_ascii=False, indent=1, default=float)
    print(out)
    print("[done] 体检_breakout2_15min.md")


if __name__ == "__main__":
    main()
