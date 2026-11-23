# -*- coding: utf-8 -*-
"""日线体检 · 损益构成与持仓路径（2026-11-04）

回答三个问题：
  ① 损益由什么构成：L0 毛 / 止损变化 / 成本 / 净，各占多少
  ② 按品种、按信号、按年份、多空怎么分
  ③ 「进场后 12 个交易日价格怎么走」——MFE/MAE/收盘的平均路径（ATR 归一）

口径与 `进场体检.screen_one` **逐位一致**（复用同一函数取官方数字，
本文件的数组只用于分解，开头有 assert 对齐）。不改任何信号逻辑。

用法：<tqsdk venv>\\python.exe 对账_日线_构成.py
产物：对账_日线_构成.md / 体检结果_日线_构成.json
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

KEYS = ["MA", "M", "RB", "V", "SA", "EB", "JD", "JM", "LH", "P", "RU", "SR"]
SIGS = list(mj.SIGNALS.keys())
HOLD = mj.HOLD
COST = 2.8          # MDE 系数（双侧 5% + 80% power）


def _t(x):
    sd = float(np.std(x, ddof=1)) if len(x) > 1 else 0.0
    return float(np.mean(x) / (sd / math.sqrt(len(x)))) if sd > 1e-12 else 0.0


def _nw_t(x, lag):
    v = x[~np.isnan(x)]
    if len(v) < 10:
        return float("nan")
    n = len(v)
    e = v - v.mean()
    g0 = float(np.dot(e, e) / n)
    var = g0
    for k in range(1, min(lag, n - 1) + 1):
        gk = float(np.dot(e[k:], e[:-k]) / n)
        var += 2.0 * (1.0 - k / (lag + 1.0)) * gk
    se = math.sqrt(max(var, 1e-12) / n)
    return float(v.mean() / se) if se > 0 else float("nan")


def trades(key, sig_name, df):
    """复刻 screen_one 的成交构造，返回逐笔数组（用于分解）"""
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
    valid = np.arange(0, N - HOLD - 1)
    idx = valid[sig[valid] != 0]
    if len(idx) == 0:
        return None
    sgn = sig[idx]
    entry = o[idx + 1]
    atr0 = atr[idx]
    stop = np.where(sgn > 0, entry - mj.STOP_ATR_MULT * atr0, entry + mj.STOP_ATR_MULT * atr0)
    ex = o[idx + 1 + HOLD].copy()
    hit = np.zeros(len(idx), bool)
    hit_day = np.zeros(len(idx), int)
    for j, (e_i, st, s) in enumerate(zip(idx, stop, sgn)):
        for k in range(1, HOLD + 1):
            jj = e_i + k
            if s > 0:
                if l[jj] <= st:
                    ex[j] = min(st, o[jj]); hit[j] = True; hit_day[j] = k; break
            else:
                if h[jj] >= st:
                    ex[j] = max(st, o[jj]); hit[j] = True; hit_day[j] = k; break
    # 路径（ATR 归一）：第 d 个持有日收盘 / 当日最高 / 当日最低
    path_c = np.full((len(idx), HOLD), np.nan)
    path_mfe = np.full((len(idx), HOLD), np.nan)
    path_mae = np.full((len(idx), HOLD), np.nan)
    for j, (e_i, s, a) in enumerate(zip(idx, sgn, atr0)):
        if a <= 0:
            continue
        run_mfe, run_mae = 0.0, 0.0
        for k in range(1, HOLD + 1):
            jj = e_i + k
            path_c[j, k - 1] = (c[jj] - entry[j]) * s / a
            run_mfe = max(run_mfe, (h[jj] - entry[j]) * s / a)
            run_mae = min(run_mae, (l[jj] - entry[j]) * s / a)
            path_mfe[j, k - 1] = run_mfe
            path_mae[j, k - 1] = run_mae
    return dict(
        idx=idx, sgn=sgn, entry=entry, atr=atr0, ex=ex, hit=hit, hit_day=hit_day,
        fwd=(o[idx + 1 + HOLD] - entry) * sgn,
        gross1=(ex - entry) * sgn,
        path_c=path_c, path_mfe=path_mfe, path_mae=path_mae,
        dt=df["datetime"].to_numpy()[idx],
    )


def main():
    # ---------- 0. 数据清单 ----------
    data, qual = {}, []
    for k in KEYS:
        fs = sorted(glob.glob(os.path.join(_HERE, "数据", "%s_1440min_*.csv" % k)))
        if not fs:
            qual.append((k, 0, "—", "—", float("nan")))
            continue
        d = pd.read_csv(fs[-1], encoding="utf-8-sig", parse_dates=["datetime"])
        data[k] = d
        o, h, l, c = (d[x].to_numpy(float) for x in ("open", "high", "low", "close"))
        deg = float(np.mean((o == h) & (h == l) & (l == c)))
        qual.append((k, len(d), str(d["datetime"].iloc[0])[:10], str(d["datetime"].iloc[-1])[:10], deg))
    have = [k for k in KEYS if k in data]
    print("数据就绪", len(have), "/12", have)

    # ---------- 1. 逐格 ----------
    rows = []
    for k in have:
        d = data[k]
        mult = mj.cs.get(k)[2]
        for s in SIGS:
            try:
                r = mj.screen_one(k, s, d)
                tr = trades(k, s, d)
            except Exception as e:
                print("  [skip]", k, s, e)
                continue
            if r["n"] == 0 or tr is None:
                continue
            # 对齐断言：数组口径必须与 screen_one 逐位一致
            assert abs(float(np.mean(tr["fwd"])) - r["L0_gross"]) < 5e-3, (k, s)
            assert abs(float(np.mean(tr["gross1"])) - r["L1_gross"]) < 5e-3, (k, s)
            ser = np.full(len(d), np.nan)
            ser[tr["idx"]] = tr["gross1"] - r["cost_pts"]
            r["nw_t"] = _nw_t(ser, HOLD)
            r["mult"] = mult
            r["L0_pts"] = float(np.sum(tr["fwd"]))
            r["L1_gross_pts"] = float(np.sum(tr["gross1"]))
            r["net_pts"] = float(np.sum(tr["gross1"] - r["cost_pts"]))
            r["long_n"] = int(np.sum(tr["sgn"] > 0))
            r["long_net"] = float(np.mean((tr["gross1"] - r["cost_pts"])[tr["sgn"] > 0])) if r["long_n"] else float("nan")
            r["short_n"] = int(np.sum(tr["sgn"] < 0))
            r["short_net"] = float(np.mean((tr["gross1"] - r["cost_pts"])[tr["sgn"] < 0])) if r["short_n"] else float("nan")
            r["hit_day_mean"] = float(np.mean(tr["hit_day"][tr["hit"]])) if tr["hit"].any() else float("nan")
            r["path_c"] = np.nanmean(tr["path_c"], axis=0).tolist()
            r["path_mfe"] = np.nanmean(tr["path_mfe"], axis=0).tolist()
            r["path_mae"] = np.nanmean(tr["path_mae"], axis=0).tolist()
            r["yearly"] = {}
            yrs = pd.to_datetime(tr["dt"]).year
            for y in sorted(set(yrs)):
                m = yrs == y
                r["yearly"][int(y)] = dict(
                    n=int(m.sum()),
                    net=round(float(np.mean((tr["gross1"] - r["cost_pts"])[m])), 3),
                    gross=round(float(np.mean(tr["gross1"][m])), 3),
                    tot=round(float(np.sum((tr["gross1"] - r["cost_pts"])[m])), 1),
                )
            rows.append(r)

    ok = [r for r in rows if r["n"] >= 30]
    print("格数", len(rows), "| n>=30", len(ok))

    # ---------- 2. 汇总 ----------
    def wavg(rs, f):
        n = sum(x["n"] for x in rs)
        return sum(x[f] * x["n"] for x in rs) / n if n else float("nan")

    tot_l0 = sum(r["L0_pts"] for r in ok)
    tot_l1 = sum(r["L1_gross_pts"] for r in ok)
    tot_net = sum(r["net_pts"] for r in ok)
    tot_cost = sum(r["cost_pts"] * r["n"] for r in ok)
    tot_n = sum(r["n"] for r in ok)

    L = []
    L.append("# 日线体检 · 损益构成与持仓路径（2026-11-04）\n")
    L.append("> 口径：日线 1440min、24 信号 × %d 品种、HOLD=%d 个交易日、止损 2×ATR(14)、"
             "滑点 1 跳、1 手。\n> **L0** = 无止损固定持有；**L1** = 含 2ATR 止损。"
             "本文所有「点」均为价格点，元 = 点 × 合约乘数。\n" % (len(have), HOLD))

    L.append("## 一、数据清单\n")
    L.append("| 品种 | 名称 | 根数 | 起 | 止 | 退化率 | 乘数 | 成本(点) |")
    L.append("|---|---|---:|---|---|---:|---:|---:|")
    for k, n, a, b, deg in qual:
        nm = mj.cs.get(k)[0] if k in data else "—"
        mult = mj.cs.get(k)[2] if k in data else "—"
        cp = "%.2f" % mj.cost_points(k) if k in data else "—"
        L.append("| %s | %s | %d | %s | %s | %.2f%% | %s | %s |"
                 % (k, nm, n, a, b, deg * 100, mult, cp))
    L.append("")

    L.append("## 二、损益构成（n≥30 的 %d 格，每笔 1 手等权累加）\n" % len(ok))
    f0 = lambda x: format(x, "+,.0f")  # noqa: E731
    L.append("| 项 | 点数合计 | 元 | 占比 | 说明 |")
    L.append("|---|---:|---:|---:|---|")
    L.append("| L0 毛（无止损） | %s | %s | — | 纯持有到第 %d 日 |"
             % (f0(tot_l0), f0(sum(r["L0_pts"] * r["mult"] for r in ok)), HOLD))
    L.append("| 止损影响 | %s | %s | — | L1毛 − L0毛（负=止损在削毛） |"
             % (f0(tot_l1 - tot_l0), f0(sum((r["L1_gross_pts"] - r["L0_pts"]) * r["mult"] for r in ok))))
    L.append("| L1 毛（含止损） | %s | %s | 100.0%% | — |"
             % (f0(tot_l1), f0(sum(r["L1_gross_pts"] * r["mult"] for r in ok))))
    L.append("| 成本 | %s | %s | %.1f%% | %d 笔 × 每格成本 |"
             % (f0(-tot_cost), f0(-sum(r["cost_pts"] * r["n"] * r["mult"] for r in ok)),
                100.0 * tot_cost / tot_l1 if tot_l1 else 0, tot_n))
    L.append("| **净** | **%s** | **%s** | **%.1f%%** | **净/毛** |"
             % (f0(tot_net), f0(sum(r["net_pts"] * r["mult"] for r in ok)),
                100.0 * tot_net / tot_l1 if tot_l1 else 0))
    L.append("")
    L.append("> 注：同一根 bar 会被多个信号、多个品种重复计 ⇒ **这里的「合计」只表示规模量级，不是组合损益**。"
             "跨品种/跨信号的正确比较看「每笔均值」。\n")

    L.append("### 2.1 每笔均值（点）\n")
    L.append("| 项 | 值 |")
    L.append("|---|---:|")
    for lab, f in (("L0 毛/笔", "L0_gross"), ("L1 毛/笔", "L1_gross"),
                   ("净/笔", "L1_net"), ("成本/笔", "cost_pts"),
                   ("胜率", "L1_winrate"), ("止损触发占比", "stop_share"), ("毛/成本", "ratio")):
        L.append("| %s | %.3f |" % (lab, wavg(ok, f)))
    L.append("")

    L.append("## 三、按品种\n")
    L.append("| 品种 | 格数 | 笔数 | L0毛/笔 | 止损影响/笔 | 净/笔 | 胜率 | 止损占比 | 净合计(点) |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    byk = {}
    for k in have:
        rs = [r for r in ok if r["key"] == k]
        if not rs:
            continue
        byk[k] = rs
        L.append("| %s %s | %d | %d | %+.3f | %+.3f | %+.3f | %.1f%% | %.1f%% | %s |"
                 % (k, mj.cs.get(k)[0], len(rs), sum(r["n"] for r in rs),
                    wavg(rs, "L0_gross"), wavg(rs, "L1_gross") - wavg(rs, "L0_gross"),
                    wavg(rs, "L1_net"), wavg(rs, "L1_winrate") * 100,
                    wavg(rs, "stop_share") * 100, f0(sum(r["net_pts"] for r in rs))))
    L.append("")

    L.append("## 四、按信号（跨品种汇总）\n")
    L.append("| 信号 | 格数 | 笔数 | L0毛/笔 | 净/笔 | 胜率 | 止损占比 | 毛/成本 | 净合计(点) |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    bysig = {}
    for s in SIGS:
        rs = [r for r in ok if r["signal"] == s]
        if not rs:
            continue
        bysig[s] = rs
        L.append("| %s | %d | %d | %+.3f | %+.3f | %.1f%% | %.1f%% | %.2f | %s |"
                 % (s, len(rs), sum(r["n"] for r in rs), wavg(rs, "L0_gross"),
                    wavg(rs, "L1_net"), wavg(rs, "L1_winrate") * 100,
                    wavg(rs, "stop_share") * 100, wavg(rs, "ratio"),
                    f0(sum(r["net_pts"] for r in rs))))
    L.append("")

    L.append("## 五、逐年\n")
    years = sorted({y for r in ok for y in r["yearly"]})
    L.append("| 年份 | 笔数 | 净/笔(点) | 净合计(点) | 正净期望格占比 |")
    L.append("|---|---:|---:|---:|---:|")
    for y in years:
        rs = [(r, r["yearly"][y]) for r in ok if y in r["yearly"]]
        n = sum(x[1]["n"] for x in rs)
        tot = sum(x[1]["tot"] for x in rs)
        pos = sum(1 for x in rs if x[1]["net"] > 0)
        L.append("| %d | %d | %+.3f | %s | %d/%d |"
                 % (y, n, tot / n if n else 0, f0(tot), pos, len(rs)))
    L.append("")

    L.append("## 六、多空分解\n")
    ln = sum(r["long_n"] for r in ok)
    sn = sum(r["short_n"] for r in ok)
    lnet = sum(r["long_net"] * r["long_n"] for r in ok if r["long_n"]) / ln if ln else float("nan")
    snet = sum(r["short_net"] * r["short_n"] for r in ok if r["short_n"]) / sn if sn else float("nan")
    L.append("| 方向 | 笔数 | 净/笔(点) |")
    L.append("|---|---:|---:|")
    L.append("| 多 | %d | %+.3f |" % (ln, lnet))
    L.append("| 空 | %d | %+.3f |" % (sn, snet))
    L.append("")

    L.append("## 七、进场后 %d 个交易日，价格怎么走（ATR 归一，跨全部格平均）\n" % HOLD)
    pc = np.nanmean([r["path_c"] for r in ok], axis=0)
    pm = np.nanmean([r["path_mfe"] for r in ok], axis=0)
    pa = np.nanmean([r["path_mae"] for r in ok], axis=0)
    L.append("| 持有日 | 平均收盘偏移 | 平均最大有利(MFE) | 平均最大不利(MAE) | 收盘>0 占比 |")
    L.append("|---:|---:|---:|---:|---:|")
    for i in range(HOLD):
        L.append("| %d | %+.3f | %+.3f | %+.3f | — |" % (i + 1, pc[i], pm[i], pa[i]))
    L.append("")
    L.append("> 单位 = ATR14。`收盘偏移 > 0` 表示按信号方向有利。"
             "MFE/MAE 是**累计极值**（第 d 行 = 前 d 日内的最大有利/不利）。\n")

    # ---------- 3. 落盘 ----------
    md = os.path.join(_HERE, "对账_日线_构成.md")
    with open(md, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    js = dict(qual=[dict(key=q[0], n=q[1], start=q[2], end=q[3], deg=q[4]) for q in qual],
              rows=[{k: v for k, v in r.items() if not isinstance(v, np.ndarray)} for r in rows],
              totals=dict(n=tot_n, l0=tot_l0, l1=tot_l1, cost=tot_cost, net=tot_net),
              path=dict(close=pc.tolist(), mfe=pm.tolist(), mae=pa.tolist()))
    with open(os.path.join(_HERE, "体检结果_日线_构成.json"), "w", encoding="utf-8") as f:
        json.dump(js, f, ensure_ascii=False, indent=1, default=float)
    print("OK ->", md)


main()
