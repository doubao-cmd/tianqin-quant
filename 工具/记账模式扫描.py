# -*- coding: utf-8 -*-
"""
★ 全项目「权益记账 bug 模式」静态扫描器（2026-10-11 建立，防复发）
=================================================================
## 背景
2026-10-11 发现 06 MA13 回测存在严重记账 bug：
    equity += pos * (close[i] - close[i-1]) * MULT
在开盘/平仓 bar 未排除，把 bar 内价差算进持仓盈亏 ⇒ 顺势策略收益系统性虚高。
（该 bug 已在 12+ 个脚本中修复，但同类写法可能散落全项目 ⇒ 需要静态体检。）

## 它检测什么（两条模式）
  P1（高危）`equity += pos * (X[i] - X[i-1]) * ...`
     —— 逐 bar 浮动盈亏累加进 equity。若循环内有开/平仓，则必然产生边界误差。
  P2（中危）`equity -= <成本变量>` 与平仓 pnl 中又扣成本并存
     —— 成本被记两套，逐笔合计 ≠ 权益变化。

## 输出
逐文件列出命中行；对每个命中，判断其所在目录是否属于「留证区」
（_收纳/ _归档/ 备份/ _输出/_*复现*/ 等），区分「需处理」与「已豁免」。

## 用法
    python 工具/记账模式扫描.py            # 扫描全项目
    python 工具/记账模式扫描.py --strict   # 只列「需处理」项（留证区不进输出）
退出码：0=无待处理；1=有「需处理」命中（可用于 CI/自检链）。
"""
import argparse
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── 项目根：向上找 lib/ ──
ROOT = os.path.dirname(os.path.abspath(__file__))
while ROOT != os.path.dirname(ROOT) and not os.path.isdir(os.path.join(ROOT, "lib")):
    ROOT = os.path.dirname(ROOT)

# ── 模式定义 ──
P1 = re.compile(r"equity\s*\+=\s*pos\s*\*\s*\(\s*(\w+)\s*\[\s*i\s*\]\s*-\s*\1\s*\[\s*i\s*-\s*1\s*\]")
P1B = re.compile(r"equity\s*\+=\s*pos\s*\*\s*\(\s*(\w+)\s*-\s*(\w+)\s*\)")   # 变体（非 [i] 索引）
P2 = re.compile(r"equity\s*-=\s*(oc|entry_cost|exit_cost|open_cost)")

# ── 留证/豁免区（这些目录里的命中不算「需处理」）──
EXEMPT_DIR_PARTS = [
    os.sep + "_收纳",
    os.sep + "_归档",
    os.sep + "_修复前备份",
    os.sep + "_失真产物备份",
    os.sep + "备份",
    os.sep + "_输出",          # 诊断/复现脚本：刻意复刻 bug 供取证
    os.sep + "_冻结" + os.sep + "策略",  # carry 已验证无此 bug；冻结区不主动改
    os.sep + "_临时探针",
]
EXEMPT_FILE_PARTS = [
    "诊断_回测实测差异",       # 刻意复刻旧口径以做四口径对照（已加警告注释）
    "记账模式扫描",           # 本文件自己
]


def is_exempt(path: str) -> bool:
    rel = os.path.relpath(path, ROOT)
    for p in EXEMPT_DIR_PARTS:
        if p in os.sep + rel:
            return True
    for f in EXEMPT_FILE_PARTS:
        if f in rel:
            return True
    return False


def scan():
    hits = {"P1": [], "P2": []}
    for dirpath, dirnames, filenames in os.walk(ROOT):
        # 跳过明显无关的目录，加速
        dirnames[:] = [d for d in dirnames
                       if d not in (".git", "__pycache__", ".workbuddy", "node_modules")]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            fp = os.path.join(dirpath, fn)
            try:
                lines = open(fp, encoding="utf-8", errors="replace").read().splitlines()
            except Exception:
                continue
            for idx, ln in enumerate(lines, 1):
                s = ln.strip()
                if s.startswith("#"):
                    continue          # 纯注释行不算
                if P1.search(ln) or P1B.search(ln):
                    hits["P1"].append((fp, idx, ln.strip()))
                elif P2.search(ln):
                    hits["P2"].append((fp, idx, ln.strip()))
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true",
                    help="只列「需处理」项（留证区不进输出）")
    args = ap.parse_args()

    hits = scan()
    need_action = 0

    for tag, desc in (("P1", "高危：逐 bar 浮动盈亏累加进 equity"),
                      ("P2", "中危：equity 直接扣成本（可能与 pnl 重复计成本）")):
        rows = hits[tag]
        keep = []
        for fp, idx, ln in rows:
            if is_exempt(fp):
                if not args.strict:
                    keep.append((fp, idx, ln, "留证区(豁免)"))
            else:
                keep.append((fp, idx, ln, "★需处理"))
                need_action += 1
        if not keep:
            continue
        print(f"\n{'='*76}")
        print(f"[{tag}] {desc}  —— 命中 {len(keep)} 处")
        print(f"{'='*76}")
        for fp, idx, ln, flag in keep:
            rel = os.path.relpath(fp, ROOT)
            print(f"  [{flag}] {rel}:{idx}")
            print(f"           {ln[:110]}")

    print(f"\n{'='*76}")
    if need_action == 0:
        print("✅ 未发现「需处理」的记账 bug 模式。")
    else:
        print(f"⚠️  发现 {need_action} 处「需处理」记账模式，请人工核对。")
    print("（口径：留证区 = _收纳/ _归档/ 备份/ _输出/ _冻结/策略/ —— 刻意保留）")
    return 1 if need_action else 0


if __name__ == "__main__":
    sys.exit(main())
