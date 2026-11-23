# -*- coding: utf-8 -*-
"""
排列信号 · 止损宽度与出场速度研究（2026-11-22）
==============================================================
只回答两个结构性问题，不做参数择优：

  Q1 价格止损能不能把「最终会赚的笔」和「最终会亏的笔」分开？
     → 算每笔持有期间的 MAE（最大不利偏移，ATR 倍数），按最终盈亏分两群看分位数。
     → 若两群分布重叠严重，任何 a×ATR 止损都是同时砍两边，不是"止损"而是"随机切"。

  Q2 该不该把出场放慢（让利润跑）？
     → 出场变体 base=MA5反叉MA10（现行） / slow=MA10反叉MA20
     → 分 2016–2020 / 2021–2026 两段各报一次，看方向是否一致（不挑最优）。

口径与 screen.py 完全一致：T日收盘判定 → T+1开盘成交，ATR20 用 i-1 及之前，1跳/边滑点。
"""
from __future__ import annotations
import os, sys, glob
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import screen as S

DATA = os.path.join(HERE, "..", "_supertrend_ts", "data")
STOP_GRID = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0]


def run_trades(o: pd.DataFrame, tier: str, exit_mode: str = "base") -> list[dict]:
    """返回逐笔，含 mae / mfe（ATR 倍数）与入场/出场行号。"""
    col = "confirm_trig" if tier == "confirm" else "early"
    trades, pos = [], None
    n = len(o)
    for i in range(1, n):
        if pos is not None:
            m_a, m_b = (o.at[i - 1, "ma5"], o.at[i - 1, "ma10"]) if exit_mode == "base" \
                else (o.at[i - 1, "ma10"], o.at[i - 1, "ma20"])
            if pd.notna(m_a) and pd.notna(m_b):
                hit = (pos["dir"] == 1 and m_a <= m_b) or (pos["dir"] == -1 and m_a >= m_b)
                if hit:
                    trades.append(_close(o, pos, i))
                    pos = None
        if pos is None:
            sig = int(o.at[i - 1, col]) if pd.notna(o.at[i - 1, col]) else 0
            atr = o.at[i - 1, "atr"]
            if sig != 0 and pd.notna(atr) and atr > 0:
                pos = dict(entry_i=i, entry=float(o.at[i, "open"]), dir=sig, atr=float(atr))
    if pos is not None:                      # 尾部未平：按最后收盘平
        trades.append(_close(o, pos, n - 1, last=True))
    return trades


def _close(o, pos, j, last=False):
    px = float(o.at[j, "close"]) if last else float(o.at[j, "open"])
    seg = o.iloc[pos["entry_i"]:j]           # 入场当日 + 中间整根（不含出场那根，保守）
    hi = float(seg["high"].max()) if len(seg) else px
    lo = float(seg["low"].min()) if len(seg) else px
    d, atr = pos["dir"], pos["atr"]
    mfe = (hi - pos["entry"]) / atr if d == 1 else (pos["entry"] - lo) / atr
    mae = (pos["entry"] - lo) / atr if d == 1 else (hi - pos["entry"]) / atr
    return dict(entry=pos["entry"], exit=px, dir=d, atr=atr,
                bars=j - pos["entry_i"], r=(px - pos["entry"]) * d / atr,
                mfe=max(0.0, float(mfe)), mae=max(0.0, float(mae)),
                exit_date=o.at[j, "datetime"])


def pct(a, qs=(10, 25, 50, 75, 90)):
    a = np.asarray(a, dtype=float)
    return {f"p{q}": round(float(np.percentile(a, q)), 2) for q in qs}


def describe(tr: list[dict], tag: str):
    if not tr:
        print(f"  {tag}: 无样本")
        return
    r = np.array([t["net"] for t in tr])
    win = [t for t in tr if t["net"] > 0]
    los = [t for t in tr if t["net"] <= 0]
    print(f"  {tag:14s} n={len(tr):4d} 净均值{r.mean():+.3f} 胜率{(r > 0).mean():.1%} "
          f"中位持有{int(np.median([t['bars'] for t in tr]))}根")
    if win:
        print(f"    赢家 n={len(win):4d}  MAE {pct([t['mae'] for t in win])}   "
              f"MFE中位{np.median([t['mfe'] for t in win]):.2f}")
    if los:
        print(f"    输家 n={len(los):4d}  MAE {pct([t['mae'] for t in los])}   "
              f"MFE中位{np.median([t['mfe'] for t in los]):.2f}")


def stop_curve(tr: list[dict]) -> pd.DataFrame:
    rows, base = [], np.array([t["net"] for t in tr])
    for s in STOP_GRID:
        adj = np.array([(-s if t["mae"] >= s else t["r"]) - t["cost_atr"] for t in tr])
        killed_win = sum(1 for t in tr if t["mae"] >= s and t["net"] > 0)
        saved_los = sum(1 for t in tr if t["mae"] >= s and t["net"] <= 0)
        worse = sum(1 for t in tr if t["mae"] >= s and (t["r"] - t["cost_atr"]) > (-s - t["cost_atr"]))
        rows.append(dict(stop=s, mean=adj.mean(), total=adj.sum(),
                         delta=(adj.mean() - base.mean()) * len(tr),
                         killed_win=killed_win, saved_los=saved_los, hurt=worse))
    return pd.DataFrame(rows)


def main():
    files = sorted(glob.glob(os.path.join(DATA, "*_1440min.csv")))
    print(f"数据 {len(files)} 品种\n" + "=" * 78)
    pool = {"base": [], "slow": []}
    for f in files:
        sym = os.path.basename(f).split("_")[0]
        d = S.load(f)
        if d is None:
            continue
        o = S.signals(S.indicators(d))
        for mode in ("base", "slow"):
            tr = run_trades(o, "confirm", mode)
            for t in tr:
                t["sym"] = sym
            S.add_cost(tr, 1.0)
            pool[mode].extend(tr)

    print("\n【Q1】赢家 vs 输家的 MAE 分布（confirm，持有期间 ATR 倍数）")
    print("-" * 78)
    describe(pool["base"], "全部")

    print("\n  按持有期分群（现行出场）")
    for lo, hi, lab in [(1, 3, "1-3根"), (4, 6, "4-6根"), (7, 12, "7-12根"),
                        (13, 25, "13-25根"), (26, 999, "26根+")]:
        sub = [t for t in pool["base"] if lo <= t["bars"] <= hi]
        if sub:
            describe(sub, lab)

    print("\n【Q1b】止损有效性曲线（confirm，全样本）")
    print("-" * 78)
    sc = stop_curve(pool["base"])
    print(sc.to_string(index=False,
                       formatters={"mean": "{:+.3f}".format, "total": "{:+.1f}".format,
                                   "delta": "{:+.1f}".format}))
    print("  hurt = 被止损后比原结果更差的笔数（原结果本来就亏更多的不算）")

    print("\n【Q2】出场速度：MA5×MA10（现行） vs MA10×MA20（放慢），分两段")
    print("-" * 78)
    for lab, y0, y1 in [("2016–2020", 2016, 2020), ("2021–2026", 2021, 2026), ("全样本", 2016, 2026)]:
        for mode in ("base", "slow"):
            sub = [t for t in pool[mode] if y0 <= t["exit_date"].year <= y1]
            describe(sub, f"{lab} {mode}")

    out = os.path.join(HERE, "out_stop_study.csv")
    pd.DataFrame([{k: t[k] for k in ("sym", "entry_date" if "entry_date" in t else "exit_date",
                                     "bars", "r", "net", "mae", "mfe", "dir")}
                  for t in pool["base"]]).to_csv(out, index=False, encoding="utf-8-sig")
    print(f"\n逐笔落盘 {out}")


if __name__ == "__main__":
    main()
