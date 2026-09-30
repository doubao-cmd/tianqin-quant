# -*- coding: utf-8 -*-
"""恢复 MA13 手数上限（修复 2026-09-29 解除上限导致的权益发散）。
解除上限后 hand ∝ equity 复利发散，期末权益爆到 1.69e17（非真实结果）。
恢复 min(HAND_MAX, ...) 硬约束，回到 HAND_MAX=25 已知正常口径。"""
import os, py_compile, glob

ROOT = r"C:\Users\cc\Desktop\天勤量化\策略\06_MA13超短线\回测"
files = sorted(glob.glob(os.path.join(ROOT, "*", "MA13超短线_回测.py")))

OLD_BASE = "    return max(HAND_MIN, h)   # 上限已解除（用户 2026-09-29）"
NEW_BASE = "    return max(HAND_MIN, min(HAND_MAX, h))"

OLD_PLAN = "    return max(HAND_MIN, base * (1 + tiers))   # 上限已解除（用户 2026-09-29）"
NEW_PLAN = "    return max(HAND_MIN, min(HAND_MAX, base * (1 + tiers)))"

ok, fail = [], []
for fp in files:
    with open(fp, "r", encoding="utf-8") as f:
        txt = f.read()
    if OLD_BASE not in txt or OLD_PLAN not in txt:
        fail.append((fp, "未找到解除上限行（可能已改回）"))
        continue
    txt = txt.replace(OLD_BASE, NEW_BASE, 1).replace(OLD_PLAN, NEW_PLAN, 1)
    with open(fp, "w", encoding="utf-8") as f:
        f.write(txt)
    try:
        py_compile.compile(fp, doraise=True)
        ok.append(fp)
    except py_compile.PyCompileError as e:
        fail.append((fp, "COMPILE: " + str(e)[:80]))

print(f"恢复上限成功 {len(ok)} 个：")
for fp in ok:
    print("  OK  ", os.path.basename(os.path.dirname(fp)))
for fp, m in fail:
    print("  FAIL", os.path.basename(os.path.dirname(fp)), m)
