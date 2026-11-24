# -*- coding: utf-8 -*-
"""
排列看板 · 事件回访
==============================================================
回答一个问题：这些信号报出来之后，行情到底怎么走了？

统计四类事件：
  新多 / 新空   排列成立当天（不看之前是不是苗头）
  苗头           MA5 刚穿 MA10、后三线已排好，但排列还没成立
  破坏           原排列被打断（原多破坏按看空记，原空破坏按看多记）

★ 为什么能立刻出结果，不用「从今天开始攒」：
  事件是确定性函数。昨天的 csv 就能算出昨天该报什么信号，所以历史全在数据里，
  直接回算即可。也正因为如此，这个脚本随时可以重跑，不存在"积累"问题。

★ 无未来函数，和看板同一口径：
  T 日收盘确认信号（看板就是收盘后推的）
  → T+1 开盘入场（下一根 bar 的开盘价，这才是真能成交的价）
  → T+H 收盘平仓
  收益按「顺信号方向」记：做多记涨、做空记跌，所以正数 = 信号对了。

★ 样本边界（必须一起看，否则会高估）：
  1) 只覆盖「当前主力具体合约」的历史，约 200 根日线，不是 2016 年起的全历史。
     用主连做会引入换月拼接，MA60 在换月点被污染，那套数据不能用来验证这条判据。
  2) 事件高度重叠：同一天全市场一起转空，10 个品种的「新空」其实是 1 个独立事件。
     所以下面的「样本数」是事件数，不是独立观测数，不能当显著性检验用。

用法：
  python review_events.py           # 生成 out/事件回访_YYYYMMDD.md
  python review_events.py --quiet   # 供 scan_daily_push.py 每日自动调用
"""
from __future__ import annotations

import os
import sys
import glob
import datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import pool as P
import screen as S
from core import aligned, early_series, MIN_BARS

DATA = os.path.join(HERE, "data")
OUT = os.path.join(HERE, "out")

HOLDS = (5, 10, 20)
KINDS = ("新多", "新空", "苗头多", "苗头空", "破坏")


def collect_events(rows_out: list[dict] | None = None,
                   base_out: dict | None = None) -> list[dict]:
    """回算全部历史事件。返回 [{'key','date','kind','dir','r5','r10','r20'}]。

    ★ base_out 里同时收集「所有交易日」的同口径前瞻收益，用来算基准。
      没有基准的胜率是没有意义的：这一段行情整体在跌，随便做空也有 55% 胜率，
      把「新空 54%」当成信号有效就错了。要看的是**超额**。
    """
    recs: list[dict] = []
    base = {h: [] for h in HOLDS} if base_out is not None else None
    for key in P.POOL:
        p = os.path.join(DATA, f"{key}_1440min.csv")
        if not os.path.exists(p):
            continue
        d = S.load(p)
        if d is None or len(d) < MIN_BARS:
            continue
        o = aligned(S.indicators(d))
        n = len(o)
        es = early_series(o)                 # 先算一次，循环里取下标（early_of 每次都会重算整列）
        for i in range(1, n):
            # ---- 基准：不看信号，任意一天入场持有 H 天 ----
            if base is not None and i + 1 < n:
                e0 = float(o.at[i + 1, "open"])
                if e0 > 0:
                    for H in HOLDS:
                        if i + H < n:
                            base[H].append((float(o.at[i + H, "close"]) - e0) / e0 * 100.0)

            d0 = int(o.at[i, "a60"] or 0)
            prev = int(o.at[i - 1, "a60"] or 0)
            trig = int(o.at[i, "a60_trig"] or 0)
            if trig != 0:
                kind, direc = ("新多" if trig > 0 else "新空"), trig
            elif d0 == 0 and prev != 0:
                kind, direc = "破坏", -prev      # 原多被打断 → 按看空记
            else:
                e = int(es[i])
                if e != 0 and d0 == 0:
                    kind, direc = ("苗头多" if e > 0 else "苗头空"), e
                else:
                    continue
            rec = dict(key=key, date=str(o.at[i, "datetime"])[:10],
                       kind=kind, dir=direc, sector=P.sector_of(key))
            if i + 1 >= n:                  # 最后一根：事件成立，但没法入场，只记不计收益
                recs.append(rec)
                continue
            entry = float(o.at[i + 1, "open"])
            if entry <= 0:
                continue
            for H in HOLDS:
                j = i + H
                if j >= n:
                    continue
                exitp = float(o.at[j, "close"])
                rec[f"r{H}"] = (exitp - entry) / entry * 100.0 * direc
            recs.append(rec)
    if base_out is not None and base is not None:
        for H in HOLDS:
            base_out[H] = base[H]
    if rows_out is not None:
        rows_out.extend(recs)
    return recs


def _stat(vals: list[float]) -> dict:
    v = sorted(vals)
    n = len(v)
    if not n:
        return dict(n=0)
    return dict(
        n=n,
        win=sum(1 for x in v if x > 0) / n * 100,
        mean=sum(v) / n,
        med=v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2,
        best=v[-1], worst=v[0],
    )


def _row(label: str, h: int, s: dict, base: float = 0.0) -> str:
    if not s.get("n"):
        return f"| {label} | {h}日 | 0 | — | — | — | — | — | — |"
    ex = s["mean"] - base
    return (f"| {label} | {h}日 | {s['n']} | {s['win']:.0f}% | {s['mean']:+.2f}% | "
            f"{base:+.2f}% | **{ex:+.2f}%** | {s['med']:+.2f}% | {s['worst']:+.1f}% |")


def _bm(recs: list[dict], kind: str, h: int, base_mean: dict) -> float:
    """该事件方向的基准：把所有交易日的前瞻收益，按每笔的方向对齐后取均值。

    空头事件的基准 = −(全体平均涨幅)，因为市场整体跌的时候做空本来就在赚。
    """
    v = [base_mean[h] * r["dir"] for r in recs if r["kind"] == kind]
    return sum(v) / len(v) if v else 0.0


def build(recs: list[dict], asof: str, base: dict | None = None) -> str:
    base = base or {}
    bm = {h: (sum(base[h]) / len(base[h]) if base.get(h) else 0.0) for h in HOLDS}
    lines = [
        f"# 事件回访 · 数据截至 {asof}",
        "",
        "> 口径：T 日收盘确认信号（看板就是收盘后推的）→ **T+1 开盘入场** → T+H 收盘平仓。",
        "> 收益按「顺信号方向」计：做多记涨、做空记跌，**正数 = 信号方向对了**。",
        f"> 覆盖 {len(P.POOL)} 个品种的当前主力合约（约 200 根日线），共 {len(recs)} 个事件。",
        "",
        "★ **看「超额」那一列，不是「均值」**。基准 = 同期不看信号、随便哪天入场持有 H 天"
        "的平均收益（按同方向对齐）。这一段行情整体偏空，做空类事件不管有没有信息都会是正收益；"
        "超额才是信号本身贡献的部分。",
        "",
        "## 一、总览",
        "",
        "| 事件 | 持有 | 样本 | 胜率 | 均值 | 基准 | 超额 | 中位 | 最差 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for k in KINDS:
        for h in HOLDS:
            vals = [r[f"r{h}"] for r in recs if r["kind"] == k and f"r{h}" in r]
            lines.append(_row(k, h, _stat(vals), _bm(recs, k, h, bm)))
    lines.append("")

    lines += ["## 二、分板块（只列 10 日）", "",
              "| 事件 | 板块 | 样本 | 胜率 | 均值 | 中位 |",
              "|---|---|---|---|---|---|"]
    for k in KINDS:
        for sec in P.SECTOR_ORDER:
            vals = [r["r10"] for r in recs
                    if r["kind"] == k and r["sector"] == sec and "r10" in r]
            s = _stat(vals)
            if not s.get("n"):
                continue
            lines.append(f"| {k} | {sec} | {s['n']} | {s['win']:.0f}% | "
                         f"{s['mean']:+.2f}% | {s['med']:+.2f}% |")
    lines.append("")

    lines += [
        "## 三、怎么读这张表",
        "",
        "1. **先看「超额」。** 超额 = 均值 − 同方向随机入场的基准。超额接近 0 或为负，"
        "说明这个事件只是在跟随大环境，本身没有信息。胜率 60% 但超额 −0.1% 的情况很常见。",
        "2. **样本数不是独立观测数**。同一天全市场一起转空，10 个品种的「新空」"
        "只等于 1 个独立事件。所以胜率 60% 不能按 100 个样本去算置信区间，"
        "真实有效样本可能只有十几二十个。这是描述性统计，不是显著性检验。",
        "3. **均值会被极端值拽走**，尤其 20 日。想判断「大多数时候怎么样」看中位。",
        "4. **只有约 200 根日线**，横跨的行情类型有限（当前这段是空头环境）。"
        "多头事件样本天然少，别拿小样本的胜率下结论。",
        "5. 事件之间会互相重叠：一个品种「新空」之后 5 天又出现「破坏」，"
        "两笔收益有重叠区间，不能当成两笔独立交易相加。",
        "",
    ]
    return "\n".join(lines)


def main(quiet: bool = False) -> int:
    base: dict = {}
    recs = collect_events(base_out=base)
    if not recs:
        print("[warn] 没有可回算的事件（数据不足或路径不对）")
        return 1
    asof = max(r["date"] for r in recs)
    os.makedirs(OUT, exist_ok=True)
    md = build(recs, asof, base)
    p = os.path.join(OUT, f"事件回访_{asof.replace('-', '')}.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write(md)
    # 固定名，方便每天覆盖同一份
    with open(os.path.join(OUT, "事件回访.md"), "w", encoding="utf-8") as f:
        f.write(md)
    if not quiet:
        print(md)
        print(f"\n落盘 {p}")
    else:
        print(f"事件回访 {len(recs)} 条 → {os.path.join(OUT, '事件回访.md')}")
    return 0


if __name__ == "__main__":
    sys.exit(main("--quiet" in sys.argv))
