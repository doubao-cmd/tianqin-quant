# -*- coding: utf-8 -*-
"""MA13 加保证金约束手数（用户 2026-09-29 14:44 "加上试试"）。
hand = min(风险预算手数[本金保本+等差加仓], 保证金约束手数[权益×30%÷每手保证金])。
HAND_MAX 改为 200 作极端兜底（正常由保证金约束绑定）。12 个脚本统一。"""
import os, py_compile, glob

ROOT = r"C:\Users\cc\Desktop\天勤量化\策略\06_MA13超短线\回测"
files = sorted(glob.glob(os.path.join(ROOT, "*", "MA13超短线_回测.py")))

OLD_HMAX = "HAND_MAX    = 25"
NEW_HMAX = "HAND_MAX    = 200   # 安全阀（极端兜底；正常由保证金约束手数生效）"

OLD_BLOCK = '''# ---- 动态仓位：本金保本 + 等差加仓（用户 2026-09-29 口径，修正阈值）----
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

NEW_BLOCK = '''# ---- 动态仓位：本金保本 + 等差加仓 + 保证金约束（用户 2026-09-29 口径）----
PRINCIPAL  = 200000.0   # 本金 / 保本线：本金部分手数恒定，不随浮盈放大（减本金）
FIRST_ADD = 400000.0    # 首个加仓档：累计利润达 40 万（= 权益 60 万）才加第一档
STEP      = 200000.0    # 等差档距：之后每再多赚 20 万加一档（权益 80/100/120万…）
MARGIN_CAP_PCT = 0.30   # 保证金占用上限：手数不超 权益×该比例÷每手保证金
MARGIN_RATE    = 0.10   # 保证金率（商品期货近似，可按品种微调）
def _base_hand(a):
    """本金基准块：用本金算出的波动率手数（恒定，保本=本金不放大）。"""
    if math.isnan(a) or a <= 0:
        return HAND_MIN
    h = int(round((PRINCIPAL * RISK_PCT) / (a * TICK * MULT)))
    return max(HAND_MIN, min(HAND_MAX, h))

def hand_plan(a, equity, price):
    """手数 = min(风险预算手数[本金保本+等差加仓], 保证金约束手数[权益×30%÷每手保证金])。"""
    base = _base_hand(a)
    profit = max(equity - PRINCIPAL, 0.0)
    if profit < FIRST_ADD:
        tiers = 0
    else:
        tiers = int((profit - FIRST_ADD) // STEP) + 1   # 权益60万→1档, 80万→2档, 100万→3档…
    risk_hand = base * (1 + tiers)                              # 风险预算手数
    per_hand_margin = price * MULT * MARGIN_RATE                # 每手保证金 ≈ 价格×乘数×保证金率
    margin_hand = (equity * MARGIN_CAP_PCT) / per_hand_margin if per_hand_margin > 0 else risk_hand
    return max(HAND_MIN, min(HAND_MAX, risk_hand, int(margin_hand)))   # 双约束取小，HAND_MAX 仅极端兜底'''

ok, fail = [], []
for fp in files:
    with open(fp, "r", encoding="utf-8") as f:
        txt = f.read()
    if OLD_BLOCK not in txt or OLD_HMAX not in txt:
        fail.append((fp, "未找到目标块（可能已改）"))
        continue
    txt = txt.replace(OLD_HMAX, NEW_HMAX, 1).replace(OLD_BLOCK, NEW_BLOCK, 1)
    txt = txt.replace("hand_plan(a, equity)", "hand_plan(a, equity, open_[i])", 1)
    txt = txt.replace("手数1-25ATR预算", "手数=min(风险预算,权益×30%÷每手保证金)", 1)
    txt = txt.replace(
        "(\"手数范围\", f'{params[\"hand_range\"][0]}-{params[\"hand_range\"][1]}')",
        "(\"手数约束\", \"保证金≤权益30%; min(风险预算, 权益×30%÷每手保证金)\")", 1)
    with open(fp, "w", encoding="utf-8") as f:
        f.write(txt)
    try:
        py_compile.compile(fp, doraise=True)
        ok.append(fp)
    except py_compile.PyCompileError as e:
        fail.append((fp, "COMPILE: " + str(e)[:80]))

print(f"加保证金约束成功 {len(ok)} 个：")
for fp in ok:
    print("  OK  ", os.path.basename(os.path.dirname(fp)))
for fp, m in fail:
    print("  FAIL", os.path.basename(os.path.dirname(fp)), m)
