# -*- coding: utf-8 -*-
"""
排列信号 · 日线三线基线
==============================================================
定义（写死，任何改动须另立预注册）：

  均线：MA5 / MA10 / MA20，收盘价简单均线
  确认档 confirm：
      多  MA5 > MA10 > MA20
      空  MA5 < MA10 < MA20
      只在「从非排列变为排列」的那一天记一次信号（状态转移，非状态持续）
  苗头档 early：
      多  MA5 上穿 MA10（昨 MA5<=MA10 且今 MA5>MA10）且 MA10 > MA20
      空  MA5 下穿 MA10 且 MA10 < MA20
  出场：
      MA5 与 MA10 反向交叉（多：MA5<=MA10；空：MA5>=MA10）→ 次日开盘平

时点（铁律 1）：
  信号用第 i 日「收盘后」的数据判定 → 第 i+1 日「开盘」成交
  ATR20 用 i-1 及之前（避免用当日振幅归一化当日入场）

口径：
  固定 1 手、毛层（不扣手续费滑点）、收益以 ATR 倍数计（跨品种可比）
"""
from __future__ import annotations
import os, sys, json, glob
import numpy as np
import pandas as pd

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
MA = (5, 10, 20)
ATR_N = 20


def load(path: str) -> pd.DataFrame | None:
    d = pd.read_csv(path, encoding="utf-8-sig")
    d.columns = [str(c).strip().lower() for c in d.columns]
    need = {"datetime", "open", "high", "low", "close"}
    if not need.issubset(d.columns):
        return None
    d["datetime"] = pd.to_datetime(d["datetime"])
    d = d.sort_values("datetime").drop_duplicates("datetime").reset_index(drop=True)
    for c in ("open", "high", "low", "close"):
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
    if len(d) < 80:
        return None
    return d


def indicators(d: pd.DataFrame) -> pd.DataFrame:
    o = d.copy()
    for n in MA:
        o[f"ma{n}"] = o["close"].rolling(n).mean()
    # ATR20：TR = max(H-L, |H-C_prev|, |L-C_prev|)
    pc = o["close"].shift(1)
    tr = pd.concat([o["high"] - o["low"], (o["high"] - pc).abs(), (o["low"] - pc).abs()], axis=1).max(axis=1)
    o["atr"] = tr.rolling(ATR_N).mean()
    # 全部平移一格 = 「收盘后可得」，供次日开盘决策
    for c in ("ma5", "ma10", "ma20", "atr"):
        o[f"{c}_prev"] = o[c].shift(1)
    return o


def signals(o: pd.DataFrame) -> pd.DataFrame:
    # ★ 用第 i 行「原始」均线 = 第 i 日「收盘后」可得；backtest 在 i+1 行开盘成交。
    #   （早期版本这里错用了 _prev，导致信号实际晚一天，已修 2026-11-21）
    ma5, ma10, ma20 = o["ma5"], o["ma10"], o["ma20"]
    bull_c = (ma5 > ma10) & (ma10 > ma20)
    bear_c = (ma5 < ma10) & (ma10 < ma20)
    o["confirm"] = np.where(bull_c, 1, np.where(bear_c, -1, 0))

    up = (ma5 > ma10) & (ma5.shift(1) <= ma10.shift(1))
    dn = (ma5 < ma10) & (ma5.shift(1) >= ma10.shift(1))
    o["early"] = np.where(up & (ma10 > ma20), 1, np.where(dn & (ma10 < ma20), -1, 0))

    # 确认档只在状态转移那天算新信号
    prev_c = o["confirm"].shift(1).fillna(0)
    o["confirm_trig"] = np.where((o["confirm"] != 0) & (o["confirm"] != prev_c), o["confirm"], 0)
    return o


def backtest(o: pd.DataFrame, tier: str) -> list[dict]:
    """tier: confirm / early。单笔、不加仓、反向信号不平仓（出场只看 MA5/MA10 反叉）。"""
    col = "confirm_trig" if tier == "confirm" else "early"
    trades, pos = [], None
    for i in range(1, len(o)):
        if pos is not None:
            # 出场：用 i-1 收盘的 ma5_prev/ma10_prev 判反叉（即 i-1 收盘已知）→ i 开盘平
            m5, m10 = o.at[i - 1, "ma5"], o.at[i - 1, "ma10"]
            if pd.notna(m5) and pd.notna(m10):
                hit = (pos["dir"] == 1 and m5 <= m10) or (pos["dir"] == -1 and m5 >= m10)
                if hit:
                    px = o.at[i, "open"]
                    trades.append(dict(
                        entry_date=pos["date"], exit_date=o.at[i, "datetime"],
                        dir=pos["dir"], entry=pos["px"], exit=float(px),
                        atr=pos["atr"], bars=i - pos["i"],
                        r=(float(px) - pos["px"]) * pos["dir"] / pos["atr"],
                    ))
                    pos = None
        if pos is None:
            sig = int(o.at[i - 1, col]) if pd.notna(o.at[i - 1, col]) else 0
            atr = o.at[i - 1, "atr"]
            if sig != 0 and pd.notna(atr) and atr > 0:
                pos = dict(date=o.at[i, "datetime"], px=float(o.at[i, "open"]),
                           dir=sig, atr=float(atr), i=i)
    return trades


def _spec():
    """读项目根 config_symbols，拿每手手续费与最小跳动。缺失则返回 None（跳过成本口径）。"""
    try:
        root = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
        if root not in sys.path:
            sys.path.insert(0, root)
        import config_symbols as cs
        return cs
    except Exception as e:
        print(f"[warn] 成本口径不可用：{e}")
        return None


def add_cost(tr: list[dict], ticks_per_side: float = 1.0) -> None:
    """每笔扣「双边手续费 + ticks_per_side 跳/边滑点」，结果写回 t['net']（ATR 倍数）。"""
    cs = _spec()
    if cs is None:
        for t in tr:
            t["net"] = t["r"]
        return
    for t in tr:
        k = t["sym"].upper()
        try:
            _, _, mult, tick = cs.SYMBOLS[k]
            fee = float(cs.comm_per_hand(k, t["entry"])) * 2
            slip = float(tick) * float(mult) * 2 * ticks_per_side
            atr_amt = t["atr"] * float(mult)
            t["cost_atr"] = (fee + slip) / atr_amt if atr_amt > 0 else 0.0
        except Exception:
            t["cost_atr"] = 0.0
        t["net"] = t["r"] - t["cost_atr"]


def stats(tr: list[dict], key: str = "r") -> dict:
    if not tr:
        return dict(n=0)
    r = np.array([t[key] for t in tr], dtype=float)
    n = len(r)
    mean, sd = r.mean(), r.std(ddof=1) if n > 1 else np.nan
    t = mean / (sd / np.sqrt(n)) if n > 1 and sd > 0 else np.nan
    s = np.sort(r)[::-1]
    return dict(
        n=int(n), mean=round(float(mean), 4),
        t=round(float(t), 2) if np.isfinite(t) else None,
        win=round(float((r > 0).mean()), 3),
        worst=round(float(r.min()), 2), best=round(float(r.max()), 2),
        ex_top5=round(float(r.sum() - s[:5].sum()) / n, 4) if n > 5 else None,
        ex_top10=round(float(r.sum() - s[:10].sum()) / n, 4) if n > 10 else None,
        ex_top20=round(float(r.sum() - s[:20].sum()) / n, 4) if n > 20 else None,
        top10_share=round(float(s[:10].sum() / r.sum()), 3) if r.sum() > 0 else None,
        med_bars=int(np.median([t["bars"] for t in tr])),
    )


def main():
    files = sorted(glob.glob(os.path.join(DATA, "*_1440min.csv")))
    if not files:
        files = sorted(glob.glob(os.path.join("..", "_supertrend_ts", "data", "*_1440min.csv")))
    print(f"数据文件 {len(files)} 个\n")
    all_res, pools = {}, {}
    for tier in ("confirm", "early"):
        per_sym, pool = {}, []
        for f in files:
            sym = os.path.basename(f).split("_")[0]
            d = load(f)
            if d is None:
                continue
            o = signals(indicators(d))
            tr = backtest(o, tier)
            for t in tr:
                t["sym"] = sym
            add_cost(tr, 1.0)                      # 1 跳/边 + 双边手续费
            per_sym[sym] = dict(gross=stats(tr, "r"), net=stats(tr, "net"))
            pool.extend(tr)
        all_res[tier] = dict(gross=stats(pool, "r"), net=stats(pool, "net"), per_sym=per_sym)
        pools[tier] = pool
        g, nt = all_res[tier]["gross"], all_res[tier]["net"]
        print(f"===== {tier} =====")
        print(f"  毛：n={g['n']} 均值{g['mean']:+.4f} t={g['t']} 胜率{g['win']:.1%} "
              f"最差{g['worst']:+.2f} 最好{g['best']:+.2f} 中位持有{g['med_bars']}根")
        print(f"  净：均值{nt['mean']:+.4f} t={nt['t']} 胜率{nt['win']:.1%} "
              f"剔Top10后{nt['ex_top10']} Top10占{nt['top10_share']}")
        print(f"  平均成本 {np.mean([t['cost_atr'] for t in pool]):.4f} ATR/笔")
        print()

    # 按品种（confirm，净口径）
    print("=== confirm 按品种（净）===")
    for sym, v in all_res["confirm"]["per_sym"].items():
        g, nt = v["gross"], v["net"]
        print(f"  {sym:4s} n={g['n']:4d}  毛{g['mean']:+.3f}  净{nt['mean']:+.3f}  t={nt['t']}")

    rows = []
    per_year = {}
    for t in pools["confirm"]:
        per_year.setdefault(t["exit_date"].year, []).append(t)
    print("=== confirm 分年（净）===")
    for y in sorted(per_year):
        a = np.array([x["net"] for x in per_year[y]])
        rows.append({"year": int(y), "n": len(a), "mean": round(float(a.mean()), 4),
                     "sum": round(float(a.sum()), 2), "win": round(float((a > 0).mean()), 3)})
        print(f"  {y}  n={len(a):4d}  净均值{a.mean():+.3f}  合计{a.sum():+7.1f}  胜率{(a>0).mean():.1%}")

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out_baseline.json")
    with open(out, "w", encoding="utf-8") as fp:
        json.dump({"config": {"ma": MA, "atr": ATR_N, "tiers": ["confirm", "early"],
                              "cost": "双边手续费 + 1跳/边滑点"},
                   "result": all_res, "by_year_confirm": rows}, fp, ensure_ascii=False, indent=1)
    print(f"\n落盘 {out}")


if __name__ == "__main__":
    main()
