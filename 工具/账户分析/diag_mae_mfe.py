# -*- coding: utf-8 -*-
"""
MA13 持仓内浮盈路径归因（MFE / MAE）

目的：回答「MA13 进场之后，盘中到底发生了什么」——
  · 止损单里，有多少在止损前曾经浮盈（浮盈回吐型）？
  · 有多少进场就一路逆行（假信号型）？
  · 这两类的可救空间差多少？

★ 纯归因，不构成回测、不调参、不改策略。
★ 数据：tqsdk 免费版 get_kline_serial（8964 根 5min，约覆盖 6 个月）+ 现成逐笔成交报告。

MFE = 持仓期间最大浮盈（点数）；MAE = 持仓期间最大浮亏（点数）。
口径：取 开仓bar ~ 平仓bar 的 high/low 极值（含平仓bar，轻微高估，多空对称）。
"""
import io
import os
import sys
import csv
import glob
import datetime
import pandas as pd
from collections import defaultdict

ROOT = r"C:\Users\cc\Desktop\天勤量化"
sys.path.insert(0, ROOT)

import config_account  # noqa: E402
import config_symbols  # noqa: E402

BASE = os.path.join(ROOT, "回测结果", "06_MA13超短线")
SYMS = [("V", "PVC"), ("MA", "甲醇"), ("SA", "纯碱"), ("RB", "螺纹钢"), ("M", "豆粕")]
KLINE_SEC = 300
DATA_LEN = 8964


def load_trades(path, sym_code):
    rows = []
    with io.open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
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
                t0 = datetime.datetime.strptime(d["开仓时间"], "%Y-%m-%d %H:%M")
                t1 = datetime.datetime.strptime(d["平仓时间"], "%Y-%m-%d %H:%M")
                rows.append({
                    "sym": sym_code,
                    "dir": 1 if d["方向"] == "多" else -1,
                    "entry_dt": d["开仓时间"],
                    "exit_dt": d["平仓时间"],
                    "t0": t0,
                    "t1": t1,
                    "entry": float(d["开仓价"]),
                    "exit": float(d["平仓价"]),
                    "pnl": float(d["盈亏(元)"]),
                    "reason": d.get("离场原因", "?").strip(),
                })
            except Exception:
                continue
    return rows


def gap_kind(t):
    """持仓是否跨交易中断：'none' 连续 / 'lunch' 跨午休 / 'night' 跨夜"""
    t0, t1 = t["t0"], t["t1"]
    if t0.date() != t1.date():
        return "night"
    # 同日：开在上午、平在下午 → 跨午休
    if t0.hour * 60 + t0.minute <= 11 * 60 + 30 and t1.hour * 60 + t1.minute >= 13 * 60 + 30:
        return "lunch"
    return "none"


KDIR = os.path.join(ROOT, "工具", "账户分析", "_缓存", "kline2026_5min")


def fetch_klines(sym_cn, kdir=None):
    """读 dump_kline_csv.py 导出的本地 CSV（与回测同源，避免重复拉取）。

    kdir 可指定别的缓存目录，用于「新数据 vs 旧坏快照」对照复算。
    """
    path = os.path.join(kdir or KDIR, "%s.csv" % sym_cn)
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    df = pd.read_csv(path, encoding="utf-8-sig")
    df["dtstr"] = df["dt"].astype(str)
    return df


def main(kdir=None):
    print("[K线目录] %s" % (kdir or KDIR), flush=True)
    all_stats = []
    for sym_code, sym_cn in SYMS:
        files = glob.glob(os.path.join(BASE, sym_cn, "*探针窗2026-01-01_2026-10-31.txt"))
        if not files:
            print("[缺报告]", sym_cn)
            continue
        trades = load_trades(files[0], sym_code)
        print("[读取] %-6s %4d 笔成交，读本地K线..." % (sym_cn, len(trades)), flush=True)
        df = fetch_klines(sym_cn, kdir)
        idx = {s: i for i, s in enumerate(df["dtstr"].tolist())}
        print("       K线 %d 根，范围 %s ~ %s" % (
            len(df), df["dtstr"].iloc[0], df["dtstr"].iloc[-1]), flush=True)

        hi = df["high"].values
        lo = df["low"].values
        op = df["open"].values
        cl = df["close"].values

        hit = 0
        for t in trades:
            i = idx.get(t["entry_dt"])
            j = idx.get(t["exit_dt"])
            if i is None or j is None or j < i:
                continue
            sign = t["dir"]
            entry = t["entry"]
            # 持仓期间极值（含平仓bar）
            if sign > 0:
                mfe = max(hi[i:j + 1]) - entry
                mae = entry - min(lo[i:j + 1])
            else:
                mfe = entry - min(lo[i:j + 1])
                mae = max(hi[i:j + 1]) - entry
            t["mfe"] = max(0.0, mfe)
            t["mae"] = max(0.0, mae)
            t["real"] = sign * (t["exit"] - entry)   # 实际毛点数
            t["bars"] = j - i + 1
            t["gap"] = gap_kind(t)
            # 前 3 / 5 根浮亏（判断「早不早就能看出不行」）
            for n in (3, 5):
                end = min(i + n - 1, j)
                if sign > 0:
                    t["dd%d" % n] = entry - min(lo[i:end + 1])
                else:
                    t["dd%d" % n] = max(hi[i:end + 1]) - entry
            hit += 1

        valid = [t for t in trades if "mfe" in t]
        print("       可匹配 %d / %d 笔" % (hit, len(trades)), flush=True)
        all_stats.extend(valid)

    # ---------------- 汇总 ----------------
    print("\n" + "=" * 100)
    print("【一】止损单的两种形态（持仓期间 MFE 分布）")
    print("=" * 100)
    stop = [t for t in all_stats if t["reason"] == "止损"]
    total = len(stop)
    print("止损单共 %d 笔。按「止损前最大浮盈」分档：" % total)
    buckets = [(0, 0.01, "从未浮盈（进场即逆行）"),
               (0.01, 3, "浮盈 <3 点"),
               (3, 8, "浮盈 3~8 点"),
               (8, 15, "浮盈 8~15 点"),
               (15, 10 ** 9, "浮盈 >15 点")]
    for lo_, hi_, label in buckets:
        sub = [t for t in stop if lo_ <= t["mfe"] < hi_]
        if not sub:
            continue
        print("  %-22s %5d 笔 (%5.1f%%) | 实际毛均 %+7.2f | MAE均 %6.2f | 平均持仓 %.1f 根" % (
            label, len(sub), len(sub) / total * 100,
            sum(t["real"] for t in sub) / len(sub),
            sum(t["mae"] for t in sub) / len(sub),
            sum(t["bars"] for t in sub) / len(sub)))

    print("\n" + "=" * 100)
    print("【二】分「连续 / 跨午休 / 跨夜」看止损单的 MFE")
    print("=" * 100)
    for key, label in [("none", "连续持仓"), ("lunch", "跨午休"), ("night", "跨夜")]:
        sub = [t for t in stop if t["gap"] == key]
        if not sub:
            continue
        never = [t for t in sub if t["mfe"] < 0.01]
        print("%-10s %5d 笔 | 从未浮盈 %5d 笔(%.1f%%) | 实际毛均 %+7.2f | 平均 %d 根" % (
            label, len(sub), len(never), len(never) / len(sub) * 100,
            sum(t["real"] for t in sub) / len(sub),
            sum(t["bars"] for t in sub) / len(sub)))

    print("\n" + "=" * 100)
    print("【三】「早识别」空间：持仓第 3 / 5 根时，最终止损的单子浮亏已经多少")
    print("=" * 100)
    intra_stop = [t for t in stop if t["bars"] <= 16]
    for n in (3, 5):
        key = "dd%d" % n
        sub = [t for t in intra_stop if key in t and t["bars"] >= n]
        if not sub:
            continue
        dds = sorted(t[key] for t in sub)
        p50 = dds[len(dds) // 2]
        p75 = dds[int(len(dds) * 0.75)]
        hit = sum(1 for x in dds if x >= 3.0)
        print("  持仓第 %d 根：%5d 笔 | 浮亏中位 %5.2f 点 | 75分位 %5.2f 点 | 浮亏≥3点占 %.1f%%" % (
            n, len(sub), p50, p75, hit / len(sub) * 100))
    print("  （对照：时段内止损单实际毛均 %.2f 点）" %
          (sum(t["real"] for t in intra_stop) / max(1, len(intra_stop))))

    print("\n" + "=" * 100)
    print("【四】时间止损（盈利单）：浮盈回吐了多少（判断「锁盈」空间）")
    print("=" * 100)
    te = [t for t in all_stats if t["reason"] == "时间止损"]
    for key, label in [("none", "连续"), ("lunch", "跨午休"), ("night", "跨夜"), ("all", "全部")]:
        sub = te if key == "all" else [t for t in te if t["gap"] == key]
        if not sub:
            continue
        avg_mfe = sum(t["mfe"] for t in sub) / len(sub)
        avg_real = sum(t["real"] for t in sub) / len(sub)
        giveback = sum(max(0.0, t["mfe"] - t["real"]) for t in sub) / len(sub)
        print("%-8s %5d 笔 | MFE均 %6.2f | 实际毛均 %6.2f | 平均回吐 %6.2f 点（回吐率 %.0f%%）" % (
            label, len(sub), avg_mfe, avg_real, giveback,
            giveback / avg_mfe * 100 if avg_mfe > 0 else 0))

    print("\n" + "=" * 100)
    print("【五】上限测算：把「进场即逆行」的亏损砍一半，毛期望能到多少")
    print("=" * 100)
    n_all = len(all_stats)
    gross_pts = sum(t["real"] for t in all_stats)
    print("当前：%d 笔 | 毛点数合计 %+.1f | 毛期望 %+.3f 点/笔" % (
        n_all, gross_pts, gross_pts / n_all))
    never_in = [t for t in intra_stop if t["mfe"] < 0.01]
    if never_in:
        loss = sum(t["real"] for t in never_in)          # 负数
        save = -loss * 0.5                                # 亏损减半省下的点数（正）
        print("「进场即逆行」%d 笔（均持仓 %.1f 根）合计 %+.0f 点；亏损减半可省 %+.0f 点" % (
            len(never_in), sum(t["bars"] for t in never_in) / len(never_in), loss, save))
        print("  → 毛期望变为 %+.3f 点/笔" % ((gross_pts + save) / n_all))
        # 理论极限：这笔全不亏
        print("  → 若这 %d 笔完全不亏（上限，不可实现）：%+.3f 点/笔" % (
            len(never_in), (gross_pts - loss) / n_all))


if __name__ == "__main__":
    import argparse
    _ap = argparse.ArgumentParser()
    _ap.add_argument("--kdir", default=None,
                     help="K 线缓存目录；默认 _缓存/kline2026_5min。用于新旧数据对照复算")
    _a = _ap.parse_args()
    main(_a.kdir)
