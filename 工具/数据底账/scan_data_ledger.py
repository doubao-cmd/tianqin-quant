# -*- coding: utf-8 -*-
"""
数据底账扫描器 —— 「多 agent 数据扫盲」的地基。

干什么：把所有 K 线 csv 数据源扫一遍，产出每个文件的
        ① 文件名 ② mtime ③ 行数/区间 ④ 退化率(O==H==L==C) ⑤ 其它质量信号
        写成 数据底账.json（机器读）+ 数据底账.md（人/agent 读）。

为什么：10-14 那批判死证据跑在 94.3% 退化的 5min 上，九天无人发现，
        根因不是算错，是产物里没有记录"数据版本"。
        ⇒ 任何结论落地前，先在这里查三件套。

用法：
    python scan_data_ledger.py                # 扫全部源，写 json+md
    python scan_data_ledger.py --quiet        # 只写文件，不打印
    python scan_data_ledger.py --source 进场体检  # 只扫某个源

红线：本脚本只读数据、只写自己的输出目录，不改任何数据、不删任何文件。
"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # 天勤量化/
OUT_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------- 数据源登记
# (key, 相对路径, 用途说明)
SOURCES = [
    ("进场体检", "工具/_进场体检/数据",
     "24 信号 × 12 品种判死主证据链（5min/15min/1440min）"),
    ("指数日线40", "工具/_进场体检/数据_指数",
     "40 品种 × 2016-2025 指数日线 KQ.i@（趋势性排名检验底座，2026-11-07 补 28 个）"),
    ("工作台", "策略/06_MA13超短线/工作台/data",
     "MA13 回测/选池用的日线与 5min（bt_ 前缀）"),
    ("复盘缓存", "工具/复盘/_缓存/kline2026",
     "复盘终端 13 品种 × 1/5/15/1440min，2026 年"),
    ("复盘归档", "工具/复盘/_缓存/kline_archive", "复盘终端历史归档"),
    ("账户分析", "工具/账户分析/_缓存/kline2026_5min",
     "MA13 分年回测的 5min（中文文件名 + 纳秒时间戳）"),
    ("成本因子", "工具/_r1_cost/out/kline",
     "R1 成本-信息半径，CZCE 合约级 1/5min"),
    ("超级趋势", "工具/_supertrend_ts/data", "Supertrend 检测数据"),
    ("基差因子", "工具/_截面因子_基差/数据", "截面基差因子数据"),
    ("隔夜跳空", "研究/N1_隔夜跳空风险/data", "隔夜跳空研究数据"),
    ("仓单因子", "工具/_仓单因子/数据", "仓单因子数据"),
    ("真实HL", "讨论/真实HL_日线", "2016-2021 日线真 HL 母本（2026-11-04 已接入进场体检）"),
]

CN2SYM = {
    "甲醇": "MA", "天然橡胶": "RU", "棕榈油": "P", "乙二醇": "EG",
    "鸡蛋": "JD", "原木": "LG", "PVC": "V", "纯碱": "SA",
    "螺纹钢": "RB", "豆粕": "M", "豆油": "Y", "燃料油": "FU",
    "铁矿石": "I", "焦煤": "JM", "苯乙烯": "EB", "白糖": "SR",
    "生猪": "LH",
}
PERIOD_PAT = [
    ("1440min", "1440min"), ("1d", "1440min"), ("daily", "1440min"),
    ("60min", "60min"), ("30min", "30min"), ("15min", "15min"),
    ("5min", "5min"), ("1min", "1min"),
]


def parse_name(fname: str, parent: str) -> tuple[str, str]:
    """从文件名推 (symbol, period)。推不出就给 '?'。"""
    stem = fname[:-4] if fname.lower().endswith(".csv") else fname
    low = stem.lower()

    period = "?"
    for key, val in PERIOD_PAT:
        if key in low:
            period = val
            break

    sym = "?"
    # CZCE.MA701_5min
    if "." in stem.split("_")[0]:
        code = stem.split("_")[0].split(".")[-1]  # MA701
        letters = "".join(ch for ch in code if ch.isalpha())
        if letters:
            sym = letters
    if sym == "?":
        if stem in CN2SYM:
            sym = CN2SYM[stem]
        else:
            for cn, s in CN2SYM.items():
                if cn in stem:
                    sym = s
                    break
    if sym == "?":
        # bt5min_MA / bt_daily_MA / daily_MA / MA_5min_... / MA.csv
        parts = [p for p in stem.split("_") if p]
        for p in parts:
            if p.lower() in ("bt5min", "bt1min", "btdaily", "bt", "daily", "5min",
                             "1min", "15min", "1440min", "60min") or p[:1].isdigit():
                continue
            if p.isupper() and 1 <= len(p) <= 3:
                sym = p
                break
    if period == "?" and low.startswith("daily"):
        period = "1440min"
    if period == "?" and "bt5min" in low:
        period = "5min"
    if period == "?" and "bt1min" in low:
        period = "1min"
    return sym, period


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def scan_file(path: Path, src_key: str) -> dict:
    st = path.stat()
    fname = path.name
    rec = {
        "source": src_key,
        "file": fname,
        "path": str(path.relative_to(ROOT)).replace("\\", "/"),
        "mtime": _dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
        "size_mb": round(st.st_size / 1048576, 2),
        "rows": 0,
        "cols": [],
        "has_ohlc": False,
        "first": "",
        "last": "",
        "degenerate": None,     # 退化率 %
        "zero_vol": None,
        "bad_px": None,
        "dup_dt": None,
        "err": "",
    }
    sym, period = parse_name(fname, path.parent.name)
    rec["sym"], rec["period"] = sym, period

    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            rd = csv.DictReader(fh)
            cols = [c.strip() for c in (rd.fieldnames or [])]
            rec["cols"] = cols
            lowmap = {c.lower(): c for c in cols}
            o = lowmap.get("open"); h = lowmap.get("high")
            l = lowmap.get("low"); cl = lowmap.get("close")
            v = lowmap.get("volume")
            tcol = lowmap.get("datetime") or lowmap.get("dt") or lowmap.get("date")
            rec["has_ohlc"] = all([o, h, l, cl])

            n = deg = zvol = badpx = 0
            seen = set()
            dup = 0
            first = last = ""
            for row in rd:
                n += 1
                if tcol:
                    t = (row.get(tcol) or "").strip()
                    if t:
                        if not first:
                            first = t
                        last = t
                        if t in seen:
                            dup += 1
                        elif n <= 300000:
                            seen.add(t)
                if rec["has_ohlc"]:
                    oo, hh, ll, cc = (_num(row.get(o)), _num(row.get(h)),
                                      _num(row.get(l)), _num(row.get(cl)))
                    if None not in (oo, hh, ll, cc):
                        if oo == hh == ll == cc:
                            deg += 1
                        if oo <= 0 or hh <= 0 or ll <= 0 or cc <= 0:
                            badpx += 1
                if v is not None:
                    vv = _num(row.get(v))
                    if vv == 0:
                        zvol += 1
            rec["rows"] = n
            if n:
                if rec["has_ohlc"]:
                    rec["degenerate"] = round(deg / n * 100, 1)
                    rec["bad_px"] = round(badpx / n * 100, 3)
                if v is not None:
                    rec["zero_vol"] = round(zvol / n * 100, 1)
                rec["dup_dt"] = dup
            rec["first"], rec["last"] = first[:19], last[:19]
    except Exception as e:  # 单文件失败不影响整体
        rec["err"] = f"{type(e).__name__}: {e}"
    return rec


def grade(rec: dict) -> str:
    if rec["err"]:
        return "🔴 读取失败"
    if rec["rows"] == 0:
        return "🔴 空文件"
    if not rec["has_ohlc"]:
        return "⚪ 无OHLC"
    d = rec["degenerate"] or 0
    if d >= 30:
        return "🔴 退化≥30%"
    if d >= 10:
        return "🟠 退化10-30%"
    if d >= 1:
        return "🟡 退化1-10%"
    return "✅ 干净"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=None, help="只扫指定源（key 名）")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    records = []
    src_stat = []
    for key, rel, note in SOURCES:
        if args.source and args.source != key:
            continue
        d = ROOT / rel
        if not d.is_dir():
            src_stat.append({"source": key, "dir": rel, "note": note,
                             "n_files": 0, "missing": True})
            continue
        files = sorted(p for p in d.glob("*.csv") if not p.name.startswith("_"))
        for p in files:
            records.append(scan_file(p, key))
        src_stat.append({"source": key, "dir": rel, "note": note,
                         "n_files": len(files), "missing": False})

    for r in records:
        r["grade"] = grade(r)

    payload = {
        "generated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "root": str(ROOT),
        "sources": src_stat,
        "files": records,
    }
    (OUT_DIR / "数据底账.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    # ------------------------------------------------------------- md 报告
    L = []
    L.append("# 数据底账（自动生成，勿手改）\n")
    L.append(f"> 生成时间：**{payload['generated_at']}** ｜ 扫描根：`{ROOT}`\n")
    L.append("> 由 `工具/数据底账/scan_data_ledger.py` 生成。"
             "**任何判死结论引用数据前，必须先在这里查三件套 = 文件名 + mtime + 退化率。**\n")
    L.append("> 退化 = 该根 K 线 `open==high==low==close`（tqsdk 拉空了才会这样，正常市场几乎不可能）。\n")

    L.append("\n## 一、数据源总览\n")
    L.append("| 源 | 目录 | 文件数 | 用途 |")
    L.append("|---|---|---:|---|")
    for s in src_stat:
        n = "缺失" if s.get("missing") else str(s["n_files"])
        L.append(f"| **{s['source']}** | `{s['dir']}` | {n} | {s['note']} |")

    L.append("\n## 二、质量分级汇总\n")
    from collections import Counter
    by_grade = Counter(r["grade"] for r in records)
    L.append("| 分级 | 文件数 |")
    L.append("|---|---:|")
    for g, n in sorted(by_grade.items()):
        L.append(f"| {g} | {n} |")
    L.append(f"| **合计** | **{len(records)}** |")

    L.append("\n## 三、按源 × 周期 的质量分布\n")
    pair = Counter((r["source"], r["period"], r["grade"]) for r in records)
    L.append("| 源 | 周期 | 分级 | 文件数 |")
    L.append("|---|---|---|---:|")
    for (s, p, g), n in sorted(pair.items()):
        L.append(f"| {s} | {p} | {g} | {n} |")

    L.append("\n## 四、问题文件（退化 ≥1% 或异常）\n")
    bad = [r for r in records
           if r["err"] or r["rows"] == 0
           or (r["degenerate"] or 0) >= 1
           or (r["bad_px"] or 0) > 0]
    if not bad:
        L.append("无。\n")
    else:
        L.append("| 源 | 文件 | 行数 | 退化 | mtime | 判定 |")
        L.append("|---|---|---:|---:|---|---|")
        for r in sorted(bad, key=lambda x: -(x["degenerate"] or 0)):
            L.append(f"| {r['source']} | `{r['file']}` | {r['rows']:,} | "
                     f"{'-' if r['degenerate'] is None else str(r['degenerate'])+'%'} | "
                     f"{r['mtime']} | {r['grade']} |")

    L.append("\n## 五、全量清单\n")
    L.append("| 源 | 品种 | 周期 | 行数 | 区间 | 退化 | mtime | 文件 |")
    L.append("|---|---|---|---:|---|---:|---|---|")
    for r in sorted(records, key=lambda x: (x["source"], x["sym"], x["period"])):
        deg = "-" if r["degenerate"] is None else f"{r['degenerate']}%"
        rng = f"{r['first'][:10]}~{r['last'][:10]}" if r["first"] else "-"
        L.append(f"| {r['source']} | {r['sym']} | {r['period']} | {r['rows']:,} | "
                 f"{rng} | {deg} | {r['mtime']} | `{r['file']}` |")

    (OUT_DIR / "数据底账.md").write_text("\n".join(L) + "\n", encoding="utf-8")

    if not args.quiet:
        print(f"[数据底账] {payload['generated_at']}  扫描 {len(records)} 个文件")
        for s in src_stat:
            n = "缺失" if s.get("missing") else s["n_files"]
            print(f"  {s['source']:8s} {n:>4} 文件   {s['dir']}")
        print("\n分级：")
        for g, n in sorted(by_grade.items()):
            print(f"  {g:14s} {n:>4}")
        print(f"\n输出：{OUT_DIR / '数据底账.json'}")
        print(f"      {OUT_DIR / '数据底账.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
