# -*- coding: utf-8 -*-
"""
排列信号 · 第二层首次可回测检验：仓单方向（2026-11-22）
==============================================================
只用有 6.5 年仓单的化工四品种：MA / EG / PP / V。

信号（全部 T 日收盘可得，T+1 开盘成交）：
  排列   = 多 MA5>MA10>MA20 / 空 MA5<MA10<MA20，状态转移记一次
  仓单   = qty 的 20 日变化率 r20 = qty/qty.shift(20) - 1
           r20 < 0 = 去库（利多）；r20 > 0 = 累库（利空）

  组 A 纯排列（对照，同品种同区间）
  组 B 排列 + 仓单同向（多排+去库 / 空排+累库）
  组 C 排列 + 仓单反向（多排+累库 / 空排+去库）

出场：MA10 × MA20 反叉 → 次日开盘平。固定 1 手，ATR20 归一，1 跳/边 + 双边手续费。
"""
from __future__ import annotations
import os, sys
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import screen as S

REC = os.path.join(HERE, "_receipt")
SYMS = ["MA", "EG", "PP", "V"]
WIN = 20


def load_receipt(sym: str) -> pd.Series:
    p = os.path.join(REC, f"receipt_{sym}.csv")
    d = pd.read_csv(p, encoding="utf-8-sig")
    d["dt"] = pd.to_datetime(d["dt"].astype(str), format="%Y%m%d")
    d["qty"] = pd.to_numeric(d["qty"], errors="coerce")
    d = d[d["qty"] > 0]                     # 0 = 数据缺失/当日无仓单，不当"去库"
    return d.set_index("dt")["qty"].sort_index()


def prep(sym: str):
    d = S.load(os.path.join(HERE, "..", "_supertrend_ts", "data", f"{sym}_1440min.csv"))
    if d is None:
        return None
    o = S.signals(S.indicators(d))
    rq = load_receipt(sym)
    idx = pd.DatetimeIndex(o["datetime"]).normalize()
    # 仓单按交易日对齐：当日无值用前值（不后填）
    aligned = rq.reindex(rq.index.union(idx)).sort_index().ffill().reindex(idx)
    o["rq"] = aligned.to_numpy()
    o["r20"] = o["rq"] / o["rq"].shift(WIN) - 1
    return o


def backtest(o, mode):
    """mode: A=纯排列 / B=同向 / C=反向"""
    trades, pos = [], None
    op, cl = o["open"].to_numpy(), o["close"].to_numpy()
    ma5, ma10, ma20 = o["ma5"].to_numpy(), o["ma10"].to_numpy(), o["ma20"].to_numpy()
    atr, ct, r20 = o["atr"].to_numpy(), o["confirm_trig"].to_numpy(), o["r20"].to_numpy()

    for i in range(1, len(o)):
        if pos is not None:
            if np.isfinite(ma10[i - 1]) and np.isfinite(ma20[i - 1]):
                hit = (pos["dir"] == 1 and ma10[i - 1] <= ma20[i - 1]) or \
                      (pos["dir"] == -1 and ma10[i - 1] >= ma20[i - 1])
                if hit:
                    px = float(op[i])
                    trades.append(dict(entry=pos["entry"], exit=px, dir=pos["dir"], atr=pos["atr"],
                                       bars=i - pos["i"], r=(px - pos["entry"]) * pos["dir"] / pos["atr"],
                                       exit_date=o.at[i, "datetime"]))
                    pos = None
        if pos is None:
            sig = int(ct[i - 1]) if np.isfinite(ct[i - 1]) else 0
            a = atr[i - 1]
            if sig == 0 or not np.isfinite(a) or a <= 0:
                continue
            rr = r20[i - 1]
            ok = True
            if mode in ("B", "C") and np.isfinite(rr):
                same = (sig == 1 and rr < 0) or (sig == -1 and rr > 0)
                ok = same if mode == "B" else (not same)
            elif mode in ("B", "C"):
                ok = False                      # 仓单缺失 → 不进 B/C 组
            if ok:
                pos = dict(i=i, entry=float(op[i]), dir=sig, atr=float(a))
    return trades


def tstat(a):
    a = np.asarray(a, float)
    return a.mean() / (a.std(ddof=1) / np.sqrt(len(a))) if len(a) > 1 else np.nan


def main():
    allp = {"A": [], "B": [], "C": []}
    per = {}
    for sym in SYMS:
        o = prep(sym)
        if o is None:
            print(f"[warn] {sym} 无价格数据")
            continue
        per[sym] = {}
        for mode in ("A", "B", "C"):
            tr = backtest(o, mode)
            for t in tr:
                t["sym"] = sym
            S.add_cost(tr, 1.0)
            per[sym][mode] = tr
            allp[mode].extend(tr)

    print(f"品种 {SYMS}，仓单窗口 {WIN} 日，区间 {len(SYMS)} 个品种 2020-06 ~ 2026-11\n")
    print(f"{'组':4s}{'n':>6s}{'净均值':>10s}{'t':>7s}{'胜率':>8s}{'中位持有':>9s}{'合计ATR':>10s}")
    for mode, name in (("A", "纯排列"), ("B", "仓单同向"), ("C", "仓单反向")):
        tr = allp[mode]
        r = np.array([t["net"] for t in tr])
        print(f"{name:8s}{len(r):6d}{r.mean():+10.3f}{tstat(r):7.2f}"
              f"{(r > 0).mean():8.1%}{int(np.median([t['bars'] for t in tr])):9d}{r.sum():+10.1f}")

    print("\n按品种（净均值 / n）")
    print(f"{'品种':6s}{'A纯排列':>14s}{'B同向':>14s}{'C反向':>14s}")
    for sym in SYMS:
        cells = []
        for mode in ("A", "B", "C"):
            tr = per.get(sym, {}).get(mode, [])
            cells.append(f"{np.mean([t['net'] for t in tr]):+.3f}/{len(tr)}" if tr else "—")
        print(f"{sym:6s}{cells[0]:>14s}{cells[1]:>14s}{cells[2]:>14s}")

    # 分年（B 组）
    print("\nB 组（仓单同向）分年")
    for y in sorted({t["exit_date"].year for t in allp["B"]}):
        g = [t for t in allp["B"] if t["exit_date"].year == y]
        r = np.array([t["net"] for t in g])
        print(f"  {y}  n={len(r):3d}  净{r.mean():+.3f}  t={tstat(r):5.2f}  胜率{(r > 0).mean():5.1%}")


if __name__ == "__main__":
    main()
