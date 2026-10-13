# -*- coding: utf-8 -*-
"""
近似订单流探针 —— 用 CTP 五档快照（非逐笔）探查微观结构是否携带 bar 数据没有的信息
====================================================================================
⚠️ 数据源：工具/_CTP接入/tick/ 下 SimNow 模拟盘口快照（2026-10-12 录制）。
   SimNow 是模拟环境、盘口无真实主力行为，本探针**只验证工具逻辑与指标形态**，
   不构成任何 edge 证据。真实验证需交易所 Level-2 / 逐笔行情授权。

⚠️ 标准 CTP 行情是「快照」（≈2 笔/秒），不是「逐笔成交」：
   - 没有每笔成交的主动买卖方向；
   - 只能近似（五档失衡 / 深度 delta / last 穿档推断）。

指标：
   1) imbalance_t = (Σbidv1..5 − Σaskv1..5) / (Σbidv1..5 + Σaskv1..5)   五档量失衡
   2) depth_t     = Σbidv + Σaskv；depth_delta = depth_t − depth_{t-1}
   3) tick_dir    = last≤bid1 → 主动卖(−1) / last≥ask1 → 主动买(+1)（近似成交方向）
   4) 预测力检验：imbalance_t 与「未来 K 个 tick 的 mid 收益」的 Spearman 相关
      |rho| 显著 ≠ 0 ⇒ 微观结构有短周期信息；否则近似订单流无效。

用法：
   python 探针.py [--dir ../_CTP接入/tick] [--k 20] [--out 探针结果.md]
"""
import os
import glob
import argparse
import numpy as np
import pandas as pd

try:
    from scipy.stats import spearmanr
    _HAVE_SCIPY = True
except Exception:
    _HAVE_SCIPY = False

_HERE = os.path.dirname(os.path.abspath(__file__))


def load(dir_):
    files = sorted(glob.glob(os.path.join(dir_, "**", "*.parquet"), recursive=True))
    if not files:
        raise SystemExit(f"无 parquet：{dir_}")
    import duckdb
    con = duckdb.connect()
    src = "read_parquet(?, hive_partitioning=1)"
    q = f"""
    SELECT sym, ts,
           (bidv1+bidv2+bidv3+bidv4+bidv5) AS bid_vol,
           (askv1+askv2+askv3+askv4+askv5) AS ask_vol,
           (bid1+ask1)/2.0                  AS mid,
           last, bid1, ask1
    FROM {src}
    ORDER BY sym, ts
    """
    df = con.execute(q, [files]).df()
    con.close()
    return df


def analyze(df, k):
    rows = []
    for sym, g in df.groupby("sym"):
        g = g.sort_values("ts").reset_index(drop=True)
        n = len(g)
        bv = g["bid_vol"].to_numpy(float)
        av = g["ask_vol"].to_numpy(float)
        tot = bv + av
        imb = np.where(tot > 0, (bv - av) / tot, 0.0)
        mid = g["mid"].to_numpy(float)
        ret = np.zeros(n)
        ret[1:] = np.diff(mid) / mid[:-1]
        fwd = np.zeros(n)
        fwd[:-k] = mid[k:] / mid[:-k] - 1.0
        m = (~np.isnan(imb)) & (~np.isnan(fwd)) & (fwd != 0)
        rho = p = np.nan
        if m.sum() > 10:
            s_imb = pd.Series(imb[m])
            s_fwd = pd.Series(fwd[m])
            rho = s_imb.corr(s_fwd, method="spearman")
            if _HAVE_SCIPY:
                rho, p = spearmanr(imb[m], fwd[m])
        last = g["last"].to_numpy(float)
        bid1 = g["bid1"].to_numpy(float)
        ask1 = g["ask1"].to_numpy(float)
        direction = np.where(last <= bid1, -1,
                     np.where(last >= ask1, 1, 0))
        buy = float((direction == 1).mean())
        sell = float((direction == -1).mean())
        rows.append({
            "品种": sym,
            "条数": n,
            "imbalance均值": round(float(np.mean(imb)), 4),
            "imbalance_std": round(float(np.std(imb)), 4),
            f"未来{k}tick_spearman_rho": (round(float(rho), 4)
                                          if rho == rho else None),
            "p值": (round(float(p), 4) if (p == p and _HAVE_SCIPY) else "n/a"),
            "主动买占比%": round(buy * 100, 1),
            "主动卖占比%": round(sell * 100, 1),
        })
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.join(_HERE, "..", "_CTP接入", "tick"))
    ap.add_argument("--k", type=int, default=20)
    ap.add_argument("--out", default=os.path.join(_HERE, "探针结果.md"))
    a = ap.parse_args()

    df = load(a.dir)
    res = analyze(df, a.k)

    lines = [
        "# 近似订单流探针结果",
        "",
        f"- 数据源：`{a.dir}`",
        "- ⚠️ **SimNow 模拟盘口快照，仅验证工具逻辑，不构成 edge 证据**",
        f"- 预测力窗口：未来 {a.k} 个 tick 的 mid 收益 vs 五档量失衡（Spearman）",
        "- 指标：imbalance=(Σ买量−Σ卖量)/(Σ买量+Σ卖量)；tick_dir 由 last 落档推断（近似）",
        "",
        "## 逐品种",
        "",
        res.to_markdown(index=False),
        "",
        "## 解读",
        "",
        "- `未来Ktick_spearman_rho` 显著 ≠ 0 才说明微观结构有短周期预测力；",
        "  若全品种 |rho|≈0 ⇒ 近似订单流（五档快照）**无效**，需真逐笔(L2)才有意义。",
        "- SimNow 盘口无真实主力行为，即使此处出现显著相关也**不能**当证据，",
        "  须在真实盘口 + 历史逐笔数据集上重做。",
    ]
    txt = "\n".join(lines)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(txt)
    print(txt)
    print(f"\n[done] -> {a.out}")


if __name__ == "__main__":
    main()
