# -*- coding: utf-8 -*-
"""2026 全年 K 线抓取（2026-11-07）：把复盘终端的数据从「最近 30 天」扩到「2026 全年」。

为什么不能用 `get_kline_data_series`
------------------------------------
它是**专业版**接口，普通账号报 `数据获取方式仅限专业版用户使用`（2026-11-07 实测）。
所以走回测模式：`TqBacktest(start, end)` + `get_kline_serial(data_length=10000)`。

核心技巧（本脚本存在的理由）
----------------------------
`get_kline_serial` 在回测**开始那一刻**就把序列回填到 data_length 根（**回测起点之前**的历史），
所以**不需要把回测跑到终点**——把起点设成想要的"截止时刻"，等一次 wait_update 就能拿到
截止该时刻的 10000 根。实测：跑到终点 38.6s，只开跑 11s，拿到的是同一批数据。

单次上限是 **10000 根**（实测 data_length=20000 只回填 10000）：
  · 5min 10000 根 ≈ 7 个月 ⇒ 全年要用两个截止时刻拼（2026-07-01 + 2026-11-07）
  · 15min 10000 根 ≈ 3.5 年  ⇒ 一次够
  · 1440  10000 根 ≈ 40 年    ⇒ 一次够
  · 1min  10000 根 ≈ 28 个交易日 ⇒ **1min 只能覆盖最近约 1.5 个月**（体量所限，见下）

体量说明（为什么 1min 不拉全年）
--------------------------------
每根 5min 打包成 JSON 约 52 字节（去掉指标后）、1min 约 38 字节。
  · 5min 全年 12 品种 ≈ 14,000×12×52 ≈ 8.7MB
  · 1min 全年 12 品种 ≈ 65,000×12×38 ≈ 30MB   ← 单文件 HTML 放不下
⇒ 1min 只保留最近 10000 根（≈28 个交易日），主图（5min）才是全年。

产物：`_缓存/kline2026/{KEY}_{period}min.csv`，列 datetime,open,high,low,close,volume
      datetime 保持 tqsdk 原始口径（UTC-naive；1440 的日期即交易日），与旧缓存一致。

用法：
    .venv_tq\\Scripts\\python.exe 工具\\复盘\\fetch_2026.py            # 全部品种
    .venv_tq\\Scripts\\python.exe 工具\\复盘\\fetch_2026.py MA LG      # 只跑指定品种
"""
from __future__ import annotations

import sys
import time
import datetime as dt
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import data as D

# 两个截止时刻（北京时）。取 03:00 是为了把前一交易日的夜盘（21:00–23:00）也含进来。
CUT_MAIN = dt.datetime(2026, 11, 7, 3, 0)
CUT_MID = dt.datetime(2026, 7, 1, 3, 0)
DLEN = 10000

# 主会话一次连上要拉的周期（截止 CUT_MAIN）
MAIN_PERIODS = (5, 15, 1440, 1)
# 中段会话只补 5min（截止 CUT_MID）
MID_PERIODS = (5,)


def _pull(api_mod, auth, cutoff: dt.datetime, periods) -> dict[int, pd.DataFrame]:
    """一次连接，把若干周期的历史回填到 cutoff 时刻。"""
    TqApi, TqAuth, TqSim, TqBacktest = api_mod
    api = TqApi(TqSim(200_000.0), auth=TqAuth(auth[0], auth[1]),
                backtest=TqBacktest(cutoff, cutoff + dt.timedelta(days=1)))
    try:
        serials = {p: api.get_kline_serial(codes[CUR_SYM], int(p) * 60, data_length=DLEN)
                   for p in periods}
        api.wait_update()
        out = {}
        for p, k in serials.items():
            df = pd.DataFrame(k)
            df["datetime"] = pd.to_datetime(df["datetime"], unit="ns")
            df = df[[c for c in ("datetime", "open", "high", "low", "close", "volume")
                     if c in df.columns]]
            # ★ 起点必须就设在 cutoff：若提前一天，wait_update 会推进到下一根，
            #   生成一根**未成形**的 bar（实测截止 11-07 03:00 却多出 11-06 09:00 一根）。
            #   起点=cutoff 时其后已无行情，序列停在最后一根完整 bar 上。
            df = df[df["datetime"] < pd.Timestamp(cutoff)]
            out[p] = D._clean_recent_df(df)
        return out
    finally:
        api.close()


CUR_SYM = None
codes: dict = {}


def fetch_one(key: str, api_mod, auth) -> list[tuple]:
    global CUR_SYM
    CUR_SYM = key
    cn = D.RECENT_SPECS[key][0]
    t0 = time.time()
    got: dict[int, pd.DataFrame] = {}

    main = _pull(api_mod, auth, CUT_MAIN, MAIN_PERIODS)
    got.update(main)
    mid = _pull(api_mod, auth, CUT_MID, MID_PERIODS)
    # 5min 两段拼接（中段在前、主段在后），按 datetime 去重
    got[5] = (pd.concat([mid[5], main[5]], ignore_index=True)
              .drop_duplicates(subset="datetime", keep="last")
              .sort_values("datetime").reset_index(drop=True))

    rows = []
    for p, df in sorted(got.items()):
        # 起点裁到 2025-11-01：留够 MA13/ATR14 的预热，再往前没意义（也省体量）
        df = df[df["datetime"] >= pd.Timestamp("2025-11-01")].reset_index(drop=True)
        if df.empty:
            rows.append((key, p, 0, "—", "—"))
            continue
        D._save_recent(df, key, p)
        bj = D.to_beijing(df["datetime"]) if p != 1440 else df["datetime"]
        rows.append((key, p, len(df), str(bj.min()), str(bj.max())))
    print(f"  ✔ {cn} {key}  {time.time()-t0:.0f}s  "
          + " | ".join(f"{p}min={n}" for _k, p, n, _a, _b in rows), flush=True)
    return rows


def main():
    only = [a.upper() for a in sys.argv[1:]] or None
    try:
        from tqsdk import TqApi, TqAuth, TqSim, TqBacktest  # noqa: F401
    except ImportError:
        raise SystemExit("当前解释器没有 tqsdk，请用：.venv_tq\\Scripts\\python.exe")
    api_mod = (TqApi, TqAuth, TqSim, TqBacktest)
    auth = D._load_auth()
    if not auth or not auth[0]:
        raise SystemExit("config_account.py 里没有账号，无法连天勤")

    keys = [k for k in D.RECENT_SPECS if (not only or k in only)]
    print(f"[fetch] 目标 {len(keys)} 品种：{' '.join(keys)}", flush=True)
    t0 = time.time()
    all_rows = []
    for i, key in enumerate(keys, 1):
        codes[key] = D.RECENT_SPECS[key][1]
        print(f"[{i}/{len(keys)}] {D.RECENT_SPECS[key][0]} …", flush=True)
        try:
            all_rows += fetch_one(key, api_mod, auth)
        except Exception as e:
            print(f"  ✘ {key} 失败：{e!r}", flush=True)
    print(f"\n[done] {time.time()-t0:.0f}s", flush=True)
    for k, p, n, a, b in all_rows:
        print(f"  {k:3} {p:5}min n={n:6}  {a} ~ {b}", flush=True)

    # ★ 写新鲜度记录：界面的「数据更新于 X 小时前」角标读的是 _缓存/pool/_last_update.json。
    #   本脚本不是走 exec4/daily_update.py，不写这个文件的话角标会一直显示旧时间（实测 479 小时）。
    try:
        import json
        from datetime import datetime
        pool = D.CACHE_DIR / "pool"
        pool.mkdir(parents=True, exist_ok=True)
        rec = {
            "ok": True,
            "started_at": datetime.fromtimestamp(t0).isoformat(timespec="seconds"),
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "data_updated": True,
            "account": "backtest",
            "source": "fetch_2026.py（全年回填）",
            "n_symbols": len({k for k, *_ in all_rows}),
            "bars": {f"{k}_{p}min": n for k, p, n, _a, _b in all_rows},
        }
        (pool / "_last_update.json").write_text(
            json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[freshness] 已写 {pool / '_last_update.json'}", flush=True)
    except Exception as e:
        print(f"[warn] 新鲜度记录写入失败：{e!r}", flush=True)


if __name__ == "__main__":
    main()
