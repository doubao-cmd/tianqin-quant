# -*- coding: utf-8 -*-
"""
ATR 止损倍数鲁棒性扫描 —— 以 2.0×ATR（v5 现行）为基线，向紧侧取 1.5 / 1.2 / 1.0 / 0.8 四点，
用于判断「1.0×ATR 的优势」是参数邻域内的平台，还是孤立尖峰。其余与 v5 完全一致。
========================================================================
动机（2026-09-19，用户要求）：
  v5 的止损是「固定线」：开仓那根收盘价锁定基准 + 锁定当时 ATR(14)，
  止损线 = 基准 ∓ 2×ATR，持仓期间一动不动（保本机制 PVC 是关的）。
  → 单笔最大亏损 ≈ 2×ATR（中位约 53 点 / 263 元）。
  用户问：把倍数收紧到 1.5×ATR 会怎样？（2026-09-19 追加：再收到 1.0× 看有什么区别）
  2026-09-19 下午追加：**对 1.0× 做鲁棒性测试** —— 补 0.8 / 1.2 两点，判曲线是平台还是尖峰。

性质：**单变量参数对照**（规则 4：每次只改动一处逻辑）。
  · 唯一变量 = 初始止损倍数（2.0 / 1.5 / 1.2 / 1.0 / 0.8）
  · 其余全不动：MA5/MA20/MA60 闸门、保本关、ATR 周期 14、1 手、
    1 跳滑点、30 分钟 K 线、同一区间、同一预热
  · 每档只跑一次、只改这一个数 → 三档互为单变量对照，不是「同时改多处」
  · 不调用 汇总报告.更新()  → 账本 JSON 零污染（v1~v5 不受影响）
  · sample_tag = atr20 / atr15 / atr12 / atr10 / atr08 → 报告另存，不覆盖其余档

⚠️ 规则 2 边界（必须写在明面上）—— 本轮性质已从「取值对照」升级为「鲁棒性检验」：
   2026-09-19 用户要求对 1.0×ATR 做鲁棒测试。区别在于：
     · 择优 = 找最高点并采纳  → **禁止**
     · 稳定性检验 = 看曲线形状，**任何一档都不采纳**，只输出「平台 / 尖峰」二元判断 → 允许
   所以脚本只在 1.0 两侧各取一点（0.8 / 1.2），**不向外扩到 0.5 / 3.0，不细化、不择优**。
   本区间只有 30 笔量级，按既有结论「半年 30 笔以下差异基本是噪声」，
   **即便某一档明显更好，也不构成采纳依据**。
   若真要定倍数，正确做法是换一段没看过的独立区间再验一次。

⭐ 事前判据（先定后跑，避免事后解释）—— 把 0.8 / 1.0 / 1.2 / 1.5 / 2.0 五点净额连成曲线：
   判为「平台（参数不敏感）」：0.8 / 1.0 / 1.2 三点**同号**，且相邻落差最大值 ≤ 770 元
                              （770 = 上一轮读数里 1.0 相对 2.0 的优势幅度）
   判为「尖峰（噪声）」：出现符号翻转，或任一相邻落差 > 770 元
   ⚠️ 判据在跑之前写死在函数 `判读曲线()` 里，跑完不改。

用法：
    python 工具/ATR对照.py [周期分钟] [品种KEY]
例：
    python 工具/ATR对照.py 30 V

产出：工具/_输出/_ATR对照_<tag>.json + 控制台对照表。
"""
import io
import json
import os
import sys
from datetime import date, timedelta

_ROOT = os.path.dirname(os.path.abspath(__file__))
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, 'lib')):
    _ROOT = os.path.dirname(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lib import engine

# ---- 与 v5 完全一致的策略参数（写死，本脚本不做任何扫描）----
SYMBOL_KEY = 'V'          # PVC
N1, N2, N3 = 5, 20, 60    # MA5 / MA20 / MA60 闸门
VOLUME = 1
BREAKEVEN_ATR = 0.0       # 保本关闭（与 v5 同）
ATR_PERIOD = 14
SLIP_TICKS = 1            # 1 跳滑点（与 v5 同）
INIT_BALANCE = 200000

# 五个对照点：基线 2.0（现行），向紧侧依次 1.5 / 1.2 / 1.0 / 0.8
# ⚠️ 只在这四个点上取，不向外扩（0.5 / 3.0）、不细化 —— 见文件头「规则 2 边界」
ATR_POINTS = [('atr20', 2.0), ('atr15', 1.5), ('atr12', 1.2),
              ('atr10', 1.0), ('atr08', 0.8)]

IS_START = date(2026, 3, 13)   # 练习（样本内）区间，与 v5 同
IS_END = date(2026, 9, 13)
WARMUP_DAYS = 7                # 预热：评估区间前 1 周不交易

MULT = 5.0                     # PVC 每跳 5 元/手


class Tee(object):
    """把 stdout 同时写到控制台与日志文件（engine 的 [止损] 记录只在 stdout）。"""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for s in self.streams:
            try:
                s.write(data)
            except Exception:
                pass

    def flush(self):
        for s in self.streams:
            try:
                s.flush()
            except Exception:
                pass

    def isatty(self):
        return False


def _pick(sd, *names, default=None):
    """在不同版本的统计字典里按候选键名取值。"""
    for n in names:
        if n in sd:
            return sd[n]
    return default


# ⭐ 事前判据 —— 跑之前写死，跑完不改（避免「看结果再找解释」）
判据说明 = '紧侧三点(0.8/1.0/1.2)同号，且相邻落差最大值 ≤ 770 元 → 平台；否则尖峰'


def 判读曲线(结果):
    """按事前判据判读五点曲线。返回 (结论, 明细列表, 最大落差, 阈值)。"""
    pts = sorted([(r['ATR倍数'], r['净额元'], r['档位'])
                  for r in 结果 if r['净额元'] is not None], key=lambda x: x[0])
    if len(pts) < 3:
        return '数据不足，无法判读', pts, None, None
    紧侧 = [v for m, v, _ in pts if 0.75 <= m <= 1.25]
    落差 = [abs(pts[i + 1][1] - pts[i][1]) for i in range(len(pts) - 1)]
    最大落差 = max(落差) if 落差 else 0.0
    净额表 = {m: v for m, v, _ in pts}
    阈值 = abs(净额表.get(1.0, 0.0) - 净额表.get(2.0, 0.0))     # 770 元的通用形式
    同号 = (len(紧侧) >= 3) and (all(v > 0 for v in 紧侧) or all(v < 0 for v in 紧侧))
    if 同号 and 最大落差 <= 阈值:
        return '平台（参数不敏感）—— 1.0 的效应有可能是真的', pts, 最大落差, 阈值
    if not 同号:
        return '尖峰（噪声）—— 紧侧三点符号翻转，1.0 的优势是偶然读数', pts, 最大落差, 阈值
    return '尖峰（噪声）—— 相邻落差 %.0f 元 > 阈值 %.0f 元，曲线起伏比效应本身还大' % (最大落差, 阈值), pts, 最大落差, 阈值


def 跑一档(label, atr_mult, cycle_sec, sym):
    """跑一档，返回结果 dict。"""
    print()
    print('=' * 100)
    print('  档位 %s | 初始止损 %.1f×ATR(%d)' % (label, atr_mult, ATR_PERIOD))
    print('  区间 %s ~ %s（%s 起仅预热不交易）| 周期 %d 分钟 | 品种 %s'
          % (IS_START, IS_END, IS_START - timedelta(days=WARMUP_DAYS),
             cycle_sec // 60, sym))
    print('  MA%d/MA%d + MA%d 闸门 | 保本关 | %d 手 | 滑点 %d 跳 | 执行 对价'
          % (N1, N2, N3, VOLUME, SLIP_TICKS))
    print('=' * 100)

    _od = os.path.join(_ROOT, '工具', '_输出')
    if not os.path.isdir(_od):
        os.makedirs(_od)
    _lp = os.path.join(_od, '_ATR_log_%s.txt' % label)

    with io.open(_lp, 'w', encoding='utf-8') as _lf:
        _old = sys.stdout
        sys.stdout = Tee(_old, _lf)
        try:
            stat = engine.run_backtest(
                sym, engine.trend_reverse_signal, N1, N2, VOLUME,
                IS_START - timedelta(days=WARMUP_DAYS), IS_END, INIT_BALANCE, cycle_sec,
                strategy_name='双均线',
                open_html=False,
                eval_start=IS_START,
                sample_tag=label,
                pass_dir=True, ma_filter=N3,
                slip_ticks=SLIP_TICKS,
                atr_stop_mult=atr_mult, breakeven_atr=BREAKEVEN_ATR, atr_period=ATR_PERIOD,
            )
        finally:
            sys.stdout = _old
    print('  [日志] %s' % _lp)

    sd = {}
    try:
        sd = dict(engine._stat_to_dict(stat))
    except Exception as e:
        print('  [!] _stat_to_dict 失败：%s' % e)

    balance = _pick(sd, 'balance', '结束权益', '期末权益')
    commission = _pick(sd, 'commission', '总手续费')
    open_times = _pick(sd, 'open_times', '开仓次数')

    净额 = (float(balance) - INIT_BALANCE) if balance is not None else None
    成本 = float(commission) if commission is not None else None
    毛价差 = (净额 + 成本) if (净额 is not None and 成本 is not None) else None

    print('  ---- 本档小结 ----')
    print('    结束权益 = %s' % balance)
    print('    净额     = %s 元' % ('%.0f' % 净额 if 净额 is not None else '—'))
    print('    总成本   = %s 元' % ('%.0f' % 成本 if 成本 is not None else '—'))
    print('    毛价差   = %s 元  （净额 + 成本）'
          % ('%.0f' % 毛价差 if 毛价差 is not None else '—'))
    if 净额 is not None:
        print('    收益率   = %+.2f%%' % (净额 / INIT_BALANCE * 100))
    if open_times is not None:
        print('    开仓次数 = %s' % open_times)
    if 毛价差 is not None and open_times:
        print('    每笔毛价差 ≈ %.2f 元 = %.2f 点'
              % (毛价差 / open_times, 毛价差 / open_times / MULT))

    out = {
        '档位': label,
        'ATR倍数': atr_mult,
        '周期分钟': cycle_sec // 60,
        '品种': sym,
        '区间': '%s ~ %s' % (IS_START, IS_END),
        '初始资金': INIT_BALANCE,
        '结束权益': balance,
        '净额元': 净额,
        '总成本元': 成本,
        '毛价差元': 毛价差,
        '收益率': (净额 / INIT_BALANCE) if 净额 is not None else None,
        '开仓次数': open_times,
        '统计字典': sd,
    }
    _od = os.path.join(_ROOT, '工具', '_输出')
    if not os.path.isdir(_od):
        os.makedirs(_od)
    _op = os.path.join(_od, '_ATR对照_%s.json' % label)
    with io.open(_op, 'w', encoding='utf-8') as f:
        f.write(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print('  [落盘] %s' % _op)
    return out


def 主():
    cycle_sec = int(sys.argv[1]) * 60 if len(sys.argv) > 1 else 30 * 60
    sym = sys.argv[2] if len(sys.argv) > 2 else SYMBOL_KEY

    print('#' * 100)
    print('#  ATR 止损倍数鲁棒性扫描（基线 2.0 → 紧侧 1.5 / 1.2 / 1.0 / 0.8）')
    print('#  前置自检：atr20 必须复现账本 v5 的 +0.25% / 491 元 / 30 笔')
    print('#            atr15 / atr10 必须与上午那两轮逐字一致（证明扩成五点没引入偏差）')
    print('#' * 100)

    if cycle_sec // 60 == 30 and sym == 'V':
        参照 = 'v5 = +0.25%（491 元 / 30 笔），账本 工具/_账本/PVC_双均线.json'
    else:
        参照 = '（本档无账本参照，仅两档互相对照）'
    print('  参照：%s' % 参照)

    结果 = []
    for label, mult in ATR_POINTS:
        结果.append(跑一档(label, mult, cycle_sec, sym))

    print()
    print('#' * 100)
    print('#  对照表')
    print('#' * 100)
    print('  %-8s %10s %12s %12s %12s %8s %8s %9s %8s' %
          ('档位', 'ATR倍数', '结束权益', '净额(元)', '毛价差(元)', '成本(元)', '开仓', '胜率', '盈亏比'))
    print('  ' + '-' * 100)
    for r in 结果:
        sd = r['统计字典'] or {}
        wr = _pick(sd, 'winning_rate', '胜率')
        plr = _pick(sd, 'profit_loss_ratio', '盈亏比')
        print('  %-8s %10.1f %12s %12s %12s %12s %8s %9s %8s' % (
            r['档位'], r['ATR倍数'],
            ('%.0f' % r['结束权益']) if r['结束权益'] is not None else '—',
            ('%.0f' % r['净额元']) if r['净额元'] is not None else '—',
            ('%.0f' % r['毛价差元']) if r['毛价差元'] is not None else '—',
            ('%.0f' % r['总成本元']) if r['总成本元'] is not None else '—',
            r['开仓次数'] if r['开仓次数'] is not None else '—',
            ('%.2f%%' % (wr * 100)) if isinstance(wr, (int, float)) else '—',
            ('%.2f' % plr) if isinstance(plr, (int, float)) else '—'))

    print()
    print('  ---- 五点曲线（按倍数升序，便于看形状）----')
    排列 = sorted([(r['ATR倍数'], r['净额元'], r['档位'])
                   for r in 结果 if r['净额元'] is not None], key=lambda x: x[0])
    for m, v, tag in 排列:
        bar = '#' * max(1, int(abs(v) / 50))
        print('    %4.1f× %-7s %+9.0f 元   %s%s'
              % (m, tag, v, bar, '' if v >= 0 else '   ← 亏'))
    结论, _, 最大落差, 阈值 = 判读曲线(结果)
    print()
    print('  ⭐ 事前判据：%s' % 判据说明)
    if 最大落差 is not None:
        print('     实测：紧侧三点净额 = %s ；相邻最大落差 = %.0f 元 ；阈值 = %.0f 元'
              % (' / '.join('%+.0f' % v for m, v, _ in 排列 if 0.75 <= m <= 1.25),
                 最大落差, 阈值))
    print('     判定 → %s' % 结论)
    print()
    基线 = 结果[0]
    for r in 结果[1:]:
        if 基线['净额元'] is None or r['净额元'] is None:
            continue
        d = r['净额元'] - 基线['净额元']
        print('  【%s】收紧到 %.1f×ATR → 净额变化 %+.0f 元（%+.2f 个百分点）' %
              (r['档位'], r['ATR倍数'], d, d / INIT_BALANCE * 100))
        if r['开仓次数'] and 基线['开仓次数']:
            print('    开仓次数变化：%s → %s（%+d 笔，%+.0f%%）' % (
                基线['开仓次数'], r['开仓次数'],
                r['开仓次数'] - 基线['开仓次数'],
                (r['开仓次数'] / 基线['开仓次数'] - 1) * 100))
        if r['毛价差元'] is not None and 基线['毛价差元'] is not None:
            print('    毛价差变化 %+.0f 元；成本变化 %+.0f 元'
                  % (r['毛价差元'] - 基线['毛价差元'],
                     (r['总成本元'] or 0) - (基线['总成本元'] or 0)))
        print()
    print('  ⚠️ 提醒（规则 2）：本区间只有 30 笔量级，按既有结论「半年 30 笔以下差异基本是噪声」。')
    print('     即便某一档明显更好，也**不构成采纳依据**，只能当作「止损宽窄是否有害」的初步信号。')
    print('     「固定止损不动」是结论 16（MFE 诊断，p=0.802）有意保留的选择，')
    print('      但结论 16 关闭的是「止盈 / 跟踪」，**不是**「初始止损宽度」——本次属于新的一条线。')
    print()
    return 0


if __name__ == '__main__':
    sys.exit(主())
