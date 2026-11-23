# -*- coding: utf-8 -*-
"""
信息源覆盖矩阵 —— 一眼看清每个品种手里有什么、缺什么、缺的那块能不能补。

七类信息源：
    价格      data/<SYM>_1440min.csv        （自己拉，2016 起）
    持仓量    data_oi/<SYM>_oi.csv          （自己拉，2016 起）
    仓单      np.db  metric=receipt         （2018 起，大商所缺）
    基差      np.db  metric=dom_basis       （2018 起）
    现货价    np.db  metric=spot_price      （2018 起）
    会员排名  np.db  metric=top20_long      （2018 起，大商所缺）
    库存      _inventory/<SYM>.csv          （东财，仅 2026-08 起）
    成本端    按品种归类的 _macro 序列       （见 COST_MAP）

用法：python coverage.py          打印矩阵
      python coverage.py --csv    同时导出 coverage.csv
"""
import glob
import os
import sqlite3
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
NP = os.path.join(HERE, "..", "_np_data", "np.db")

# 每个品种的成本端外盘序列（空 = 无对应外盘/由国内定价）
COST_MAP = {
    "M": ["S"], "Y": ["S"], "A": ["S"], "P": ["FCPO"],
    "C": ["C"], "CS": ["C"], "JD": ["C"], "LH": ["C"],
    "CU": ["CAD", "HG"], "AL": ["CAD"], "ZN": ["CAD"], "NI": ["CAD"],
    "SN": ["CAD"], "PB": ["CAD"],
    "SC": ["CL", "OIL"], "FU": ["CL", "OIL"], "BU": ["CL", "OIL"], "PG": ["CL", "OIL"],
    "TA": ["CL", "OIL"], "EG": ["CL", "OIL"], "PP": ["CL", "OIL"],
    "L": ["CL", "OIL"], "V": ["CL", "OIL"], "EB": ["CL", "OIL"],
    "RU": ["CL"], "SP": ["CL"],
    "AU": ["GC"], "AG": ["GC", "SI"], "CF": ["CT"],
}


def _np_counts(metric):
    if not os.path.exists(NP):
        return {}
    c = sqlite3.connect(NP)
    try:
        return {r[0]: r[1] for r in c.execute(
            "select symbol,count(*) from np_data where metric=? group by 1", (metric,))}
    except Exception:
        return {}
    finally:
        c.close()


def _csv_info(path, col=None):
    if not os.path.exists(path):
        return None
    try:
        d = pd.read_csv(path)
    except Exception:
        return None
    if len(d) < 50:
        return None
    first = str(d.iloc[0, 0])[:10]
    if col and col in d:
        d = d[d[col] > 0]
    if len(d) < 50:
        return None
    return len(d), first


def main():
    syms = sorted(os.path.basename(f).split("_")[0]
                  for f in glob.glob(os.path.join(HERE, "data", "*_1440min.csv")))
    npc = {m: _np_counts(m) for m in ("receipt", "dom_basis", "spot_price", "top20_long")}

    rows = []
    for s in syms:
        px = _csv_info(os.path.join(HERE, "data", f"{s}_1440min.csv"))
        oi = _csv_info(os.path.join(HERE, "data_oi", f"{s}_oi.csv"), "open_interest")
        inv = _csv_info(os.path.join(HERE, "_inventory", f"{s}.csv"), "inventory")
        cost = COST_MAP.get(s, [])
        rows.append(dict(
            sym=s,
            价格=px[0] if px else 0,
            价格起=px[1] if px else "",
            持仓量=oi[0] if oi else 0,
            仓单=npc["receipt"].get(s, 0),
            基差=npc["dom_basis"].get(s, 0),
            现货=npc["spot_price"].get(s, 0),
            会员排名=npc["top20_long"].get(s, 0),
            库存=inv[0] if inv else 0,
            成本端="+".join(cost) if cost else "",
        ))
    df = pd.DataFrame(rows)

    print(f"品种数 {len(df)}   价格起 {df.价格起.min()}\n")
    hdr = f"{'品种':4s}{'价格':>7s}{'持仓量':>8s}{'仓单':>7s}{'基差':>7s}{'现货':>7s}{'会员排名':>8s}{'库存':>7s}  {'成本端'}"
    print(hdr)
    print("-" * len(hdr))
    for _, r in df.iterrows():
        def c(v):
            return f"{int(v):>7,d}" if v else f"{'—':>7s}"
        print(f"{r.sym:4s}{c(r.价格)}{c(r.持仓量)}{c(r.仓单)}{c(r.基差)}{c(r.现货)}{c(r.会员排名)}{c(r.库存):>8s}  {r.成本端}")

    print()
    n = len(df)
    for col in ("价格", "持仓量", "仓单", "基差", "现货", "会员排名", "库存"):
        k = int((df[col] > 0).sum())
        print(f"  {col:5s} {k:2d}/{n}  缺: {'、'.join(df[df[col] == 0].sym.tolist()) if k < n else '无'}")
    k = int((df.成本端 != "").sum())
    print(f"  成本端 {k:2d}/{n}  缺: {'、'.join(df[df.成本端 == ''].sym.tolist())}")

    if "--csv" in sys.argv:
        p = os.path.join(HERE, "coverage.csv")
        df.to_csv(p, index=False, encoding="utf-8-sig")
        print(f"\n已导出 {p}")


if __name__ == "__main__":
    main()
