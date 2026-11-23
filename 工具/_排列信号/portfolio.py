# -*- coding: utf-8 -*-
"""
A2：仓位槽位 + 截面强度排序（存量驱动，替代「新成立触发」）
================================================================
逻辑（先有逻辑）：
  排列存量占 67%，而持有 16 根导致 39 个仓位并存 —— 资金被摊薄到 39 份。
  趋势跟踪的钱在「最强的那几条」而不是「全部」。
  故：每日从「已处于排列」的品种中按 20 日风险调整动量排序，只填有限槽位。

口径（见 §十四）：
  score = (close - close[-20]) / ATR20       多排取最大、空排取最小
  T 日收盘算 → T+1 开盘成交；出场 MA10×MA20 反叉
  固定 1 手；手续费按 comm_per_hand，滑点 1 跳/边；RMB 口径
"""
from __future__ import annotations
import os, sys, glob
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..")))
import screen as S
import config_symbols as cs


def build():
    D = {}
    for f in sorted(glob.glob(os.path.join(S.DATA, "*_1440min.csv"))):
        sym = os.path.basename(f).split("_")[0]
        if sym not in cs.SYMBOLS:
            continue
        d = S.load(f)
        if d is None or len(d) < 200:
            continue
        o = S.signals(S.indicators(d))          # ★ 与 screen.py 同源：confirm / confirm_trig
        o["ret20"] = o["close"] - o["close"].shift(20)
        o["score"] = o["ret20"] / o["atr"]
        o["dir"] = o["confirm"].astype(int)     # 状态持续（仅参考）
        o["trig"] = o["confirm_trig"].astype(int)   # 转移日（入场唯一依据）
        o["date"] = pd.DatetimeIndex(pd.to_datetime(o["datetime"])).normalize()
        o = o.set_index("date", drop=False)
        D[sym] = dict(o=o, idx={d: i for i, d in enumerate(o.index)})
    return D


def run(D, n_slots, cal=None, equity=1_000_000., risk_pct=0.01, max_margin_pct=0.60, fixed_lot=False):
    if cal is None:
        cal = sorted(set().union(*[set(v["o"].index) for v in D.values()]))
    pos = {}
    trades = []
    margin_by_day = {}
    for day in cal:
        # ---- 出场：用该品种前一根的 ma10/ma20 判反叉 → 当日开盘平 ----
        for sym in list(pos):
            v = D[sym]; i = v["idx"].get(day)
            if i is None or i < 1:
                continue
            o = v["o"]; p = o.iloc[i - 1]
            m10, m20 = p["ma10"], p["ma20"]
            if not (np.isfinite(m10) and np.isfinite(m20)):
                continue
            d = pos[sym]["dir"]
            if (d == 1 and m10 <= m20) or (d == -1 and m10 >= m20):
                px = float(o.iloc[i]["open"])
                e = pos.pop(sym)
                gross = (px - e["entry"]) * d * e["qty"] * e["mult"]
                fee = float(cs.comm_per_hand(sym, e["entry"])) + float(cs.comm_per_hand(sym, px))
                slip = float(cs.SYMBOLS[sym][3]) * e["mult"] * 2 * e["qty"]
                fee = fee * e["qty"]
                trades.append(dict(sym=sym, dir=d, entry_date=e["date"], exit_date=o.iloc[i]["datetime"],
                                   entry=e["entry"], exit=px, atr=e["atr"], bars=i - e["i"],
                                   rmb=gross - fee - slip, margin=e["margin"]))
        # ---- 入场：剩余槽位按 score 排序填 ----
        if n_slots is not None:
            nl = n_slots - sum(1 for v in pos.values() if v["dir"] == 1)
            ns = n_slots - sum(1 for v in pos.values() if v["dir"] == -1)
        else:
            nl = ns = 10 ** 6
        cand = []
        for sym, v in D.items():
            if sym in pos:
                continue
            i = v["idx"].get(day)
            if i is None or i < 1:
                continue
            p = v["o"].iloc[i - 1]
            if p["trig"] == 0 or not np.isfinite(p["score"]) or not np.isfinite(p["atr"]):
                continue
            cand.append((sym, int(p["trig"]), float(p["score"]), i))
        longs = sorted([c for c in cand if c[1] == 1], key=lambda x: -x[2])[:max(nl, 0)]
        shorts = sorted([c for c in cand if c[1] == -1], key=lambda x: x[2])[:max(ns, 0)]
        for sym, d, sc, i in longs + shorts:
            v = D[sym]; row = v["o"].iloc[i]
            px = float(row["open"]); mult = cs.SYMBOLS[sym][2]
            mr = cs.margin_rate(sym) or 10.0
            atr = float(v["o"].iloc[i - 1]["atr"])
            if fixed_lot:
                qty = 1
            else:
                qty = int(equity * risk_pct / (atr * mult))
            if qty < 1:
                continue
            mg = px * mult * mr / 100.0 * qty
            if sum(x["margin"] for x in pos.values()) + mg > equity * max_margin_pct:
                continue
            pos[sym] = dict(dir=d, entry=px, atr=atr, qty=qty,
                            date=row["datetime"], i=i, mult=mult, margin=mg)
        margin_by_day[day] = sum(v["margin"] for v in pos.values())
    return trades, margin_by_day


def report(tag, trades, mbd, eq0=1_000_000.):
    """回撤口径：账户权益（eq0 + 累计盈亏），不是峰值利润。
    旧版 dd.min()/peak.max() 分母是峰值利润，会放大成 −60% 以上，属口径错误。"""
    if not trades:
        print(f"{tag}: 无成交")
        return
    df = pd.DataFrame(trades).sort_values("exit_date").reset_index(drop=True)
    rmb = df.rmb.to_numpy()
    df["y"] = pd.to_datetime(df.entry_date).dt.year
    cum = rmb.cumsum()
    peak = np.maximum(np.maximum.accumulate(cum), 0.0)      # 初始权益即峰值起点
    dd = cum - peak
    dd_pct = dd / (eq0 + peak)
    m = np.array(list(mbd.values()))
    years = (pd.to_datetime(df.exit_date).max() - pd.to_datetime(df.entry_date).min()).days / 365.25
    ann = rmb.sum() / years
    calmar = (ann / eq0) / abs(dd_pct.min()) if dd_pct.min() < 0 else np.nan
    print(f"\n=== {tag} ===")
    print(f"  笔数 {len(df)}  年数 {years:.1f}  RMB合计 {rmb.sum():+,.0f}  年化 {ann:+,.0f} ({ann/eq0:.1%} of {eq0:,.0f})")
    print(f"  每笔均值 {rmb.mean():+.0f}  中位 {np.median(rmb):+.0f}  胜率 {(rmb>0).mean():.1%}")
    print(f"  保证金 均值 {m.mean():,.0f}  最大 {m.max():,.0f}  (占账户 {m.mean()/eq0:.1%})")
    print(f"  账户最大回撤 {dd.min():+,.0f} ({dd_pct.min():.1%})  Calmar {calmar:.2f}")
    yr = df.groupby("y").rmb.sum()
    print("  分年:", {int(k): int(v) for k, v in yr.items()})
    s1 = yr.loc[[y for y in yr.index if y <= 2020]].sum()
    s2 = yr.loc[[y for y in yr.index if y >= 2021]].sum()
    print(f"  分段 2016-20 {s1:+,.0f} | 2021-26 {s2:+,.0f}  同号={'是' if s1*s2 > 0 else '否'}")


if __name__ == "__main__":
    D = build()
    print(f"品种 {len(D)}")
    cal = sorted(set().union(*[set(v["o"].index) for v in D.values()]))
    cal = [d for d in cal if d >= pd.Timestamp("2016-06-01")]
    for n in (3, 5, 10, 20, None):
        tr, mb = run(D, n, cal)
        report(f"等风险 槽位{'无限' if n is None else n}", tr, mb)
    print("\n\n########## 对照：固定 1 手 ##########")
    for n in (5, None):
        tr, mb = run(D, n, cal, fixed_lot=True)
        report(f"1手 槽位{'无限' if n is None else n}", tr, mb)
