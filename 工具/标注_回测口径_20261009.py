# -*- coding: utf-8 -*-
"""
给 06 回测结果打「口径标签」并生成索引表（2026-10-09）
================================================================
★ **只读+新建**，不改任何回测结果、不改任何生产脚本。
   存在的理由：`回测结果/` 里24 份`_参数.json` **混了三种仓位口径**，
   横向比较会得出错误结论（AI 自己已在 10-09 被误导过一次）。
   本脚本给每份结果标注「是否与当前实盘对齐」，并出一张 Markdown 索引表。

## 三种口径（判据来自每份 JSON 自带的 cost_assumption 字段）
  固定20万: cost_assumption 含 "目标风险2000"
            → ✅ **与实盘对齐**（ma13_live_multi.py:114RISK_EQUITY=200000锁死）
  复利     : 含 "当前权益"        → ❌ 手数随权益浮动，实盘不是这个
  阶梯     : 含 "利润满40万"      → ❌ 更早的特殊规则，已废弃

怎么用
    python 工具/标注_回测口径_20261009.py
"""
import os
import sys
import json
import glob

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)

RESULT_DIR = os.path.join(_ROOT, "回测结果", "06_MA13超短线")
OUT_MD = os.path.join(_ROOT, "工具", "_输出", "_回测口径索引.md")
SKIP = ("备份", "隔离", "改前")


def 判口径(cost_assumption):
    ca = cost_assumption or ""
    if "目标风险2000" in ca:
        return "固定20万", True, "含「目标风险2000」"
    if "利润满40万" in ca:
        return "阶梯", False, "含「利润满40万」（已废弃的特殊加仓规则）"
    if "当前权益" in ca:
        return "复利", False, "含「当前权益」（手数随权益浮动）"
    return "未知", False, "cost_assumption 无法识别"


def main():
    rows = []
    for f in glob.glob(os.path.join(RESULT_DIR, "*", "*_参数.json")):
        if any(x in f for x in SKIP):
            continue
        try:
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception as e:
            print(f"[skip] {f}: {e}")
            continue
        口径, 对齐, 依据 = 判口径(d.get("cost_assumption"))
        rows.append({
            "品种": d.get("symbol_cn") or "?",
            "起点": (d.get("start") or "")[:10],
            "止点": (d.get("end") or "")[:10],
            "笔数": d.get("trades"),
            "胜率": d.get("win_rate"),
            "Sharpe": d.get("sharpe"),
            "总收益": d.get("total_return"),
            "最大回撤": d.get("max_drawdown"),
            "口径": 口径,
            "与实盘对齐": 对齐,
            "判据": 依据,
            "文件": os.path.relpath(f, _ROOT).replace("\\", "/"),
        })

    rows.sort(key=lambda r: (not r["与实盘对齐"], r["品种"], r["起点"]))
    对齐n = sum(1 for r in rows if r["与实盘对齐"])

    L = []
    L.append("# 06 MA13 回测结果 · 仓位口径索引\n")
    L.append(f"> 由 `工具/标注_回测口径_20261009.py` 自动生成于 2026-10-09。")
    L.append(f"> 共 **{len(rows)}** 份结果，其中**与当前实盘对齐 {对齐n} 份**、不对齐 {len(rows)-对齐n} 份。\n")
    L.append("## 为什么要这张表\n")
    L.append("`回测结果/` 里历史结果**混了三种仓位口径**。口径不同 ⇒ 手数不同 ⇒ "
             "笔数/胜率/Sharpe 全部不可比。")
    L.append("2026-10-09 AI 曾把这些数字横向排在一起，据此得出「2026 有两个品种崩了」"
             "和「回测 47% 胜率 vs 实测 19%」两个结论——**两个都是错的**。\n")
    L.append("## 判据\n")
    L.append("**实盘（`ma13_live_multi.py:114`）锁死 `RISK_EQUITY = 200000.0`**，"
             "即手数按固定 20 万基数算，与账户实际余额（约 100 万）脱钩。")
    L.append("因此只有 `固定20万` 口径的结果才能与模拟盘对账。\n")
    L.append("| 口径 | 与实盘对齐 | 判据 |")
    L.append("|---|---|---|")
    L.append("| `固定20万` | ✅ 是 | cost_assumption 含「目标风险2000」 |")
    L.append("| `复利` | ❌ 否 | 含「当前权益」，手数随权益浮动 |")
    L.append("| `阶梯` | ❌ 否 | 含「利润满40万」，已废弃的特殊规则 |")
    L.append("")
    L.append("## 全表\n")
    L.append("| 对齐 | 品种 | 窗口 | 笔数 | 胜率 | Sharpe | 总收益 | 回撤 | 口径 |")
    L.append("|---|---|---|---:|---:|---:|---:|---:|---|")
    for r in rows:
        窗 = f"{r['起点']}~{r['止点']}" if r["起点"] != r["止点"] else r["起点"]
        标 = "✅" if r["与实盘对齐"] else "❌"
        L.append(f"| {标} | {r['品种']} | {窗} | {r['笔数']} | "
                 f"{r['胜率']:.1%} | {r['Sharpe']:.2f} | {r['总收益']:.1%} | "
                 f"{r['最大回撤']:.1%} | {r['口径']} |")
    L.append("")
    L.append("## 使用纪律\n")
    L.append("1. **只与 ✅ 行比较**。❌ 行的手数逻辑与实盘不同，混比会得出假结论。")
    L.append("2. ❌ 行的数字**不是错的**，只是另一个口径下的历史存档，保留是为了可追溯。")
    L.append("3. 要让 ❌ 行变成 ✅，需用当前脚本重跑该品种该窗口（重跑会覆盖 `_参数.json`），"
             "**建议先备份**。")
    L.append("")

    os.makedirs(os.path.dirname(OUT_MD), exist_ok=True)
    with open(OUT_MD, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(L))

    print(f"[ok] 共 {len(rows)} 份，对齐 {对齐n} 份 / 不对齐 {len(rows)-对齐n} 份")
    print(f"[save] {OUT_MD}")
    print()
    print(f"{'对齐':5}{'品种':7}{'窗口':24}{'笔数':>7}{'胜率':>8}{'Sharpe':>9}{'口径':>9}")
    print("-" * 74)
    for r in rows:
        print(f"{'YES' if r['与实盘对齐'] else 'NO':5}{r['品种']:7}"
              f"{r['起点']+'~'+r['止点']:24}{r['笔数']:>7}{r['胜率']:>8.1%}"
              f"{r['Sharpe']:>9.2f}{r['口径']:>9}")


if __name__ == "__main__":
    main()