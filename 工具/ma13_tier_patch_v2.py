# -*- coding: utf-8 -*-
"""
MA13 加仓阈值修正（用户 2026-09-29 二次澄清）：
“40万/60万/80万”是【减本金后的利润】，对应权益 60/80/100 万。
=> 首个加仓档 = 累计利润达 40 万（权益 60 万），之后每多赚 20 万加一档。
即：tiers = (profit - 40万)//20万 + 1  (profit>=40万)，否则 0。
"""
import os, py_compile, glob

ROOT = r"C:\Users\cc\Desktop\天勤量化\策略\06_MA13超短线\回测"
files = sorted(glob.glob(os.path.join(ROOT, "*", "MA13超短线_回测.py")))

OLD_BLOCK = '''# ---- 动态仓位：本金保本 + 等差加仓（用户 2026-09-29 口径）----
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

NEW_BLOCK = '''# ---- 动态仓位：本金保本 + 等差加仓（用户 2026-09-29 口径，修正阈值）----
PRINCIPAL  = 200000.0   # 本金 / 保本线：本金部分手数恒定，不随浮盈放大（减本金）
FIRST_ADD = 400000.0    # 首个加仓档：累计利润达 40 万（= 权益 60 万）才加第一档
STEP      = 200000.0    # 等差档距：之后每再多赚 20 万加一档（权益 80/100/120万…）
def _base_hand(a):
    """本金基准块：用本金算出的波动率手数（恒定，保本=本金不放大）。"""
    if math.isnan(a) or a <= 0:
        return HAND_MIN
    h = int(round((PRINCIPAL * RISK_PCT) / (a * TICK * MULT)))
    return max(HAND_MIN, min(HAND_MAX, h))

def hand_plan(a, equity):
    """手数 = 本金基准块 + 利润档位加仓（等差）；减本金后利润满 40 万起加。"""
    base = _base_hand(a)
    profit = max(equity - PRINCIPAL, 0.0)
    if profit < FIRST_ADD:
        tiers = 0
    else:
        tiers = int((profit - FIRST_ADD) // STEP) + 1   # 权益60万→1档, 80万→2档, 100万→3档…
    return max(HAND_MIN, min(HAND_MAX, base * (1 + tiers)))'''

OLD_COST = "本金{PRINCIPAL:.0f}保本+等差加仓(每{STEP/10000:.0f}万利润加一档); 目标风险{RISK_PCT*100:.0f}%"
NEW_COST = "本金{PRINCIPAL:.0f}保本+减本金后利润满40万(权益60万)起、每多20万加一档; 目标风险{RISK_PCT*100:.0f}%"

OLD_PARAM = "('目标风险/仓位', f'本金{PRINCIPAL:.0f}保本+等差加仓(每{STEP/10000:.0f}万利润加一档)'),"
NEW_PARAM = "('目标风险/仓位', f'本金{PRINCIPAL:.0f}保本+减本金后利润满40万(权益60万)起、每多20万加一档'),"

ok, fail = [], []
for fp in files:
    with open(fp, "r", encoding="utf-8") as f:
        txt = f.read()
    if OLD_BLOCK in txt:
        txt = txt.replace(OLD_BLOCK, NEW_BLOCK, 1)
    else:
        fail.append((fp, "OLD_BLOCK not found"))
        continue
    txt = txt.replace(OLD_COST, NEW_COST, 1).replace(OLD_PARAM, NEW_PARAM, 1)
    with open(fp, "w", encoding="utf-8") as f:
        f.write(txt)
    try:
        py_compile.compile(fp, doraise=True)
        ok.append(fp)
    except py_compile.PyCompileError as e:
        fail.append((fp, "COMPILE: " + str(e)[:80]))

print(f"修正成功 {len(ok)} 个")
for fp in ok:
    print("  OK  ", os.path.basename(os.path.dirname(fp)))
for fp, m in fail:
    print("  FAIL", os.path.basename(os.path.dirname(fp)), m)
