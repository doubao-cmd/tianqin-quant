# -*- coding: utf-8 -*-
"""
进场体检 · 数据缓存层（跑一次，之后所有候选信号复用同一份数据）
================================================================
为什么先落盘：出场/进场体检要在**同一份数据**上比不同信号，否则每次重拉
既慢又可能因数据源更新而不可比。本脚本把 12 品种的 5min K 线缓存成本地 CSV，
体检脚本只读缓存、**不连网**。

口径（与 06 回测脚本逐字一致，保证可比）：
  · 周期 5min（300 秒）
  · 窗口：回测起点 = 2021-01-01 前 7 天（预热），终点 2023-12-31
  · TqSim + TqBacktest（回测模式才能取到多年的 5min；实时模式 data_length 上限 ≈ 8964 根 ≈ 1 个月）
  · 只落 datetime/open/high/low/close（volume 在回测模式 2018 年后不可靠，见 README §18）

产物：`数据/{key}_5min_{start}_{end}.csv`
用法：python 拉数据.py            # 全部 12 品种
      python 拉数据.py JM M P     # 只拉指定品种
"""
import os
import sys
import time

import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = _HERE
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from tqsdk import TqApi, TqSim, TqBacktest, TqAuth  # noqa: E402
import config_account  # noqa: E402
import config_symbols  # noqa: E402

KSEC = 300
START = pd.Timestamp("2021-01-01")
END = pd.Timestamp("2023-12-31")
WARMUP_DAYS = 7
INIT = 200000.0
DATA_DIR = os.path.join(_HERE, "数据")


def pull_one(key, user, pwd):
    name_cn, main_code, MULT, TICK = config_symbols.get(key)
    bt_start = START - pd.Timedelta(days=WARMUP_DAYS)
    api = TqApi(TqSim(INIT), backtest=TqBacktest(bt_start, END), auth=TqAuth(user, pwd))
    k = api.get_kline_serial(main_code, KSEC, data_length=3000)
    print(f"[拉数] {name_cn}({key}) {main_code} ...", flush=True)

    dts_l, o_l, h_l, l_l, c_l = [], [], [], [], []
    last_ns = 0
    t0 = time.time()
    while True:
        try:
            ok = api.wait_update()
        except Exception:
            break
        if not ok:
            break
        if len(k) == 0:
            continue
        dts = k["datetime"].to_numpy()
        cl = k["close"].to_numpy(float)
        op = k["open"].to_numpy(float)
        hi = k["high"].to_numpy(float)
        lo = k["low"].to_numpy(float)
        for j in range(len(dts)):
            if dts[j] > last_ns:
                dts_l.append(dts[j]); c_l.append(cl[j]); o_l.append(op[j])
                h_l.append(hi[j]); l_l.append(lo[j]); last_ns = dts[j]
        if last_ns > 0 and pd.Timestamp(last_ns) >= END:
            break
        if time.time() - t0 > 1200:
            print("  [warn] 超时强制退出", flush=True)
            break
    try:
        api.close()
    except Exception:
        pass

    df = pd.DataFrame({
        "datetime": pd.to_datetime(dts_l, unit="ns"),
        "open": o_l, "high": h_l, "low": l_l, "close": c_l,
    })
    os.makedirs(DATA_DIR, exist_ok=True)
    out = os.path.join(DATA_DIR, f"{key}_5min_{START.date()}_{END.date()}.csv")
    df.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"  [ok] {len(df)} 根  {df['datetime'].min()} ~ {df['datetime'].max()}  -> {out}", flush=True)
    return len(df)


def main():
    args = [a.upper() for a in sys.argv[1:]]
    keys = args if args else list(config_symbols.SYMBOLS.keys())
    bad = [k for k in keys if k not in config_symbols.SYMBOLS]
    if bad:
        raise SystemExit(f"品种代号写错：{bad}\n可用：{', '.join(config_symbols.SYMBOLS)}")
    user, pwd = config_account.check()
    t0 = time.time()
    total = 0
    for i, key in enumerate(keys, 1):
        print(f"--- [{i}/{len(keys)}] ---", flush=True)
        try:
            total += pull_one(key, user, pwd)
        except Exception as e:
            print(f"  [ERR] {key}: {type(e).__name__} {e}", flush=True)
    print(f"\n[done] {len(keys)} 品种 / {total} 根 / {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
