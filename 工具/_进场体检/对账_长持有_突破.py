# -*- coding: utf-8 -*-
r"""
突破型 × 长持有（T1）
================================================================================
预注册：`交接\突破型长持有_预注册_20261104.md`（21:52 落盘，判据未改）

唯一变量：信号（breakout_20 / tsmom_20）× 持有期（12 / 60 / 120）。
其余全部与 `对账_长持有.py` 逐位一致（同一 trades 构造、同一成本、同一 NW）。

★ 数据用指数合约 `数据_指数/`（主连换月 8.7% 跳空，hold=120 时 44.9% 交易跨换月）。
★ 无干净样本外 ⇒ 定位=诊断，不是验证。

用法：<tqsdk venv>\python.exe 对账_长持有_突破.py
产物：对账_长持有_突破.md / 体检结果_长持有_突破.json
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

SIGS = ["breakout_20", "tsmom_20"]
HOLDS = [12, 60, 120]
KEYS = ["M", "MA", "RB", "V", "SA", "EB", "JD", "JM", "P", "RU", "SR"]
EXTRA = ["LH"]
SEGS = [("2016-2020", "2016-01-01", "2021-01-01"),
        ("2021-2025", "2021-01-01", "2026-01-01")]
IDX_DIR = os.path.join(_HERE, "数据_指数")


def _nw_t(x, lag):
    v = np.asarray(x, float)
    v = v[~np.isnan(v)]
    n = len(v)
    if n < 10:
        return float("nan")
    e = v - v.mean()
    var = float(np.dot(e, e) / n)
    for k in range(1, min(lag, n - 1) + 1):
        gk = float(np.dot(e[k:], e[:-k]) / n)
        var += 2.0 * (1.0 - k / (lag + 1.0)) * gk
    se = math.sqrt(max(var, 1e-12) / n)
    return float(v.mean() / se) if se > 0 else float("nan")


def trades(key, sig_name, df, hold):
    """与 对账_长持有.trades 逐位一致，仅信号名参数化。"""
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    N = len(c)
    atr = mj._atr(h, l, c, mj.ATR_N)
    ma = mj._sma(c, mj.MA_N)
    sig = mj.SIGNALS[sig_name][1](o, h, l, c, atr, ma)
    bad = np.isnan(atr) | np.isnan(ma)
    sig = np.where(bad, 0.0, np.nan_to_num(sig, nan=0.0))
    valid = np.arange(0, N - hold - 1)
    idx = valid[sig[valid] != 0]
    if len(idx) == 0:
        return None
    sgn = sig[idx]
    entry = o[idx + 1]
    atr0 = atr[idx]
    stop = np.where(sgn > 0, entry - mj.STOP_ATR_MULT * atr0, entry + mj.STOP_ATR_MULT * atr0)
    ex = o[idx + 1 + hold].copy()
    hit = np.zeros(len(idx), bool)
    hit_day = np.zeros(len(idx), int)
    for j, (e_i, st, s) in enumerate(zip(idx, stop, sgn)):
        for k in range(1, hold + 1):
            jj = e_i + k
            if s > 0:
                if l[jj] <= st:
                    ex[j] = min(st, o[jj]); hit[j] = True; hit_day[j] = k; break
            else:
                if h[jj] >= st:
                    ex[j] = max(st, o[jj]); hit[j] = True; hit_day[j] = k; break
    return dict(idx=idx, sgn=sgn, entry=entry, atr=atr0, ex=ex, hit=hit, hit_day=hit_day,
                fwd=(o[idx + 1 + hold] - entry) * sgn,
                gross1=(ex - entry) * sgn,
                dt=df["datetime"].to_numpy()[idx])


def daily_series(tr, hold, N):
    d = np.zeros(N)
    for j, e_i in enumerate(tr["idx"]):
        days = int(tr["hit_day"][j]) if tr["hit"][j] else hold
        days = max(days, 1)
        d[e_i + 1: e_i + 1 + days] += tr["_net"][j] / days
    return d


def stats_of(key, sig_name, df, hold, t_lo=None, t_hi=None):
    mj.HOLD = hold
    r = mj.screen_one(key, sig_name, df, t_lo=t_lo, t_hi=t_hi)
    if r["n"] < 10:
        return r, None
    tr = trades(key, sig_name, df, hold)
    if tr is None:
        return r, None
    dt = pd.to_datetime(tr["dt"])
    keep = np.ones(len(tr["idx"]), bool)
    if t_lo is not None:
        keep &= dt >= pd.Timestamp(t_lo)
    if t_hi is not None:
        keep &= pd.to_datetime(df["datetime"].to_numpy()[tr["idx"] + 1 + hold]) < pd.Timestamp(t_hi)
    assert abs(float(np.mean(tr["gross1"][keep])) - r["L1_gross"]) < 5e-3, (key, sig_name, hold, "gross")
    assert abs(float(np.mean(tr["fwd"][keep])) - r["L0_gross"]) < 5e-3, (key, sig_name, hold, "fwd")
    net = tr["gross1"] - r["cost_pts"]
    tr2 = {k: (v[keep] if isinstance(v, np.ndarray) and len(v) == len(keep) else v)
           for k, v in tr.items()}
    tr2["_net"] = net[keep]
    n = int(keep.sum())
    if n < 10:
        return r, None
    r["n"] = n
    r["L1_net"] = round(float(np.mean(tr2["_net"])), 3)
    r["L1_gross"] = round(float(np.mean(tr["gross1"][keep])), 3)
    r["L0_gross"] = round(float(np.mean(tr["fwd"][keep])), 3)
    r["stop_share"] = round(float(np.mean(tr["hit"][keep])), 4)
    r["hold_days"] = round(float(np.mean(np.where(tr["hit"][keep], tr["hit_day"][keep], hold))), 2)
    r["nw_t"] = round(_nw_t(tr2["_net"], hold), 2)
    r["iid_t"] = r.get("L1_t")
    # tr2 内的 ndarray 已按 keep 过滤过 ⇒ 此处不得再索引一次
    r["L0_nw_t"] = round(_nw_t(tr2["fwd"], hold), 2)
    dd = daily_series(tr2, hold, len(df))
    sd = float(np.std(dd, ddof=1))
    r["daily_sharpe"] = round(float(np.mean(dd) / sd * math.sqrt(242)), 3) if sd > 1e-12 else None
    r["daily_nw_t"] = round(_nw_t(dd, hold), 2)
    r["long_share"] = round(float(np.mean(tr2["sgn"] > 0)), 3)
    return r, tr2


def load_idx(k):
    fs = sorted(glob.glob(os.path.join(IDX_DIR, "%s_1440min_*.csv" % k)))
    if not fs:
        return None
    d = pd.read_csv(fs[-1], encoding="utf-8-sig", parse_dates=["datetime"])
    return d.sort_values("datetime").reset_index(drop=True)


def main():
    data, qual = {}, []
    for k in KEYS + EXTRA:
        d = load_idx(k)
        if d is None:
            continue
        data[k] = d
        o, h, l, c = (d[x].to_numpy(float) for x in ("open", "high", "low", "close"))
        deg = float(np.mean((o == h) & (h == l) & (l == c)))
        qual.append((k, len(d), str(d["datetime"].iloc[0])[:10], str(d["datetime"].iloc[-1])[:10], deg))
    print("数据就绪 %d 品种（指数日线）" % len(data), flush=True)

    results = {}
    for s in SIGS:
        results[s] = {}
        for k in data:
            results[s][k] = {}
            for hold in HOLDS:
                r, _ = stats_of(k, s, data[k], hold)
                results[s][k][hold] = r
                print("  %-12s %-3s hold=%-3d n=%-5d L0=%8.2f L1net=%8.2f cost=%.2f ratio=%-6s "
                      "stop=%.0f%% 持天=%.1f nw_t=%6.2f" % (
                          s, k, hold, r["n"], r["L0_gross"], r["L1_net"], r["cost_pts"],
                          r.get("ratio"), r["stop_share"] * 100, r.get("hold_days") or 0,
                          r.get("nw_t") or 0), flush=True)

    seg_res = {}
    for s in SIGS:
        seg_res[s] = {}
        for name, lo, hi in SEGS:
            seg_res[s][name] = {}
            for k in data:
                seg_res[s][name][k] = {}
                for hold in HOLDS:
                    r, _ = stats_of(k, s, data[k], hold, t_lo=lo, t_hi=hi)
                    seg_res[s][name][k][hold] = r

    # ---------- 判定（预注册 §四，取 HOLD=120）----------
    judge = {}
    for s in SIGS:
        judge[s] = {}
        for hold in HOLDS:
            rows = [results[s][k][hold] for k in KEYS if k in results[s]]
            ok = [r for r in rows if r["n"] >= 30]
            l0 = [r["L0_gross"] for r in ok]
            nets = [r["L1_net"] for r in ok]
            ratios = [r["ratio"] for r in ok if r.get("ratio") is not None]
            judge[s][hold] = dict(
                n_sym=len(ok),
                l0_median=round(float(np.median(l0)), 3) if l0 else None,
                l0_pos=sum(1 for x in l0 if x > 0),
                net_median=round(float(np.median(nets)), 3) if nets else None,
                net_pos=sum(1 for x in nets if x > 0),
                ratio_median=round(float(np.median(ratios)), 3) if ratios else None,
                stop_median=round(float(np.median([r["stop_share"] for r in ok])), 3) if ok else None,
                holddays_median=round(float(np.median([r["hold_days"] for r in ok])), 1) if ok else None,
                nw_t_median=round(float(np.median([r["nw_t"] for r in ok if r.get("nw_t") is not None])), 2),
                nw_t_max=round(float(np.max([r["nw_t"] for r in ok if r.get("nw_t") is not None])), 2) if ok else None,
                l0_nw_t_median=round(float(np.median([r["L0_nw_t"] for r in ok if r.get("L0_nw_t") is not None])), 2),
            )

    out = dict(period="1440min-index", signals=SIGS, holds=HOLDS, keys=KEYS, extra=EXTRA,
               quality=[dict(key=q[0], n=q[1], start=q[2], end=q[3], deg=q[4]) for q in qual],
               results={s: {k: {str(h): v for h, v in d.items()} for k, d in dd.items()}
                        for s, dd in results.items()},
               judge={s: {str(h): v for h, v in d.items()} for s, d in judge.items()},
               seg={s: {nm: {k: {str(h): v for h, v in d.items()} for k, d in kk.items()}
                        for nm, kk in ss.items()} for s, ss in seg_res.items()})
    jf = os.path.join(_HERE, "体检结果_长持有_突破.json")
    with open(jf, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=float)

    # ---------- 报告 ----------
    L = []
    A = L.append
    A("# 突破型 × 长持有（HOLD 12 / 60 / 120）· 指数日线\n")
    A("> 预注册：`交接\\突破型长持有_预注册_20261104.md`（21:52 落盘，判据未改）  ")
    A("> 数据：tqsdk `KQ.i@` 指数日线，退化 0.00%  ")
    A("> 判定池 **11 品种（剔 LH）**；LH 单列不参与判定（2021-01 上市）  ")
    A("> ⚠️ **无干净样本外**（2016-2025 已被 HOLD=12 的 288 格扫过）⇒ 定位=诊断，不是验证。\n")
    A("## 〇、数据清单\n")
    A("| 品种 | n | 起点 | 终点 | 退化 |")
    A("|---|---:|---|---|---:|")
    for q in qual:
        A("| %s | %d | %s | %s | %.2f%% |" % (q[0], q[1], q[2], q[3], q[4] * 100))
    A("")
    for s in SIGS:
        A("## %s · %s\n" % ("一" if s == SIGS[0] else "二", s + " — " + mj.SIGNALS[s][0]))
        for hold in HOLDS:
            A("### HOLD = %d 日\n" % hold)
            A("| 品种 | n | **L0 毛（无止损）** | L1 净 | L1 毛 | 成本 | 毛/成本 | 止损率 | 实际持天 | NW t | 日频Sh | 多头占比 |")
            A("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
            for k in KEYS + EXTRA:
                if k not in results[s]:
                    continue
                r = results[s][k][hold]
                A("| %s | %d | **%.2f** | %.2f | %.2f | %.2f | %s | %.0f%% | %.1f | %s | %s | %.0f%% |" % (
                    ("LH*" if k in EXTRA else k), r["n"], r["L0_gross"], r["L1_net"], r["L1_gross"],
                    r["cost_pts"], ("%.2f" % r["ratio"]) if r.get("ratio") is not None else "—",
                    r["stop_share"] * 100, r.get("hold_days") or 0,
                    ("%.2f" % r["nw_t"]) if r.get("nw_t") is not None else "—",
                    ("%.2f" % r["daily_sharpe"]) if r.get("daily_sharpe") is not None else "—",
                    (r.get("long_share") or 0) * 100))
            j = judge[s][hold]
            A("")
            A("**汇总（11 品种）**：**L0 毛中位 %.2f**（为正 %d/11）；L1 净中位 %.2f（为正 %d/11）；"
              "毛/成本中位 %s；止损率中位 %.0f%%；实际持天中位 %.1f；NW t 中位 %.2f（max %.2f）\n" % (
                  j["l0_median"], j["l0_pos"], j["net_median"], j["net_pos"],
                  ("%.2f" % j["ratio_median"]) if j["ratio_median"] is not None else "—",
                  (j["stop_median"] or 0) * 100, j["holddays_median"] or 0,
                  j["nw_t_median"], j["nw_t_max"] or 0))

    A("## 三、单调性诊断（趋势的指纹：持有越久毛越大？）\n")
    A("| 信号 | h=12 L0 毛 | h=60 L0 毛 | h=120 L0 毛 | 方向 |")
    A("|---|---:|---:|---:|---|")
    for s in SIGS:
        v = [judge[s][h]["l0_median"] for h in HOLDS]
        mono = "✅ 单调改善" if v[0] < v[1] < v[2] else ("❌ 单调恶化" if v[0] > v[1] > v[2] else "— 非单调")
        A("| %s | %.2f | %.2f | %.2f | %s |" % (s, v[0], v[1], v[2], mono))
    A("")
    A("> 对照：`retrace_ma13` 的 L0 毛为 **+9.58 → −34.56 → −38.60**（单调恶化）。\n")

    A("## 四、分段（符号是否翻）\n")
    for s in SIGS:
        A("### %s\n" % s)
        A("| 品种 | 16-20 h12 | 16-20 h60 | 21-25 h12 | 21-25 h60 |")
        A("|---|---:|---:|---:|---:|")
        for k in KEYS + EXTRA:
            if k not in seg_res[s]["2016-2020"]:
                continue
            a = seg_res[s]["2016-2020"][k]
            b = seg_res[s]["2021-2025"][k]
            A("| %s | %.2f | %.2f | %.2f | %.2f |" % (
                ("LH*" if k in EXTRA else k),
                a[12]["L1_net"], a[60]["L1_net"], b[12]["L1_net"], b[60]["L1_net"]))
        A("")

    A("## 五、判定（按预注册 §四，取 HOLD=120）\n")
    for s in SIGS:
        j = judge[s][120]
        cA = (j["l0_median"] or -1) > 0
        cB = j["l0_pos"] >= 7
        cC = (j["net_median"] or -1) > 0
        cD = (j["ratio_median"] or 0) >= 3.0
        A("### %s\n" % s)
        A("| # | 条件 | 阈值 | 实测 | 结果 |")
        A("|:-:|---|---|---|---|")
        A("| A | L0 毛中位（无止损，120 日）> 0 | > 0 | **%.2f** | %s |" % (j["l0_median"], "✅" if cA else "❌"))
        A("| B | L0 毛为正品种数 | ≥7/11 | **%d/11** | %s |" % (j["l0_pos"], "✅" if cB else "❌"))
        A("| C | L1 净/笔中位 > 0 | > 0 | **%.2f** | %s |" % (j["net_median"], "✅" if cC else "❌"))
        A("| D | 毛/成本中位 | ≥3.0 | **%s** | %s |" % (
            ("%.2f" % j["ratio_median"]) if j["ratio_median"] is not None else "—",
            "✅" if cD else "❌"))
        A("")
        if cA and cB and cC and cD:
            A("⇒ **PASS：有候选资格**（不是「有效」；须独立区间 + 预注册前向）。\n")
        else:
            A("⇒ **FAIL**：%s\n" % (
                "A 条不过 ⇒ **日线尺度无方向性（与进场形态无关）**，可作跨形态判死证据。"
                if not cA else "部分条件不过，不具候选资格。"))
    A("⚠️ **功效声明**：T=10 年、K=2 ⇒ MDE(Sharpe) ≈ **0.974**。"
      "**NW t < 2.24 时不得写「证伪」，只能写「测不出」**——唯一例外是 A 条（L0 毛 ≤ 0 是点估计，不需功效）。\n")

    md = os.path.join(_HERE, "对账_长持有_突破.md")
    with open(md, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("\n产物：%s\n      %s" % (md, jf))
    print("\n【判定 HOLD=120】")
    for s in SIGS:
        j = judge[s][120]
        print("  %-12s L0毛中位=%8.2f (正%d/11) 净中位=%8.2f (正%d/11) 毛/成本=%s 止损=%.0f%% nw_t中位=%.2f"
              % (s, j["l0_median"], j["l0_pos"], j["net_median"], j["net_pos"],
                 ("%.2f" % j["ratio_median"]) if j["ratio_median"] is not None else "—",
                 (j["stop_median"] or 0) * 100, j["nw_t_median"]))


if __name__ == "__main__":
    main()
