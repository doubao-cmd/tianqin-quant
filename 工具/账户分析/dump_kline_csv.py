# -*- coding: utf-8 -*-
"""
导出 MA13 归因所用的 5min K 线（2025-12-25 ~ 2026-10-31，主连），供持仓内归因复用。

为什么要它：tqsdk 免费版 get_kline_data_series 要专业版；非回测模式下
wait_update 在非交易时段会阻塞；所以走 TqBacktest 回放逐根收集（与回测同源，
保证时间戳与成交价口径一致）。

★★ 2026-11-04 修复（重要）：
   旧版在 wait_update() 后「见到新 dt 就 append」，录到的是 bar **诞生瞬间的快照**
   —— 只有 open/close 是对的，high==low==close、volume==0。
   后果：2026 段 13,518 根 bar 退化 100%，据 high/low 算的 MFE/MAE 全部失真。
   修法：只采集**已收盘**的 bar（排除数组最后一根 = 正在形成的那根），
        并用 dict 覆盖式写入，保证拿到的是该 bar 的终值。

输出：工具/账户分析/_缓存/kline2026_5min/<品种>.csv
自检：落盘后打印退化率 / 零量率，>5% 会告警（正常应 <1%）
"""
import argparse
import bisect
import os
import sys
import time

import pandas as pd

ROOT = r"C:\Users\cc\Desktop\天勤量化"
sys.path.insert(0, ROOT)

import config_account  # noqa: E402
import config_symbols  # noqa: E402
from tqsdk import TqApi, TqSim, TqBacktest, TqAuth  # noqa: E402

OUT_DIR = os.path.join(ROOT, "工具", "账户分析", "_缓存", "kline2026_5min")

# 与 diag_mae_mfe.py / diag_reverse_test.py / diag_random_baseline2.py 的 SYMS 保持一致
SYMS = [("V", "PVC"), ("MA", "甲醇"), ("SA", "纯碱"), ("RB", "螺纹钢"), ("M", "豆粕")]

START = pd.Timestamp("2026-01-01") - pd.Timedelta(days=7)
END = pd.Timestamp("2026-10-31")
TIMEOUT_SEC = 2400


def dump(code, name_cn, main_code):
    user, pwd = config_account.check()
    api = TqApi(TqSim(200000), backtest=TqBacktest(START, END),
                auth=TqAuth(user, pwd))
    k = api.get_kline_serial(main_code, 300, data_length=3000)

    snap = {}          # dt_ns -> (o, h, l, c, v)，覆盖式，保证是终值
    last_ns = 0
    t0 = time.time()
    n_wait = 0
    hit_end = False

    while True:
        try:
            ok = api.wait_update()
        except Exception as e:
            print("  [warn] wait_update 异常: %s" % e, flush=True)
            break
        if not ok:
            break
        n_wait += 1
        if len(k) < 2:
            continue

        dts = k["datetime"].to_numpy().tolist()
        o = k["open"].to_numpy()
        h = k["high"].to_numpy()
        l = k["low"].to_numpy()
        c = k["close"].to_numpy()
        v = k["volume"].to_numpy()

        n = len(dts)
        # ★ 只采已收盘：下标 0 ~ n-2；最后一根 n-1 是正在形成的，丢弃
        j0 = bisect.bisect_right(dts, last_ns)
        if j0 < n - 1:
            for j in range(j0, n - 1):
                snap[dts[j]] = (float(o[j]), float(h[j]), float(l[j]),
                                float(c[j]), float(v[j]))
            last_ns = dts[n - 2]
            if pd.Timestamp(last_ns) >= END:
                hit_end = True
                break
        if time.time() - t0 > TIMEOUT_SEC:
            print("  [warn] 超时退出（已收 %d 根）" % len(snap), flush=True)
            break

    api.close()

    if not snap:
        print("  [FAIL] %s 未采到任何已收盘 bar" % name_cn, flush=True)
        return None

    keys = sorted(snap)
    df = pd.DataFrame([(kk,) + snap[kk] for kk in keys],
                      columns=["datetime", "open", "high", "low", "close", "volume"])
    df["dt"] = pd.to_datetime(df["datetime"]).dt.strftime("%Y-%m-%d %H:%M")

    deg = float(((df.open == df.high) & (df.high == df.low) & (df.low == df.close)).mean() * 100)
    zv = float((df.volume == 0).mean() * 100)

    out = os.path.join(OUT_DIR, "%s.csv" % name_cn)
    df.to_csv(out, index=False, encoding="utf-8-sig")

    flag = "" if deg <= 5 else "  ⚠️退化异常"
    print("  [ok] %s %s %d 根 %s ~ %s | 退化 %.2f%% 零量 %.2f%% | wait %d 次 用时 %.0fs%s" % (
        name_cn, main_code, len(df), df["dt"].iloc[0], df["dt"].iloc[-1],
        deg, zv, n_wait, time.time() - t0, flag), flush=True)
    return dict(sym=name_cn, rows=len(df), deg=deg, zv=zv,
                first=df["dt"].iloc[0], last=df["dt"].iloc[-1], hit_end=hit_end)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--syms", nargs="*", default=None,
                    help="只跑指定代码，如 --syms MA M；默认全部")
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    todo = SYMS
    if args.syms:
        want = {s.upper() for s in args.syms}
        todo = [x for x in SYMS if x[0] in want]

    results = []
    for code, cn in todo:
        name_cn, main_code, MULT, TICK = config_symbols.get(code)
        print("[导出] %s (%s)" % (cn, main_code), flush=True)
        r = dump(code, cn, main_code)
        if r:
            results.append(r)

    print("\n[汇总] %d 个品种" % len(results), flush=True)
    for r in results:
        print("  %-6s %6d 根  退化 %.2f%%  零量 %.2f%%  %s ~ %s  %s" % (
            r["sym"], r["rows"], r["deg"], r["zv"], r["first"], r["last"],
            "✅" if r["hit_end"] else "⚠️未到END"), flush=True)


if __name__ == "__main__":
    main()
