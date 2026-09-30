# -*- coding: utf-8 -*-
"""把 PVC 双均线账本 v7 的 5 个字段还原回去。

事件
--------------------------------------------------------------
2026-09-28 00:20 跑了一次「新练习区间（2016-01-01 ~ 2019-12-31）基线回测」，
回测结束时的自动抓取把【最新一条版本】当成落点，于是 v7 被覆盖了 5 个字段：

    报告       ..._30min_out.html        ->  ..._30min_in.html
    标记       out                       ->  in
    指标       （期末考读数 −7.49%）      ->  （新练习区间读数 −31.83%）
    快照时间   2026-09-21 23:24          ->  2026-09-28 00:20
    代码快照   2026-09-21_232225         ->  2026-09-28_000930

而 v7 自身的「区间」字段仍是 2025-03-13 ~ 2025-09-13（样本外·期末考）——
两者对不上 = 本卡自相矛盾。

本脚本做的事
--------------------------------------------------------------
1. 断言现状确实是被污染的那个样子（对不上就拒绝动手）
2. 按 _归档/账本备份_20260925_第三轮/PVC_双均线.json 还原这 5 个字段
3. 在「修订记录」里追加一条，把这次覆盖记清楚
4. 保持原格式：indent=2 + CRLF

不动的东西：「区间」「累计试验次数」以及其余 5 条版本记录，一个字节不碰。

用法：
    python _fix_PVC账本v7.py          # 干跑
    python _fix_PVC账本v7.py --go     # 真改（先把当前版本拷进本目录）
"""
import json
import os
import shutil
import sys

本目录 = os.path.dirname(os.path.abspath(__file__))
_ROOT = 本目录
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)

账本 = os.path.join(_ROOT, "工具", "_账本", "PVC_双均线.json")
备份源 = os.path.join(_ROOT, "_归档", "账本备份_20260925_第三轮", "PVC_双均线.json")

# 期望的「被污染现状」——对不上就说明情况变了，必须人工看一眼
期望_污染 = {
    "报告": "回测结果/01_双均线/PVC/PVC_双均线_30min_in.html",
    "标记": "in",
    "快照时间": "2026-09-28 00:20",
    "代码快照": "2026-09-28_000930",
}
期望_污染指标_收益率 = "-31.83%"

修订说明 = (
    "2026-09-28：本卡【报告 / 标记 / 指标 / 快照时间 / 代码快照】五个字段，"
    "被本日 00:20 一次「新练习区间（2016-01-01 ~ 2019-12-31）基线回测」结束时的自动抓取覆盖"
    "（标记 out→in、报告 _out→_in、指标换成该区间读数 −31.83%），"
    "与本卡自身区间 2025-03-13 ~ 2025-09-13 冲突、自相矛盾。"
    "现依据 _归档/账本备份_20260925_第三轮/PVC_双均线.json 还原这五个字段。"
    "该次基线跑不属于 v7（v7 是「开盘后 30 分钟不开新仓」那次改动），"
    "它的读数只是「策略在新练习区间上的首次基线」，不是一次策略实验，故不计入试验次数。"
)


def 读账本(p):
    with open(p, encoding="utf-8", newline="") as f:
        return f.read()


def 写账本(p, d):
    s = json.dumps(d, ensure_ascii=False, indent=2).replace("\n", "\r\n")
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(s)


def main():
    真改 = "--go" in sys.argv
    print("=" * 74)
    print("  PVC 双均线账本 v7 字段还原  " + ("【真改】" if 真改 else "【干跑】"))
    print("=" * 74)

    d = json.loads(读账本(账本))
    v7 = [r for r in d["版本"] if r.get("版本") == "v7"]
    if not v7:
        print("  ✗ 找不到 v7，中止")
        return 1
    v7 = v7[0]

    # ---- 1. 断言现状 ----
    问题 = []
    for k, want in 期望_污染.items():
        if str(v7.get(k)) != want:
            问题.append("「%s」现在是 %r，期望 %r" % (k, v7.get(k), want))
    if str(v7.get("指标", {}).get("收益率")) != 期望_污染指标_收益率:
        问题.append("「指标.收益率」现在是 %r，期望 %r"
                    % (v7.get("指标", {}).get("收益率"), 期望_污染指标_收益率))
    if "2025-03-13" not in str(v7.get("区间", "")):
        问题.append("v7「区间」已不是 2025-03-13 那一段：%r" % v7.get("区间"))
    if not os.path.exists(备份源):
        问题.append("还原依据不存在：%s" % 备份源)
    if 问题:
        print("  ✗ 现状与预期不符，拒绝动手：")
        for q in 问题:
            print("     " + q)
        return 1

    # ---- 2. 从备份取原始值 ----
    旧 = json.loads(读账本(备份源))
    旧v7 = [r for r in 旧["版本"] if r.get("版本") == "v7"][0]
    还原项 = ["报告", "标记", "指标", "快照时间", "代码快照"]
    print("  还原以下字段（依据：_归档/账本备份_20260925_第三轮）：")
    for k in 还原项:
        现在 = json.dumps(v7.get(k), ensure_ascii=False)
        改成 = json.dumps(旧v7.get(k), ensure_ascii=False)
        print("    %-6s  现在 %s" % (k, 现在[:110]))
        print("    %-6s  还原 %s" % ("", 改成[:110]))
        if 现在 == 改成:
            问题.append("「%s」备份值与现值相同，无需还原 —— 请人工确认" % k)
    if 问题:
        for q in 问题:
            print("  ✗ " + q)
        return 1

    # ---- 3. 落地 ----
    for k in 还原项:
        v7[k] = 旧v7[k]
    v7.setdefault("修订记录", [])
    if any("五个字段" in x for x in v7["修订记录"]):
        print("  （修订记录里已有本次说明，不再重复追加）")
    else:
        v7["修订记录"].append(修订说明)

    # ---- 4. 落地 + 自检 ----
    # ★ 必须拿「改动前的原文」做对比基线；改动落在 d 上（v7 是 d 里的同一个 dict）。
    #   上一版这里错写成「再读一次文件赋给 d2」，结果是：① 写入的是 d2（没改动），
    #   ② 自检拿 d2 与 d 比 —— 其实原文件 vs 原文件，永远通过。已改正。
    原文 = 读账本(账本)
    原d = json.loads(原文)

    if 真改:
        备份 = os.path.join(本目录, "PVC_双均线.json")
        shutil.copy2(账本, 备份)
        写账本(账本, d)

        # --- 复核：写回后重新读，确认「只改了该改的」 ---
        t = 读账本(账本)
        if "\n" in t.replace("\r\n", ""):
            问题.append("写回后出现裸 LF")
        新d = json.loads(t)
        for k in ("品种", "策略", "周期", "区间", "累计试验次数", "说明", "写法约定"):
            if k in 新d and 新d[k] != 原d[k]:
                问题.append("顶层「%s」被意外改动" % k)
        if len(新d["版本"]) != len(原d["版本"]):
            问题.append("版本条数被改动")
        for a, b in zip(原d["版本"], 新d["版本"]):
            if a.get("版本") == "v7":
                continue
            if a != b:
                问题.append("非 v7 的记录被意外改动：%s" % a.get("版本"))
        新v7 = [r for r in 新d["版本"] if r.get("版本") == "v7"][0]
        for k in 还原项:
            if 新v7.get(k) != 旧v7.get(k):
                问题.append("还原后「%s」仍未回到备份值" % k)
        if 新d == 原d:
            问题.append("写回后内容与原文完全相同 —— 说明根本没写进去")
        if 问题:
            print("  ✗ 写回后复核不通过（污染版已备份，可还原）：")
            for q in 问题:
                print("     " + q)
            return 1
        print()
        print("  已还原并通过复核；污染版备份在 %s" % os.path.relpath(备份, _ROOT))
    else:
        print()
        print("  这是干跑。要真改请加 --go")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
