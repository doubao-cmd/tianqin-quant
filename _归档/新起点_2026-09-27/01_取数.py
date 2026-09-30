# -*- coding: utf-8 -*-
"""01 取数 —— 甲醇 MA · 30 分钟 K 线（新起点 · 期望拆解实验）
==============================================================
一次取最近 N 根。tqsdk 的 get_kline_serial 上限就是 8964 根。
8964 根 30 分钟 ≈ 747 个交易日 ≈ 3 年。

时间戳：tqsdk 的 datetime 是纳秒整数。本项目 engine 的 _dt_str 按 UTC 解析，
       会让报告时间早 8 小时。本脚本按正确方式转：
       Timestamp(ns/1e9, unit='s', tz='UTC') -> Asia/Shanghai

取数铁律（项目约定）：禁并发；一次慢请求污染整会话 -> 单会话只干这一件事；
                     空数据禁写盘。

用法：python 新起点/01_取数.py
输出：新起点/数据/MA_30min.csv
"""
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config_account  # noqa: E402
import pandas as pd  # noqa: E402
from tqsdk import TqApi, TqAuth  # noqa: E402

SYM = 'KQ.m@CZCE.MA'
SEC = 1800
N = 8964


def 主流程():
    USER, PWD = config_account.check()
    print(f'>>> 取 {SYM}  30分钟  最近 {N} 根', flush=True)
    t0 = time.time()

    api = TqApi(auth=TqAuth(USER, PWD), web_gui=False)
    try:
        ks = api.get_kline_serial(SYM, SEC, data_length=N)
        for _ in range(8):
            api.wait_update(deadline=time.time() + 30)
            if not pd.isna(ks['datetime'].iloc[0]):
                break
        df = pd.DataFrame(ks)
    finally:
        api.close()

    df = df[df['datetime'].notna()].copy()
    if len(df) == 0:
        print('!! 空数据，按铁律不写盘', flush=True)
        return 1

    df['时间'] = pd.to_datetime(df['datetime'], unit='ns', utc=True).dt.tz_convert('Asia/Shanghai')
    df = df[['时间', 'open', 'high', 'low', 'close', 'volume', 'close_oi']]
    df.columns = ['时间', '开', '高', '低', '收', '量', '持仓']

    outdir = os.path.join(_HERE, '数据')
    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, 'MA_30min.csv')
    df.to_csv(out, index=False, encoding='utf-8-sig')

    print(f'共 {len(df):,} 根，用时 {time.time() - t0:.0f} 秒', flush=True)
    print(f'  首 {df["时间"].iloc[0]}', flush=True)
    print(f'  尾 {df["时间"].iloc[-1]}', flush=True)
    print(f'  收盘价 {df["收"].iloc[0]:.0f} -> {df["收"].iloc[-1]:.0f}', flush=True)
    print(f'  已存 新起点/数据/MA_30min.csv', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(主流程())
