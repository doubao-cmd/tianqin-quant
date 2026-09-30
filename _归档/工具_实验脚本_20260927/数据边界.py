# -*- coding: utf-8 -*-
"""探明各品种在指定周期下的数据边界（最早可用日期 / 根数 / 交集）。
==========================================================
用途：转大周期（日线/周线）前先问清楚「数据到底能拉到多长」。
     样本划分（IS/VAL/OOS）必须建在**所有品种都有的交集**上，
     否则早期会静默丢掉几个品种，腿数/收益都被低估。

用法：
    python 工具/数据边界.py                     # 日线，扫 品种池/ 全部
    python 工具/数据边界.py --sec 86400         # 日线（默认）
    python 工具/数据边界.py --sec 604800        # 周线
    python 工具/数据边界.py --keys V,M,JD,SR    # 只看指定品种
    python 工具/数据边界.py --根数 12000         # 拉更多根（日线 12000 根 ≈ 48 年）

口径：用 api.get_kline_serial(主连, sec, data_length=N) 取**最近 N 根**。
     主连（KQ.m@）自带完整历史，不需要回放，秒级返回。
     ⚠️ 不足 N 根时 tqsdk 在前面补 datetime=0 的占位行 ⇒ 本脚本按 datetime>0 过滤，
        过滤后的首根就是**该品种真实最早可用日**。

输出：表格 + 交集区间 + 逐年根数（判断是否有断档）。
"""
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config_account  # noqa: E402
import config_symbols  # noqa: E402
import pandas as pd  # noqa: E402
from tqsdk import TqApi, TqAuth  # noqa: E402


def 周期名(sec):
    if sec % 604800 == 0:
        return '%d周' % (sec // 604800)
    if sec % 86400 == 0:
        return '%d日' % (sec // 86400)
    if sec % 3600 == 0:
        return '%dh' % (sec // 3600)
    return '%dmin' % (sec // 60)


def 时间(ns):
    """tqsdk 纳秒 → 北京时间。基准是 1970-01-01 08:00 北京 ⇒ 按 UTC 解析即正确。"""
    return pd.Timestamp(int(ns) / 1e9, unit='s', tz='UTC').tz_convert('Asia/Shanghai')


def 扫品种(api, key, sym, sec, 根数):
    """返回 (最早, 最晚, 有效根数, 总数)；失败返回 ('错误', ...)。

    ⚠️ 踩过的坑：tqsdk 在**可用历史不足 data_length** 时，会在 DataFrame **前面**
       补 `datetime=0` 的占位行。所以「等第一行非零」这个条件**永远不成立**，
       会白等到超时（2026-09-25 实测：每品种白等 60 秒，9 品种跑 2 分钟无输出）。
       正确做法 = 等「非零根数」连续两次不再增长，再按 `datetime > 0` 过滤。
    """
    try:
        ks = api.get_kline_serial(sym, sec, data_length=根数)
    except Exception as e:
        return ('错误', str(e)[:40], 0, 0)
    上次 = -1
    t0 = time.time()
    while time.time() - t0 < 45:
        try:
            api.wait_update(deadline=time.time() + 6)
        except Exception:
            break
        现在 = int((ks['datetime'] > 0).sum())
        if 现在 > 0 and 现在 == 上次:      # 稳定了
            break
        上次 = 现在
    有效 = ks[ks['datetime'] > 0]
    if len(有效) == 0:
        return ('无数据', '', 0, len(ks))
    return (时间(有效.iloc[0]['datetime']).strftime('%Y-%m-%d'),
            时间(有效.iloc[-1]['datetime']).strftime('%Y-%m-%d'),
            len(有效), len(ks))


def main():
    argv = sys.argv[1:]
    sec = 86400
    if '--sec' in argv:
        sec = int(argv[argv.index('--sec') + 1])
    根数 = 8000
    if '--根数' in argv:
        根数 = int(argv[argv.index('--根数') + 1])
    keys = []
    if '--keys' in argv:
        keys = [k.strip() for k in argv[argv.index('--keys') + 1].split(',') if k.strip()]
    if not keys:
        keys = sorted(config_symbols.SYMBOLS.keys())

    USER, PWD = config_account.check()
    print('=' * 96)
    print(' 数据边界体检  ｜ 周期 %s（%d 秒） ｜ 拉 %s 根 ｜ %d 个品种'
          % (周期名(sec), sec, format(根数, ','), len(keys)))
    print('=' * 96)
    print('  %-6s %-10s %-16s %-12s %-12s %8s %8s' %
          ('key', '中文名', '主连', '最早', '最晚', '有效根', '返回根'))

    api = TqApi(auth=TqAuth(USER, PWD), web_gui=False)
    结果 = {}
    try:
        for k in keys:
            try:
                中文名, sym, _, _ = config_symbols.get(k)
            except BaseException:
                print('  %-6s [跳过] 不在 config_symbols 里' % k)
                continue
            r = 扫品种(api, k, sym, sec, 根数)
            结果[k] = r
            if r[0] == '错误':
                print('  %-6s %-10s %-16s [错误] %s' % (k, 中文名, sym, r[1]))
            elif r[0] == '无数据':
                print('  %-6s %-10s %-16s [无数据]' % (k, 中文名, sym))
            else:
                print('  %-6s %-10s %-16s %-12s %-12s %8d %8d' %
                      (k, 中文名, sym, r[0], r[1], r[2], r[3]))
    finally:
        api.close()

    好的 = {k: v for k, v in 结果.items() if v[0] not in ('错误', '无数据')}
    if not 好的:
        print('\n [!] 没有任何品种拿到数据')
        return 1

    首 = max(v[0] for v in 好的.values())
    尾 = min(v[1] for v in 好的.values())
    print('-' * 96)
    print(' ⇒ 交集区间：**%s ~ %s**（%d 个品种全都有数据）' % (首, 尾, len(好的)))
    print(' ⇒ 交集长度：约 %d 个日历天（%.2f 年）'
          % ((pd.Timestamp(尾) - pd.Timestamp(首)).days,
             (pd.Timestamp(尾) - pd.Timestamp(首)).days / 365.25))
    if len(set(v[0] for v in 好的.values())) > 1:
        最晚 = max(好的.items(), key=lambda kv: kv[1][0])
        print(' ⚠️ 各品种起点不一致，**交集由 %s（%s 起）决定** —— 它上市最晚（或主连最晚）。'
              % (最晚[0], 最晚[1][0]))
        print('    ⇒ 约束链（按起点从晚到早；去掉一个才能把交集往前推）：')
        排序 = sorted(好的.items(), key=lambda kv: kv[1][0], reverse=True)
        for i, (k, v) in enumerate(排序):
            if i == 0:
                continue                    # 跳过约束本身
            剩 = len(好的) - i
            print('       去掉 %-4s（%s 起）⇒ 剩 %d 品种，交集起点提前到 %s（%.2f 年）'
                  % (k, v[0], 剩, v[0],
                     (pd.Timestamp(尾) - pd.Timestamp(v[0])).days / 365.25))

    if sec == 86400 and len(好的) > 1:
        print('\n 逐年有效根数（同一年各品种的最大/最小值，判断断档）：')
        try:
            参考 = min(好的.items(), key=lambda kv: kv[1][0])[0]
            _, sym, _, _ = config_symbols.get(参考)
            api2 = TqApi(auth=TqAuth(USER, PWD), web_gui=False)
            try:
                ks = api2.get_kline_serial(sym, sec, data_length=根数)
                上次 = -1
                t0 = time.time()
                while time.time() - t0 < 45:
                    try:
                        api2.wait_update(deadline=time.time() + 6)
                    except Exception:
                        break
                    现在 = int((ks['datetime'] > 0).sum())
                    if 现在 > 0 and 现在 == 上次:
                        break
                    上次 = 现在
                d = ks[ks['datetime'] > 0].copy()
                d['年'] = [时间(x).year for x in d['datetime']]
                print('   参考品种 %s（%s）：' % (参考, sym))
                for y, g in d.groupby('年'):
                    print('     %d 年 %3d 根' % (y, len(g)))
            finally:
                api2.close()
        except Exception as e:
            print('   [逐年统计失败] %s' % e)
    return 0


if __name__ == '__main__':
    sys.exit(main())
