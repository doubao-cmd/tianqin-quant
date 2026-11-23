# -*- coding: utf-8 -*-
"""跨期价差均值回归 · 成本可行性验算（不是回测，只算"成本/波动"够不够）
数据：工具/_数据/展期_合约日线.csv（9 品种 × 日线 × 2016-2025）
输出：每品种 主力-次主力 固定合约对的 价差波动、AR1 半衰期、成本占比
"""
import numpy as np
import pandas as pd
import config_symbols as cs

SRC = r"C:/Users/cc/Desktop/天勤量化/工具/_数据/展期_合约日线.csv"
d = pd.read_csv(SRC, encoding="utf-8-sig")
d["日期"] = pd.to_datetime(d["日期"])
print(f"[data] {len(d)} 行  {d['日期'].min().date()} ~ {d['日期'].max().date()}\n")

rows = []
for key, g in d.groupby("key"):
    mult = float(cs.SYMBOLS[key][2])
    tick = float(cs.SYMBOLS[key][3])
    # 每日按 volume 取前 2 个合约
    g = g.sort_values(["日期", "volume"], ascending=[True, False])
    top2 = g.groupby("日期").head(2)
    if top2.groupby("日期").size().max() < 2:
        continue
    piv_c = top2.pivot_table(index="日期", columns="sym", values="close")
    piv_v = top2.pivot_table(index="日期", columns="sym", values="volume")
    recs = []
    for day in piv_c.index:
        v = piv_v.loc[day].dropna().sort_values(ascending=False)
        if len(v) < 2:
            continue
        s1, s2 = v.index[0], v.index[1]
        c1, c2 = piv_c.loc[day, s1], piv_c.loc[day, s2]
        if pd.isna(c1) or pd.isna(c2):
            continue
        recs.append((day, s1, s2, float(c1) - float(c2), float(c1)))
    x = pd.DataFrame(recs, columns=["day", "s1", "s2", "spread", "px"])
    x["pair"] = x["s1"].astype(str) + "|" + x["s2"].astype(str)

    # 只保留连续 >=40 天的固定对（避开换月跳变污染波动估计）
    stats = []
    for pair, gg in x.groupby("pair"):
        gg = gg.sort_values("day")
        if len(gg) < 40:
            continue
        sp = gg["spread"].to_numpy(float)
        dsp = np.diff(sp)
        sd = np.std(dsp, ddof=1)
        if sd <= 0:
            continue
        # AR1: sp_t = a + b*sp_{t-1}，半衰期 = -ln2/ln(1+b)（b<0 才回归）
        b = np.polyfit(sp[:-1], sp[1:], 1)[0] if len(sp) > 5 else np.nan
        hl = (-np.log(2) / np.log(b)) if (b and 0 < b < 1) else np.nan
        stats.append({"pair": pair, "n": len(gg), "sd_d": sd,
                      "lvl": float(np.median(sp)), "b": b, "hl": hl})
    if not stats:
        continue
    st = pd.DataFrame(stats)
    med_price = float(x["px"].median())
    comm = float(cs.comm_per_hand(key, med_price))
    slip = 4 * tick * mult          # 4 笔成交 × 1 跳
    cost = slip + 4 * comm
    sd_d = float(st["sd_d"].median())
    hl = float(st["hl"].median()) if st["hl"].notna().any() else np.nan
    rows.append({
        "key": key, "pairs": len(st), "days": int(st["n"].sum()),
        "spread中位": round(float(st["lvl"].median()), 1),
        "日Δσ": round(sd_d, 2),
        "成本元": round(cost, 1),
        "佣金4笔": round(4 * comm, 1),
        "滑点4笔": round(slip, 1),
        "成本/日σ": f"{cost / sd_d * 100:.0f}%",
        "成本/10日σ": f"{cost / (sd_d * np.sqrt(10)) * 100:.0f}%",
        "AR1_b": round(float(st["b"].median()), 3),
        "半衰期天": round(hl, 1) if hl == hl else None,
    })

out = pd.DataFrame(rows).sort_values("成本/10日σ")
pd.set_option("display.width", 250)
pd.set_option("display.max_columns", 30)
print(out.to_string(index=False))
print()
print("判据：成本/10日σ < 30% ⇒ 10 天持有期有经济空间；> 60% ⇒ 需更长持有或放弃")
print("（跨品种配对的历史基准：成本/σ 6.6~12% vs 门槛 4.7% ⇒ 全部判死）")
