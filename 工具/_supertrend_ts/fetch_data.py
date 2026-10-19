# -*- coding: utf-8 -*-
"""ST-TS-01 第 0 步：拉 10 个确认样本品种的日线（1440min）。

品种名单来自预注册 §6，禁止改动：
    AU EG RU MA AG CU BU TA SN PP

口径与 工具/复盘/data.py::fetch_recent_all 一致：
  - TqSim 本地模拟账户（绝不用 TqKq，会踢掉手机端云端模拟盘）
  - get_kline_serial(code, 86400, data_length=8800)，单连接批量订阅
  - datetime 是纳秒整数 -> pd.to_datetime(unit="ns")
  - 清洗：去 datetime<=1990 占位 / 去 OHLC 空值 / close>0 且 high>=low / 去未来未成形 bar

用法：
    <tqsdk-python> fetch_data.py
产物：
    工具/_supertrend_ts/data/{KEY}_1440min.csv   （utf-8-sig，列 datetime,open,high,low,close,volume）
"""
from __future__ import annotations

import os
import sys
import time
import importlib.util
from pathlib import Path

# ---- 清代理：本机 http_proxy 等会让 tqsdk 连接异常 ----
for _k in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
    os.environ.pop(_k, None)

import pandas as pd

PROJ = Path(__file__).resolve().parents[2]          # 天勤量化
sys.path.insert(0, str(PROJ))

KEYS = ["AU", "V", "RU", "MA", "AG", "CU", "BU", "TA", "SN", "PP"]
# 预注册 §6 原名单为 AU EG RU MA AG CU BU TA SN PP。
# 实测 EG 首根 = 2018-12-09（上市晚，样本 < 2300 行）⇒ 按 §6「顺延规则」替换为第 11 位 V。
# V 若也不满足，继续顺延 FU / CF，并在报告点名。
OUT_DIR = Path(__file__).resolve().parent / "data"
DATA_LENGTH = 8800
PERIOD_SEC = 86400                                   # 1440min


def load_auth():
    cfg = PROJ / "config_account.py"
    if not cfg.exists():
        raise FileNotFoundError(f"缺少 {cfg}")
    spec = importlib.util.spec_from_file_location("_cfg_account", cfg)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return getattr(m, "USER", None), getattr(m, "PASSWORD", None)


def clean(df: pd.DataFrame, now_beijing: pd.Timestamp) -> pd.DataFrame:
    df = df.copy()
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df[df["datetime"] > pd.Timestamp("1990-01-01")]
    for c in ("open", "high", "low", "close", "volume"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=[c for c in ("open", "high", "low", "close") if c in df.columns])
    df = df[(df["close"] > 0) & (df["high"] >= df["low"])]
    # 未成形 bar：日线 label 是 UTC 16:00（= 北京次日 00:00），允许到「现在」为止
    now_utc = now_beijing - pd.Timedelta(hours=8)
    df = df[df["datetime"] <= now_utc]
    return df.sort_values("datetime").reset_index(drop=True)


def main() -> int:
    import config_symbols as cs
    from tqsdk import TqApi, TqAuth, TqSim

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    user, pwd = load_auth()
    now_bj = pd.Timestamp.now()

    api = TqApi(TqSim(200_000.0), auth=TqAuth(user, pwd))
    try:
        serials = {}
        for key in KEYS:
            spec = cs.SYMBOLS.get(key)
            if not spec:
                raise KeyError(f"{key} 不在 config_symbols.SYMBOLS 里")
            code = spec[1]
            serials[key] = (code, api.get_kline_serial(code, PERIOD_SEC, data_length=DATA_LENGTH))
            print(f"  [sub] {key:3s} {code}", flush=True)
        api.wait_update(deadline=time.time() + 45)

        rows = []
        for key in KEYS:
            code, k = serials[key]
            df = pd.DataFrame(k)
            df["datetime"] = pd.to_datetime(df["datetime"], unit="ns")
            df = df[[c for c in ("datetime", "open", "high", "low", "close", "volume")
                     if c in df.columns]]
            df = clean(df, now_bj)
            p = OUT_DIR / f"{key}_1440min.csv"
            df.to_csv(p, index=False, encoding="utf-8-sig")
            rows.append(dict(key=key, code=code, n=len(df),
                             first=str(df["datetime"].iloc[0]) if len(df) else "-",
                             last=str(df["datetime"].iloc[-1]) if len(df) else "-",
                             tz_tail=str(df["datetime"].iloc[-1])[11:] if len(df) else "-",
                             vol_nonzero=bool(df["volume"].abs().sum() > 0) if "volume" in df else False))
        out = pd.DataFrame(rows)
        print("\n" + "=" * 96)
        print("拉取结果核对表（预注册 §6）")
        print("=" * 96)
        print(out.to_string(index=False))
        print("=" * 96)
        print(f"行数 min={out.n.min()}  品种数={len(out)}  期望：≥2300 行、首行≤2016-02-01、末行≈2026-10-15")
        print(f"目录：{OUT_DIR}")
        return 0
    finally:
        api.close()


if __name__ == "__main__":
    raise SystemExit(main())
