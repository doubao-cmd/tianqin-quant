# -*- coding: utf-8 -*-
"""
周期深度探针：给定合约，测各周期在免费号下的可得根数 / 覆盖区间 / 每日 bar 数。

用途：任何新品种立项前先跑一次，确认「想做的频率有没有足够历史」。
免费号限制：`get_kline_data_series` 需专业版，故单序列 data_length 上限 10000 根即为历史天花板。

用法：
    python probe_depth.py                # 默认 KQ.m@CFFEX.T
    python probe_depth.py KQ.m@SHFE.rb
输出：报告/probe_depth.json（按合约累积）
"""
import json
import os
import sys
import time

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "报告")
os.makedirs(OUT_DIR, exist_ok=True)

sys.path.insert(0, r"C:\Users\cc\Desktop\天勤量化")
import config_account  # noqa: E402
from tqsdk import TqApi, TqAuth  # noqa: E402

DURS = [(300, "5min"), (900, "15min"), (1800, "30min"), (3600, "60min"), (86400, "日线")]
N = 10000


def clean(df):
    d = df.copy()
    d = d[pd.to_numeric(d["datetime"], errors="coerce").fillna(0) > 0]
    d = d[pd.to_numeric(d["close"], errors="coerce").notna()]
    return d


def bj(ns):
    return pd.to_datetime(ns, unit="ns") + pd.Timedelta(hours=8)


def main():
    sym = sys.argv[1] if len(sys.argv) > 1 else "KQ.m@CFFEX.T"
    user, pwd = config_account.check()
    api = TqApi(auth=TqAuth(user, pwd))
    print("[conn] ok  symbol=%s" % sym, flush=True)
    out = {"symbol": sym, "probe_time": time.strftime("%Y-%m-%d %H:%M:%S"), "rows": []}
    try:
        for dur, name in DURS:
            t = time.time()
            item = {"duration": dur, "name": name}
            try:
                df = clean(api.get_kline_serial(sym, dur, data_length=N))
                if len(df):
                    dt = bj(pd.to_numeric(df["datetime"], errors="coerce"))
                    days = int(dt.dt.date.nunique())
                    item.update({
                        "real_rows": int(len(df)), "days": days,
                        "first_bj": str(dt.iloc[0]), "last_bj": str(dt.iloc[-1]),
                        "bars_per_day": round(len(df) / max(days, 1), 1),
                        "years": round(days / 244.0, 2),
                    })
                    print("%-6s rows=%5d days=%5d %s ~ %s  bars/day=%.1f  %.1fs" % (
                        name, len(df), days, str(dt.iloc[0])[:10], str(dt.iloc[-1])[:10],
                        len(df) / max(days, 1), time.time() - t), flush=True)
                else:
                    item["real_rows"] = 0
                    print("%-6s 无数据" % name, flush=True)
            except BaseException as e:  # noqa: BLE001
                item["error"] = "%s: %s" % (type(e).__name__, e)
                print("%-6s ERROR %s: %s" % (name, type(e).__name__, e), flush=True)
            out["rows"].append(item)

        out["history_series_perm"] = None
        try:
            api.get_kline_data_series(sym, 300, start_dt="2019-01-01", end_dt="2019-02-01")
            out["history_series_perm"] = True
        except BaseException as e:  # noqa: BLE001
            out["history_series_perm"] = False
            out["history_series_error"] = str(e)[:200]
    finally:
        api.close()

    path = os.path.join(OUT_DIR, "probe_depth.json")
    old = {}
    if os.path.isfile(path):
        try:
            old = json.load(open(path, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            old = {}
    if not isinstance(old, dict) or "symbols" not in old:
        old = {"symbols": {}}
    old["symbols"][sym] = out
    json.dump(old, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("[done] -> %s" % path, flush=True)


if __name__ == "__main__":
    main()
