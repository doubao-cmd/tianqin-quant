# -*- coding: utf-8 -*-
"""回滚 MA13 全策略到「扁平 8 手封顶」安全地基：HAND_MAX=8，去掉权益缩放与保证金约束。"""
import os, re, glob, py_compile

ROOT = r"C:/Users/cc/Desktop/天勤量化"
files = glob.glob(os.path.join(ROOT, "策略/06_MA13超短线/回测/*/MA13超短线_回测.py"))

new_block = '''# ---- 动态仓位（回滚到安全地基：本金保本基准块 + 封顶 8 手，不上杠杆）----
PRINCIPAL  = 200000.0   # 本金 / 保本线：本金部分手数恒定，不随浮盈放大（减本金）
def _base_hand(a):
    """本金基准块：用本金算出的波动率手数（恒定，保本=本金不放大）。"""
    if math.isnan(a) or a <= 0:
        return HAND_MIN
    h = int(round((PRINCIPAL * RISK_PCT) / (a * TICK * MULT)))
    return max(HAND_MIN, min(HAND_MAX, h))

def hand_plan(a, equity, price=None):
    """手数 = 本金保本基准块，封顶 HAND_MAX=8（安全地基；不参与权益缩放、不加杠杆）。"""
    return _base_hand(a)   # 扁平 8 手封顶：手数只随 ATR 变动（波动大->手数小），与浮盈无关
'''

pat = re.compile(r"# ---- 动态仓位.*?\n(?=\n\ndef run_backtest)", re.DOTALL)

ok, fail = [], []
for f in files:
    src = open(f, encoding="utf-8").read()
    if "HAND_MAX    = 200" not in src:
        fail.append((f, "HAND_MAX=200 未找到"))
        continue
    if "# ---- 动态仓位" not in src:
        fail.append((f, "动态仓位块未找到"))
        continue
    src = src.replace(
        "HAND_MAX    = 200   # 安全阀（极端兜底；正常由保证金约束手数生效）",
        "HAND_MAX    = 8     # 封顶 8 手（安全地基，不上杠杆）", 1)
    src = src.replace(
        "每多赚 20 万加一档（等差），夹在 [1, 25]",
        "封顶 8 手（安全地基，不上杠杆）", 1)
    src = pat.sub(new_block, src, count=1)
    open(f, "w", encoding="utf-8").write(src)
    try:
        py_compile.compile(f, doraise=True)
        ok.append(f)
    except Exception as e:
        fail.append((f, f"编译失败: {e}"))

print(f"回滚成功 {len(ok)} 个，失败 {len(fail)} 个")
for f in ok:
    print("  OK  ", os.path.basename(os.path.dirname(f)))
for f, why in fail:
    print("  FAIL", os.path.basename(os.path.dirname(f)), why)
