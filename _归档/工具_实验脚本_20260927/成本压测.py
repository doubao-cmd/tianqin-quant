# -*- coding: utf-8 -*-
"""
成本压力测试 —— 固定策略参数，只改「滑点跳数」，看 v5 还剩多少安全边际。
========================================================================
动机（2026-09-18，用户四点诊断的第 ④ 条）：
  v5（30 分钟）首次正收益 +0.25%，但把成本口径拆开后发现：
    每次成交 6.4 元 = 手续费约 1.4 + 滑点 5（1 跳 = 1 元/吨 × 5 吨）
    每笔往返 12.8 元 = 2.56 点  →  滑点占成本 78%
  于是必须回答：如果实盘滑点比回测差 1~2 跳，v5 还在不在正区间？

性质：**成本假设的稳健性检验**，不是参数择优（规则 2 禁止遍历参数择优）。
  · 策略参数一个都不动（MA5/MA20 + MA60 闸门 · 2×ATR 初始止损 · 保本关 · 1 手）
  · 唯一变量 = slip_ticks
  · 不调用 汇总报告.更新()  → 账本 JSON 零污染（v1~v5 不受影响）
  · sample_tag="slipN"      → 报告落 <品种>_双均线_<周期>_slipN.html，
                              **不覆盖** _in / _out / _diag

用法：
    python 工具/成本压测.py <滑点跳数> [周期分钟] [品种KEY]
例：
    python 工具/成本压测.py 1 30 V

每档结果落 工具/_输出/_成本压测_slip<N>.json，全部跑完用 工具/成本压测_汇总.py 出对比表。
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
ATR_STOP_MULT = 2.0       # 初始 ATR 止损
BREAKEVEN_ATR = 0.0       # 保本关闭
ATR_PERIOD = 14
INIT_BALANCE = 200000

IS_START = date(2026, 3, 13)   # 练习（样本内）区间，与 v5 同
IS_END = date(2026, 9, 13)


def _pick(sd, *names, default=None):
    """在不同版本的统计字典里按候选键名取值。"""
    for n in names:
        if n in sd:
            return sd[n]
    return default


def 主():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    slip = int(sys.argv[1])
    kline_sec = int(sys.argv[2]) * 60 if len(sys.argv) > 2 else 30 * 60
    sym = sys.argv[3] if len(sys.argv) > 3 else SYMBOL_KEY

    print('[压测] 滑点 %d 跳 | 区间 %s ~ %s | 周期 %d 分钟 | 品种 %s'
          % (slip, IS_START, IS_END, kline_sec // 60, sym))
    print('[压测] 策略参数全不动：MA%d/MA%d + MA%d闸门 | ATR止损 %.1f×ATR(%d) | 保本关 | 1 手'
          % (N1, N2, N3, ATR_STOP_MULT, ATR_PERIOD))
    print('[压测] 不写账本；报告落 _slip%d' % slip)

    stat = engine.run_backtest(
        sym, engine.trend_reverse_signal, N1, N2, VOLUME,
        IS_START - timedelta(days=7), IS_END, INIT_BALANCE, kline_sec,
        strategy_name='双均线',
        open_html=False,
        eval_start=IS_START,
        sample_tag='slip%d' % slip,
        pass_dir=True, ma_filter=N3,
        slip_ticks=slip,
        atr_stop_mult=ATR_STOP_MULT, breakeven_atr=BREAKEVEN_ATR, atr_period=ATR_PERIOD,
    )

    sd = {}
    try:
        sd = dict(engine._stat_to_dict(stat))
    except Exception as e:
        print('  [!] _stat_to_dict 失败：%s' % e)

    print('  ---- 统计字典全部字段 ----')
    for k in sorted(sd.keys(), key=lambda x: str(x)):
        print('    %-26s = %s' % (k, sd[k]))

    balance = _pick(sd, 'balance', '结束权益', '期末权益')
    commission = _pick(sd, 'commission', '总手续费')
    close_profit = _pick(sd, 'close_profit', '平仓盈亏')
    open_times = _pick(sd, 'open_times', '开仓次数')

    净额 = None
    成本 = None
    毛价差 = None
    if balance is not None:
        净额 = float(balance) - INIT_BALANCE
    if commission is not None:
        成本 = float(commission)
    if 净额 is not None and 成本 is not None:
        毛价差 = 净额 + 成本          # 权益恒等式：净额 = 毛价差 - 成本

    print('  ---- 本档小结 ----')
    print('    结束权益 = %s' % balance)
    print('    净额     = %s 元' % ('%.0f' % 净额 if 净额 is not None else '—'))
    print('    总成本   = %s 元' % ('%.0f' % 成本 if 成本 is not None else '—'))
    print('    毛价差   = %s 元  （净额 + 成本，应与信号无关、各档相同）'
          % ('%.0f' % 毛价差 if 毛价差 is not None else '—'))
    if 净额 is not None:
        print('    收益率   = %+.2f%%' % (净额 / INIT_BALANCE * 100))
    if open_times is not None:
        print('    开仓次数 = %s' % open_times)

    out = {
        '滑点跳数': slip,
        '周期分钟': kline_sec // 60,
        '品种': sym,
        '区间': '%s ~ %s' % (IS_START, IS_END),
        '初始资金': INIT_BALANCE,
        '结束权益': balance,
        '净额元': 净额,
        '总成本元': 成本,
        '毛价差元': 毛价差,
        '收益率': (净额 / INIT_BALANCE) if 净额 is not None else None,
        '开仓次数': open_times,
        '平仓盈亏': close_profit,
        '统计字典': sd,
    }
    _od = os.path.join(_ROOT, '工具', '_输出')
    if not os.path.isdir(_od):
        os.makedirs(_od)
    _op = os.path.join(_od, '_成本压测_slip%d.json' % slip)
    with io.open(_op, 'w', encoding='utf-8') as f:
        f.write(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print('  [压测] 结果已落盘：%s' % _op)
    return 0


if __name__ == '__main__':
    sys.exit(主())
