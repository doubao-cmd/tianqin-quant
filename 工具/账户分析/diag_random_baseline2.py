# -*- coding: utf-8 -*-
"""
随机入场基准 v2 —— 按 MA13 实际持仓长度加权

v1 的问题：用固定根数（3/4/6/10/15）对照，而 MA13 实际持仓长度分布很散，
           短持仓天然更容易「从未浮盈」⇒ 会高估 MA13 的异常程度。
v2：从 MA13 逐笔成交里抽出真实的 bars 分布，随机入场时按该分布抽样长度。

同时输出 MA13 自身的「全部交易」指标（不按离场原因筛选，避免选择偏差）。
"""
import os
import glob
import csv

import pandas as pd
import numpy as np

ROOT = r"C:\Users\cc\Desktop\天勤量化"
BASE = os.path.join(ROOT, "回测结果", "06_MA13超短线")
KDIR = os.path.join(ROOT, "工具", "账户分析", "_缓存", "kline2026_5min")
SYMS = ["PVC", "甲醇", "纯碱", "螺纹钢", "豆粕"]
NSIM = 30000
np.random.seed(20261104)


def load_trades(path):
    out = []
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
        header = None
        for r in csv.reader(f):
            if not r or r[0].startswith("#"):
                continue
            if r[0] == "方向":
                header = r
                continue
            if header is None or len(r) < len(header):
                continue
            d = dict(zip(header, r))
            try:
                out.append({
                    "dir": 1 if d["方向"] == "多" else -1,
                    "entry_dt": d["开仓时间"],
                    "exit_dt": d["平仓时间"],
                    "entry": float(d["开仓价"]),
                    "exit": float(d["平仓价"]),
                    "reason": d.get("离场原因", "").strip(),
                })
            except Exception:
                continue
    return out


def main():
    print("按 MA13 实际持仓长度加权（%d 次/品种）" % NSIM)
    print("%-8s %10s %12s %12s %10s" % ("品种", "MA13实际", "随机同长度", "随机持仓均", "MA13持仓均"))
    tot = {"ma13": [], "rand": []}
    for sym in SYMS:
        files = glob.glob(os.path.join(BASE, sym, "*探针窗2026-01-01_2026-10-31.txt"))
        if not files:
            continue
        trades = load_trades(files[0])
        df = pd.read_csv(os.path.join(KDIR, "%s.csv" % sym), encoding="utf-8-sig")
        dt = df["dt"].astype(str).tolist()
        idx = {s: i for i, s in enumerate(dt)}
        hi = df["high"].to_numpy(float)
        lo = df["low"].to_numpy(float)
        op = df["open"].to_numpy(float)
        n = len(df)

        # MA13 实际：bars 与 MFE=0 比例
        bars_list, zero_actual = [], []
        for t in trades:
            i = idx.get(t["entry_dt"])
            j = idx.get(t["exit_dt"])
            if i is None or j is None or j < i:
                continue
            b = j - i + 1
            bars_list.append(b)
            seg_h = hi[i:j + 1]
            seg_l = lo[i:j + 1]
            mfe = (seg_h.max() - t["entry"]) if t["dir"] > 0 else (t["entry"] - seg_l.min())
            zero_actual.append(1 if mfe <= 0 else 0)
        bars_arr = np.array(bars_list)
        ma13_zero = np.mean(zero_actual) * 100

        # 随机：同长度分布
        picks = np.random.choice(bars_arr, NSIM, replace=True)
        starts = np.random.randint(0, n - max(picks) - 2, NSIM)
        signs = np.random.choice([1.0, -1.0], NSIM)
        zeros = 0
        valid = 0
        for k in range(NSIM):
            i, b, s = starts[k], int(picks[k]), signs[k]
            if i + b + 1 >= n:
                continue
            e = op[i + 1]
            seg_h = hi[i + 1:i + 1 + b]
            seg_l = lo[i + 1:i + 1 + b]
            mfe = (seg_h.max() - e) if s > 0 else (e - seg_l.min())
            zeros += 1 if mfe <= 0 else 0
            valid += 1
        rand_zero = zeros / valid * 100

        print("%-8s %9.1f%% %11.1f%% %12.1f %10.1f" % (
            sym, ma13_zero, rand_zero, float(np.mean(picks)), float(np.mean(bars_arr))))
        tot["ma13"].extend(zero_actual)

    print("%-8s %9.1f%%" % ("合计", np.mean(tot["ma13"]) * 100))
    print("\n判读：若两列接近 ⇒ MA13『从未浮盈』比例只是持仓短，入场无额外偏差；")
    print("      若 MA13 明显更高 ⇒ 入场点确有系统性不利偏移。")


if __name__ == "__main__":
    main()
