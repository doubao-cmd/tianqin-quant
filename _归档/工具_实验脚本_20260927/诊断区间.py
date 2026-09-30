# -*- coding: utf-8 -*-
"""
诊断区间 —— 在「任意历史区间」上跑一次双均线回测，只看成交形态，不入账本。
==========================================================
定位：**诊断**，不是开发。用来把某个历史区间当材料看「几笔、每月多少、
      收益是否集中在单月」，回答「某形态是常态还是偶然」。

为了让「诊断」和「开发」在物理上分开，本工具刻意做了两件事：
  1. sample_tag 自动生成 -> 报告落成 <品种>_双均线_<周期>_diag.html，**不覆盖 _in / _out**
     · 区间落在 2026 年之前 -> 标签自动带「（往年<起年>-<止年>）」，一眼可辨（2026-09-19 用户要求）
     · 2026 年及以后        -> 标签为 diag，不重复标注
  2. 不调用 汇总报告.更新() -> **账本 JSON 一个字节都不动**，v1~v5 记录不受影响
     （汇总页只收 _in / _out，天然不含往年区间）

策略参数与现行 v5 完全一致（写死、不做任何扫描）：
  MA5 / MA20 / MA60 闸门 · 1 手 · 1 跳滑点 · 初始 ATR 止损 2xATR(14) · 保本关闭

用法：
    python 工具/诊断区间.py <起> <止> [周期分钟] [品种KEY]
例：
    python 工具/诊断区间.py 2020-01-01 2021-12-31 30 V

跑完用 工具/分段拆解.py 按年/按月拆报告 txt。
"""
import os
import sys
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.abspath(__file__))
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, 'lib')):
    _ROOT = os.path.dirname(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lib import engine

SYMBOL_KEY = 'V'          # PVC
N1, N2, N3 = 5, 20, 60    # MA5 / MA20 / MA60 闸门（与 v5 一致）
VOLUME = 1
ATR_STOP_MULT = 2.0       # 初始 ATR 止损（与 v5 一致）
BREAKEVEN_ATR = 0.0       # 保本关闭（与 v5 一致）
ATR_PERIOD = 14
INIT_BALANCE = 200000
SLIP_TICKS = 1


def 报告标签(d1, d2):
    """报告 sample_tag：往年区间自动加「（往年）」标记，便于与今年的报告分辨。

    2026-09-19 用户要求：「把往年那些测试报告都加上（往年）标签，容易分辨」。
    判据用区间【止】日所在年份：< 2026 即视为往年（练习/期末考试区间都在 2026）。
    标签里带上具体年份区间，将来跑多个往年区间也能区分开。
    """
    if d2.year < 2026:
        if d1.year == d2.year:
            return 'diag（往年%d）' % d1.year
        return 'diag（往年%d-%d）' % (d1.year, d2.year)
    return 'diag'


def 主():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    d1 = datetime.strptime(sys.argv[1], '%Y-%m-%d').date()
    d2 = datetime.strptime(sys.argv[2], '%Y-%m-%d').date()
    kline_sec = int(sys.argv[3]) * 60 if len(sys.argv) > 3 else 30 * 60
    sym = sys.argv[4] if len(sys.argv) > 4 else SYMBOL_KEY
    标签 = 报告标签(d1, d2)

    print('[诊断] 区间 %s ~ %s | 周期 %d 分钟 | 品种 %s | 1 手' % (d1, d2, kline_sec // 60, sym))
    print('[诊断] 不写账本、不覆盖 _in/_out 报告；报告落 _%s' % 标签)
    print('[诊断] 参数：MA%d/MA%d + MA%d闸门 | ATR止损 %.1fxATR(%d) | 保本 %s | 滑点 %d 跳'
          % (N1, N2, N3, ATR_STOP_MULT, ATR_PERIOD,
             '关' if BREAKEVEN_ATR == 0 else '%.1fxATR' % BREAKEVEN_ATR, SLIP_TICKS))

    engine.run_backtest(
        sym, engine.trend_reverse_signal, N1, N2, VOLUME,
        d1 - timedelta(days=7), d2, INIT_BALANCE, kline_sec,
        strategy_name='双均线',
        open_html=False,
        eval_start=d1,
        sample_tag=标签,
        pass_dir=True, ma_filter=N3,
        slip_ticks=SLIP_TICKS,
        atr_stop_mult=ATR_STOP_MULT, breakeven_atr=BREAKEVEN_ATR, atr_period=ATR_PERIOD,
    )
    print('[诊断] 完成。报告：回测结果/01_双均线/<品种>/<品种>_双均线_%dmin_%s.html'
          % (kline_sec // 60, 标签))
    return 0


if __name__ == '__main__':
    sys.exit(主())
