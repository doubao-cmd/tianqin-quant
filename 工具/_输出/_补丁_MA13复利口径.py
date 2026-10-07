# -*- coding: utf-8 -*-
"""一次性补丁：12 个 MA13 回测脚本 手数口径 固定20万 → 当前权益（复利）。
带全有或全无检查：三段旧文本必须各自恰好出现 1 次，否则该文件不动。"""
import glob
import io

OLD1 = '''# ---- 动态仓位（回滚到安全地基：本金保本基准块 + 封顶 8 手，不上杠杆）----
PRINCIPAL  = 200000.0   # 本金 / 保本线：本金部分手数恒定，不随浮盈放大（减本金）
def _base_hand(a):
    """本金基准块：用本金算出的波动率手数（恒定，保本=本金不放大）。"""
    if math.isnan(a) or a <= 0:
        return HAND_MIN
    h = int(round((PRINCIPAL * RISK_PCT) / (a * TICK * MULT)))
    return max(HAND_MIN, min(HAND_MAX, h))

def hand_plan(a, equity, price=None):
    """手数 = 本金保本基准块，封顶 HAND_MAX=8（安全地基；不参与权益缩放、不加杠杆）。"""
    return _base_hand(a)   # 扁平 8 手封顶：手数只随 ATR 变动（波动大->手数小），与浮盈无关'''

NEW1 = '''# ---- 动态仓位（2026-10-07 改：预算基数 固定本金20万 → 当前权益（复利）。唯一改动，其余不动）----
PRINCIPAL  = 200000.0   # 兜底：权益取不到时回落到 20 万（正常路径不会用到）
def _base_hand(a, equity):
    """波动率手数：预算 = 当前权益 ×1% ÷ 每手风险（ATR 口径）。
    改动史：原为固定 PRINCIPAL=20 万保本块（扁平，与浮盈无关）；
    2026-10-07 用户拍板改为按当前权益（复利）⇒ 赚钱自动加注、亏钱自动减注，
    代价=收益丧失单品种可加性。权益为决策时点（上一根收盘标记）——无未来函数。"""
    if math.isnan(a) or a <= 0:
        return HAND_MIN
    if equity is None or math.isnan(equity) or equity <= 0:
        equity = PRINCIPAL
    h = int(round((equity * RISK_PCT) / (a * TICK * MULT)))
    return max(HAND_MIN, min(HAND_MAX, h))

def hand_plan(a, equity, price=None):
    """手数 = 当前权益×1%÷(ATR×一跳×乘数)，夹 [1,8]。"""
    return _base_hand(a, equity)'''

OLD2 = ('本金{PRINCIPAL:.0f}保本+减本金后利润满40万(权益60万)起、每多20万加一档; '
        '目标风险{RISK_PCT*100:.0f}%; 手数=min(风险预算,权益×30%÷每手保证金); ')
NEW2 = '手数=当前权益×{RISK_PCT*100:.0f}%÷ATR风险夹[1,8]（20261007改复利口径）; '

OLD3 = '''("目标风险/仓位", f'本金{PRINCIPAL:.0f}保本+减本金后利润满40万(权益60万)起、每多20万加一档'),
                     ("手数约束", "保证金≤权益30%; min(风险预算, 权益×30%÷每手保证金)"),'''
NEW3 = '''("目标风险/仓位", f'当前权益×{RISK_PCT*100:.0f}%÷ATR风险，夹[1,8]（2026-10-07改复利）'),
                     ("手数约束", "权益为决策时点（上一根收盘标记），无未来函数"),'''

import re
RE3 = re.compile(
    r'\("目标风险/仓位", f\'[^\n]*\),\n\s*\("手数约束", "[^\n]*\),')

OLD3a = """('目标风险/仓位', f'本金{PRINCIPAL:.0f}保本+减本金后利润满40万(权益60万)起、每多20万加一档')"""
NEW3a = """('目标风险/仓位', f'当前权益×{RISK_PCT*100:.0f}%÷ATR风险，夹[1,8]（2026-10-07改复利）')"""
OLD3b = '''("手数约束", "保证金≤权益30%; min(风险预算, 权益×30%÷每手保证金)")'''
NEW3b = '''("手数约束", "权益为决策时点（上一根收盘标记），无未来函数")'''

files = sorted(glob.glob('策略/06_MA13超短线/回测/*/MA13超短线_回测.py'))
print('找到', len(files), '个回测脚本')
ok = fail = 0
for f in files:
    s = io.open(f, encoding='utf-8').read()
    c = (s.count(OLD1), s.count(OLD2), s.count(OLD3a), s.count(OLD3b))
    if c != (1, 1, 1, 1):
        print('⛔ 结构不符，未动', f, c)
        fail += 1
        continue
    s = s.replace(OLD1, NEW1).replace(OLD2, NEW2).replace(OLD3a, NEW3a).replace(OLD3b, NEW3b)
    io.open(f, 'w', encoding='utf-8', newline='\n').write(s)
    ok += 1
    print('改好', f)
print(f'替换完成 {ok}/{len(files)}，失败 {fail}')
