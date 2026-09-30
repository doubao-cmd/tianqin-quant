# -*- coding: utf-8 -*-
"""
MA13 双鲁棒品种深挖（任务A）：逐笔明细 + 实盘滑点可达性
=====================================================
对象：棕榈油(P)、苯乙烯(EB)——2345 验证中唯二跨窗存活的品种。
数据：回测结果/06_MA13超短线/<品种>/ 开发窗(2021-2023)与探针窗(2026)逐笔 txt。

口径修正（重要）：逐笔 txt 的 pnl 只扣了【平仓边】滑点+佣金；
开仓边 oc = SLIP×TICK×MULT×hand + COMM×hand 只从权益扣、未记进单笔。
本分析把每笔修正为全净：pnl_full = pnl - COMM*hand（SLIP=1 基线下）。

滑点敏感性（解析重算，非重跑）：SLIP 每增 1 跳，双边各多 1 跳：
  pnl(k) = pnl_full - (k-1)*TICK*MULT*hand*2
纯算术推导，不改任何策略参数。

统计维度：分布分位 / 离场原因 / 连亏 / 持仓时长 / 时段 / 方向 / 月度 / 手数
输出：控制台文字结论（不生成 HTML）。
"""
import os
import sys
import csv
import math
import statistics as st

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
import config_symbols

FILES = {
    "棕榈油": [
        ("开发窗2021-2023", "回测结果/06_MA13超短线/棕榈油/棕榈油_MA13超短线_5min_开发窗2021-01-01_2023-12-31.txt"),
        ("探针窗2026", "回测结果/06_MA13超短线/棕榈油/棕榈油_MA13超短线_5min_探针窗2026-01-01_2026-09-26.txt"),
    ],
    "苯乙烯": [
        ("开发窗2021-2023", "回测结果/06_MA13超短线/苯乙烯/苯乙烯_MA13超短线_5min_开发窗2021-01-01_2023-12-31.txt"),
        ("探针窗2026", "回测结果/06_MA13超短线/苯乙烯/苯乙烯_MA13超短线_5min_探针窗2026-01-01_2026-09-26.txt"),
    ],
}
KEY = {"棕榈油": "P", "苯乙烯": "EB"}


def load_trades(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        lines = [ln.rstrip("\n") for ln in f if ln.strip()]
    body = [ln for ln in lines if not ln.startswith("#")]
    rdr = csv.DictReader(body)
    for r in rdr:
        rows.append({
            "dir": r["方向"],
            "entry_dt": r["开仓时间"],
            "exit_dt": r["平仓时间"],
            "entry": float(r["开仓价"]),
            "exit": float(r["平仓价"]),
            "hand": int(r["手数"]),
            "pnl": float(r["盈亏(元)"]),
            "pts": float(r["每手(点)"]),
            "reason": r["离场原因"],
        })
    return rows


def pct(vals, q):
    if not vals:
        return float("nan")
    s = sorted(vals)
    idx = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[idx]


def seg_of(dt_str):
    hhmm = dt_str[11:16]
    if hhmm >= "21:00":
        return "夜盘"
    if hhmm < "10:15":
        return "早盘前"
    if hhmm < "11:30":
        return "早盘后"
    if hhmm < "15:00":
        return "午盘"
    return "其他"


def analyze(sym, label, trades, tick, mult, comm):
    n = len(trades)
    print(f"\n{'='*64}\n◆ {sym} · {label} · {n} 笔")
    if n == 0:
        return

    # 口径修正：全净（补扣开仓边佣金；SLIP=1 基线）
    for t in trades:
        t["full"] = t["pnl"] - comm * t["hand"]
        d = 1 if t["dir"] == "多" else -1
        t["gross_pts"] = d * (t["exit"] - t["entry"])          # 每手毛点数
        t["net_pts"] = t["gross_pts"] - comm / mult / tick if tick else t["gross_pts"]
    wins = [t for t in trades if t["full"] > 0]
    losses = [t for t in trades if t["full"] <= 0]
    wr = len(wins) / n
    avg_w = st.mean(t["full"] for t in wins) if wins else 0.0
    avg_l = st.mean(t["full"] for t in losses) if losses else 0.0
    exp = st.mean(t["full"] for t in trades)
    print(f"  胜率 {wr*100:.1f}% | 期望 {exp:,.0f} 元/笔 | "
          f"均盈 {avg_w:,.0f} / 均亏 {avg_l:,.0f} | 盈亏比 {abs(avg_w/avg_l) if avg_l else 0:.2f}")
    print(f"  每手毛点数：均值 {st.mean(t['gross_pts'] for t in trades):.2f} | "
          f"P25 {pct([t['gross_pts'] for t in trades], .25):.1f} / "
          f"P50 {pct([t['gross_pts'] for t in trades], .5):.1f} / "
          f"P75 {pct([t['gross_pts'] for t in trades], .75):.1f}")

    # 滑点敏感性（解析重算）
    per_tick_hand = tick * mult      # 每手每跳元
    print(f"  -- 滑点敏感性（每手每跳 {per_tick_hand:.0f} 元，双边）--")
    for k in (1, 1.5, 2, 3, 4):
        tot = sum(t["full"] - (k - 1) * per_tick_hand * t["hand"] * 2 for t in trades)
        ek = st.mean(t["full"] - (k - 1) * per_tick_hand * t["hand"] * 2 for t in trades)
        print(f"    SLIP={k}: 净利 {tot:,.0f} 元 | 每笔期望 {ek:,.0f} 元")
    # 期望归零临界跳数
    base_exp = exp
    per_hand_extra = per_tick_hand * 2    # 双边每加 1 跳每手成本
    avg_hand = st.mean(t["hand"] for t in trades)
    if base_exp > 0:
        k_crit = base_exp / (per_hand_extra * avg_hand) + 1
        print(f"    期望归零临界滑点 ≈ {k_crit:.2f} 跳（平均手数 {avg_hand:.1f}）")

    # 离场原因
    print(f"  -- 离场原因 --")
    by_reason = {}
    for t in trades:
        by_reason.setdefault(t["reason"], []).append(t)
    for r, ts in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        e = st.mean(t["full"] for t in ts)
        print(f"    {r}: {len(ts)} 笔 ({len(ts)/n*100:.0f}%) | 期望 {e:,.0f} 元/笔")

    # 连亏
    max_streak = cur = 0
    streaks = []
    for t in trades:
        if t["full"] <= 0:
            cur += 1
            max_streak = max(max_streak, cur)
        else:
            if cur:
                streaks.append(cur)
            cur = 0
    if cur:
        streaks.append(cur)
    ge5 = sum(1 for s in streaks if s >= 5)
    ge8 = sum(1 for s in streaks if s >= 8)
    print(f"  -- 连亏 -- 最长 {max_streak} 连亏 | ≥5连亏次数 {ge5} | ≥8连亏次数 {ge8}")

    # 持仓时长（分钟）
    from datetime import datetime
    durs = []
    for t in trades:
        try:
            a = datetime.strptime(t["entry_dt"], "%Y-%m-%d %H:%M")
            b = datetime.strptime(t["exit_dt"], "%Y-%m-%d %H:%M")
            durs.append((b - a).total_seconds() / 60.0)
        except ValueError:
            pass
    if durs:
        print(f"  -- 持仓时长(分钟) -- P25 {pct(durs,.25):.0f} / P50 {pct(durs,.5):.0f} / "
              f"P75 {pct(durs,.75):.0f} / 最长 {max(durs):.0f}")

    # 时段
    by_seg = {}
    for t in trades:
        by_seg.setdefault(seg_of(t["entry_dt"]), []).append(t)
    print(f"  -- 开仓时段 --")
    for s, ts in sorted(by_seg.items(), key=lambda kv: -len(kv[1])):
        e = st.mean(t["full"] for t in ts)
        print(f"    {s}: {len(ts)} 笔 | 期望 {e:,.0f} 元/笔")

    # 方向
    by_dir = {}
    for t in trades:
        by_dir.setdefault(t["dir"], []).append(t)
    for d, ts in by_dir.items():
        e = st.mean(t["full"] for t in ts)
        print(f"    方向{d}: {len(ts)} 笔 | 期望 {e:,.0f} 元/笔")

    # 月度
    by_month = {}
    for t in trades:
        by_month[t["entry_dt"][:7]] = by_month.get(t["entry_dt"][:7], 0.0) + t["full"]
    pos_m = sum(1 for v in by_month.values() if v > 0)
    worst = min(by_month.items(), key=lambda kv: kv[1]) if by_month else ("-", 0)
    best = max(by_month.items(), key=lambda kv: kv[1]) if by_month else ("-", 0)
    print(f"  -- 月度 -- 共 {len(by_month)} 个月 | 正收益月 {pos_m} ({pos_m/len(by_month)*100:.0f}%) | "
          f"最差 {worst[0]}: {worst[1]:,.0f} | 最好 {best[0]}: {best[1]:,.0f}")

    # 手数分布
    hands = [t["hand"] for t in trades]
    print(f"  -- 手数 -- P25 {pct(hands,.25):.0f} / P50 {pct(hands,.5):.0f} / P75 {pct(hands,.75):.0f} / "
          f"max {max(hands)} | 均值 {avg_hand:.1f}")

    # 逐日密度
    days = {}
    for t in trades:
        days[t["entry_dt"][:10]] = days.get(t["entry_dt"][:10], 0) + 1
    dv = list(days.values())
    print(f"  -- 日密度 -- {len(days)} 个交易日 | 日均 {st.mean(dv):.1f} 笔 | 最多 {max(dv)} 笔/日")


def main():
    for sym, files in FILES.items():
        key = KEY[sym]
        _, _, mult, tick = config_symbols.get(key)
        comm = float(config_symbols.COST[key])
        print(f"\n{'#'*64}\n## {sym}（TICK={tick} MULT={mult} 佣金={comm}元/手/边）")
        for label, rel in files:
            p = os.path.join(_ROOT, rel)
            if not os.path.exists(p):
                print(f"  [缺失] {rel}")
                continue
            analyze(sym, label, load_trades(p), tick, mult, comm)


if __name__ == "__main__":
    main()
