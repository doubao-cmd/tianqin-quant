# -*- coding: utf-8 -*-
"""
MA13 动态仓位改造补丁：本金保本 + 等差加仓
- 本金 PRINCIPAL=200000 算基准块（恒定，减本金=本金不随浮盈放大）
- 每多赚 STEP=200000 加一档（40万/60万/80万…等差）
- 手数 = 基准块 × (1 + 档位数)，夹 [HAND_MIN, HAND_MAX]
仅改 06_MA13超短线 下 12 个脚本；不改布林带。
"""
import os, py_compile, glob

ROOT = r"C:\Users\cc\Desktop\天勤量化\策略\06_MA13超短线\回测"
files = sorted(glob.glob(os.path.join(ROOT, "*", "MA13超短线_回测.py")))

OLD_HAND_OF = '''def hand_of(a, equity):
    if math.isnan(a) or a <= 0:
        return HAND_MIN
    h = int(round((equity * RISK_PCT) / (a * TICK * MULT)))
    return max(HAND_MIN, min(HAND_MAX, h))'''

NEW_HAND = '''# ---- 动态仓位：本金保本 + 等差加仓（用户 2026-09-29 口径）----
PRINCIPAL = 200000.0   # 本金 / 保本线：本金部分手数恒定，不随浮盈放大（减本金）
STEP     = 200000.0    # 等差档距：每多赚 20 万加一档（40万/60万/80万…）
def _base_hand(a):
    """本金基准块：用本金算出的波动率手数（恒定，保本=本金不放大）。"""
    if math.isnan(a) or a <= 0:
        return HAND_MIN
    h = int(round((PRINCIPAL * RISK_PCT) / (a * TICK * MULT)))
    return max(HAND_MIN, min(HAND_MAX, h))

def hand_plan(a, equity):
    """手数 = 本金基准块 + 利润档位加仓（等差）。"""
    base = _base_hand(a)
    profit = max(equity - PRINCIPAL, 0.0)
    tiers = int(profit // STEP)        # 40万→1档, 60万→2档, 80万→3档 …
    return max(HAND_MIN, min(HAND_MAX, base * (1 + tiers)))'''

REPLACEMENTS = [
    # (old, new, required)
    (OLD_HAND_OF, NEW_HAND, True),
    ("hand = hand_of(a, equity)", "hand = hand_plan(a, equity)", True),
    ("目标风险{RISK_PCT*100:.0f}%×当前权益(动态)",
     "本金{PRINCIPAL:.0f}保本+等差加仓(每{STEP/10000:.0f}万利润加一档); 目标风险{RISK_PCT*100:.0f}%",
     False),
    ('("目标风险", f\'{params["RISK_PCT"]*100:.0f}%×当前权益(动态)\'),',
     "('目标风险/仓位', f'本金{PRINCIPAL:.0f}保本+等差加仓(每{STEP/10000:.0f}万利润加一档)'),",
     False),
    ("  4. 手数 = 波动率预算（20 万 × 1% ÷ (ATR×一跳×乘数)），夹在 [1, 25]。",
     "  4. 手数 = 本金 20 万算基准块（1%÷ATR，恒定保本），每多赚 20 万加一档（等差），夹在 [1, 25]。",
     False),
]

ok, fail = [], []
for fp in files:
    with open(fp, "r", encoding="utf-8") as f:
        txt = f.read()
    for old, new, required in REPLACEMENTS:
        if old in txt:
            txt = txt.replace(old, new, 1)
        elif required:
            fail.append((fp, "REQUIRED not found: " + old[:40]))
            break
    else:
        with open(fp, "w", encoding="utf-8") as f:
            f.write(txt)
        try:
            py_compile.compile(fp, doraise=True)
            ok.append(fp)
        except py_compile.PyCompileError as e:
            fail.append((fp, "COMPILE FAIL: " + str(e)[:80]))

print(f"成功改造 {len(ok)} 个文件")
for fp in ok:
    print("  OK  ", os.path.basename(os.path.dirname(fp)))
if fail:
    print(f"\n失败 {len(fail)} 个：")
    for fp, msg in fail:
        print("  FAIL", os.path.basename(os.path.dirname(fp)), msg)
