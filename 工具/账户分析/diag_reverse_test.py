# -*- coding: utf-8 -*-
"""
「进场后 N 根没浮盈 → 反手」的可执行性检验

背景：MA13 止损单 54% 从未浮盈，而随机入场同长度只有 5.7%~10.2%。
      差 6 倍意味着入场点有系统性方向偏差 —— 那么「反向」可能是 alpha。

本脚本检验一条**无未来函数**的盘中规则：
    开仓后第 K 根收盘时，若仍从未浮盈（MFE=0），
    则平仓并在该收盘价**反手**，持有 H 根后平掉。
    （对照：不反手，继续原仓）

只算毛点数（乘数在各组内相同），成本单列，便于按四层口径判读。
"""
import os
import glob
import csv
import datetime

import pandas as pd
import numpy as np

ROOT = r"C:\Users\cc\Desktop\天勤量化"
BASE = os.path.join(ROOT, "回测结果", "06_MA13超短线")
KDIR = os.path.join(ROOT, "工具", "账户分析", "_缓存", "kline2026_5min")
SYMS = [("PVC", "PVC"), ("甲醇", "甲醇"), ("纯碱", "纯碱"), ("螺纹钢", "螺纹钢"), ("豆粕", "豆粕")]

K_TRIGGER = 3      # 第 3 根收盘判定
H_HOLD = 10        # 反手后持有根数


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
                    "reason": d.get("离场原因", "").strip(),
                })
            except Exception:
                continue
    return out


def main():
    rows = []
    for sym_cn, _ in SYMS:
        files = glob.glob(os.path.join(BASE, sym_cn, "*探针窗2026-01-01_2026-10-31.txt"))
        if not files:
            continue
        trades = load_trades(files[0])
        df = pd.read_csv(os.path.join(KDIR, "%s.csv" % sym_cn), encoding="utf-8-sig")
        dt = df["dt"].astype(str).tolist()
        idx = {s: i for i, s in enumerate(dt)}
        hi = df["high"].to_numpy(float)
        lo = df["low"].to_numpy(float)
        cl = df["close"].to_numpy(float)
        n = len(df)

        for t in trades:
            i = idx.get(t["entry_dt"])
            if i is None or i + K_TRIGGER + H_HOLD + 1 >= n:
                continue
            sign = t["dir"]
            entry = t["entry"]
            # 前 K_TRIGGER 根（含开仓根）的 MFE
            seg_h = hi[i:i + K_TRIGGER]
            seg_l = lo[i:i + K_TRIGGER]
            if sign > 0:
                mfe = seg_h.max() - entry
            else:
                mfe = entry - seg_l.min()
            # 第 K 根收盘价：反手入场价
            rev_entry = cl[i + K_TRIGGER - 1]
            # 反手后 H 根
            j0 = i + K_TRIGGER
            j1 = min(i + K_TRIGGER + H_HOLD, n - 1)
            # 反手后 H 根：★ 必须用同一时刻的收盘价平仓。
            #   （先前版本用持有期内极值平仓 = 未来函数，t=20 是假的）
            if j1 >= n:
                continue
            rev_gross = -sign * (cl[j1] - rev_entry)
            # 原仓继续持有 H 根（对照，同一平仓价，完全对称）
            keep_gross = sign * (cl[j1] - rev_entry)
            rows.append({
                "sym": sym_cn, "mfe": mfe, "trigger": mfe <= 0,
                "rev_gross": rev_gross, "keep_gross": keep_gross,
            })

    a = pd.DataFrame(rows)
    print("样本 %d 笔（%d 品种）" % (len(a), len(SYMS)))
    trig = a[a["trigger"]]
    print("触发「%d 根内从未浮盈」: %d 笔（%.1f%%）" % (K_TRIGGER, len(trig), len(trig) / len(a) * 100))

    print("\n" + "=" * 92)
    print("触发后的后续 %d 根：反手 vs 原仓继续持有（毛点数，未扣成本）" % H_HOLD)
    print("=" * 92)
    print("%-8s %6s %12s %12s %10s" % ("品种", "触发笔数", "反手毛均", "原仓毛均", "差值"))
    for s, g in trig.groupby("sym"):
        print("%-8s %6d %12.2f %12.2f %10.2f" % (
            s, len(g), g["rev_gross"].mean(), g["keep_gross"].mean(),
            g["rev_gross"].mean() - g["keep_gross"].mean()))
    print("%-8s %6d %12.2f %12.2f %10.2f" % (
        "合计", len(trig), trig["rev_gross"].mean(), trig["keep_gross"].mean(),
        trig["rev_gross"].mean() - trig["keep_gross"].mean()))

    print("\n" + "=" * 92)
    print("反手毛收益的分布（判断是否靠少数极端样本撑起来）")
    print("=" * 92)
    q = trig["rev_gross"].quantile([0.1, 0.25, 0.5, 0.75, 0.9])
    print("  10%% %.2f | 25%% %.2f | 中位 %.2f | 75%% %.2f | 90%% %.2f" % tuple(q))
    print("  均值 %.2f | 标准差 %.2f | 胜率 %.1f%%" % (
        trig["rev_gross"].mean(), trig["rev_gross"].std(),
        (trig["rev_gross"] > 0).mean() * 100))
    t = trig["rev_gross"].mean() / (trig["rev_gross"].std() / np.sqrt(len(trig)))
    print("  均值 t 统计量 = %.2f（|t|>2 才算有信息）" % t)
    # 去极值：截掉 1%/99%
    tr = trig["rev_gross"].clip(trig["rev_gross"].quantile(0.01), trig["rev_gross"].quantile(0.99))
    t2 = tr.mean() / (tr.std() / np.sqrt(len(tr)))
    print("  截尾 1%%/99%% 后：均值 %.2f | t = %.2f" % (tr.mean(), t2))

    print("\n" + "=" * 92)
    print("全样本净效应：所有交易都按此规则（触发才反手，未触发保持原仓）")
    print("=" * 92)
    ok = a.dropna()
    old = ok["keep_gross"].mean()
    new = np.where(ok["trigger"], ok["rev_gross"], ok["keep_gross"]).mean()
    print("  原样持有 %d 根：毛均 %+.3f 点/笔" % (H_HOLD, old))
    print("  触发反手：     毛均 %+.3f 点/笔（差 %+.3f）" % (new, new - old))
    print("  注：反手多一次开平仓，成本约为单边的 2 倍，需另行扣除再判")


if __name__ == "__main__":
    main()
