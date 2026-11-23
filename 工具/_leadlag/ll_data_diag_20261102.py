# -*- coding: utf-8 -*-
"""
lead-lag 数据诊断（不检验信号，只判断数据能不能用来做统计）
口径：
  - 5min bar，时间戳 = bar 开始时刻（UTC）→ 北京 +8h
  - 收益 r[i] = log(close[i]/close[i-1])，仅当 dt[i]-dt[i-1] == 5min
    （跨时段不算收益：避免把隔夜跳空当成 5min 收益）
  - 异常剔除：|r| > JUMP_CAP 视为换月/极端行情，置 NaN 并计数
判据：每品种有效收益占比 > 95%，异常剔除 < 0.5%，否则该品种不进检验。
"""
import os
import glob
import numpy as np
import pandas as pd

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "_进场体检", "数据"))
JUMP_CAP = 0.025          # 单根 5min 收益超过 2.5% 视为换月/异常
OUT = os.path.dirname(os.path.abspath(__file__))


def load_one(sym):
    f = os.path.join(SRC, f"{sym}_5min_2021-01-01_2023-12-31.csv")
    d = pd.read_csv(f, usecols=["datetime", "open", "high", "low", "close"])
    d["dt"] = pd.to_datetime(d["datetime"]) + pd.Timedelta(hours=8)
    d = d.drop_duplicates("dt").sort_values("dt").reset_index(drop=True)
    gap = d["dt"].diff() == pd.Timedelta(minutes=5)
    r = np.log(d["close"] / d["close"].shift(1))
    r = r.where(gap)                                  # 跨时段置 NaN
    jump = r.abs() > JUMP_CAP
    r = r.where(~jump)
    return pd.DataFrame({
        "dt": d["dt"],
        "close": d["close"].astype(float),
        "r": r.astype(float),
        "jump": jump,
        "valid": r.notna(),
    })


def main():
    syms = ["JM", "RB", "M", "JD", "LH", "P", "SA", "V", "MA", "RU", "SR", "EB"]
    rows, store = [], {}
    for s in syms:
        df = load_one(s)
        store[s] = df
        v = df["valid"]
        r = df.loc[v, "r"]
        rows.append({
            "sym": s,
            "bars": len(df),
            "valid": int(v.sum()),
            "valid%": round(v.mean() * 100, 2),
            "jumps": int(df["jump"].sum()),
            "jump%": round(df["jump"].mean() * 100, 3),
            "r_mean_bp": round(r.mean() * 1e4, 3),
            "r_std_bp": round(r.std() * 1e4, 1),
            "r_kurt": round(r.kurt(), 1),
            "ac1": round(r.autocorr(1), 4),
            "ac12": round(r.autocorr(12), 4),
        })

    t = pd.DataFrame(rows)
    print("=== 逐品种数据质量（5min，2020-10-26 ~ 2023-12-29）===")
    print(t.to_string(index=False))
    print()

    # 换月跳空的时间分布：若集中在某几个日期，说明是主连换月
    print("=== 异常 bar 最大的 12 个（判断是否主连换月）===")
    allj = []
    for s in syms:
        d = store[s]
        j = d[d["jump"]].copy()
        j["sym"] = s
        allj.append(j)
    j = pd.concat(allj).sort_values("r", key=lambda x: x.abs(), ascending=False)
    j["r%"] = (j["r"] * 100).round(2)
    print(j.head(12)[["dt", "sym", "r%"]].to_string(index=False))
    print()

    # 同一时刻有多少品种在交易（决定配对可用样本）
    print("=== 各配对共同有效样本量 ===")
    pairs = [("JM", "RB"), ("M", "JD"), ("M", "LH"), ("M", "P"), ("SA", "V"),
             ("RB", "JM"), ("JD", "M"), ("LH", "M"), ("RB", "RU"), ("MA", "EB")]
    pivot = pd.DataFrame({s: store[s].set_index("dt")["r"] for s in syms})
    for a, b in pairs:
        sub = pivot[[a, b]].dropna()
        print(f"  {a:3s} -> {b:3s}  共同有效 {len(sub):>6} 根  "
              f"同期 corr={sub[a].corr(sub[b]):+.3f}")

    # 保存宽表供后续复用
    pivot.to_parquet(os.path.join(OUT, "_r_wide_5min.parquet"))
    print()
    print(f"[saved] {os.path.join(OUT, '_r_wide_5min.parquet')}  shape={pivot.shape}")


if __name__ == "__main__":
    main()
