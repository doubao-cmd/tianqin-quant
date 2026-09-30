# -*- coding: utf-8 -*-
"""
分段拆解 —— 从回测报告的成交明细里，按 年 / 月 分组统计价差盈亏。
==========================================================
用途：长区间回测之后，判断「优势是全时段稳定，还是只集中在某几段」。
      这是把「样本量不足」变成「看得见结构」的最小工具，不涉及任何调参。

用法：
    python 工具/分段拆解.py <报告.txt> [合约乘数]
例：
    python 工具/分段拆解.py 回测结果/01_双均线/PVC/PVC_双均线_30min_in.txt 5

口径：
    价差盈亏 = (平仓价 - 开仓价) x 乘数 x 手数 x 方向
    —— 不含手续费、不含滑点（报告成交价是市场价，滑点由 TqSim 费率另收）。
    配对方式：(OPEN, 其后第一笔 CLOSE / CLOSETODAY)。末尾未平仓的那笔放行不统计。
"""
import io
import os
import re
import sys
from collections import OrderedDict

_行 = re.compile(
    r'^(\d{4})-(\d{2})-(\d{2}) \d{2}:\d{2}:\d{2}\s+(\S+)\s+'
    r'(OPEN|CLOSE|CLOSETODAY)\s+(\d+)手\s+([\d.]+)',
    re.M,
)


def 解析成交(文本):
    out = []
    for m in _行.finditer(文本):
        y, mo, d, side, act, lots, px = m.groups()
        out.append({
            'ym': '%s-%s' % (y, mo),
            'y': y,
            'side': side,                       # BUY / SELL
            'act': act,                         # OPEN / CLOSE / CLOSETODAY
            'lots': int(lots),
            'px': float(px),
        })
    return out


def 配对(成交):
    """(OPEN, 其后第一笔 CLOSE) 配对，返回 [(平仓月份, 方向, 手数, 价差点数)]"""
    pairs = []
    open_row = None
    for r in 成交:
        if r['act'] == 'OPEN':
            open_row = r
        elif open_row is not None:
            sign = 1 if open_row['side'] == 'BUY' else -1
            pairs.append({
                'ym': r['ym'],
                'y': r['y'],
                'dir': open_row['side'],
                'lots': open_row['lots'],
                'pts': (r['px'] - open_row['px']) * sign,
            })
            open_row = None
    return pairs


def 汇总(pairs, 乘数, 分组键):
    g = OrderedDict()
    for p in pairs:
        k = p[分组键]
        d = g.setdefault(k, {'n': 0, 'pts': 0.0, 'win': 0})
        d['n'] += 1
        d['pts'] += p['pts'] * p['lots']
        if p['pts'] > 0:
            d['win'] += 1
    return g


def 打印(标题, g, 乘数, 总笔, 总点):
    print()
    print('== %s ==' % 标题)
    print('%-10s %6s %12s %12s %10s' % ('分档', '笔数', '价差点数', '点数/笔', '胜率'))
    for k, d in g.items():
        print('%-10s %6d %12.1f %12.2f %9.1f%%' % (
            k, d['n'], d['pts'], d['pts'] / d['n'], 100.0 * d['win'] / d['n']))
    print('%-10s %6d %12.1f %12.2f' % ('合计', 总笔, 总点, 总点 / 总笔 if 总笔 else 0))


def main():
    乘数 = 5.0
    args = [a for a in sys.argv[1:]]
    if not args:
        print(__doc__)
        return 2
    p = args[0]
    if len(args) > 1:
        乘数 = float(args[1])
    if not os.path.isabs(p):
        # 相对路径按项目根目录解析（向上找含 lib 的目录）
        root = os.path.dirname(os.path.abspath(__file__))
        while root != os.path.dirname(root) and not os.path.isdir(os.path.join(root, 'lib')):
            root = os.path.dirname(root)
        p = os.path.join(root, p)
    if not os.path.exists(p):
        print('找不到报告：%s' % p)
        return 1

    文本 = io.open(p, encoding='utf-8', errors='replace').read()
    成交 = 解析成交(文本)
    含方向 = len(re.findall(r'\b(BUY|SELL)\b', 文本))
    print('报告：%s' % os.path.relpath(p, os.path.dirname(os.path.dirname(p))))
    print('解析成交 %d 条；含 BUY/SELL 的行 %d 条 -> %s'
          % (len(成交), 含方向, '自检通过' if len(成交) == 含方向 else '!! 条数不符，请检查'))

    pairs = 配对(成交)
    未平 = len([r for r in 成交 if r['act'] == 'OPEN']) - len(pairs)
    总点 = sum(x['pts'] * x['lots'] for x in pairs)
    print('闭合交易 %d 笔；未平仓放行 %d 笔；价差点数合计 %.1f（每笔 %.2f 点，%.1f 元/笔）'
          % (len(pairs), 未平, 总点, 总点 / len(pairs) if pairs else 0,
             乘数 * 总点 / len(pairs) if pairs else 0))

    打印('按年', 汇总(pairs, 乘数, 'y'), 乘数, len(pairs), 总点)
    打印('按月', 汇总(pairs, 乘数, 'ym'), 乘数, len(pairs), 总点)
    return 0


if __name__ == '__main__':
    sys.exit(main())
