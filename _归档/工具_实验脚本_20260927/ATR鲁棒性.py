# -*- coding: utf-8 -*-
"""
1.0×ATR 止损的鲁棒性检验（只读，不跑回测）
========================================================================
背景（2026-09-19 下午）：
  三档对照发现 2.0 → +491 元、1.5 → +68 元、1.0 → +1,261 元（不单调）。
  用户要求对 1.0×ATR 做鲁棒测试，先定方案再跑。

四种「假」及对应检验：
  L1 尖峰假  —— 1.0 恰好卡在好位置，0.8 / 1.2 就掉回去
                 → 五点曲线形状（需先跑 ATR对照.py 补 0.8 / 1.2）
  L2 时段假  —— 优势全靠某一个月
                 → 逐月剔除（leave-one-month-out）+ 前后两半
  L3 单笔假  —— 优势全靠少数几笔
                 → 逐个剔除最大贡献笔（jackknife）
  L4 执行假  —— 优势来自回测没建模的东西（止损跳空穿越 / 追价）
                 → breakeven 核算：多出的那几次止损要多差几跳才打平

⭐ 事前判据（与 ATR对照.py 里写死的完全一致，跑完不改）：
   L1 平台 = 0.8 / 1.0 / 1.2 三点同号，且相邻落差最大值 ≤ 770 元（= 1.0 相对 2.0 的优势）
   L2 稳定 = 6 次逐月剔除后，1.0 相对 2.0 的差值符号不翻转
   L3 稳定 = 剔除前 3 笔最大贡献后，1.0 仍不劣于 2.0
   L4 安全 = 每次止损可容忍的额外滑点 ≥ 3 跳（现实量级 1~2 跳）

只读：不改任何回测产出，不碰账本，不动主策略。
用法：
    python 工具/ATR鲁棒性.py
产出：
    工具/_输出/_ATR鲁棒性_0919.txt（完整结论）+ 控制台
"""
import collections
import io
import json
import os
import re
import statistics as st
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE) if os.path.basename(_HERE) == '工具' else _HERE
if not os.path.isdir(os.path.join(ROOT, 'lib')):
    _r = _HERE
    while _r != os.path.dirname(_r) and not os.path.isdir(os.path.join(_r, 'lib')):
        _r = os.path.dirname(_r)
    ROOT = _r

D_REPORT = os.path.join(ROOT, '回测结果', '01_双均线', 'PVC')
D_OUT = os.path.join(ROOT, '工具', '_输出')
MULT = 5.0          # PVC 每跳 5 元/手
BASELINE = 'atr20'  # 现行 2.0×ATR

TAGS = [('atr08', 0.8), ('atr10', 1.0), ('atr12', 1.2), ('atr15', 1.5), ('atr20', 2.0)]

PAT = re.compile(r'^\s*(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})\s+(\S+)\s+'
                 r'(OPEN|CLOSE|CLOSETODAY)\s+(\d+)手\s+([\d.]+)')

_OUT = []


def P(s=''):
    print(s)
    _OUT.append(s)


def _月(d, t):
    """按真实时间（报告时间 +8h）取月份，避免月初/月末错归组。"""
    from datetime import datetime, timedelta
    x = datetime.strptime(d + ' ' + t, '%Y-%m-%d %H:%M:%S') + timedelta(hours=8)
    return x.strftime('%Y-%m')


def 读笔(tag):
    """解析报告成交明细 → 配对成笔（开仓时间 / 平仓时间 / 平仓月 / 点数）。"""
    p = os.path.join(D_REPORT, 'PVC_双均线_30min_%s.txt' % tag)
    if not os.path.isfile(p):
        return None
    rows = [m.groups() for m in (PAT.match(l)
            for l in io.open(p, encoding='utf-8').read().splitlines()) if m]
    out = []
    hold = None
    for d, t, bs, act, v, px in rows:
        px = float(px)
        if act == 'OPEN':
            hold = (d + ' ' + t, 1 if bs == 'BUY' else -1, px)
        else:
            if hold is None:
                continue
            ot, dr, ep = hold
            out.append({'开': ot, '平': d + ' ' + t, '月': _月(d, t),
                        '方向': dr, '点': (px - ep) if dr > 0 else (ep - px)})
            hold = None
    return out


def 读净额(tag):
    p = os.path.join(D_OUT, '_ATR对照_%s.json' % tag)
    if not os.path.isfile(p):
        return None
    try:
        d = json.load(io.open(p, encoding='utf-8'))
    except Exception:
        return None
    return d


def 止损次数(tag):
    """返回 (打印次数, 真实次数) —— 真实 = 04-16 之后（此前 1 手开不进去，是幽灵止损）。"""
    p = os.path.join(D_OUT, '_ATR_log_%s.txt' % tag)
    if not os.path.isfile(p):
        return None, None
    L = io.open(p, encoding='utf-8', errors='replace').read().splitlines()
    tot = real = 0
    for i, l in enumerate(L):
        if '[止损] 触及' in l:
            tot += 1
            for j in range(i, min(i + 6, len(L))):
                m = re.match(r'^时间 (\d{4}-\d{2}-\d{2}) ', L[j].strip())
                if m:
                    if m.group(1) >= '2026-04-16':
                        real += 1
                    break
    return tot, real


# ============================================================================
def L1_曲线():
    P('=' * 92)
    P('  L1 · 尖峰假 —— 五点曲线形状（判据：紧侧三点同号 且 相邻落差 ≤ 770 元）')
    P('=' * 92)
    点 = []
    for tag, m in TAGS:
        d = 读净额(tag)
        if d and d.get('净额元') is not None:
            点.append((m, float(d['净额元']), tag))
    点.sort(key=lambda x: x[0])
    if len(点) < 3:
        P('  [!] 数据不足（只有 %d 个点），先跑 工具/ATR对照.py' % len(点))
        return None
    P('   %-8s %-7s %10s %12s %10s %8s' % ('倍数', '档位', '净额(元)', '收益率', '毛价差', '成本'))
    for m, v, tag in 点:
        d = 读净额(tag)
        P('   %-8s %-7s %+10.0f %11.2f%% %10.0f %8.0f'
          % ('%.1f×' % m, tag, v, v / 200000 * 100,
             float(d.get('毛价差元') or 0), float(d.get('总成本元') or 0)))
    P()
    P('   曲线形状（按倍数升序）：')
    for m, v, tag in 点:
        bar = '#' * max(1, int(abs(v) / 50))
        P('     %4.1f×  %+9.0f 元  %s%s' % (m, v, bar, '' if v >= 0 else '   ← 亏'))
    P()
    P('   相邻落差：')
    for i in range(len(点) - 1):
        P('     %.1f× → %.1f× :  %+9.0f 元' % (点[i][0], 点[i + 1][0], 点[i + 1][1] - 点[i][1]))

    净额表 = {m: v for m, v, _ in 点}
    紧侧 = [v for m, v, _ in 点 if 0.75 <= m <= 1.25]
    落差 = [abs(点[i + 1][1] - 点[i][1]) for i in range(len(点) - 1)]
    最大落差 = max(落差) if 落差 else 0.0
    阈值 = abs(净额表.get(1.0, 0.0) - 净额表.get(2.0, 0.0))
    同号 = len(紧侧) >= 3 and (all(v > 0 for v in 紧侧) or all(v < 0 for v in 紧侧))
    P()
    P('   【判定】紧侧三点(0.8/1.0/1.2) = %s ；同号 = %s'
      % (' / '.join('%+.0f' % v for v in 紧侧), '是' if 同号 else '否'))
    P('           相邻最大落差 = %.0f 元 ；阈值（1.0 vs 2.0 的优势）= %.0f 元' % (最大落差, 阈值))
    if 同号 and 最大落差 <= 阈值:
        结论 = '平台（参数不敏感）—— 1.0 的效应有可能是真的 ✓'
    elif not 同号:
        结论 = '尖峰（噪声）—— 紧侧三点符号翻转 ✗'
    else:
        结论 = '尖峰（噪声）—— 落差 %.0f 元 > 阈值 %.0f 元，起伏比效应本身还大 ✗' % (最大落差, 阈值)
    P('           → %s' % 结论)
    P()
    return 结论


def L2_时段(笔10, 笔20):
    P('=' * 92)
    P('  L2 · 时段假 —— 逐月剔除（判据：6 次剔除后差值符号不翻转）')
    P('=' * 92)
    if not 笔10 or not 笔20:
        P('  [!] 缺 atr10 / atr20 报告')
        return None
    m20 = {x['开']: x for x in 笔20}
    配对 = [(m20[k], v) for k, v in ((x['开'], x) for x in 笔10) if k in m20]
    P('   逐笔配对：atr10 %d 笔 / atr20 %d 笔 → 配对成功 %d 笔'
      % (len(笔10), len(笔20), len(配对)))
    P('   （配对按开仓时间；未配上的笔单列，不塞进月度比较）')
    仅10 = [x for x in 笔10 if x['开'] not in m20]
    仅20 = [x for x in 笔20 if x['开'] not in {y['开'] for y in 笔10}]
    if 仅10 or 仅20:
        P('   ⚠️ atr10 独有 %d 笔 / atr20 独有 %d 笔'
          % (len(仅10), len(仅20)))
        for x in 仅10:
            P('       仅 atr10：%s → %s  %+.0f 点' % (x['开'][5:], x['平'][5:], x['点']))
        for x in 仅20:
            P('       仅 atr20：%s → %s  %+.0f 点' % (x['开'][5:], x['平'][5:], x['点']))
    P()
    P('   %-9s %8s %8s %9s %8s' % ('平仓月', 'atr10点', 'atr20点', '差(10-20)', '笔数'))
    月度 = collections.OrderedDict()
    for x20, x10 in 配对:
        月度.setdefault(x10['月'], []).append((x10['点'], x20['点']))
    for k, v in sorted(月度.items()):
        a = sum(u[0] for u in v)
        b = sum(u[1] for u in v)
        P('   %-9s %+8.0f %+8.0f %+9.0f %8d' % (k, a, b, a - b, len(v)))
    总差 = sum(u[0] - u[1] for v in 月度.values() for u in v)
    P('   %-9s %+8.0f %+8.0f %+9.0f %8d'
      % ('合计', sum(u[0] for v in 月度.values() for u in v),
         sum(u[1] for v in 月度.values() for u in v), 总差,
         sum(len(v) for v in 月度.values())))
    P()
    P('   逐月剔除（leave-one-month-out）：')
    其余 = []
    for k in sorted(月度):
        剩 = 总差 - sum(u[0] - u[1] for u in 月度[k])
        其余.append((k, 剩))
        符号 = '仍为正 ✓' if 剩 > 0 else '**翻转为负 ✗**'
        P('     剔除 %s → 剩余差 %+7.0f 点（%+7.0f 元）  %s' % (k, 剩, 剩 * MULT, 符号))
    P()
    P('   前后两半：')
    月序 = sorted(月度)
    left = [k for k in 月序 if k <= '2026-06']
    right = [k for k in 月序 if k > '2026-06']
    for name, ks in (('04-16~06 月', left), ('07~09 月', right)):
        剩 = sum(u[0] - u[1] for k in ks for u in 月度[k])
        P('     %-12s 差 %+7.0f 点（%+7.0f 元）  %s'
          % (name, 剩, 剩 * MULT, '正 ✓' if 剩 > 0 else '负 ✗'))
    P()
    通过 = all(x[1] > 0 for x in 其余)
    P('   【判定】6 次剔除，符号翻转次数 = %d → %s'
      % (sum(1 for x in 其余 if x[1] <= 0),
         '稳定 ✓' if 通过 else '不稳定 ✗'))
    P()
    return 通过


def L3_单笔(笔10, 笔20):
    P('=' * 92)
    P('  L3 · 单笔假 —— 逐个剔除最大贡献笔（判据：剔除前 3 笔后 1.0 仍不劣于 2.0）')
    P('=' * 92)
    m20 = {x['开']: x for x in 笔20}
    配对 = [(m20[k], v) for k, v in ((x['开'], x) for x in 笔10) if k in m20]
    差 = sorted([(x10['点'] - x20['点'], x10, x20) for x20, x10 in 配对],
                key=lambda t: t[0], reverse=True)
    总10 = sum(x10['点'] for _, x10, _ in 差)
    总20 = sum(x20['点'] for _, _, x20 in 差)
    P('   汇总：atr10 %+.0f 点 | atr20 %+.0f 点 | 差 %+.0f 点（%+.0f 元）'
      % (总10, 总20, 总10 - 总20, (总10 - 总20) * MULT))
    P()
    P('   (a) 按「两档共同的单笔盈利」降序，逐个剔除：')
    P('       %-4s %10s %10s %10s' % ('剔除', 'atr10剩', 'atr20剩', '差'))
    byprofit = sorted(配对, key=lambda p: (p[1]['点'] + p[0]['点']) / 2, reverse=True)
    for k in range(0, 4):
        drop = byprofit[:k]
        a = 总10 - sum(x10['点'] for _, x10 in drop)
        b = 总20 - sum(x20['点'] for x20, _ in drop)
        P('       %-4s %+10.0f %+10.0f %+10.0f  %s'
          % ('前%d笔' % k, a, b, a - b, '10 优 ✓' if a > b else '10 劣 ✗'))
        if k and k <= len(drop):
            P('            (剔掉的是 %s 那笔)' % drop[k - 1][1]['开'][5:])
    P()
    P('   (b) 按「atr10 相对优势」降序，逐个剔除（测优势集中度）：')
    P('       %-4s %10s %10s %10s' % ('剔除', 'atr10剩', 'atr20剩', '差'))
    for k in range(0, 4):
        drop = 差[:k]
        a = 总10 - sum(x10['点'] for _, x10, _ in drop)
        b = 总20 - sum(x20['点'] for _, _, x20 in drop)
        P('       %-4s %+10.0f %+10.0f %+10.0f  %s'
          % ('前%d笔' % k, a, b, a - b, '10 优 ✓' if a > b else '10 劣 ✗'))
        if k:
            P('            (剔掉的是 %s 那笔，差值 %+.0f 点)' % (drop[k - 1][1]['开'][5:], drop[k - 1][0]))
    P()
    # 判据
    drop3 = 差[:3]
    a3 = 总10 - sum(x10['点'] for _, x10, _ in drop3)
    b3 = 总20 - sum(x20['点'] for _, _, x20 in drop3)
    通过 = a3 > b3
    P('   【判定】剔除优势最大的前 3 笔后：atr10 %+.0f 点 vs atr20 %+.0f 点 → %s'
      % (a3, b3, '仍不劣于 ✓' if 通过 else '已被反超 ✗'))
    P()
    return 通过


def L4_成本():
    P('=' * 92)
    P('  L4 · 执行假 —— breakeven 核算（判据：每次止损可容忍额外滑点 ≥ 3 跳）')
    P('=' * 92)
    d10, d20 = 读净额('atr10'), 读净额(BASELINE)
    t10, r10 = 止损次数('atr10')
    t20, r20 = 止损次数(BASELINE)
    if not (d10 and d20) or r10 is None or r20 is None:
        P('  [!] 缺 atr10 / atr20 的 json 或日志')
        return None
    优 = float(d10['净额元']) - float(d20['净额元'])
    P('   优势（atr10 − atr20 净额）= %+.0f 元' % 优)
    P('   止损次数（打印 / 真实）：atr10 %s / %s ｜ atr20 %s / %s' % (t10, r10, t20, r20))
    多 = r10 - r20
    P('   多出的真实止损 = %+d 次' % 多)
    if 多 <= 0:
        P('   【判定】止损次数没有增加 → 成本敏感性不构成威胁 ✓')
        P()
        return True
    可容忍 = 优 / (多 * MULT)
    P()
    P('   若「止损平仓」每笔比回测假设多差 k 跳（每跳 %.0f 元），atr10 多付 %.0f×k 元' % (MULT, 多 * MULT))
    P('   打平条件：%+.0f − %.0f×k = 0  →  k = %.1f 跳' % (优, 多 * MULT, 可容忍))
    P('   反向自检：一般平仓全部恶化 1 跳时 —— 两档同为 −%.0f 元，相对差不变（不影响结论）'
      % (30 * MULT))
    通过 = 可容忍 >= 3
    P()
    P('   【判定】可容忍 %.1f 跳 vs 现实量级 1~2 跳 → %s'
      % (可容忍, '执行成本不是威胁 ✓' if 通过 else '执行成本可能吃掉优势 ✗'))
    P()
    return 通过


def 主():
    P('=' * 92)
    P('  1.0×ATR 止损 —— 鲁棒性检验（只读分析，PVC 30 分钟 · 2026-03-13 ~ 09-13）')
    P('  2026-09-19 · 事前判据写死在 ATR对照.py / 本脚本里，跑完不改')
    P('=' * 92)
    P()
    笔10, 笔20 = 读笔('atr10'), 读笔(BASELINE)
    c1 = L1_曲线()
    c2 = L2_时段(笔10, 笔20)
    c3 = L3_单笔(笔10, 笔20)
    c4 = L4_成本()
    P('=' * 92)
    P('  总判定')
    P('=' * 92)
    项 = [('L1 尖峰假（五点曲线形状）', c1), ('L2 时段假（逐月剔除）', c2),
          ('L3 单笔假（jackknife）', c3), ('L4 执行假（成本 breakeven）', c4)]
    for name, c in 项:
        if c is None:
            P('   %-26s 未完成（数据缺）' % name)
        elif isinstance(c, bool):
            P('   %-26s %s' % (name, '通过 ✓' if c else '不通过 ✗'))
        else:
            P('   %-26s %s' % (name, c if ('平台' in c or '尖峰' in c) else c))
    P()
    P('   ⚠️ 无论结果如何，本区间（样本内、已被 v1~v5 / 闸门 / B1 用过）只能作诊断。')
    P('   ⚠️ 任何一档都不被采纳 —— 本检验只输出「平台 / 尖峰」的二元判断。')
    P('   ⚠️ 唯一真正的检验是样本外 2025-03-13 ~ 09-13（只能跑一次，跑完即污染）。')
    P()
    p = os.path.join(D_OUT, '_ATR鲁棒性_0919.txt')
    io.open(p, 'w', encoding='utf-8', newline='\n').write('\n'.join(_OUT) + '\n')
    print('[落盘] %s' % p)
    return 0


if __name__ == '__main__':
    sys.exit(主())
