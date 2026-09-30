# -*- coding: utf-8 -*-
"""MA13 解除手数上限（用户 2026-09-29：直接解除上限，不跑回测）。
只去掉 min(HAND_MAX, ...) 这层上限夹取，保留 HAND_MIN 下限。"""
import os, py_compile, glob

ROOT = r"C:\Users\cc\Desktop\天勤量化\策略\06_MA13超短线\回测"
files = sorted(glob.glob(os.path.join(ROOT, "*", "MA13超短线_回测.py")))

OLD_BASE = "    return max(HAND_MIN, min(HAND_MAX, h))"
NEW_BASE = "    return max(HAND_MIN, h)   # 上限已解除（用户 2026-09-29）"

OLD_PLAN = "    return max(HAND_MIN, min(HAND_MAX, base * (1 + tiers)))"
NEW_PLAN = "    return max(HAND_MIN, base * (1 + tiers))   # 上限已解除（用户 2026-09-29）"

ok, fail = [], []
for fp in files:
    with open(fp, "r", encoding="utf-8") as f:
        txt = f.read()
    if OLD_BASE not in txt or OLD_PLAN not in txt:
        fail.append((fp, "未找到上限夹取行（可能已改）"))
        continue
    txt = txt.replace(OLD_BASE, NEW_BASE, 1).replace(OLD_PLAN, NEW_PLAN, 1)
    with open(fp, "w", encoding="utf-8") as f:
        f.write(txt)
    try:
        py_compile.compile(fp, doraise=True)
        ok.append(fp)
    except py_compile.PyCompileError as e:
        fail.append((fp, "COMPILE: " + str(e)[:80]))

print(f"解除上限成功 {len(ok)} 个：")
for fp in ok:
    print("  OK  ", os.path.basename(os.path.dirname(fp)))
for fp, m in fail:
    print("  FAIL", os.path.basename(os.path.dirname(fp)), m)
