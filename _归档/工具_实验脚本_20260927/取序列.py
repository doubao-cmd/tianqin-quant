# -*- coding: utf-8 -*-
"""取 K 线序列存 CSV（通用，任意周期）。
==========================================================
为什么不用 api.get_kline_data_series：那是**专业版**功能，免费账号报
「数据获取方式仅限专业版用户使用」。所以走已知可行的路子 ——
回放模式（TqSim + TqBacktest + wait_update 推进），每根 K 线收盘时记一次。

时间戳：tqsdk 的 datetime 是**纳秒整数**，基准是「1970-01-01 08:00 北京」。
  本项目 engine 的 _dt_str 按 UTC 解析 → 报告时间比真实时间**早 8 小时**（已实测确认）。
  本脚本按正确方式转：Timestamp(ns/1e9, unit='s', tz='UTC') → Asia/Shanghai。
  ⚠️ 因此：**报告时间 + 8 小时 = 本脚本 CSV 的时间**。

用法：
    python 工具/取序列.py <起> <止> [--sec 1800] [--sym KQ.m@DCE.v]
例：
    python 工具/取序列.py 2026-03-13 2026-09-13 --sec 1800

输出：工具/_归档/序列/<合约>_<周期>_<起>_<止>.csv
      列：datetime,open,high,low,close,volume,close_oi
"""
import csv
import os
import sys
import time
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config_account  # noqa: E402
import pandas as pd  # noqa: E402
from tqsdk import TqApi, TqAuth, TqSim, TqBacktest  # noqa: E402

try:
    from tqsdk import BacktestFinished
except ImportError:  # 老版本在 exceptions 里
    from tqsdk.exceptions import BacktestFinished


def 周期名(sec):
    if sec % 86400 == 0:
        return '%dd' % (sec // 86400)
    if sec % 3600 == 0:
        return '%dh' % (sec // 3600)
    return '%dmin' % (sec // 60)


def 时间(ns):
    return pd.Timestamp(int(ns) / 1e9, unit='s', tz='UTC').tz_convert('Asia/Shanghai')


def main():
    argv = [a for a in sys.argv[1:] if not a.startswith('--')]
    if len(argv) < 2:
        print(__doc__)
        return 2
    s = datetime.strptime(argv[0], '%Y-%m-%d').date()
    e = datetime.strptime(argv[1], '%Y-%m-%d').date()
    sec = 1800
    sym = 'KQ.m@DCE.v'
    if '--sec' in sys.argv:
        sec = int(sys.argv[sys.argv.index('--sec') + 1])
    if '--sym' in sys.argv:
        sym = sys.argv[sys.argv.index('--sym') + 1]
    USER, PWD = config_account.check()

    print('>>> 回放取数 %s  %s  %s ~ %s' % (sym, 周期名(sec), s, e), flush=True)
    t0 = time.time()
    api = TqApi(TqSim(), backtest=TqBacktest(start_dt=s, end_dt=e),
                auth=TqAuth(USER, PWD), web_gui=False)
    klines = api.get_kline_serial(sym, sec, data_length=800)
    rows = []
    last = None
    try:
        api.wait_update(deadline=time.time() + 30)
        while True:
            api.wait_update(deadline=time.time() + 30)
            if api.is_changing(klines.iloc[-1], 'datetime'):
                bar = klines.iloc[-2]          # -1 是正在形成的那根，-2 才是已收盘
                t = bar['datetime']
                if t != t:                     # NaN：一开始还没数据
                    continue
                t = int(t)
                if t == last:                  # 同一根重复触发，跳过
                    continue
                last = t
                rows.append((时间(t).strftime('%Y-%m-%d %H:%M:%S'),
                             float(bar['open']), float(bar['high']), float(bar['low']),
                             float(bar['close']), float(bar['volume']),
                             float(bar['close_oi'])))
    except BacktestFinished:
        pass
    finally:
        api.close()

    out = os.path.join(ROOT, '工具', '_归档', '序列',
                       '%s_%s_%s_%s.csv' % (sym.replace('@', '_').replace('.', '-'),
                                            周期名(sec), s, e))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['datetime', 'open', 'high', 'low', 'close', 'volume', 'close_oi'])
        w.writerows(rows)
    print('共 %s 根，用时 %.0f 秒' % (format(len(rows), ','), time.time() - t0), flush=True)
    if rows:
        print('  首 %s   尾 %s' % (rows[0][0], rows[-1][0]))
    print('  已存 %s' % os.path.relpath(out, ROOT))
    return 0


if __name__ == '__main__':
    sys.exit(main())
