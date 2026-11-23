# -*- coding: utf-8 -*-
"""
豆系进口成本 / 盘面压榨利润腿。

逻辑：进口大豆到岸成本 = CBOT 大豆折算 + 关税 + 增值税 + 港杂。
      盘面压榨利润 = 0.785×豆粕 + 0.185×豆油 − 进口成本 − 加工费。
      利润高 → 油厂开机增 → 粕/油供给增 → 压制未来价格（或反之，两条互斥假设都测）。

防未来函数：外盘/汇率一律取 date < 国内交易日 T（allow_exact_matches=False）。
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fetch_macro as F  # noqa: E402

DATA = os.path.join(HERE, "data")

BU_PER_T = 36.7437   # 1 吨大豆 = 36.7437 蒲式耳
FREIGHT = 40.0       # USD/t，美湾→中国海运费（常数，滚动 z 中抵消）
PORT_FEE = 120.0     # CNY/t 港杂
CRUSH_FEE = 150.0    # CNY/t 加工费
TARIFF = 1.03
VAT = 1.09
MEAL_YIELD = 0.785
OIL_YIELD = 0.185


def load_domestic(sym):
    d = pd.read_csv(os.path.join(DATA, f"{sym}_1440min.csv"), parse_dates=["datetime"])
    d = d.rename(columns={"datetime": "dt"}).set_index("dt").sort_index()
    return d


def build():
    """返回 DataFrame：index=国内交易日，列 = meal/oil/cost/profit/z/z_meal/z_oil"""
    m = load_domestic("M")["close"].rename("meal")
    y = load_domestic("Y")["close"].rename("oil")
    df = pd.concat([m, y], axis=1).dropna()
    df.index = pd.to_datetime(df.index).normalize()

    s = F.load("S").rename("s")
    fx = F.load("USDCNY", "usdcny").rename("fx")
    ext = pd.concat([s, fx], axis=1).sort_index()
    ext.index = pd.to_datetime(ext.index).normalize()
    # 外盘汇率按「严格早于国内交易日」对齐
    left = pd.DataFrame({"dt": df.index})
    ext_r = ext.reset_index().rename(columns={"index": "dt"})
    mg = pd.merge_asof(left, ext_r, on="dt", direction="backward", allow_exact_matches=False)
    mg.index = df.index
    df["s"] = mg["s"].values
    df["fx"] = mg["fx"].values
    df = df.dropna(subset=["s", "fx"])

    cif_usd = df["s"] / 100.0 * BU_PER_T + FREIGHT
    df["cost"] = cif_usd * df["fx"] * TARIFF * VAT + PORT_FEE
    df["profit"] = MEAL_YIELD * df["meal"] + OIL_YIELD * df["oil"] - df["cost"] - CRUSH_FEE

    for col in ("profit", "meal", "oil"):
        mu = df[col].rolling(250, min_periods=200).mean()
        sd = df[col].rolling(250, min_periods=200).std()
        df["z" if col == "profit" else f"z_{col}"] = (df[col] - mu) / sd
    return df


def fwd(df, sym, h):
    """T+1 开盘入场，持有 h 根，开盘出。返回点数收益与 ATR（T 日值）"""
    d = load_domestic(sym)
    px = d["close"]
    op = d["open"].reindex(df.index)
    # ATR20
    hi, lo, cl = d["high"], d["low"], d["close"]
    tr = pd.concat([hi - lo, (hi - cl.shift()).abs(), (lo - cl.shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(20).mean().reindex(df.index)
    idx = df.index
    pos = {v: i for i, v in enumerate(idx)}
    # 入场 = 次日开盘；需要 idx 位置 i+1 存在
    fut_op = op.to_numpy()
    out = np.full(len(idx), np.nan)
    out_atr = atr.to_numpy()
    for i in range(len(idx) - h - 1):
        entry = fut_op[i + 1]
        exit_ = fut_op[i + 1 + h]
        if np.isfinite(entry) and np.isfinite(exit_) and entry > 0:
            out[i] = exit_ - entry
    return pd.Series(out, index=idx), pd.Series(out_atr, index=idx)


def tstat(x):
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    if len(x) < 3:
        return np.nan
    return x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))


def main():
    df = build()
    print(f"样本 {len(df)} 交易日  {df.index[0].date()} ~ {df.index[-1].date()}")
    print(f"压榨利润 均值 {df.profit.mean():.0f}  中位 {df.profit.median():.0f}  "
          f"p5 {df.profit.quantile(.05):.0f}  p95 {df.profit.quantile(.95):.0f}")
    print()

    for sym in ("M", "Y"):
        r, atr = fwd(df, sym, 20)
        df[f"f20_{sym}"] = r
        df[f"atr_{sym}"] = atr

    for h in (5, 10, 20):
        for sym in ("M", "Y"):
            r, _ = fwd(df, sym, h)
            df[f"f{h}_{sym}"] = r

    for h in (5, 10, 20):
        for sym in ("M", "Y"):
            sub = df.dropna(subset=["z", f"f{h}_{sym}"])
            lab = f"f{h}_{sym}"
            sub = sub.assign(
                rn=sub[lab] / sub[f"atr_{sym}"],
                quint=pd.qcut(sub["z"], 5, labels=["极低", "低", "中", "高", "极高"]),
            )
            print(f"=== {sym} 持有 {h} 根，z 分档（ATR 归一）===")
            print(f"{'档':>5s}{'n':>6s}{'z均值':>8s}{'净均值':>9s}{'t':>7s}{'胜率':>8s}")
            for k, g in sub.groupby("quint", observed=True):
                v = g["rn"].to_numpy()
                print(f"{str(k):>5s}{len(v):6d}{g['z'].mean():8.2f}{v.mean():+9.3f}{tstat(v):7.2f}{(v>0).mean():8.1%}")
            lo = sub[sub.quint == "极低"]["rn"]
            hi = sub[sub.quint == "极高"]["rn"]
            print(f"  极高 − 极低 = {hi.mean() - lo.mean():+.3f}")
            print()


if __name__ == "__main__":
    main()
