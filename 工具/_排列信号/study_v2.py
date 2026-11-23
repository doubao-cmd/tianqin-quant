# -*- coding: utf-8 -*-
"""
排列信号 · 第二轮结构研究（2026-11-22）
==============================================================
S1 苗头档分年：趋势能走多久、胜率多少（逐年全列，不做挑选）
S2 事件代理：单日大波动后追入，T+1开盘 / T+2开盘 两档，量化「慢几拍」
S3 时间止损：在 MA10×MA20 出场体系下，三种形式的时间规则
S4 收敛度：|MA5-MA20|/ATR 分箱后的前向收益（回答「纠缠怎么量化」）

口径：T 日收盘判定 → T+1 开盘成交；ATR20 取 i-1 及之前；1 跳/边滑点 + 双边手续费。
"""
from __future__ import annotations
import os, sys, glob
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import screen as S

DATA = os.path.join(HERE, "..", "_supertrend_ts", "data")


def load_all():
    out = {}
    for f in sorted(glob.glob(os.path.join(DATA, "*_1440min.csv"))):
        sym = os.path.basename(f).split("_")[0]
        d = S.load(f)
        if d is None:
            continue
        o = S.signals(S.indicators(d))
        o["conv"] = (o["ma5"] - o["ma20"]).abs() / o["atr"]
        o["slope20"] = (o["ma20"] - o["ma20"].shift(5)) / o["atr"]
        out[sym] = o
    return out


def tstat(a):
    a = np.asarray(a, float)
    if len(a) < 2:
        return np.nan
    sd = a.std(ddof=1)
    return a.mean() / (sd / np.sqrt(len(a))) if sd > 0 else np.nan


def blk(tag, nums, bars=None):
    a = np.asarray(nums, float)
    if len(a) == 0:
        print(f"  {tag:16s} 无样本")
        return
    s = f"  {tag:16s} n={len(a):5d}  净均值{a.mean():+.3f}  t={tstat(a):5.2f}  胜率{(a > 0).mean():5.1%}"
    if bars is not None:
        s += f"  中位持有{int(np.median(bars)):3d}根"
    print(s)


def backtest(o, tier="confirm", exit_fast=False, time_rule=None):
    """time_rule: None | ('hard',n) | ('mfe',n,th) | ('mae',n,th)"""
    col = "confirm_trig" if tier == "confirm" else "early"
    trades, pos = [], None
    n = len(o)
    hi_a, lo_a = o["high"].to_numpy(), o["low"].to_numpy()
    op_a, cl_a = o["open"].to_numpy(), o["close"].to_numpy()
    ma5, ma10, ma20 = (o["ma5"].to_numpy(), o["ma10"].to_numpy(), o["ma20"].to_numpy())

    for i in range(1, n):
        if pos is not None:
            k = i - pos["i"]                      # 已持有完整根数
            hit, why = False, ""
            a_, b_ = (ma5[i - 1], ma10[i - 1]) if exit_fast else (ma10[i - 1], ma20[i - 1])
            if np.isfinite(a_) and np.isfinite(b_):
                hit = (pos["dir"] == 1 and a_ <= b_) or (pos["dir"] == -1 and a_ >= b_)
                why = "cross"
            if (not hit) and time_rule is not None and k >= time_rule[1]:
                typ = time_rule[0]
                if typ == "hard":
                    hit, why = True, "time"
                elif typ == "mfe" and pos["mfe"] < time_rule[2]:
                    hit, why = True, "time"
                elif typ == "mae" and pos["mae"] > time_rule[2]:
                    hit, why = True, "time"
            if hit:
                px = float(op_a[i])
                trades.append(dict(entry=pos["entry"], exit=px, dir=pos["dir"], atr=pos["atr"],
                                   bars=k, r=(px - pos["entry"]) * pos["dir"] / pos["atr"],
                                   mfe=pos["mfe"], mae=pos["mae"],
                                   exit_date=o.at[i, "datetime"], why=why))
                pos = None
        if pos is None:
            sig = int(o.at[i - 1, col]) if np.isfinite(o.at[i - 1, col]) else 0
            atr = o.at[i - 1, "atr"]
            if sig != 0 and np.isfinite(atr) and atr > 0:
                pos = dict(i=i, entry=float(op_a[i]), dir=sig, atr=float(atr),
                           mfe=0.0, mae=0.0)
        if pos is not None:
            j = i                                # 用 i 这根已完成的高低更新 MFE/MAE
            if j >= pos["i"]:
                hi, lo = hi_a[j], lo_a[j]
                if pos["dir"] == 1:
                    pos["mfe"] = max(pos["mfe"], (hi - pos["entry"]) / pos["atr"])
                    pos["mae"] = max(pos["mae"], (pos["entry"] - lo) / pos["atr"])
                else:
                    pos["mfe"] = max(pos["mfe"], (pos["entry"] - lo) / pos["atr"])
                    pos["mae"] = max(pos["mae"], (hi - pos["entry"]) / pos["atr"])
    return trades


# ---------------- S1 ----------------
def s1(data):
    print("\n" + "=" * 82)
    print("【S1】苗头档 / 确认档 分年（逐年全列）")
    print("=" * 82)
    for tier, tname in (("early", "苗头"), ("confirm", "确认")):
        alltr = []
        for sym, o in data.items():
            tr = backtest(o, tier)
            for t in tr:
                t["sym"] = sym
            S.add_cost(tr, 1.0)
            alltr.extend(tr)
        blk(f"{tname}·全样本", [t["net"] for t in alltr], [t["bars"] for t in alltr])
        for y in sorted({t["exit_date"].year for t in alltr}):
            g = [t for t in alltr if t["exit_date"].year == y]
            blk(f"  {y}", [t["net"] for t in g], [t["bars"] for t in g])
        print()


# ---------------- S2 ----------------
def s2(data, k_atr=2.0, hs=(1, 3, 5, 10, 20)):
    print("\n" + "=" * 82)
    print(f"【S2】事件代理：单日变动 ≥ {k_atr}×ATR 后按事件方向追入")
    print("=" * 82)
    r1 = {h: [] for h in hs}
    r2 = {h: [] for h in hs}
    n_ev = 0
    for sym, o in data.items():
        atr = o["atr"].shift(1)
        chg = o["close"] - o["close"].shift(1)
        ev = (chg.abs() >= k_atr * atr) & atr.gt(0)
        op, cl = o["open"].to_numpy(), o["close"].to_numpy()
        for i in np.where(ev.to_numpy())[0]:
            d = np.sign(chg.iloc[i])
            a = float(atr.iloc[i])
            if not np.isfinite(a) or a <= 0 or i + 1 >= len(o):
                continue
            n_ev += 1
            for ob, store in ((1, r1), (2, r2)):
                for h in hs:
                    j = i + ob + h - 1
                    if j >= len(o):
                        continue
                    store[h].append(d * (cl[j] - op[i + ob]) / a)
    print(f"  事件数 {n_ev}（占全部交易日约 {n_ev / sum(len(v) for v in data.values()):.1%}）")
    print("  -- T+1 开盘入场（日线可做到的最快执行）")
    for h in hs:
        blk(f"持有{h}根", r1[h])
    print("  -- T+2 开盘入场（再慢一拍）")
    for h in hs:
        blk(f"持有{h}根", r2[h])


# ---------------- S3 ----------------
def s3(data):
    print("\n" + "=" * 82)
    print("【S3】时间止损（出场基准 = MA10×MA20）")
    print("=" * 82)

    def run(rule, tag):
        pool = []
        for sym, o in data.items():
            tr = backtest(o, "confirm", exit_fast=False, time_rule=rule)
            for t in tr:
                t["sym"] = sym
            S.add_cost(tr, 1.0)
            pool.extend(tr)
        blk(tag, [t["net"] for t in pool], [t["bars"] for t in pool])
        return pool

    run(None, "无时间止损")
    print("\n  (a) 到期无条件平仓")
    for nn in (8, 12, 16, 20, 30):
        run(("hard", nn), f"{nn}根强平")
    print("\n  (b) 到期时 MFE 不足即平（「没起来就走」）")
    for nn, th in ((8, 0.5), (8, 1.0), (12, 0.5), (12, 1.0), (16, 1.0), (20, 1.0)):
        run(("mfe", nn, th), f"{nn}根 MFE<{th}")
    print("\n  (c) 到期时 MAE 超限即平（时间+止损组合）")
    for nn, th in ((8, 1.0), (12, 1.0), (12, 1.5), (16, 1.5), (20, 2.0)):
        run(("mae", nn, th), f"{nn}根 MAE>{th}")


# ---------------- S4 ----------------
def s4(data, hs=(5, 10, 20)):
    print("\n" + "=" * 82)
    print("【S4】收敛度 |MA5-MA20|/ATR 分箱 → 前向收益（仅排列成立期间）")
    print("=" * 82)
    edges = [0, 0.2, 0.35, 0.5, 0.7, 1.0, 99]
    labels = ["<0.2", "0.2-0.35", "0.35-0.5", "0.5-0.7", "0.7-1.0", ">1.0"]
    for h in hs:
        buckets = {l: [] for l in labels}
        for sym, o in data.items():
            conv, atr = o["conv"].to_numpy(), o["atr"].to_numpy()
            conf = o["confirm"].to_numpy()
            op, cl = o["open"].to_numpy(), o["close"].to_numpy()
            for i in range(25, len(o) - h - 1):
                if conf[i] == 0 or not np.isfinite(conv[i]) or atr[i] <= 0:
                    continue
                b = int(np.digitize(conv[i], edges)) - 1
                if 0 <= b < len(labels):
                    buckets[labels[b]].append(conf[i] * (cl[i + h] - op[i + 1]) / atr[i])
        print(f"\n  前向 {h} 根（ATR 倍数，按排列方向取号）")
        for l in labels:
            a = np.asarray(buckets[l], float)
            if len(a) < 30:
                print(f"    {l:10s} n={len(a)} 样本不足")
                continue
            print(f"    {l:10s} n={len(a):6d}  均值{a.mean():+.3f}  t={tstat(a):5.2f}  胜率{(a > 0).mean():5.1%}")


def main():
    data = load_all()
    print(f"品种 {len(data)} 个")
    s1(data)
    s2(data)
    s3(data)
    s4(data)


if __name__ == "__main__":
    main()
