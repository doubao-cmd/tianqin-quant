# -*- coding: utf-8 -*-
"""
换月错位巡检（只读，不连网，不碰实盘进程）

背景（2026-11-17~11-20 鸡蛋实锤）：
    q.underlying_symbol 在进程存活期间不跟随主连切换。
    K 线已换到 jd2701（约 3200），下单/平仓仍在 jd2612（约 3590）。
    后果：止损位与判定价分属两套价格体系；方向决定「永不触发」或「立刻误平」。

本脚本不依赖 underlying_symbol（它本身就是被污染的值），只依赖两条**真实数据**：
    1) 决策价 = 滑点表的「信号根收盘」（来自 K 线，新合约价格体系）
    2) 成交价 = 滑点表的「成交均价」（来自真实成交回报，实际下单合约的价格体系）
    两者的偏离就是错位量，跨品种用百分比归一。

判据（阈值集中在顶部，改之前先想清楚）：
    P0  系统性错位           —— 同品种同合约连续 >= 2 笔 |偏离| >= P0_PCT
                                （连续才证明是价格体系错，单笔可能是跳空）
    P1  单笔大偏离           —— |偏离| >= P0_PCT，但孤立（多为周末跳空/穿价，需人工核）
    P1  连续偏离             —— 同品种同合约连续 3 笔 >= RUN_PCT（阈值低于 P0 的早期征兆）
    P1  心跳停滞             —— 交易时段内最新心跳超过 15 分钟没更新
    P1  持仓/成交合约不一致  —— 有持仓，但程序认为的合约 != 最后一笔成交合约

    为什么单笔不算 P0：2026-11-02 MA701 决策 3550 / 成交 3612（-1.75%）是周五夜盘
    收盘价对周一开盘价的跳空，合约没变，不是错位。判错位必须看"连续性 + 合约变更"。

用法：
    python 巡检_换月错位.py              # 全量扫
    python 巡检_换月错位.py --days 10    # 只看最近 10 天
    python 巡检_换月错位.py --quiet      # 只输出结论（给计划任务用）

退出码：发现 P0 → 1，否则 0
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from datetime import datetime, timedelta

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(HERE, "台账")

# ---------------- 阈值（改这里 = 改判据，先想再改） ----------------
P0_PCT = 1.5      # 单笔偏离 >= 此值 → P0
P1_PCT = 1.0      # 单笔偏离 >= 此值 → P1
RUN_PCT = 0.5     # 连续判定用的单笔下限
RUN_N = 3         # 同品种同合约连续几笔算一段
HEARTBEAT_MIN = 15    # 心跳停滞分钟数
# 交易时段（含夜盘）。只在这些区间内才告警心跳停滞。
SESSIONS = [("09:00", "10:15"), ("10:30", "11:30"),
            ("13:30", "15:00"), ("21:00", "23:59"), ("00:00", "02:30")]


def _read(path: str, **kw) -> pd.DataFrame:
    """盘中 CSV 可能正在被追加写，容忍残行。"""
    try:
        return pd.read_csv(path, encoding="utf-8-sig", dtype=str,
                           engine="python", on_bad_lines="skip", **kw)
    except Exception:
        return pd.DataFrame()


def _num(s):
    return pd.to_numeric(s, errors="coerce")


def _in_session(t: datetime) -> bool:
    hm = t.strftime("%H:%M")
    for a, b in SESSIONS:
        if a <= hm <= b:
            return True
    return False


def scan_slips(days: int | None) -> tuple[list[dict], list[dict], pd.DataFrame]:
    """返回 (P0 列表, P1 列表, 全量明细)"""
    rows = []
    for f in sorted(glob.glob(os.path.join(LEDGER, "*_滑点.csv"))):
        d = _read(f)
        if not len(d) or "成交均价" not in d.columns:
            continue
        d["_品种"] = d.get("品种", pd.Series([""] * len(d)))
        d["_t"] = pd.to_datetime(d["时间"], errors="coerce")
        d["_dec"] = _num(d["信号根收盘"])
        d["_fill"] = _num(d["成交均价"])
        d["_tick"] = _num(d["滑点跳数"])
        d["_amt"] = _num(d["滑点金额"]) if "滑点金额" in d.columns else 0.0
        d = d.dropna(subset=["_t", "_dec", "_fill"])
        d = d[d["_fill"] != 0]
        if days:
            cut = datetime.now() - timedelta(days=days)
            d = d[d["_t"] >= cut]
        if not len(d):
            continue
        d["_dev"] = (d["_dec"] - d["_fill"]) / d["_fill"] * 100
        d["_abs"] = d["_dev"].abs()
        d["_file"] = os.path.basename(f).replace("_滑点.csv", "")
        rows.append(d[["_t", "_品种", "合约", "方向", "开平", "手数",
                       "_dec", "_fill", "_tick", "_amt", "_dev", "_abs", "_file"]])
    if not rows:
        return [], [], pd.DataFrame()

    allp = pd.concat(rows, ignore_index=True).sort_values("_t").reset_index(drop=True)
    allp["_in_p0"] = False

    p0, p_iso, p1 = [], [], []

    # ---- P0：同品种同合约「连续 >= 2 笔」大偏离 = 系统性错位 ----
    for (sym, con), g in allp.groupby(["_品种", "合约"], sort=False):
        idx = g.index.to_list()
        g = g.sort_values("_t")
        big = (g["_abs"] >= P0_PCT).to_numpy()
        i = 0
        n = len(big)
        while i < n:
            if not big[i]:
                i += 1
                continue
            j = i
            while j + 1 < n and big[j + 1]:
                j += 1
            seg = g.iloc[i:j + 1]
            if len(seg) >= 2:
                allp.loc[seg.index, "_in_p0"] = True
                p0.append({
                    "品种": sym, "合约": con, "笔数": len(seg),
                    "起": seg["_t"].iloc[0], "止": seg["_t"].iloc[-1],
                    "均偏离": seg["_dev"].mean(), "极值": seg["_dev"].iloc[seg["_abs"].argmax()],
                    "金额": seg["_amt"].sum(),
                })
            else:
                p_iso.append(g.iloc[i])
            i = j + 1

    # ---- P1 明细：1.0~1.5% 单笔 / 低阈值连续段 ----
    for _, r in allp.iterrows():
        if r["_in_p0"]:
            continue
        if P1_PCT <= r["_abs"] < P0_PCT:
            p1.append(dict(r, _note="单笔 1.0~1.5%"))

    for (sym, con), g in allp.groupby(["_品种", "合约"], sort=False):
        g = g.sort_values("_t").reset_index(drop=True)
        hit = (g["_abs"] >= RUN_PCT).astype(int).to_numpy()
        run = 0
        for i, h in enumerate(hit):
            run = run + 1 if h else 0
            if run == RUN_N:
                seg = g.iloc[i - RUN_N + 1:i + 1]
                if seg["_in_p0"].all() if "_in_p0" in seg else False:
                    break
                if seg["_abs"].max() < P1_PCT:
                    p1.append(dict(seg.iloc[-1], _note=f"连续{RUN_N}笔低偏离"))
                    break
    return p0, p_iso, p1, allp


def check_heartbeat() -> list[str]:
    out = []
    now = datetime.now()
    if not _in_session(now):
        return out
    for f in sorted(glob.glob(os.path.join(LEDGER, "*_运行心跳.csv"))):
        name = os.path.basename(f).replace("_运行心跳.csv", "")
        d = _read(f)
        if not len(d) or "记录时间" not in d.columns:
            continue
        t = pd.to_datetime(d["记录时间"].iloc[-1], errors="coerce")
        if pd.isna(t):
            continue
        gap = (now - t.to_pydatetime()).total_seconds() / 60
        if gap > HEARTBEAT_MIN:
            out.append(f"{name} 心跳停在 {t}（已 {gap:.0f} 分钟）")
    return out


def check_orphan(p0: list, allp: pd.DataFrame) -> list[str]:
    """有持仓，但程序认为的合约 != 最后一笔成交合约 → 可能孤儿/错位。"""
    out = []
    if not len(allp):
        return out
    for f in sorted(glob.glob(os.path.join(LEDGER, "underlying_*.json"))):
        key = os.path.basename(f).replace("underlying_", "").replace(".json", "")
        try:
            with open(f, encoding="utf-8") as fh:
                info = json.load(fh)
        except Exception:
            continue
        claimed = (info.get("underlying") or "").strip()
        if not claimed:
            continue
        # 心跳持仓
        hb = _read(os.path.join(LEDGER, f"{key}_运行心跳.csv"))
        if not len(hb) or "持仓手数" not in hb.columns:
            continue
        pos = _num(pd.Series([hb["持仓手数"].iloc[-1]])).iloc[0]
        if pd.isna(pos) or pos == 0:
            continue
        # 该 key 自身最近的成交合约
        own = allp[allp["_file"] == key]
        if not len(own):
            continue
        last_con = str(own["合约"].iloc[-1])
        if last_con != claimed:
            out.append(f"{key} 持仓{int(pos):+d}手：程序认为在 {claimed}，"
                       f"最后一笔实际成交在 {last_con}")
    return out


def _active_p0(p0: list, allp: pd.DataFrame, within_days: int = 3) -> list:
    """仍在错位中的段：段尾在最近 within_days 天内，且之后该品种没有恢复正常成交。
    历史段（早已自愈的）不参与退出码，否则计划任务会天天报警。"""
    if not len(allp):
        return p0
    cut = pd.Timestamp.now() - pd.Timedelta(days=within_days)
    out = []
    for r in p0:
        if r["止"] < cut:
            continue
        later = allp[(allp["_品种"] == r["品种"]) & (allp["_t"] > r["止"])
                     & (allp["_abs"] < 0.5)]
        if not len(later):
            out.append(r)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None, help="只看最近 N 天")
    ap.add_argument("--quiet", action="store_true", help="只输出结论")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    a = ap.parse_args()

    p0, p_iso, p1, allp = scan_slips(a.days)
    hb_bad = check_heartbeat()
    orphan = check_orphan(p0, allp)
    act = _active_p0(p0, allp)
    act_ids = {id(x) for x in act}

    if a.json:
        print(json.dumps({
            "p0_segments": [{k: (str(v) if k in ("起", "止") else v) for k, v in r.items()}
                            for r in p0],
            "p1_isolated": [{"t": str(r["_t"]), "品种": r["_品种"], "合约": r["合约"],
                             "dev_pct": round(float(r["_dev"]), 2)} for r in p_iso],
            "p1_other_n": len(p1),
            "heartbeat": hb_bad,
            "orphan": orphan,
            "scanned": len(allp),
        }, ensure_ascii=False, indent=2, default=str))
        sys.exit(1 if _active_p0(p0, allp) else 0)

    L = []
    A = L.append
    A("=" * 68)
    A("  换月错位巡检    " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    A("=" * 68)
    A(f"扫描滑点记录 {len(allp)} 笔"
      + (f"（最近 {a.days} 天）" if a.days else "（全量）"))
    if len(allp):
        A(f"时间范围 {allp['_t'].min():%Y-%m-%d} ~ {allp['_t'].max():%Y-%m-%d}")
    A("")

    if p0:
        A(f"■ P0 系统性错位 {len(p0)} 段  "
          f"（同合约连续>=2 笔 |偏离|>={P0_PCT}%）")
        A("")
        A(f"  {'品种':<7}{'合约':<14}{'起':<13}{'止':<13}{'笔':>3}{'均偏离':>8}{'极值':>8}")
        for r in p0:
            mark = "[LIVE]" if id(r) in act_ids else "[HIST]"
            A(f"  {mark} {str(r['品种']):<5}{str(r['合约']):<14}"
              f"{r['起']:%m-%d %H:%M} {r['止']:%m-%d %H:%M} {r['笔数']:>4}"
              f"{r['均偏离']:>+7.1f}%{r['极值']:>+7.1f}%")
        A("")
        A("  是否已自愈：")
        for r in p0:
            later = allp[(allp["_品种"] == r["品种"]) & (allp["_t"] > r["止"])
                         & (allp["_abs"] < 0.5)]
            if len(later):
                A(f"    {r['品种']} {r['合约']}：{later['_t'].iloc[0]:%m-%d %H:%M} "
                  f"起恢复正常 → 已自愈")
            else:
                A(f"    {r['品种']} {r['合约']}：**之后无正常成交 → 可能仍在错位中**")
        A("")

    if p_iso:
        A(f"■ P1 单笔大偏离 {len(p_iso)} 笔（多为跳空，需人工核）")
        for r in p_iso:
            A(f"  {r['_t']:%m-%d %H:%M} {r['_品种']} {r['合约']} {r['方向']}{r['开平']} "
              f"决策{r['_dec']:.1f}/成交{r['_fill']:.1f} 偏离{r['_dev']:+.1f}%")
        A("")

    if p1:
        A(f"■ P1 其他 {len(p1)} 条")
        for r in p1:
            note = r.get("_note", "") if isinstance(r, dict) else ""
            A(f"  {r['_t']:%m-%d %H:%M} {r['_品种']} {r['合约']} "
              f"决策{r['_dec']:.1f}/成交{r['_fill']:.1f} 偏离{r['_dev']:+.1f}% {note}")
        A("")

    if hb_bad:
        A(f"■ P1 心跳停滞 {len(hb_bad)} 个")
        for s in hb_bad:
            A("  " + s)
        A("")

    if orphan:
        A(f"■ P1 持仓/成交合约不一致 {len(orphan)} 个")
        for s in orphan:
            A("  " + s)
        A("")

    if not (p0 or p_iso or p1 or hb_bad or orphan):
        A("■ 未发现换月错位迹象。")
        A("")
        if len(allp):
            A(f"  最大单笔偏离 {allp['_abs'].max():.2f}%"
              f"（{allp.loc[allp['_abs'].idxmax(), '_t']:%Y-%m-%d %H:%M}）")
            A(f"  偏离中位 {allp['_abs'].median():.2f}%   P95 {allp['_abs'].quantile(0.95):.2f}%")

    txt = "\n".join(L)
    print(txt)
    if not a.quiet:
        snap = os.path.join(LEDGER, f"_巡检_{datetime.now():%Y%m%d_%H%M}.txt")
        try:
            with open(snap, "w", encoding="utf-8") as fh:
                fh.write(txt + "\n")
            print(f"\n快照：{snap}")
        except Exception:
            pass

    sys.exit(1 if act else 0)


if __name__ == "__main__":
    main()
