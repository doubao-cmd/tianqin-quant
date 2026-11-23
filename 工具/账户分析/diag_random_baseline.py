# -*- coding: utf-8 -*-
"""
随机入场基准线（Monte Carlo baseline）

用途：给「MA13 止损单 54% 从未浮盈」这个数字一个参照系——
      随机方向 + 随机入场 + 同样持仓长度，会不会也是这个比例？
      若接近，则说明 MA13 的入场信号在「进场后是否曾浮盈」上无信息量。

★ 纯统计对照，不涉及策略改动。
"""
import os
import random

import pandas as pd
import numpy as np

ROOT = r"C:\Users\cc\Desktop\天勤量化"
KDIR = os.path.join(ROOT, "工具", "账户分析", "_缓存", "kline2026_5min")
SYMS = ["PVC", "甲醇", "纯碱", "螺纹钢", "豆粕"]
NSIM = 20000
random.seed(20261104)
np.random.seed(20261104)


def one(sym):
    df = pd.read_csv(os.path.join(KDIR, "%s.csv" % sym), encoding="utf-8-sig")
    df = df[df["volume"] > 0].reset_index(drop=True)
    hi = df["high"].to_numpy(float)
    lo = df["low"].to_numpy(float)
    op = df["open"].to_numpy(float)
    n = len(df)

    for hold in (3, 4, 6, 10, 15):
        idx = np.random.randint(0, n - hold - 1, NSIM)
        sign = np.random.choice([1.0, -1.0], NSIM)
        entry = op[idx + 1]                     # 下一根开盘成交（无未来函数）
        mfe = np.empty(NSIM)
        for k in range(NSIM):
            i = idx[k]
            seg_h = hi[i + 1:i + 1 + hold]
            seg_l = lo[i + 1:i + 1 + hold]
            if sign[k] > 0:
                mfe[k] = seg_h.max() - entry[k]
            else:
                mfe[k] = entry[k] - seg_l.min()
        zero = float(np.mean(mfe <= 0))
        print("  %-6s 持仓%2d根 | 从未浮盈 %.1f%% | MFE均 %.2f | MFE中位 %.2f" % (
            sym, hold, zero * 100, float(np.mean(np.maximum(mfe, 0))),
            float(np.median(np.maximum(mfe, 0)))), flush=True)
    return None


def main():
    print("随机入场基准（%d 次/组，下根开盘成交，随机方向）" % NSIM)
    for s in SYMS:
        one(s)
    print("\n对照：MA13「连续持仓的止损单」从未浮盈 57.7%，平均持仓 4 根")


if __name__ == "__main__":
    main()
