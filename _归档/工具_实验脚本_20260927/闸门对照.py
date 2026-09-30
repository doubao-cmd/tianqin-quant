# -*- coding: utf-8 -*-
"""
MA60 闸门开关对照 —— 只关掉 MA60 趋势过滤，其余参数与 v5 完全一致。
========================================================================
动机（2026-09-19，用户要求）：
  v5（30 分钟 + MA5/20/60 闸门 + 2×ATR）是首个正收益版本（+0.25%），
  但成交只有 30 笔、94% 收益来自 6 月单月。用户要问的是：
  **MA60 闸门到底是在帮忙还是在拖累？** 关掉它会怎样？

性质：**单变量逻辑对照**（规则 4：每次只改动一处逻辑）。
  · 唯一变量 = 是否启用 MA60 闸门
  · 其余全不动：MA5/MA20、2×ATR 初始止损、保本关、1 手、1 跳滑点、同区间
  · 不调用 汇总报告.更新()  → 账本 JSON 零污染（v1~v5 不受影响）
  · sample_tag = gateon / nogate → 报告另存，**不覆盖** _in / _out / _diag / _slipN

闸门的语义（engine.trend_reverse_signal 原文）：
  金叉 且 MA20 >  MA60 -> 目标 +1（持空则反手做多）
  金叉 且 MA20 <= MA60 -> 目标  0（只平空，空仓等待，不反手）
  死叉 且 MA20 <  MA60 -> 目标 -1（持多则反手做空）
  死叉 且 MA20 >= MA60 -> 目标  0（只平多，空仓等待，不反手）
  → 关掉闸门 = 金叉无条件做多、死叉无条件做空（纯反手，不再有「空仓等待」状态）

用法：
    python 工具/闸门对照.py [周期分钟] [品种KEY]
例：
    python 工具/闸门对照.py 30 V

产出：工具/_输出/_闸门对照_<tag>.json + 控制台对照表。
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
ATR_STOP_MULT = 2.0       # 初始 ATR 止损（阶段A）
BREAKEVEN_ATR = 0.0       # 保本关闭
ATR_PERIOD = 14
SLIP_TICKS = 1            # 1 跳滑点（与 v5 同）
INIT_BALANCE = 200000

IS_START = date(2026, 3, 13)   # 练习（样本内）区间，与 v5 同
IS_END = date(2026, 9, 13)
WARMUP_DAYS = 7                # 预热：评估区间前 1 周不交易


def 无闸门信号(cur_dir, now_f, now_s, pre_f, pre_s, now_t=None, pre_t=None):
    """engine.trend_reverse_signal 的「无 MA60 闸门」版本 —— 逐行对应，只删掉闸门条件。

    与原文的差异只有一处：把 `1 if now_s > now_t else 0` 换成无条件 `1`
    （死叉对称）。其余（交叉判定、cur_dir 重复动作保护）逐字保持。

    签名保留 now_t/pre_t 可选参数，是为了在 ma_filter=0 时引擎只传 5 个位置
    参数也不会报错（engine 在 ma_filter=0 时 extra=()）。

    规则（cur_dir = 当前持仓方向：+1 多 / -1 空 / 0 空仓）：
      金叉 -> 目标 +1（持空反手做多；已持多不动）
      死叉 -> 目标 -1（持多反手做空；已持空不动）
      无交叉 -> 维持当前方向
    """
    crossed_up = pre_f <= pre_s and now_f > now_s      # 金叉
    crossed_dn = pre_f >= pre_s and now_f < now_s      # 死叉
    if crossed_up:
        if cur_dir > 0:
            return cur_dir                             # 已持多，不重复动作
        return 1                                       # ← 原文这里是 `1 if now_s > now_t else 0`
    if crossed_dn:
        if cur_dir < 0:
            return cur_dir                             # 已持空，不重复动作
        return -1                                      # ← 原文这里是 `-1 if now_s < now_t else 0`
    return cur_dir


def _pick(sd, *names, default=None):
    """在不同版本的统计字典里按候选键名取值。"""
    for n in names:
        if n in sd:
            return sd[n]
    return default


def 跑一档(label, signal_fn, ma_filter, cycle_sec, sym):
    """跑一档，返回结果 dict。"""
    print()
    print('=' * 100)
    print('  档位 %s | %s' % (label,
          ('MA%d 闸门【开】' % N3) if ma_filter else 'MA%d 闸门【关】'))
    print('  区间 %s ~ %s（%s 起仅预热不交易）| 周期 %d 分钟 | 品种 %s'
          % (IS_START, IS_END, IS_START - timedelta(days=WARMUP_DAYS),
             cycle_sec // 60, sym))
    print('  MA%d/MA%d | ATR 止损 %.1f×ATR(%d) | 保本关 | %d 手 | 滑点 %d 跳'
          % (N1, N2, ATR_STOP_MULT, ATR_PERIOD, VOLUME, SLIP_TICKS))
    print('=' * 100)

    stat = engine.run_backtest(
        sym, signal_fn, N1, N2, VOLUME,
        IS_START - timedelta(days=WARMUP_DAYS), IS_END, INIT_BALANCE, cycle_sec,
        strategy_name='双均线',
        open_html=False,
        eval_start=IS_START,
        sample_tag=label,
        pass_dir=True, ma_filter=ma_filter,
        slip_ticks=SLIP_TICKS,
        atr_stop_mult=ATR_STOP_MULT, breakeven_atr=BREAKEVEN_ATR, atr_period=ATR_PERIOD,
    )

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
              % (毛价差 / open_times, 毛价差 / open_times / 5.0))

    out = {
        '档位': label,
        '闸门': bool(ma_filter),
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
    _op = os.path.join(_od, '_闸门对照_%s.json' % label)
    with io.open(_op, 'w', encoding='utf-8') as f:
        f.write(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print('  [落盘] %s' % _op)
    return out


def 主():
    cycle_sec = int(sys.argv[1]) * 60 if len(sys.argv) > 1 else 30 * 60
    sym = sys.argv[2] if len(sys.argv) > 2 else SYMBOL_KEY

    print('#' * 100)
    print('#  MA60 闸门开关对照（单变量：只动闸门）')
    print('#  前置：闸门【开】这一档必须复现账本 v5 的 +0.25% / 491 元，否则口径有问题')
    print('#' * 100)

    if cycle_sec // 60 == 30 and sym == 'V':
        参照 = 'v5 = +0.25%（491 元），账本 工具/_账本/PVC_双均线.json'
    else:
        参照 = '（本档无账本参照，仅两档互相对照）'
    print('  参照：%s' % 参照)

    甲 = 跑一档('gateon', engine.trend_reverse_signal, N3, cycle_sec, sym)
    乙 = 跑一档('nogate', 无闸门信号, 0, cycle_sec, sym)

    print()
    print('#' * 100)
    print('#  对照表')
    print('#' * 100)
    print('  %-14s %12s %12s %12s %10s %8s %9s %8s' %
          ('档位', '结束权益', '净额(元)', '毛价差(元)', '成本(元)', '开仓', '胜率', '盈亏比'))
    print('  ' + '-' * 96)
    for r in (甲, 乙):
        sd = r['统计字典'] or {}
        wr = _pick(sd, 'winning_rate', '胜率')
        plr = _pick(sd, 'profit_loss_ratio', '盈亏比')
        print('  %-14s %12s %12s %12s %12s %8s %9s %8s' % (
            ('闸门开' if r['闸门'] else '闸门关'),
            ('%.0f' % r['结束权益']) if r['结束权益'] is not None else '—',
            ('%.0f' % r['净额元']) if r['净额元'] is not None else '—',
            ('%.0f' % r['毛价差元']) if r['毛价差元'] is not None else '—',
            ('%.0f' % r['总成本元']) if r['总成本元'] is not None else '—',
            r['开仓次数'] if r['开仓次数'] is not None else '—',
            ('%.2f%%' % (wr * 100)) if isinstance(wr, (int, float)) else '—',
            ('%.2f' % plr) if isinstance(plr, (int, float)) else '—'))

    print()
    if 甲['净额元'] is not None and 乙['净额元'] is not None:
        d = 乙['净额元'] - 甲['净额元']
        print('  关闭闸门 → 净额变化 %+.0f 元（%+.2f 个百分点）' %
              (d, d / INIT_BALANCE * 100))
        if 乙['开仓次数'] and 甲['开仓次数']:
            print('  开仓次数变化：%s → %s（%+d 笔，%+.0f%%）' % (
                甲['开仓次数'], 乙['开仓次数'],
                乙['开仓次数'] - 甲['开仓次数'],
                (乙['开仓次数'] / 甲['开仓次数'] - 1) * 100))
        if 乙['毛价差元'] is not None and 甲['毛价差元'] is not None:
            print('  毛价差变化 %+.0f 元；成本变化 %+.0f 元'
                  % (乙['毛价差元'] - 甲['毛价差元'],
                     (乙['总成本元'] or 0) - (甲['总成本元'] or 0)))
            print()
            print('  ⚠️ 提醒：本区间只有 30 笔量级，按既有结论「半年 30 笔以下差异基本是噪声」。')
            print('     即便一档明显更好，也**不构成采纳依据**，只能当作「闸门是否有害」的初步信号。')
    print()
    return 0


if __name__ == '__main__':
    sys.exit(主())
