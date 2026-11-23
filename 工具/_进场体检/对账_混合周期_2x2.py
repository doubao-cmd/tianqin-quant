# -*- coding: utf-8 -*-
"""
2×2 归因：混合周期实验的事后诊断（**非预注册**）
=================================================
预注册实验只跑了对角线两格：
  A = 时间 12根5min(60min)  × 价格 2×ATR(5min)
  B = 时间 12根15min(180min) × 价格 2×ATR(15min)
B 与 A 同时改了两处，所以无法归因。本脚本补另外两格，凑成 2×2：

              价格 ATR(5min)      价格 ATR(15min)
  持 60min      A（-2.687）        T12/B15
  持 180min     T36/B5             B（-3.076）

四格都用同一批 5min 进场信号，只改「持有多少根」和「ATR 取哪条序列」。
本脚本产物**只能作机制解释**，不得作为发现、不得据此择优。

用法：python 对账_混合周期_2x2.py
"""
import os
import sys
import json
import math

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import 进场体检 as mj  # noqa: E402
from 对账_混合周期 import nw_t, iid_t, load5, load15, align_atr15  # noqa: E402

DATA = os.path.join(_HERE, "数据")
OUT_MD = os.path.join(_HERE, "对账_混合周期_2x2.md")
OUT_JS = os.path.join(_HERE, "体检结果_混合周期_2x2.json")

KEYS = ["MA", "M", "RB", "V", "SA", "EB", "JD", "JM", "P", "RU", "SR", "EG"]
W_START = pd.Timestamp("2024-01-01")
W_END = pd.Timestamp("2026-11-05")
STOP_MULT = 2.0
# (标签, 持有分钟, 是否用 15min ATR)
CELLS = [("A_60_5", 60, False), ("T12_B15", 60, True),
         ("T36_B5", 180, False), ("B_180_15", 180, True)]


def run_cell(df5, df15, key, hold_min, use15):
    o = df5["open"].to_numpy(float)
    h = df5["high"].to_numpy(float)
    l = df5["low"].to_numpy(float)
    c = df5["close"].to_numpy(float)
    N = len(c)
    atr5 = mj._atr(h, l, c, mj.ATR_N)
    ma = mj._sma(c, mj.MA_N)
    sig = np.nan_to_num(mj.SIGNALS["retrace_ma13"][1](o, h, l, c, atr5, ma), nan=0.0)
    sig = np.where(np.isnan(atr5) | np.isnan(ma), 0.0, sig)

    atr_use = align_atr15(df5, df15, 5) if use15 else atr5
    hb = int(round(hold_min / 5.0))
    valid = np.arange(0, N - hb - 1)
    idx = valid[sig[valid] != 0]
    idx = idx[np.isfinite(atr_use[idx])]
    if len(idx) == 0:
        return None
    sgn = sig[idx]
    entry = o[idx + 1]
    cp = mj.cost_points(key)
    fwd = (o[idx + 1 + hb] - entry) * sgn
    stop = np.where(sgn > 0, entry - STOP_MULT * atr_use[idx], entry + STOP_MULT * atr_use[idx])
    exit_px = o[idx + 1 + hb].copy()
    hit = np.zeros(len(idx), bool)
    for j, (e_i, st, s) in enumerate(zip(idx, stop, sgn)):
        for jj in range(e_i + 1, e_i + 1 + hb):
            if s > 0:
                if l[jj] <= st:
                    exit_px[j] = min(st, o[jj]); hit[j] = True; break
            else:
                if h[jj] >= st:
                    exit_px[j] = max(st, o[jj]); hit[j] = True; break
    gross = (exit_px - entry) * sgn
    net = gross - cp
    dt = df5["datetime"].to_numpy()[idx]
    keep = (dt >= np.datetime64(W_START)) & (dt < np.datetime64(W_END))
    return {"dt": dt[keep], "net": net[keep], "gross": gross[keep],
            "fwd": fwd[keep], "hit": hit[keep], "cp": cp, "hb": hb}


def main():
    rows = []
    for k in KEYS:
        d5, d15 = load5(k), load15(k)
        if d5 is None or d15 is None:
            print(f"[skip] {k}")
            continue
        rec = {"key": k, "name": mj.cs.get(k)[0]}
        for tag, mins, use15 in CELLS:
            r = run_cell(d5, d15, k, mins, use15)
            if r is None:
                rec[tag] = None; continue
            rec[tag] = {
                "n": int(len(r["net"])),
                "net": float(r["net"].mean()),
                "gross": float(r["gross"].mean()),
                "l0": float(r["fwd"].mean()),
                "t": iid_t(r["net"]),
                "nw": nw_t(r["net"], r["hb"]),
                "stop": float(r["hit"].mean()),
                "cp": r["cp"],
            }
        rows.append(rec)
        print("[%s] %s" % (k, "  ".join(
            "%s:%+.2f" % (t, rec[t]["net"]) for t, _, _ in CELLS if rec.get(t))), flush=True)

    L = []
    L.append("# 2×2 归因（事后诊断，非预注册）\n")
    L.append("> 12 品种（剔除生猪）；2024-01-01 ~ 2026-11-04。")
    L.append("> 四格全部是 **5min 进场信号**，只改「持有根数」与「ATR 取哪条序列」。")
    L.append("> **只能作机制解释，不得当发现、不得据此择优。**\n")
    L.append("| 品种 | 60min/ATR5 (A) | 60min/ATR15 | 180min/ATR5 | 180min/ATR15 (B) |")
    L.append("|---|---:|---:|---:|---:|")
    for r in rows:
        L.append("| %s %s | %s |" % (r["key"], r["name"], " | ".join(
            ("%+.3f" % r[t]["net"]) if r.get(t) else "—" for t, _, _ in CELLS)))
    L.append("")

    L.append("## 主效应（12 品种中位，每笔净点）\n")
    def med(tag):
        v = [r[tag]["net"] for r in rows if r.get(tag)]
        return float(np.median(v)), sum(1 for x in v if x > 0), len(v)
    a = med("A_60_5"); b = med("B_180_15")
    L.append("| 格 | 时间 | 价格止损 | 净/笔中位 | 为正 |")
    L.append("|---|---|---|---:|---:|")
    for tag, tmin, use15 in CELLS:
        m, p, tot = med(tag)
        L.append("| %s | %d min | 2×ATR(%s) | **%+.3f** | %d/%d |" % (
            tag, tmin, "15min" if use15 else "5min", m, p, tot))
    L.append("")
    t12_5 = med("T12_B15")[0] - med("A_60_5")[0]      # 只换价格
    t12_15 = med("B_180_15")[0] - med("T36_B5")[0]    # 只换价格（在 180min 上）
    p60 = med("T36_B5")[0] - med("A_60_5")[0]         # 只换时间
    p180 = med("B_180_15")[0] - med("T12_B15")[0]     # 只换时间（在 ATR15 上）
    L.append("| 只改一处 | Δ 净/笔（中位口径） |")
    L.append("|---|---:|")
    L.append("| 价格 ATR5→ATR15（持 60min） | %+.3f |" % t12_5)
    L.append("| 价格 ATR5→ATR15（持 180min） | %+.3f |" % t12_15)
    L.append("| 时间 60→180min（用 ATR5） | %+.3f |" % p60)
    L.append("| 时间 60→180min（用 ATR15） | %+.3f |" % p180)
    L.append("")

    L.append("## 毛（L1，含止损）中位\n")
    L.append("| 格 | 毛/笔 中位 | 成本 中位 | 毛/成本 |")
    L.append("|---|---:|---:|---:|")
    for tag, tmin, use15 in CELLS:
        g = float(np.median([r[tag]["gross"] for r in rows if r.get(tag)]))
        c = float(np.median([r[tag]["cp"] for r in rows if r.get(tag)]))
        L.append("| %s | %+.3f | %.2f | %.3f |" % (tag, g, c, g / c if c else 0))
    L.append("")

    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    with open(OUT_JS, "w", encoding="utf-8") as f:
        json.dump({"rows": rows}, f, ensure_ascii=False, indent=1)
    print("\n[done]", OUT_MD)


if __name__ == "__main__":
    main()
