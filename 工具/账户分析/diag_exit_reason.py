# -*- coding: utf-8 -*-
"""
MA13 离场通道分层诊断（第一层）

目的：回答「MA13 的钱是在哪条离场通道上流走的」，为「持仓内条件判断」定位改进空间。

口径：
  - 毛点数 = 方向 × (平仓价 − 开仓价)，即扣成本前的价差（乘数在各组内相同，不影响比较）
  - 净盈亏 = 报告里的 pnl 字段（已含双边成本）
  - 只做**描述性统计**，不调参、不择优，符合四铁律

不构成回测，纯事后归因。
"""
import io
import os
import glob
import csv
import datetime
import statistics as st

ROOT = r"C:\Users\cc\Desktop\天勤量化"
BASE = os.path.join(ROOT, "回测结果", "06_MA13超短线")
SYMS = ["PVC", "甲醇", "纯碱", "螺纹钢", "豆粕"]


def load_trades(path):
    rows = []
    with io.open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
        rd = csv.reader(f)
        header = None
        for r in rd:
            if not r:
                continue
            if r[0].startswith("#"):
                continue
            if r[0] == "方向":
                header = r
                continue
            if header is None or len(r) < len(header):
                continue
            d = dict(zip(header, r))
            try:
                entry = float(d["开仓价"])
                exitp = float(d["平仓价"])
                hand = int(d["手数"])
                pnl = float(d["盈亏(元)"])
            except Exception:
                continue
            sign = 1.0 if d["方向"] == "多" else -1.0
            gross_pts = sign * (exitp - entry)
            try:
                t0 = datetime.datetime.strptime(d["开仓时间"], "%Y-%m-%d %H:%M")
                t1 = datetime.datetime.strptime(d["平仓时间"], "%Y-%m-%d %H:%M")
                hold_min = int((t1 - t0).total_seconds() // 60)
            except Exception:
                hold_min = None
            rows.append({
                "sym": None,
                "dir": d["方向"],
                "reason": d.get("离场原因", "?").strip(),
                "gross_pts": gross_pts,
                "hand": hand,
                "pnl": pnl,
                "hold_min": hold_min,
            })
    return rows


def agg(rows, keyname):
    n = len(rows)
    if n == 0:
        return None
    gross = sum(r["gross_pts"] for r in rows)
    pnl = sum(r["pnl"] for r in rows)
    wins = sum(1 for r in rows if r["pnl"] > 0)
    gp = [r["gross_pts"] for r in rows]
    holds = [r["hold_min"] for r in rows if r["hold_min"] is not None]
    return {
        "key": keyname,
        "n": n,
        "gross_pts": gross,
        "gross_avg": gross / n,
        "gross_med": st.median(gp),
        "pnl": pnl,
        "win": wins / n * 100,
        "hold_med": st.median(holds) if holds else None,
    }


def main():
    all_rows = []
    per_sym = {}
    for s in SYMS:
        files = glob.glob(os.path.join(BASE, s, "*探针窗2026-01-01_2026-10-31.txt"))
        if not files:
            print("[缺失]", s)
            continue
        rs = load_trades(files[0])
        for r in rs:
            r["sym"] = s
        per_sym[s] = rs
        all_rows.extend(rs)
        print("[读取] %-6s %4d 笔" % (s, len(rs)))

    print("\n" + "=" * 96)
    print("【一】按离场通道分层（5 品种合计 %d 笔）" % len(all_rows))
    print("=" * 96)
    print("%-10s %6s %7s %12s %10s %10s %8s %10s" % (
        "离场原因", "笔数", "占比", "毛点数合计", "毛均点", "净盈亏(元)", "胜率", "持仓中位分"))
    reasons = {}
    for r in all_rows:
        reasons.setdefault(r["reason"], []).append(r)
    total_n = len(all_rows)
    rows_agg = [agg(v, k) for k, v in reasons.items()]
    rows_agg.sort(key=lambda x: -x["n"])
    for a in rows_agg:
        print("%-10s %6d %6.1f%% %12.1f %10.2f %10.0f %7.1f%% %10s" % (
            a["key"], a["n"], a["n"] / total_n * 100, a["gross_pts"],
            a["gross_avg"], a["pnl"], a["win"],
            ("%.0f" % a["hold_med"]) if a["hold_med"] is not None else "-"))

    print("\n" + "=" * 96)
    print("【二】按「品种 × 离场通道」看毛点数（判断是否某品种/某通道特异性）")
    print("=" * 96)
    reasons_list = [a["key"] for a in rows_agg]
    print("%-8s" % "品种" + "".join("%16s" % r for r in reasons_list))
    for s in SYMS:
        if s not in per_sym:
            continue
        line = "%-8s" % s
        for rc in reasons_list:
            sub = [r for r in per_sym[s] if r["reason"] == rc]
            if not sub:
                line += "%16s" % "-"
            else:
                a = agg(sub, rc)
                line += "%16s" % ("%d笔/%+.0f点" % (a["n"], a["gross_pts"]))
        print(line)

    print("\n" + "=" * 96)
    print("【三】按方向分层")
    print("=" * 96)
    for d in ["多", "空"]:
        sub = [r for r in all_rows if r["dir"] == d]
        a = agg(sub, d)
        print("%-4s %5d 笔 | 毛点数合计 %+9.1f | 毛均 %+7.2f | 净盈亏 %+10.0f | 胜率 %5.1f%%" % (
            d, a["n"], a["gross_pts"], a["gross_avg"], a["pnl"], a["win"]))

    print("\n" + "=" * 96)
    print("【四】止损通道：按持仓时长分箱（找「是不是拖太久/太快被扫」）")
    print("=" * 96)
    stop_rows = [r for r in all_rows if r["reason"] == "止损" and r["hold_min"] is not None]
    bins = [(0, 5), (5, 15), (15, 30), (30, 60), (60, 120), (120, 10 ** 9)]
    for lo, hi in bins:
        sub = [r for r in stop_rows if lo <= r["hold_min"] < hi]
        if not sub:
            continue
        a = agg(sub, "%d-%d分" % (lo, min(hi, 999)))
        print("%-12s %5d 笔 | 毛点数合计 %+9.1f | 毛均 %+7.2f | 胜率 %5.1f%%" % (
            a["key"], a["n"], a["gross_pts"], a["gross_avg"], a["win"]))

    print("\n" + "=" * 96)
    print("【五】时间止损通道：按持仓时长分箱（15 根=75 分是硬上限，看末尾段表现）")
    print("=" * 96)
    te_rows = [r for r in all_rows if r["reason"] == "时间止损" and r["hold_min"] is not None]
    for lo, hi in [(0, 30), (30, 60), (60, 75), (75, 10 ** 9)]:
        sub = [r for r in te_rows if lo <= r["hold_min"] < hi]
        if not sub:
            continue
        a = agg(sub, "%d-%d分" % (lo, min(hi, 999)))
        print("%-12s %5d 笔 | 毛点数合计 %+9.1f | 毛均 %+7.2f | 胜率 %5.1f%%" % (
            a["key"], a["n"], a["gross_pts"], a["gross_avg"], a["win"]))

    print("\n" + "=" * 96)
    print("【五之二】按「是否跨交易时段」分解（>80 分钟必然跨断档：15 根=75 分已是硬上限）")
    print("=" * 96)
    for grp, lo, hi in [("时段内连续", 0, 81), ("跨时段断档", 81, 10 ** 9)]:
        sub = [r for r in all_rows if r["hold_min"] is not None and lo <= r["hold_min"] < hi]
        if not sub:
            continue
        a = agg(sub, grp)
        print("%-12s %5d 笔(%4.1f%%) | 毛点数合计 %+9.1f | 毛均 %+7.2f | 净盈亏 %+10.0f | 胜率 %5.1f%%" % (
            grp, a["n"], a["n"] / total_n * 100, a["gross_pts"], a["gross_avg"], a["pnl"], a["win"]))
    print("--- 交叉：跨时段 × 离场通道 ---")
    for grp, lo, hi in [("时段内连续", 0, 81), ("跨时段断档", 81, 10 ** 9)]:
        for rc in reasons_list:
            sub = [r for r in all_rows
                   if r["reason"] == rc and r["hold_min"] is not None and lo <= r["hold_min"] < hi]
            if not sub:
                continue
            a = agg(sub, rc)
            print("  %-10s / %-8s %5d 笔 | 毛点数合计 %+9.1f | 毛均 %+7.2f | 胜率 %5.1f%%" % (
                grp, rc, a["n"], a["gross_pts"], a["gross_avg"], a["win"]))

    print("\n" + "=" * 96)
    print("【六】关键比值：亏损/盈利结构")
    print("=" * 96)
    win_rows = [r for r in all_rows if r["gross_pts"] > 0]
    loss_rows = [r for r in all_rows if r["gross_pts"] <= 0]
    gw = sum(r["gross_pts"] for r in win_rows)
    gl = sum(r["gross_pts"] for r in loss_rows)
    print("盈利笔 %d（毛 +%.1f 点，均 %+.2f）| 亏损笔 %d（毛 %.1f 点，均 %+.2f）" % (
        len(win_rows), gw, gw / max(1, len(win_rows)),
        len(loss_rows), gl, gl / max(1, len(loss_rows))))
    print("毛盈亏比 = %.3f | 毛期望/笔 = %+.3f 点" % (
        abs(gw / gl) if gl else 0, (gw + gl) / len(all_rows)))


if __name__ == "__main__":
    main()
