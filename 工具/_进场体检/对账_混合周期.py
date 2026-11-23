# -*- coding: utf-8 -*-
"""
混合周期验证：5min 进场 × {5min | 15min} 出场口径
=================================================
预注册：C:\\Users\\cc\\Desktop\\上下文交接\\交接\\混合周期_5min进场15min出场_预注册_20261104.md

三组对照（见预注册 §五）：
  A  5min 进场 + 2×ATR14(5min)  + 12 根 5min   ← 基准
  B  5min 进场 + 2×ATR14(15min) + 12 根 15min  ← 本次假设
  C  15min 进场 + 2×ATR14(15min) + 12 根 15min ← 已有的 15min 那批

口径要点
--------
· 进场价 = 信号 bar 的**下一根 open**（禁未来函数）
· ATR(15min) 必须取「信号 bar 收盘时**已可知**的最后一根 15min bar」——
  15min 的 datetime 是 bar **开始**时间，故可知时刻 = datetime + 15min。
· 时间止损 = N 根 15min = 3N 根 5min（同一段绝对时间）
· 止损触发逐根检查 5min 的 low/high，跳空按 min(止损价, 开盘) 成交
· 分年独立统计（2024 / 2025 / 2026）

用法：
  python 对账_混合周期.py
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

DATA = os.path.join(_HERE, "数据")
OUT_MD = os.path.join(_HERE, "对账_混合周期.md")
OUT_JS = os.path.join(_HERE, "体检结果_混合周期.json")

KEYS = ["MA", "M", "RB", "V", "SA", "EB", "JD", "JM", "P", "RU", "SR", "EG"]  # 剔除 LH
YEARS = [2024, 2025, 2026]
W_START = pd.Timestamp("2024-01-01")
W_END = pd.Timestamp("2026-11-05")
HOLD15 = 12          # 12 根 15min = 180 分钟
STOP_MULT = 2.0


# ------------------------------------------------------------------ 工具
def nw_t(x, lag):
    """Newey-West(Bartlett) t 值。x = 按进场时间排序的每笔收益。"""
    x = np.asarray(x, float)
    n = len(x)
    if n < 3:
        return float("nan")
    xm = x - x.mean()
    g0 = float(np.dot(xm, xm) / n)
    var = g0
    for l in range(1, min(lag, n - 1) + 1):
        w = 1.0 - l / (lag + 1.0)
        gl = float(np.dot(xm[l:], xm[:-l]) / n)
        var += 2.0 * w * gl
    if var <= 0:
        return float("nan")
    return float(x.mean() / math.sqrt(var / n))


def iid_t(x):
    x = np.asarray(x, float)
    if len(x) < 2:
        return float("nan")
    sd = x.std(ddof=1)
    return float(x.mean() / (sd / math.sqrt(len(x)))) if sd > 1e-12 else 0.0


def load5(key):
    p = os.path.join(DATA, f"{key}_5min_2024-01-01_2026-11-04.csv")
    if not os.path.exists(p):
        return None
    return pd.read_csv(p, encoding="utf-8-sig", parse_dates=["datetime"])


def load15(key):
    p = os.path.join(DATA, f"{key}_15min_2024-01-01_2026-11-04.csv")
    if not os.path.exists(p):
        return None
    return pd.read_csv(p, encoding="utf-8-sig", parse_dates=["datetime"])


def align_atr15(df5, df15, bar_min=5):
    """把 15min ATR 对齐到**进场周期**的 bar（用该 bar 收盘时已可知的信息）。

    返回长度 = len(df5) 的数组；无效处为 nan。
    15min bar 的可知时刻 = datetime + 15min（datetime 是 bar 开始）；
    进场 bar（5min 或 15min）的可知时刻 = datetime + bar_min。
    """
    h, l, c = df15["high"].to_numpy(float), df15["low"].to_numpy(float), df15["close"].to_numpy(float)
    atr15 = mj._atr(h, l, c, mj.ATR_N)
    known15 = (df15["datetime"] + pd.Timedelta(minutes=15)).to_numpy()
    cut5 = (df5["datetime"] + pd.Timedelta(minutes=bar_min)).to_numpy()
    pos = np.searchsorted(known15, cut5, side="right") - 1
    out = np.full(len(df5), np.nan)
    ok = pos >= 0
    out[ok] = atr15[pos[ok]]
    return out


# ------------------------------------------------------------------ 核心
def run_mixed(df5, df15, keys_tag, atr15_mode, hold_min, bar_min=5):
    """在 5min 上生成信号，按指定口径出场。

    atr15_mode: True 用 15min ATR，False 用 5min ATR
    hold_min  : 时间止损的绝对分钟数（5min 口径=60，15min 口径=180）
    """
    o = df5["open"].to_numpy(float)
    h = df5["high"].to_numpy(float)
    l = df5["low"].to_numpy(float)
    c = df5["close"].to_numpy(float)
    N = len(c)
    atr5 = mj._atr(h, l, c, mj.ATR_N)
    ma = mj._sma(c, mj.MA_N)
    sig = mj.SIGNALS["retrace_ma13"][1](o, h, l, c, atr5, ma)
    bad = np.isnan(atr5) | np.isnan(ma)
    sig = np.where(bad, 0.0, np.nan_to_num(sig, nan=0.0))

    atr_use = align_atr15(df5, df15, bar_min) if atr15_mode else atr5
    hold_bars = int(round(hold_min / float(bar_min)))   # 进场周期的根数

    valid = np.arange(0, N - hold_bars - 1)
    idx = valid[sig[valid] != 0]
    # ATR 无效的丢掉（15min 预热期）
    idx = idx[np.isfinite(atr_use[idx])]
    if len(idx) == 0:
        return None

    sgn = sig[idx]
    entry = o[idx + 1]
    cp = mj.cost_points(keys_tag)

    # L0：固定持有期、无止损
    fwd = (o[idx + 1 + hold_bars] - entry) * sgn
    # L1：固定止损
    stop = np.where(sgn > 0, entry - STOP_MULT * atr_use[idx], entry + STOP_MULT * atr_use[idx])
    exit_px = o[idx + 1 + hold_bars].copy()
    hit = np.zeros(len(idx), dtype=bool)
    for j, (e_i, st, s) in enumerate(zip(idx, stop, sgn)):
        for jj in range(e_i + 1, e_i + 1 + hold_bars):
            if s > 0:
                if l[jj] <= st:
                    exit_px[j] = min(st, o[jj]); hit[j] = True; break
            else:
                if h[jj] >= st:
                    exit_px[j] = max(st, o[jj]); hit[j] = True; break
    gross = (exit_px - entry) * sgn
    net = gross - cp

    dt_in = df5["datetime"].to_numpy()[idx]
    return {
        "dt": dt_in, "net": net, "gross": gross, "fwd": fwd,
        "hit": hit, "cp": cp, "hold_bars": hold_bars,
    }


def agg_year(res, y, lag):
    m = (res["dt"] >= np.datetime64(pd.Timestamp(f"{y}-01-01"))) & \
        (res["dt"] < np.datetime64(pd.Timestamp(f"{y + 1}-01-01")))
    x = res["net"][m]
    if len(x) == 0:
        return None
    return {
        "n": int(len(x)),
        "net": float(x.mean()),
        "t": iid_t(x),
        "nw": nw_t(x, lag),
        "win": float((x > 0).mean()),
        "stop": float(res["hit"][m].mean()),
        "gross": float(res["gross"][m].mean()),
        "l0": float(res["fwd"][m].mean()),
    }


def main():
    rows = []
    for k in KEYS:
        d5 = load5(k)
        d15 = load15(k)
        if d5 is None:
            print(f"[skip] {k} 无 5min 数据")
            continue
        print(f"[{k}] 5min {len(d5)} 根 {d5['datetime'].iloc[0]} ~ {d5['datetime'].iloc[-1]}", flush=True)
        rec = {"key": k, "name": mj.cs.get(k)[0], "n5": len(d5),
               "n15": len(d15) if d15 is not None else 0}
        # A=5min进场+5min出场  B=5min进场+15min出场  C=15min进场+15min出场
        for tag, src, use15, mins, bmin in (("A", d5, False, 60, 5),
                                            ("B", d5, True, 180, 5),
                                            ("C", d15, True, 180, 15)):
            if src is None or (use15 and d15 is None):
                rec[tag] = None
                continue
            r = run_mixed(src, d15, k, use15, mins, bmin)
            if r is None:
                rec[tag] = None
                continue
            # 只保留窗口内的成交
            keep = (r["dt"] >= np.datetime64(W_START)) & (r["dt"] < np.datetime64(W_END))
            for kk in ("dt", "net", "gross", "fwd", "hit"):
                r[kk] = r[kk][keep]
            sub = {"n": int(len(r["net"])), "net_all": float(r["net"].mean()) if len(r["net"]) else 0,
                   "t_all": iid_t(r["net"]), "nw_all": nw_t(r["net"], r["hold_bars"]),
                   "years": {}}
            for y in YEARS:
                a = agg_year(r, y, r["hold_bars"])
                if a:
                    sub["years"][str(y)] = a
            rec[tag] = sub
            print("   %s: n=%5d 净%+.3f t=%+.2f NW=%+.2f | %s" % (
                tag, sub["n"], sub["net_all"], sub["t_all"], sub["nw_all"],
                " ".join(f"{y}:{v['net']:+.2f}" for y, v in sub["years"].items())), flush=True)
        rows.append(rec)

    # ---------------- 输出 ----------------
    L = []
    L.append("# 混合周期验证：5min 进场 × 15min 出场口径\n")
    L.append("> 预注册：`交接\\混合周期_5min进场15min出场_预注册_20261104.md`（跑前落盘）")
    L.append("> 品种 12 个（**剔除生猪 LH**）；窗口 2024-01-01 ~ 2026-11-04，分年独立报。")
    L.append("> 2026 数据截至 2026-11-04，不足一整年。\n")
    L.append("## 一、三年合并（每笔净，点）\n")
    L.append("| 品种 | n(A) | A 全5min | B 5进15出 | B t | B NW | B−A | C 15进15出 |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for r in rows:
        a, b, c = r.get("A"), r.get("B"), r.get("C")
        if not a or not b:
            continue
        L.append("| %s %s | %d | %+.3f | **%+.3f** | %+.2f | %+.2f | %+.3f | %s |" % (
            r["key"], r["name"], b["n"], a["net_all"],
            b["net_all"], b["t_all"], b["nw_all"], b["net_all"] - a["net_all"],
            ("%+.3f" % c["net_all"]) if c else "—"))
    L.append("")

    L.append("## 二、分年（每笔净，点）\n")
    L.append("| 品种 | 组 | " + " | ".join(str(y) for y in YEARS) + " | 三年合并 |")
    L.append("|---|---|" + "---:|" * len(YEARS) + "---:|")
    for r in rows:
        for tag, lab in (("A", "A 5进5出"), ("B", "B 5进15出"), ("C", "C 15进15出")):
            s = r.get(tag)
            if not s:
                continue
            cells = []
            for y in YEARS:
                v = s["years"].get(str(y))
                cells.append("%+.2f (n=%d)" % (v["net"], v["n"]) if v else "—")
            L.append("| %s | %s | %s | %+.2f |" % (r["key"], lab, " | ".join(cells), s["net_all"]))
    L.append("")

    # 汇总
    L.append("## 三、汇总判定\n")
    for tag, lab in (("A", "A 全 5min 口径"), ("B", "B 5min 进场 + 15min 出场"), ("C", "C 全 15min 口径")):
        nets = [r[tag]["net_all"] for r in rows if r.get(tag)]
        if not nets:
            continue
        pos = sum(1 for x in nets if x > 0)
        L.append("**%s**：%d 品种净/笔中位 %+.3f，为正 %d/%d" % (lab, len(nets), float(np.median(nets)), pos, len(nets)))
        for y in YEARS:
            vals = [r[tag]["years"][str(y)]["net"] for r in rows if r.get(tag) and str(y) in r[tag]["years"]]
            if vals:
                L.append("  - %d：中位 %+.3f，为正 %d/%d" % (y, float(np.median(vals)), sum(1 for x in vals if x > 0), len(vals)))
        L.append("")

    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    with open(OUT_JS, "w", encoding="utf-8") as f:
        json.dump({"keys": KEYS, "years": YEARS, "rows": rows}, f, ensure_ascii=False, indent=1)
    print("\n[done]", OUT_MD)


if __name__ == "__main__":
    main()
