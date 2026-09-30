# -*- coding: utf-8 -*-
"""
ATR 倍数多档逐笔对比（只读，不改任何东西）
==========================================
读 回测结果/01_双均线/PVC/PVC_双均线_30min_atr{20,15,10}.txt，
把成交明细配成「笔」，输出：
  1. 各档笔数 / 合计点数 / 每笔均值 / 标准差 / t / 胜率 / 最大最小
  2. 以 atr20 为基线，逐档按【开仓时间】配对 → 差异归因（笔数不同也能对齐）
  3. 集中度（前 N 笔占合计的比例）+ 月度拆解 + Welch t
用法： python 工具/ATR逐笔对比.py
"""
import collections
import io
import math
import os
import re
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, 'lib')):
    _ROOT = os.path.dirname(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

D = os.path.join(_ROOT, '回测结果', '01_双均线', 'PVC')
PAT = re.compile(r'^\s*(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})\s+(\S+)\s+'
                 r'(OPEN|CLOSE|CLOSETODAY)\s+(\d+)手\s+([\d.]+)')
MULT = 5.0
# 基线放第一个；其余各档都与它对比
FILES = [('atr20', 2.0, 'PVC_双均线_30min_atr20.txt'),
         ('atr15', 1.5, 'PVC_双均线_30min_atr15.txt'),
         ('atr10', 1.0, 'PVC_双均线_30min_atr10.txt')]


def 读成交(fn):
    p = os.path.join(D, fn)
    if not os.path.isfile(p):
        return None
    rows = []
    for l in io.open(p, encoding='utf-8').read().splitlines():
        m = PAT.match(l)
        if m:
            rows.append(m.groups())
    return rows


def 配笔(rows):
    """按出现顺序把 OPEN / CLOSE 配成笔。返回 [(开时间, 平时间, 方向, 点数)]"""
    out = []
    hold = None
    for d, t, bs, act, v, px in rows:
        px = float(px)
        if act == 'OPEN':
            hold = (1 if bs == 'BUY' else -1, px, d + ' ' + t)
        else:
            if hold is None:
                continue
            dr, ep, ot = hold
            pts = (px - ep) if dr > 0 else (ep - px)
            out.append((ot, d + ' ' + t, dr, pts))
            hold = None
    return out


def 统计(v):
    n = len(v)
    if n == 0:
        return None
    m = sum(v) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in v) / (n - 1)) if n > 1 else 0.0
    t = (m / (sd / math.sqrt(n))) if sd > 0 else 0.0
    return dict(n=n, mean=m, sd=sd, t=t,
                win=sum(1 for x in v if x > 0) / n * 100,
                mx=max(v), mn=min(v))


def main():
    data = {}
    for tag, mult, fn in FILES:
        rows = 读成交(fn)
        if rows is None:
            print('[!] 缺少报告：%s（请先跑 工具/ATR对照.py）' % fn)
            return 1
        data[tag] = 配笔(rows)

    print('#' * 96)
    print('#  ATR 止损倍数逐笔对比（PVC 30min，2026-03-13 ~ 09-13）')
    print('#' * 96)
    print()
    print('  %-8s %6s %6s %9s %10s %10s %8s %8s %9s %9s' %
          ('档位', '倍数', '笔数', '合计点', '每笔点', '标准差', 't', '胜率', '最大', '最小'))
    print('  ' + '-' * 92)
    stats = {}
    for tag, mult, _ in FILES:
        v = [x[3] for x in data[tag]]
        s = 统计(v)
        stats[tag] = s
        print('  %-8s %6.1f %6d %9.1f %10.2f %10.2f %8.2f %7.1f%% %9.0f %9.0f' %
              (tag, mult, s['n'], sum(v), s['mean'], s['sd'], s['t'],
               s['win'], s['mx'], s['mn']))
    print()

    基线tag, 基线的倍数, _ = FILES[0]
    基线 = data[基线tag]
    mA = {x[0]: x for x in 基线}

    for tag, mult, _ in FILES[1:]:
        B = data[tag]
        mB = {x[0]: x for x in B}
        keys = sorted(set(mA) | set(mB))
        共有 = [k for k in keys if k in mA and k in mB]
        独有A = [k for k in keys if k in mA and k not in mB]
        独有B = [k for k in keys if k in mB and k not in mA]

        print('  === %s（%s×ATR） vs 基线 %s（%.1f×ATR） ===' %
              (tag, mult, 基线tag, 基线的倍数))
        print('  %-19s %-19s %7s | %-19s %7s | %7s %s' %
              ('开仓时间', '%s 平仓' % 基线tag, '%.1f点' % 基线的倍数,
               '%s 平仓' % tag, '%.1f点' % mult, '差', '归因'))
        print('  ' + '-' * 100)
        同点 = 0
        改写 = []
        合计差 = 0.0
        for k in 共有:
            x, y = mA[k], mB[k]
            d = y[3] - x[3]
            合计差 += d
            if abs(d) < 0.5 and x[1] == y[1]:
                同点 += 1
                continue
            if x[1] != y[1]:
                改写.append((k, x, y, d))
            print('  %-19s %-19s %7.0f | %-19s %7.0f | %+7.0f %s' %
                  (k[5:], x[1][5:], x[3], y[1][5:], y[3], d,
                   '出场点被改写' if x[1] != y[1] else '同点但点数不同'))
        改写增益 = sum(t[3] for t in 改写)
        print('  ' + '-' * 100)
        print('  开仓时间相同 = %d 笔（其中完全相同 %d 笔、出场被改写 %d 笔）' %
              (len(共有), 同点, len(改写)))
        print('  出场改写带来的点数差合计 = %+.0f 点 = %+.0f 元' % (改写增益, 改写增益 * MULT))
        for k in 独有A:
            x = mA[k]
            print('  %-19s %-19s %7.0f | %-19s %7s | %7s  ← %s 独有' %
                  (k[5:], x[1][5:], x[3], '—', '—', '—', 基线tag))
        for k in 独有B:
            y = mB[k]
            print('  %-19s %-19s %7s | %-19s %7.0f | %7s  ← %s 独有' %
                  (k[5:], '—', '—', y[1][5:], y[3], '—', tag))
        独有A点 = sum(mA[k][3] for k in 独有A)
        独有B点 = sum(mB[k][3] for k in 独有B)
        if 独有A or 独有B:
            print('  独有笔点数：%s %+.0f 点（%d 笔） | %s %+.0f 点（%d 笔）'
                  % (基线tag, 独有A点, len(独有A), tag, 独有B点, len(独有B)))
        print('  算术闭合：共有差 %+.0f + 独有差 %+.0f = %+.0f 点 = %+.0f 元'
              % (合计差, 独有B点 - 独有A点, 合计差 + 独有B点 - 独有A点,
                 (合计差 + 独有B点 - 独有A点) * MULT))
        print()

    # ---- 集中度 + 月度 + Welch（对每一档）----
    print('  === 集中度 / 月度 / 显著性 ===')
    for tag, mult, _ in FILES:
        v = sorted([x[3] for x in data[tag]], reverse=True)
        tot = sum(v)
        print('  [%s %.1f×]  合计 %+.0f 点' % (tag, mult, tot))
        print('     前1笔 %+.0f (%+.0f%%)  前2 %+.0f  前3 %+.0f  前5 %+.0f ｜ 去掉最大1笔 %+.0f 点'
              % (v[0], (v[0] / tot * 100 if tot else 0),
                 sum(v[:2]), sum(v[:3]), sum(v[:5]), tot - v[0]))
        m = collections.OrderedDict()
        for x in data[tag]:
            m.setdefault(x[1][:7], []).append(x[3])
        print('     月度（按平仓月）: ' + ' | '.join(
            '%s %+.0f点/%d笔' % (k, sum(u), len(u)) for k, u in sorted(m.items())))
    print()
    for tag, mult, _ in FILES[1:]:
        sa, sb = stats[基线tag], stats[tag]
        d = sb['mean'] - sa['mean']
        se = math.sqrt(sa['sd'] ** 2 / sa['n'] + sb['sd'] ** 2 / sb['n'])
        print('  %s(%.1f×) vs %s(%.1f×) 每笔均值差 = %+.2f 点；Welch t = %+.2f'
              % (tag, mult, 基线tag, 基线的倍数, d, d / se if se else 0))
    print('  （|t| < 2 即不显著 —— 本区间只有 30 笔量级，预期就是这个结果）')
    print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
