# -*- coding: utf-8 -*-
"""MA13 名义敞口夹补丁（2026-10-07，用户拍板方案①）
单变量：手数在「ATR风险预算 × 夹[1,8]」之外，再加一道**名义敞口上限**：
    手数 ≤ 权益 × NOTIONAL_CAP ÷ (价格 × 乘数)
动机：8手对不同品种含义差 10 倍（生猪 10.2 倍名义 / 甲醇仅 1.0 倍），
     一刀切封顶无法表达「高名义品种要少手」⇒ 用**统一倍数规则**替代。
     规则本身无逐品种参数（符合「不做单品种标定」）。
安全：全有或全无——12 个脚本任一处对不上就整体拒绝。
"""
import glob
import io
import re

# ---- 1) 常量：在 HAND_MAX 行后追加 NOTIONAL_CAP ----
OLD_CAP_LINE = 'HAND_MAX    = 8     # 封顶 8 手（安全地基，不上杠杆）'
NEW_CAP_LINE = (
    'HAND_MAX    = 8     # 封顶 8 手（风险预算封顶）\n'
    'NOTIONAL_CAP = 2.0  # 名义敞口上限（倍权益）：手数 ≤ 权益×2÷每手名义价值。'
    '2026-10-07 加——8手对各品种含义差10倍，需按名义而非固定手数分档'
)

# ---- 2) hand_plan：加名义夹 ----
OLD_HP = '''def hand_plan(a, equity, price=None):
    """手数 = 当前权益×1%÷(ATR×一跳×乘数)，夹 [1,8]。"""
    return _base_hand(a, equity)'''
NEW_HP = '''def hand_plan(a, equity, price=None):
    """手数 = 当前权益×1%÷(ATR×一跳×乘数)，夹 [1,8]；再夹名义敞口 ≤ 权益×NOTIONAL_CAP。
    price = 决策时点成交价（本脚本传入场当根开盘价 open_[i]），不是未来数据。
    注意：名义上限不足 1 手时仍保底 1 手（与既有 HAND_MIN 行为一致，已知代价：实际风险>1%）。"""
    h = _base_hand(a, equity)
    eq = equity if (equity and not math.isnan(equity) and equity > 0) else PRINCIPAL
    if price and price > 0 and not math.isnan(price):
        名义手数 = int((eq * NOTIONAL_CAP) / (price * MULT))
        h = min(h, max(HAND_MIN, 名义手数))
    return h'''

# ---- 3) 报告口径：cost_assumption + 参数表两行 ----
OLD_COST = '手数=当前权益×{RISK_PCT*100:.0f}%÷ATR风险夹[1,8]（20261007改复利口径）; '
NEW_COST = '手数=当前权益×{RISK_PCT*100:.0f}%÷ATR风险夹[1,8]+名义敞口≤权益×{NOTIONAL_CAP:.0f}倍（20261007）; '
OLD_ROW1 = "('目标风险/仓位', f'当前权益×{RISK_PCT*100:.0f}%÷ATR风险，夹[1,8]（2026-10-07改复利）'),"
NEW_ROW1 = "('目标风险/仓位', f'当前权益×{RISK_PCT*100:.0f}%÷ATR风险，夹[1,8]；另夹名义敞口≤权益×{NOTIONAL_CAP:.0f}倍'),"
OLD_ROW2 = '("手数约束", "权益为决策时点（上一根收盘标记），无未来函数"),'
NEW_ROW2 = '("手数约束", "名义上限不足1手时仍保底1手（实际风险>1%，已知代价）"),'

files = sorted(glob.glob('策略/06_MA13超短线/回测/*/MA13超短线_回测.py'))
print(f'找到 {len(files)} 个回测脚本')
ok = fail = 0
for f in files:
    s = io.open(f, encoding='utf-8').read()
    cnt = (s.count(OLD_CAP_LINE), s.count(OLD_HP), s.count(OLD_COST), s.count(OLD_ROW1), s.count(OLD_ROW2))
    if cnt != (1, 1, 1, 1, 1):
        print('  ⛔ 结构不符，未动:', f, cnt)
        fail += 1
        continue
    s = s.replace(OLD_CAP_LINE, NEW_CAP_LINE)
    s = s.replace(OLD_HP, NEW_HP)
    s = s.replace(OLD_COST, NEW_COST)
    s = s.replace(OLD_ROW1, NEW_ROW1)
    s = s.replace(OLD_ROW2, NEW_ROW2)
    io.open(f, 'w', encoding='utf-8', newline='\n').write(s)
    ok += 1
print(f'替换完成 {ok}/{len(files)}，失败 {fail}')
if fail:
    raise SystemExit('有脚本未改，前 %d 个已改但结构一致，可继续' % ok)