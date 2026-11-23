# -*- coding: utf-8 -*-
"""
Phase 0 探针：中金所国债期货（T/TF/TS/TL）在天勤免费号下的数据可得性与结构指标。

只读探针：不写工程文件、不跑信号、不产出盈亏。
输出：报告/probe_cffex.json + 报告/phase0_国债数据探路.md

口径说明：
- tqsdk datetime 为 ns 纪元值（UTC 语义），+8h 后为北京时间；日线 +8h 后的日期 = 交易日。
- 序列前部数据不足时以 NaN 填充（datetime=0），所有统计一律只算**非 NaN 行**，不能拿 len(df) 当根数。
"""
import json
import os
import sys
import time
from datetime import datetime

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "报告")
os.makedirs(OUT_DIR, exist_ok=True)

sys.path.insert(0, r"C:\Users\cc\Desktop\天勤量化")
import config_account  # noqa: E402
from tqsdk import TqApi, TqAuth  # noqa: E402

# 中金所国债期货合约规格（交易所口径）
SPECS = {
    "T":  {"name": "10年期国债", "tick": 0.005, "mult": 10000, "margin_rate": 0.02},
    "TF": {"name": "5年期国债",  "tick": 0.005, "mult": 10000, "margin_rate": 0.012},
    "TS": {"name": "2年期国债",  "tick": 0.002, "mult": 20000, "margin_rate": 0.005},
    "TL": {"name": "30年期国债", "tick": 0.010, "mult": 10000, "margin_rate": 0.035},
}
SYMS = ["KQ.m@CFFEX.T", "KQ.m@CFFEX.TF", "KQ.m@CFFEX.TS", "KQ.m@CFFEX.TL"]
DAILY_N = 8964
MIN5_N = 8964


def log(*a):
    print(*a, flush=True)


def to_bj(ns: pd.Series) -> pd.Series:
    """ns 纪元 -> 北京时间（naive）。"""
    return pd.to_datetime(ns, unit="ns") + pd.Timedelta(hours=8)


def clean(df: pd.DataFrame) -> pd.DataFrame:
    """去掉 NaN 填充行（前部占位）。"""
    d = df.copy()
    d = d[pd.to_numeric(d["datetime"], errors="coerce").fillna(0) > 0]
    d = d[pd.to_numeric(d["close"], errors="coerce").notna()]
    return d


def stats(df: pd.DataFrame, tick: float) -> dict:
    n = len(df)
    if n == 0:
        return {"real_rows": 0}
    hi = pd.to_numeric(df["high"], errors="coerce")
    lo = pd.to_numeric(df["low"], errors="coerce")
    cl = pd.to_numeric(df["close"], errors="coerce")
    vol = pd.to_numeric(df["volume"], errors="coerce")
    dt = to_bj(pd.to_numeric(df["datetime"], errors="coerce"))

    rng = (hi - lo).replace(0, np.nan)
    # ATR14（按 bar 计）
    prev_close = cl.shift(1)
    tr = pd.concat([hi - lo, (hi - prev_close).abs(), (lo - prev_close).abs()], axis=1).max(axis=1)
    atr14 = tr.rolling(14).mean()

    out = {
        "real_rows": int(n),
        "first_bj": str(dt.iloc[0]),
        "last_bj": str(dt.iloc[-1]),
        "trading_days": int(dt.dt.date.nunique()),
        "HL_deg_pct": round(float((hi == lo).mean() * 100), 2),
        "zero_vol_pct": round(float((vol == 0).mean() * 100), 2),
        "median_range": round(float(rng.median()), 4) if rng.notna().any() else None,
        "median_volume": float(vol.median()) if vol.notna().any() else None,
    }
    if atr14.notna().any():
        med_atr = float(atr14.median())
        out["atr14_median"] = round(med_atr, 4)
        out["tick_over_atr_pct"] = round(tick / med_atr * 100, 2) if med_atr > 0 else None
        out["atr_in_ticks"] = round(med_atr / tick, 2) if tick > 0 else None
    # 每日 bar 数（5min 用）
    per_day = dt.dt.date.value_counts()
    out["bars_per_day_median"] = int(per_day.median()) if len(per_day) else None
    return out


def main():
    t0 = time.time()
    user, pwd = config_account.check()
    api = TqApi(auth=TqAuth(user, pwd))
    log("[conn] %.1fs" % (time.time() - t0))

    res = {"probe_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "items": {}}
    closes = {}
    try:
        for sym in SYMS:
            key = sym.split(".")[-1]
            spec = SPECS[key]
            item = {"symbol": sym, "name": spec["name"], "tick": spec["tick"], "mult": spec["mult"]}

            q = api.get_quote(sym)
            api.wait_update(deadline=time.time() + 10)
            last = float(q.last_price) if q.last_price == q.last_price else None
            item["quote"] = {
                "datetime": str(q.datetime),
                "last_price": last,
                "volume": float(q.volume) if q.volume == q.volume else None,
                "open_interest": float(q.open_interest) if q.open_interest == q.open_interest else None,
                "underlying_symbol": str(getattr(q, "underlying_symbol", "")),
                "expire_datetime": str(getattr(q, "expire_datetime", "")),
            }
            if last:
                item["notional_per_lot"] = round(last * spec["mult"], 0)
                item["margin_per_lot_est"] = round(last * spec["mult"] * spec["margin_rate"], 0)

            # 日线
            t = time.time()
            try:
                d1 = clean(api.get_kline_serial(sym, 86400, data_length=DAILY_N))
                item["daily"] = stats(d1, spec["tick"])
                item["daily"]["elapsed_s"] = round(time.time() - t, 2)
                if len(d1):
                    closes[key] = pd.Series(
                        pd.to_numeric(d1["close"], errors="coerce").values,
                        index=to_bj(pd.to_numeric(d1["datetime"], errors="coerce")).dt.date.values,
                    )
                log("[daily] %-4s rows=%4d %s ~ %s deg=%.1f%%" % (
                    key, item["daily"].get("real_rows", 0),
                    item["daily"].get("first_bj", "-")[:10], item["daily"].get("last_bj", "-")[:10],
                    item["daily"].get("HL_deg_pct", -1)))
            except BaseException as e:  # noqa: BLE001
                item["daily"] = {"error": "%s: %s" % (type(e).__name__, e)}
                log("[daily] %-4s ERROR %s: %s" % (key, type(e).__name__, e))

            # 5min
            t = time.time()
            try:
                m5 = clean(api.get_kline_serial(sym, 300, data_length=MIN5_N))
                item["min5"] = stats(m5, spec["tick"])
                item["min5"]["elapsed_s"] = round(time.time() - t, 2)
                log("[5min ] %-4s rows=%4d %s ~ %s days=%d bars/day=%s tick/ATR=%s%%" % (
                    key, item["min5"].get("real_rows", 0),
                    item["min5"].get("first_bj", "-")[:10], item["min5"].get("last_bj", "-")[:10],
                    item["min5"].get("trading_days", -1),
                    item["min5"].get("bars_per_day_median"),
                    item["min5"].get("tick_over_atr_pct")))
            except BaseException as e:  # noqa: BLE001
                item["min5"] = {"error": "%s: %s" % (type(e).__name__, e)}
                log("[5min ] %-4s ERROR %s: %s" % (key, type(e).__name__, e))

            res["items"][key] = item
    finally:
        api.close()

    # 四合约日收益相关矩阵
    try:
        df = pd.DataFrame(closes).sort_index()
        df = df.dropna()
        ret = np.log(df).diff().dropna()
        corr = ret.corr()
        res["corr"] = {
            "n_days": int(len(ret)),
            "matrix": {a: {b: round(float(corr.loc[a, b]), 4) for b in corr.columns} for a in corr.index},
            "mean_pairwise": round(float(corr.values[np.triu_indices_from(corr.values, 1)].mean()), 4),
            "max_pairwise": round(float(corr.values[np.triu_indices_from(corr.values, 1)].max()), 4),
        }
        log("[corr] 样本 %d 天  平均两两=%.3f  最大=%.3f" % (
            res["corr"]["n_days"], res["corr"]["mean_pairwise"], res["corr"]["max_pairwise"]))
    except BaseException as e:  # noqa: BLE001
        res["corr"] = {"error": "%s: %s" % (type(e).__name__, e)}
        log("[corr] ERROR %s: %s" % (type(e).__name__, e))

    res["elapsed_s"] = round(time.time() - t0, 1)
    path = os.path.join(OUT_DIR, "probe_cffex.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    log("\n[done] %.1fs -> %s" % (res["elapsed_s"], path))


if __name__ == "__main__":
    main()
