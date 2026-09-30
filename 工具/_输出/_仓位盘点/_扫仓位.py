# -*- coding: utf-8 -*-
"""盘点：每个策略 × 每个品种的仓位/口径参数。只读，不改任何东西。"""
import os, re, sys

sys.stdout.reconfigure(encoding="utf-8")
根 = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

策略表 = []
for f in sorted(os.listdir(os.path.join(根, "策略"))):
    p = os.path.join(根, "策略", f)
    if f.startswith("_") or not os.path.isdir(p):
        continue
    if os.path.isdir(os.path.join(p, "回测")):
        策略表.append(f)

print("策略目录：", 策略表)
print()

for s in 策略表:
    短名 = s.split("_", 1)[-1]
    d = os.path.join(根, "策略", s, "回测")
    品种 = [x for x in sorted(os.listdir(d)) if not x.startswith("_") and os.path.isdir(os.path.join(d, x))]
    print("=" * 100)
    print("【%s】品种池 %d 个：%s" % (s, len(品种), "、".join(品种)))
    主 = os.path.join(d, 品种[0], 短名 + "_回测.py")
    if not os.path.isfile(主):
        for x in sorted(os.listdir(os.path.join(d, 品种[0]))):
            if x.endswith("_回测.py") and "跨年" not in x:
                主 = os.path.join(d, 品种[0], x)
                break
    t = open(主, encoding="utf-8").read()
    print("  样本脚本：%s" % os.path.relpath(主, 根))

    # 1) 顶层常量
    print("  -- 顶层常量（形如 大写名 = 值）--")
    for m in re.finditer(r"^([A-Z][A-Z0-9_]{2,})\s*=\s*([^\n#]+)", t, re.M):
        k, v = m.group(1), m.group(2).strip()
        if k in ("OK",):
            continue
        print("     %-22s = %s" % (k, v[:70]))

    # 2) 引擎调用块
    m = re.search(r"engine\.(run_backtest\w*)\((.*?)\n(\s*)\)", t, re.S)
    if m:
        print("  -- 引擎函数：%s --" % m.group(1))
        blk = m.group(2)
        for line in blk.split("\n"):
            line = line.strip()
            if line:
                print("     %s" % line[:110])
    else:
        print("  -- ⚠ 没抓到 engine.run_backtest* 调用 --")
    print()
