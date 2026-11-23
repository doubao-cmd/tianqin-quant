# -*- coding: utf-8 -*-
"""
仓单腿第二版：250 日同比（预注册见 _口径与基线_20261121.md §十）
====================================================================
样本  AG AU BU CU MA RU SN TA（np.db receipt，2020-06 起）
信号  yoy = qty / qty.shift(250) - 1 ；<0 去库(利多) / >0 累库(利空)
组    A 纯排列 | B 排列+同比同向 | C 排列+同比反向
出场  MA10 × MA20 ；T 日收盘判定 → T+1 开盘；1 跳/边 + 双边手续费
只跑一次，不搜窗口。
"""
from __future__ import annotations
import os, sys, sqlite3
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import screen as S

NP = os.path.join(HERE, "..", "_np_data", "np.db")
SYMS = ["AG", "AU", "BU", "CU", "MA", "RU", "SN", "TA"]
START = "20200601"
YOY = 250


def receipt(sym: str) -> pd.Series:
    con = sqlite3.connect(NP)
    rows = con.execute(
        "select period_start, value from np_data where metric='receipt' and symbol=? order by 1",
        (sym,)).fetchall()
    con.close()
    s = pd.Series({pd.Timestamp(r[0]): float(r[1]) for r in rows if r[1] is not None})
    s = s[s > 0]                                 # 0 = 缺失/未报，不当去库
    return s.sort_index()


def prep(sym: str):
    d = S.load(os.path.join(HERE, "..", "_supertrend_ts", "data", f"{sym}_1440min.csv"))
    if d is None:
        return None
    o = S.signals(S.indicators(d))
    idx = pd.DatetimeIndex(o["datetime"]).normalize()
    rq = receipt(sym)
    al = rq.reindex(rq.index.union(idx)).sort_index().ffill().reindex(idx)
    o["rq"] = al.to_numpy()
    o["yoy"] = o["rq"] / o["rq"].shift(YOY) - 1
    m = idx >= pd.Timestamp(START)
    return o.loc[m].reset_index(drop=True)


def bt(o, mode):
    tr, pos = [], None
    op = o["open"].to_numpy()
    ma10, ma20 = o["ma10"].to_numpy(), o["ma20"].to_numpy()
    atr, ct, yoy = o["atr"].to_numpy(), o["confirm_trig"].to_numpy(), o["yoy"].to_numpy()
    for i in range(1, len(o)):
        if pos is not None:
            if np.isfinite(ma10[i - 1]) and np.isfinite(ma20[i - 1]):
                if (pos["d"] == 1 and ma10[i - 1] <= ma20[i - 1]) or \
                   (pos["d"] == -1 and ma10[i - 1] >= ma20[i - 1]):
                    px = float(op[i])
                    tr.append(dict(r=(px - pos["e"]) * pos["d"] / pos["a"],
                                   bars=i - pos["i"], exit_date=o.at[i, "datetime"]))
                    pos = None
        if pos is None:
            sig = int(ct[i - 1]) if np.isfinite(ct[i - 1]) else 0
            a = atr[i - 1]
            if sig == 0 or not np.isfinite(a) or a <= 0:
                continue
            y = yoy[i - 1]
            if mode in ("B", "C"):
                if not np.isfinite(y):
                    continue
                same = (sig == 1 and y < 0) or (sig == -1 and y > 0)
                if (mode == "B") != same:
                    continue
            pos = dict(i=i, e=float(op[i]), d=sig, a=float(a))
    return tr


def tst(a):
    a = np.asarray(a, float)
    return a.mean() / (a.std(ddof=1) / np.sqrt(len(a))) if len(a) > 1 else np.nan


def main():
    pool, per = {"A": [], "B": [], "C": []}, {}
    for sym in SYMS:
        o = prep(sym)
        if o is None or len(o) < 300:
            print(f"[skip] {sym}")
            continue
        per[sym] = {}
        for m in ("A", "B", "C"):
            tr = bt(o, m)
            for t in tr:
                t["sym"] = sym
            S.add_cost(tr, 1.0)
            per[sym][m] = tr
            pool[m].extend(tr)

    print(f"样本 {SYMS}｜{START} 起｜同比窗口 {YOY} 日\n")
    print(f"{'组':10s}{'n':>6s}{'净均值':>10s}{'t':>7s}{'胜率':>8s}{'中位持有':>9s}{'合计':>9s}")
    for m, name in (("A", "纯排列"), ("B", "同比同向"), ("C", "同比反向")):
        tr = pool[m]
        r = np.array([t["net"] for t in tr])
        print(f"{name:10s}{len(r):6d}{r.mean():+10.3f}{tst(r):7.2f}"
              f"{(r > 0).mean():8.1%}{int(np.median([t['bars'] for t in tr])):9d}{r.sum():+9.1f}")

    print("\n按品种（净均值 / n）")
    print(f"{'品种':6s}{'A':>15s}{'B':>15s}{'C':>15s}")
    for sym in SYMS:
        c = []
        for m in ("A", "B", "C"):
            tr = per.get(sym, {}).get(m, [])
            c.append(f"{np.mean([t['net'] for t in tr]):+.3f}/{len(tr)}" if tr else "—")
        print(f"{sym:6s}{c[0]:>15s}{c[1]:>15s}{c[2]:>15s}")

    print("\nB 组分年")
    for y in sorted({t["exit_date"].year for t in pool["B"]}):
        g = [t for t in pool["B"] if t["exit_date"].year == y]
        r = np.array([t["net"] for t in g])
        print(f"  {y} n={len(r):3d} 净{r.mean():+.3f} t={tst(r):5.2f} 胜率{(r>0).mean():5.1%}")

    # 判据
    a, b, cc = [np.mean([t["net"] for t in pool[m]]) for m in ("A", "B", "C")]
    print(f"\n判据：A={a:+.3f} B={b:+.3f} C={cc:+.3f}")
    if b > a and b > cc:
        print("⇒ 判据 1 成立：仓单腿保留")
    elif b > cc:
        print("⇒ 判据 2：有排序信息但不足成门槛，转仓位系数")
    else:
        print("⇒ 判据 3：仓单腿收档，不再调窗口")


if __name__ == "__main__":
    main()
