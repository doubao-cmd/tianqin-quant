# -*- coding: utf-8 -*-
"""
截面基差因子 —— 样本期（2019-2021）**真实流动性**核查。

为什么必须做这一步（而不是用 2026-10 的持仓快照）：
    品种池的剔除必须基于**样本期内**的事实。用"今天的持仓量"去判 2019 年的可交易性
    是时间错配 —— 会既冤枉了当年活跃、如今沉寂的品种，也放过当年就死透的品种。

数据：akshare futures_main_sina（新浪主连日线，含成交量/持仓量），只读、不连交易链路。
输出：数据/样本期流动性.csv + 屏幕排序表
"""
import os
import time
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
import akshare as ak

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "数据")
OUT = os.path.join(DATA, "样本期流动性.csv")
os.makedirs(DATA, exist_ok=True)

POOL = "A AG AL AU BU C CF CU CY EG FG FU HC I J JD JM L M MA NI OI P PB PM PP RB RM RU SF SM SN SP SR TA V WR Y ZN".split()
START, END = "2019-01-01", "2021-12-31"

# 判定阈值（**一次定死，不扫**；取自行业惯例"主力合约日均持仓 < 1 万手视为流动性不足"）
OI_MIN = 10000


def main():
    rows = []
    for i, k in enumerate(POOL, 1):
        try:
            d = ak.futures_main_sina(symbol=f"{k}0")
            d["日期"] = pd.to_datetime(d["日期"])
            s = d[(d["日期"] >= START) & (d["日期"] <= END)].copy()
            if not len(s):
                rows.append({"key": k, "days": 0, "oi_med": 0, "vol_med": 0,
                             "zero_vol_pct": 100.0, "oi_min": 0, "note": "样本期无数据"})
                print(f"[{i:2d}/39] {k:<5} 样本期无数据", flush=True)
                continue
            s["持仓量"] = pd.to_numeric(s["持仓量"], errors="coerce").fillna(0)
            s["成交量"] = pd.to_numeric(s["成交量"], errors="coerce").fillna(0)
            rows.append({
                "key": k, "days": len(s),
                "oi_med": float(s["持仓量"].median()),
                "oi_min": float(s["持仓量"].min()),
                "vol_med": float(s["成交量"].median()),
                "zero_vol_pct": float((s["成交量"] == 0).mean() * 100),
                "note": "",
            })
            print(f"[{i:2d}/39] {k:<5} 天数={len(s):4d} 持仓中位={s['持仓量'].median():>10,.0f} "
                  f"成交中位={s['成交量'].median():>10,.0f}", flush=True)
        except Exception as e:
            rows.append({"key": k, "days": 0, "oi_med": 0, "vol_med": 0,
                         "zero_vol_pct": 100.0, "oi_min": 0, "note": f"ERR {type(e).__name__}"})
            print(f"[{i:2d}/39] {k:<5} ERR {type(e).__name__}: {str(e)[:60]}", flush=True)
        time.sleep(0.2)

    df = pd.DataFrame(rows)
    df.to_csv(OUT, index=False, encoding="utf-8-sig")

    # 假定 2019-2021 约 730 个交易日；交易日覆盖太少的也算数据不足
    TRADING_DAYS = 730
    df["覆盖%"] = (df["days"] / TRADING_DAYS * 100).round(1)
    df["判定"] = np.where(
        (df["oi_med"] < 1000) | (df["days"] < 100), "⛔ 僵尸/数据不足",
        np.where(df["oi_med"] < OI_MIN, "⚠️ 流动性不足", "✅ 可用"))

    print("\n" + "=" * 104)
    print(f"样本期（{START} ~ {END}）流动性排序 —— 由低到高（阈值：主力日均持仓 ≥ {OI_MIN:,} 手）")
    print("=" * 104)
    print(f"{'品种':<5}{'交易日':>7}{'覆盖%':>8}{'持仓中位':>12}{'持仓最小':>11}{'成交中位':>12}{'零成交%':>9}  判定")
    for _, r in df.sort_values("oi_med").iterrows():
        print(f"{r['key']:<5}{int(r['days']):>7}{r['覆盖%']:>8.1f}{r['oi_med']:>12,.0f}"
              f"{r['oi_min']:>11,.0f}{r['vol_med']:>12,.0f}{r['zero_vol_pct']:>9.1f}  {r['判定']}")

    bad = df[df["判定"].str.startswith(("⛔", "⚠️"))]["key"].tolist()
    print(f"\n⛔/⚠️ 共 {len(bad)} 个：{', '.join(bad) if bad else '无'}")
    print(f"✅ 可用 {int((df['判定'].str.startswith('✅')).sum())} 个")
    print(f"\n落盘 {OUT}")


if __name__ == "__main__":
    import sys

    class _Tee:
        """同时输出到终端与内存，最后以 LF 落盘（`> 日志.txt` 重定向会写成 CRLF）。"""

        def __init__(self, real):
            self.real, self.buf = real, []

        def write(self, s):
            self.buf.append(s)
            self.real.write(s)

        def flush(self):
            self.real.flush()

    _t = _Tee(sys.stdout)
    sys.stdout = _t
    try:
        main()
    finally:
        sys.stdout = _t.real
        log = os.path.join(HERE, "流动性核查_结果.txt")
        with open(log, "w", encoding="utf-8", newline="\n") as f:
            f.write("".join(_t.buf))
        print(f"（核查日志已以 LF 落盘：{log}）")
