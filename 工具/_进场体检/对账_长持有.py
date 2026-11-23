# -*- coding: utf-8 -*-
r"""
长持有检验：HOLD 12 → 60 / 120（预注册见 `交接\长持有检验_预注册_20261104.md`）
================================================================================
唯一改动：`HOLD`。信号仍是 `retrace_ma13`（= 06 的进场条件），止损仍 2×ATR，成本不变。

★ 数据用**指数合约**（`数据_指数/`）而非主连，理由见 `_诊断_换月污染.py`：
   主连换月处平均跳空 8.7%（JD 34.4%），hold=60 时 25.8% 的交易跨换月 ⇒ 结果必然是噪声。

★ 本批数据**没有干净样本外**（2016-2025 已被 hold=12 的 288 格扫过）⇒ 定位=诊断，不是验证。

用法：<tqsdk venv>\python.exe 对账_长持有.py
产物：对账_长持有.md / 体检结果_长持有.json
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

SIG = "retrace_ma13"
HOLDS = [12, 60, 120]
# 判定池 11（剔 LH）；LH 单列不参与判定（2021-01 上市，样本仅 5 年）
KEYS = ["M", "MA", "RB", "V", "SA", "EB", "JD", "JM", "P", "RU", "SR"]
EXTRA = ["LH"]
SEGS = [("full", None, None),
        ("2016-2020", "2016-01-01", "2021-01-01"),
        ("2021-2025", "2021-01-01", "2026-01-01")]
IDX_DIR = os.path.join(_HERE, "数据_指数")


# ------------------------------------------------------------------ 工具
def _nw_t(x, lag):
    """Newey-West(Bartlett) t 值。x 允许含 NaN（先剔除）。"""
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
    """复刻 screen_one 的成交构造（参数化 hold；原版在 对账_日线_构成.py 用模块全局 HOLD）。"""
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
    """把每笔已实现净收益按「实际持有天数」均摊到日频，用于算组合 Sharpe / 日频 NW。"""
    d = np.zeros(N)
    for j, e_i in enumerate(tr["idx"]):
        days = int(tr["hit_day"][j]) if tr["hit"][j] else hold
        days = max(days, 1)
        seg = tr["_net"][j] / days
        d[e_i + 1: e_i + 1 + days] += seg
    return d


def stats_of(key, df, hold, t_lo=None, t_hi=None):
    mj.HOLD = hold
    r = mj.screen_one(key, SIG, df, t_lo=t_lo, t_hi=t_hi)
    if r["n"] < 10:
        return r, None
    tr = trades(key, SIG, df, hold)
    if tr is None:
        return r, None
    # 窗口筛（与 screen_one 同规则：进场 bar >= t_lo，出场 bar < t_hi）
    dt = pd.to_datetime(tr["dt"])
    keep = np.ones(len(tr["idx"]), bool)
    if t_lo is not None:
        keep &= dt >= pd.Timestamp(t_lo)
    if t_hi is not None:
        keep &= pd.to_datetime(df["datetime"].to_numpy()[tr["idx"] + 1 + hold]) < pd.Timestamp(t_hi)
    # 对齐断言：trades（窗口内）与 screen_one 必须逐位一致
    assert abs(float(np.mean(tr["gross1"][keep])) - r["L1_gross"]) < 5e-3, (key, hold, "gross")
    assert abs(float(np.mean(tr["fwd"][keep])) - r["L0_gross"]) < 5e-3, (key, hold, "fwd")
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
    dd = daily_series(tr2, hold, len(df))
    sd = float(np.std(dd, ddof=1))
    r["daily_sharpe"] = round(float(np.mean(dd) / sd * math.sqrt(242)), 3) if sd > 1e-12 else None
    r["daily_nw_t"] = round(_nw_t(dd, hold), 2)
    return r, tr2


def load_idx(k):
    fs = sorted(glob.glob(os.path.join(IDX_DIR, "%s_1440min_*.csv" % k)))
    if not fs:
        return None
    d = pd.read_csv(fs[-1], encoding="utf-8-sig", parse_dates=["datetime"])
    d = d.sort_values("datetime").reset_index(drop=True)
    return d


def main():
    data = {}
    qual = []
    for k in KEYS + EXTRA:
        d = load_idx(k)
        if d is None:
            continue
        data[k] = d
        o, h, l, c = (d[x].to_numpy(float) for x in ("open", "high", "low", "close"))
        deg = float(np.mean((o == h) & (h == l) & (l == c)))
        qual.append((k, len(d), str(d["datetime"].iloc[0])[:10], str(d["datetime"].iloc[-1])[:10], deg))
    print("数据就绪 %d 品种（指数合约）" % len(data))

    results = {}
    for k in data:
        results[k] = {}
        for hold in HOLDS:
            r, _ = stats_of(k, data[k], hold)
            results[k][hold] = r
            print("  %-3s hold=%-3d n=%-4d net=%8.2f gross=%8.2f cost=%.2f ratio=%-6s stop=%.0f%% "
                  "nw_t=%6.2f dailySh=%-6s 持天=%.1f" % (
                      k, hold, r["n"], r["L1_net"], r["L1_gross"], r["cost_pts"],
                      r.get("ratio"), r["stop_share"] * 100, r.get("nw_t") or 0,
                      r.get("daily_sharpe"), r.get("hold_days") or 0))

    # 分段
    seg_res = {}
    for name, lo, hi in SEGS[1:]:
        seg_res[name] = {}
        for k in data:
            seg_res[name][k] = {}
            for hold in HOLDS:
                r, _ = stats_of(k, data[k], hold, t_lo=lo, t_hi=hi)
                seg_res[name][k][hold] = r

    # ---------- 判定 ----------
    judge = {}
    for hold in HOLDS:
        rows = [results[k][hold] for k in KEYS if k in results]
        ok = [r for r in rows if r["n"] >= 30]
        nets = [r["L1_net"] for r in ok]
        ratios = [r["ratio"] for r in ok if r.get("ratio") is not None]
        judge[hold] = dict(
            n_sym=len(ok),
            net_median=round(float(np.median(nets)), 3) if nets else None,
            net_mean=round(float(np.mean(nets)), 3) if nets else None,
            pos=sum(1 for x in nets if x > 0),
            ratio_median=round(float(np.median(ratios)), 3) if ratios else None,
            stop_median=round(float(np.median([r["stop_share"] for r in ok])), 3) if ok else None,
            holddays_median=round(float(np.median([r["hold_days"] for r in ok])), 1) if ok else None,
            nw_t_median=round(float(np.median([r["nw_t"] for r in ok if r.get("nw_t") is not None])), 2),
        )

    # ---------- 输出 ----------
    out = dict(period="1440min-index", signal=SIG, holds=HOLDS, keys=KEYS, extra=EXTRA,
               quality=[dict(key=q[0], n=q[1], start=q[2], end=q[3], deg=q[4]) for q in qual],
               results={k: {str(h): v for h, v in d.items()} for k, d in results.items()},
               judge={str(h): v for h, v in judge.items()},
               seg={nm: {k: {str(h): v for h, v in d.items()} for k, d in dd.items()}
                    for nm, dd in seg_res.items()})
    jf = os.path.join(_HERE, "体检结果_长持有.json")
    with open(jf, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=float)

    lines = []
    A = lines.append
    A("# 长持有检验（HOLD 12 / 60 / 120）· retrace_ma13 · 指数日线\n")
    A("> 预注册：`交接\\长持有检验_预注册_20261104.md`（21:55 落盘，判据未改）  ")
    A("> 数据：tqsdk `KQ.i@` 指数日线，2016-01-04~2025-12-30，退化 0.00%  ")
    A("> 判定池 **11 品种（剔 LH）**；LH 单列不参与判定（2021-01 上市，样本仅 5 年）  ")
    A("> ⚠️ **本批数据没有干净样本外**（2016-2025 已被 hold=12 的 288 格扫过）⇒ 定位=诊断，不是验证。\n")
    A("## 〇、数据清单\n")
    A("| 品种 | n | 起点 | 终点 | 退化 |")
    A("|---|---:|---|---|---:|")
    for q in qual:
        A("| %s | %d | %s | %s | %.2f%% |" % (q[0], q[1], q[2], q[3], q[4] * 100))
    A("")
    A("## 一、主表：每品种 × 持有期\n")
    A("口径：L1 = 2×ATR 止损；净 = 毛 − 往返成本（1 跳滑点 + COST 表）；`nw_t` = Newey-West(lag=HOLD)；"
      "`日频Sh` = 把每笔净收益按实际持有天数摊到日频后的年化 Sharpe。\n")
    for hold in HOLDS:
        A("### HOLD = %d 日\n" % hold)
        A("| 品种 | n | 净/笔 | 毛/笔 | 成本 | 毛/成本 | 止损率 | 实际持天 | NW t | 日频Sh | 档 |")
        A("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|:-:|")
        for k in KEYS + EXTRA:
            if k not in results:
                continue
            r = results[k][hold]
            tag = "LH*" if k in EXTRA else k
            A("| %s | %d | %.2f | %.2f | %.2f | %s | %.0f%% | %.1f | %s | %s | %s |" % (
                tag, r["n"], r["L1_net"], r["L1_gross"], r["cost_pts"],
                ("%.2f" % r["ratio"]) if r.get("ratio") is not None else "—",
                r["stop_share"] * 100, r.get("hold_days") or 0,
                ("%.2f" % r["nw_t"]) if r.get("nw_t") is not None else "—",
                ("%.2f" % r["daily_sharpe"]) if r.get("daily_sharpe") is not None else "—",
                mj.grade(r)))
        j = judge[hold]
        A("")
        A("**汇总（11 品种）**：净/笔中位 **%.2f**、均值 **%.2f**、为正 **%d/11**；"
          "毛/成本中位 **%s**；止损率中位 **%.0f%%**；实际持天中位 **%.1f**；NW t 中位 **%.2f**\n" % (
              j["net_median"], j["net_mean"], j["pos"],
              ("%.2f" % j["ratio_median"]) if j["ratio_median"] is not None else "—",
              (j["stop_median"] or 0) * 100, j["holddays_median"] or 0, j["nw_t_median"]))
    A("## 二、分段（符号是否翻）\n")
    A("| 品种 | 2016-2020 hold12 | hold60 | 2021-2025 hold12 | hold60 |")
    A("|---|---:|---:|---:|---:|")
    for k in KEYS + EXTRA:
        if k not in seg_res["2016-2020"]:
            continue
        a = seg_res["2016-2020"][k]
        b = seg_res["2021-2025"][k]
        A("| %s | %.2f | %.2f | %.2f | %.2f |" % (
            ("LH*" if k in EXTRA else k),
            a[12]["L1_net"], a[60]["L1_net"], b[12]["L1_net"], b[60]["L1_net"]))
    A("")
    A("## 三、判定（按预注册 §四）\n")
    h60 = judge[60]
    cA = h60["net_median"] > 0
    cB = h60["pos"] >= 7
    cC = (h60["ratio_median"] or 0) >= 3.0
    A("| # | 条件 | 阈值 | 实测 | 结果 |")
    A("|:-:|---|---|---|---|")
    A("| A | 净/笔中位 > 0 | > 0 | **%.2f** | %s |" % (h60["net_median"], "✅" if cA else "❌"))
    A("| B | 跨品种一致性 | ≥7/11 | **%d/11** | %s |" % (h60["pos"], "✅" if cB else "❌"))
    A("| C | 毛/成本中位 | ≥3.0 | **%s** | %s |" % (
        ("%.2f" % h60["ratio_median"]) if h60["ratio_median"] is not None else "—",
        "✅" if cC else "❌"))
    A("")
    if cA and cB and cC:
        A("⇒ **A/B/C 全过：有候选资格**（可进 P1 预注册，**不是「有效」**）。")
    elif cA or cB:
        A("⇒ **FAIL（部分过）**：不满足全部三条，不具候选资格。")
    else:
        A("⇒ **FAIL**：`retrace_ma13` 拉长持有期不产生毛。")
    A("")
    A("⚠️ **功效声明**：T=10 年、K=2 ⇒ MDE(Sharpe) = **0.974** ⇒ 本实验只能检出 Sharpe ≥ 0.97。"
      "**NW t < 2.24 时不得写「无效/证伪」，只能写「测不出」。**")
    A("")
    A("⚠️ **止损率警告**：若止损率 > 80%，说明绝大多数交易在 60 天内被 2ATR 打掉，"
      "「拉长持有期」这个操作实际没生效，结论须写成「2ATR 止损约束下无法长持有」。\n")

    md = os.path.join(_HERE, "对账_长持有.md")
    with open(md, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("\n产物：%s\n      %s" % (md, jf))
    print("\n【判定】")
    for hold in HOLDS:
        j = judge[hold]
        print("  hold=%-3d 净中位=%7.2f 为正=%2d/11 毛/成本中位=%s 止损中位=%.0f%% 持天中位=%.1f nw_t中位=%.2f"
              % (hold, j["net_median"], j["pos"],
                 ("%.2f" % j["ratio_median"]) if j["ratio_median"] is not None else "—",
                 (j["stop_median"] or 0) * 100, j["holddays_median"] or 0, j["nw_t_median"]))


if __name__ == "__main__":
    main()
