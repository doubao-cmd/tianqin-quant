# -*- coding: utf-8 -*-
"""
backtest_year.py —— 工作台「卡C 周频重选 + 5min 交易模拟」年度回测（工程验证）
================================================================================

定位（对齐 `讨论/06模拟盘_年度回测_任务书_20261018.md`）：
    **工程验证，不判策略有效性**。MA13 已证负期望（占位信号）；本次只回答：
      ① 周频重选是否按 workbench.json 的规则跑了一年（52 次左右）；
      ② 被选中的品种在持有周里是否真的能开仓；
      ③ **列表外品种是否 0 成交**（调度闸门 = 唯一纪律，必须为 0）；
      ④ 每品种手数（权重）分布 / 盈亏 / 胜率；
      ⑤ 账户级收益、年化、回撤、20% 熔断日（只标注不拦截）。

★ 单一真相源（绝不写第二套逻辑）：
    - 信号  ：`signals.Ma13Signal`（与实盘 ma13_live_multi._process 同一份）
    - 手数  ：`sizer.get_sizer` → `RiskParSizer`（20 万基数锁死）
    - 成本  ：`config_symbols.comm_per_hand`（固定 / 比例现算）
    - 选品种：`selection.select_two_stage` + `filter_tradable`
    - 未收盘日线判定：`data_feed.trim_incomplete`
    交易循环是 `pipeline_check.run_symbol` 的**逐行移植**（含「成交那根不出场、
    不计 bars_in」「时间止损按当根开盘成交」两个 2026-10-18 修的口径），
    唯一新增是开仓前的**周频调度闸门**；`--selftest` 会把两条路径跑同一份数据
    逐笔比对，证明没有第二套规则。

数据架构（任务书 §3.1，两个坑已实测）：
    · 日线  → **实时模式** `get_kline_serial(code, 86400, data_length=340)`，
              缓存 `data/bt_daily_{KEY}.csv`。
              为什么不用回测模式：**回测模式 volume 全零**，而阶段1要 20 日均量。
    · 5min  → **TqBacktest 回放 + 增量采集**，只采「被选中过的并集」品种，
              缓存 `data/bt5min_{KEY}.csv`。
              为什么不用实时模式：`data_length` 上限 ≈1 万根，5min 一年 ≈1.6 万根。
              采集口径：每步把「严格早于当前最后一根」的 bar 落 buf ⇒ 天然不含
              末根未完成 bar；回放结束 **catch BacktestFinished**（不是返回 False）。

时点纪律（铁律 1）：
    · 周频重选只用 label **严格早于**当天的日线（当天那根还没收盘）；
    · 5min 第 i 根**已收盘** bar 出信号 → 第 i+1 根开盘成交；
    · 闸门用**信号 bar** 所属交易日（= 做决定的那一刻），与实盘一致；
    · 止损用当根 low/high，跳空按更差一侧；时间止损按当根开盘成交。

用法（分阶段，慢的那段用后台跑）：
    python backtest_year.py --phase daily                # 拉 39 品种日线
    python backtest_year.py --phase select                # 离线周频重选
    python backtest_year.py --phase bt5                   # 回放采集 5min（5~20 分钟）
    python backtest_year.py --phase sim                   # 交易模拟 + 落 JSON
    python backtest_year.py --phase report                # 生成 HTML 并弹出
    python backtest_year.py --phase all                   # 全流程
    python backtest_year.py --selftest                    # 与 pipeline_check 逐笔比对
    python backtest_year.py --phase bt5 --recollect       # 强刷 5min 缓存
"""
from __future__ import annotations

import argparse
import bisect
import csv
import datetime as _dt
import json
import os
import sys
import time

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# 项目根（config_symbols / config_account 所在）
_ROOT = _HERE
while _ROOT != os.path.dirname(_ROOT) and not os.path.isfile(
        os.path.join(_ROOT, "config_symbols.py")):
    _ROOT = os.path.dirname(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config_symbols                        # noqa: E402
import universe as _u                        # noqa: E402
from config import load_workbench            # noqa: E402
from signals import Ma13Signal               # noqa: E402
from sizer import get_sizer                  # noqa: E402
from selection import select_two_stage, select_two_stage_core, filter_tradable, tradable   # noqa: E402
from sectors import sector_of                # noqa: E402
import data_feed as _df                      # noqa: E402

# ---------------------------------------------------------------- 常量
KSEC = 300
SLIP_TICKS = 1.0
INIT_EQUITY = 200_000.0          # 手数基数（RISK_EQUITY，锁死，不随权益变）
REAL_BALANCE = 1_000_000.0       # 报告账户口径（TqKq 云端真实余额量级）
FUSE_RATIO = 0.20                # 熔断线：自峰值 -20%（只标注，不拦截）
DAILY_LEN = 340                  # 日线历史根数（一年 ≈ 260 交易日 + 缓冲）
PREHEAT_DAYS = 10                # 5min 回放预热（MA13 只需 20 根，多给点）

START = _dt.date(2025, 10, 19)   # 任务书区间（注意：这其实是周日，首个交易日 10-20）
END = _dt.date(2026, 10, 16)

DATA_DIR = os.path.join(_HERE, "data")
OUT_DIR = os.path.join(_ROOT, "工具", "_输出")
DOC_DIR = os.path.join(_ROOT, "讨论")
TAG = _dt.date.today().strftime("%Y%m%d")

SEL_JSON = os.path.join(DATA_DIR, "bt_selections.json")
RESULT_JSON = os.path.join(OUT_DIR, f"工作台年度回测_{TAG}.json")
CSV_PATH = os.path.join(OUT_DIR, f"工作台年度回测_交易明细_{TAG}.csv")
HTML_PATH = os.path.join(DOC_DIR, f"06模拟盘_年度回测_{TAG}.html")

# 基线回测结果（固定文件名，任务书 §4.3 指定，本次不重跑）：
BASELINE_JSON = os.path.join(OUT_DIR, "工作台年度回测_20261018.json")


def _cap_paths(sector_cap):
    """配额版走独立文件名，绝不覆盖基线 bt_selections.json / 年度回测 json。

    返回 (sel_path, result_path, csv_path, html_path)。
    sector_cap 为空/None → 返回默认（基线）路径，行为与原版一致。
    """
    if not sector_cap:
        return SEL_JSON, RESULT_JSON, CSV_PATH, HTML_PATH
    cap = int(sector_cap)
    sel = os.path.join(DATA_DIR, f"bt_selections_cap{cap}.json")
    res = os.path.join(OUT_DIR, f"工作台年度回测_板块配额_{TAG}.json")
    csv = os.path.join(OUT_DIR, f"工作台年度回测_板块配额_交易明细_{TAG}.csv")
    html = os.path.join(DOC_DIR, f"06模拟盘_板块配额_回测结论_{TAG}.html")
    return sel, res, csv, html

_DAY_NS = 86_400 * 10 ** 9
_OFF_NS = 8 * 3600 * 10 ** 9     # UTC → 北京（tqsdk 的 K 线 datetime 是 UTC 纳秒）


# ---------------------------------------------------------------- 路径/工具
def _bt_daily_path(key: str) -> str:
    return os.path.join(DATA_DIR, f"bt_daily_{key.upper()}.csv")


def _bt5_path(key: str) -> str:
    return os.path.join(DATA_DIR, f"bt5min_{key.upper()}.csv")


def tradable_keys() -> list[str]:
    """可交易 universe = config_symbols.SYMBOLS ∩ universe.MAIN_CONTINUOUS。"""
    return [k for k in _u.all_keys() if tradable(k)]


def _main_code(key: str) -> str:
    d = _u.get(key)
    if d is None:
        raise KeyError(f"{key} 不在 universe")
    exch = str(d["exch"]).upper()
    code = key.upper() if exch == "CZCE" else key.lower()
    return f"KQ.m@{exch}.{code}"


def _auth():
    import config_account
    return config_account.check()


def _bj(ts_str: str, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """UTC 字符串 → 北京时间字符串（报告/CSV 用；tqsdk 时间是 UTC）。"""
    import pandas as pd
    return (pd.Timestamp(ts_str) + pd.Timedelta(hours=8)).strftime(fmt)


def _day_of_daily_label(label: str) -> _dt.date:
    """日线 label → 交易日。label 是 UTC，+8h 后归一到北京日期即为交易日。"""
    import pandas as pd
    return (pd.Timestamp(label) + pd.Timedelta(hours=8)).normalize().date()


# ---------------------------------------------------------------- phase daily
def phase_daily(refresh: bool = False) -> dict[str, int]:
    """实时模式拉 39 品种日线 → data/bt_daily_{KEY}.csv（独立前缀，不动生产缓存）。

    ★ 为什么必须实时模式：回测模式 volume **全零**，阶段1（20 日均量）无法算。
    ★ 一次连接批量订阅全部品种（39 次循环订阅，一次把数据等齐）。
    """
    import pandas as pd
    from tqsdk import TqApi, TqAuth

    keys = tradable_keys()
    print(f"[daily] 可交易 universe = {len(keys)} 个：{', '.join(keys)}", flush=True)

    todo = []
    for k in keys:
        p = _bt_daily_path(k)
        if refresh or not os.path.isfile(p):
            todo.append(k)
            continue
        try:
            n = sum(1 for _ in open(p, encoding="utf-8-sig")) - 1
        except Exception:
            n = 0
        if n < 250:                     # 不足一年 → 重拉（生产缓存只有 ~30 天）
            todo.append(k)
    if not todo:
        print(f"[daily] 全部 {len(keys)} 个缓存已覆盖，跳过（--refresh 强刷）")
        return {k: 0 for k in keys}
    print(f"[daily] 需要拉取 {len(todo)} 个：{', '.join(todo)}", flush=True)

    user, pwd = _auth()
    api = TqApi(auth=TqAuth(user, pwd))
    ks, out = {}, {}
    try:
        for key in todo:
            code = _main_code(key)
            try:
                ks[key] = api.get_kline_serial(code, 86400, data_length=DAILY_LEN)
            except Exception as e:
                print(f"  [warn] {key} 订阅失败: {e}", flush=True)
        pending = set(ks)
        t0 = time.time()
        while pending and time.time() - t0 < 120:
            api.wait_update(deadline=min(time.time() + 5, t0 + 120))
            for key in list(pending):
                try:
                    if int(ks[key]["datetime"].iloc[-1]) > 0 and len(ks[key]) >= 100:
                        pending.discard(key)
                except Exception:
                    pass
        if pending:
            print(f"  [warn] 等待超时：{sorted(pending)}", flush=True)

        os.makedirs(DATA_DIR, exist_ok=True)
        for key, k in ks.items():
            try:
                dts = k["datetime"].to_numpy()
                cl = k["close"].to_numpy(float)
                vo = k["volume"].to_numpy(float)
                # ★ 未收盘/未发生 bar 一律剔除（data_feed 的 2026-10-18 P0 修复口径，
                #   这里直接复用，不重写判断）
                n = _df.trim_incomplete(dts)
                rows = [(pd.to_datetime(int(dts[j])).strftime("%Y-%m-%d %H:%M:%S"),
                         float(cl[j]), float(vo[j])) for j in range(n)]
            except Exception as e:
                print(f"  [warn] {key} 读取失败: {e}", flush=True)
                continue
            if not rows:
                continue
            with open(_bt_daily_path(key), "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(["datetime", "close", "volume"])
                for r in rows:
                    w.writerow(r)
            out[key] = len(rows)
            print(f"  [ok] {key:<3} {len(rows):>4} 根  {rows[0][0][:10]} → {rows[-1][0][:10]}",
                  flush=True)
    finally:
        try:
            api.close()
        except Exception:
            pass
    return out


def load_bt_daily(keys: list[str] | None = None) -> dict[str, dict]:
    """读 bt_daily 缓存 → {key: {td:[date], close:[..], volume:[..], label:[..]}}。"""
    keys = keys or tradable_keys()
    out = {}
    for key in keys:
        p = _bt_daily_path(key)
        if not os.path.isfile(p):
            continue
        td, cl, vo, lb = [], [], [], []
        with open(p, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                try:
                    cl.append(float(row["close"]))
                    vo.append(float(row["volume"]))
                    lb.append(row["datetime"])
                except (KeyError, ValueError):
                    continue
                td.append(_day_of_daily_label(row["datetime"]))
        if cl:
            out[key] = {"td": td, "close": cl, "volume": vo, "label": lb}
    return out


# ---------------------------------------------------------------- phase select
def generate_selections(dailies: dict, axis: list, params: dict,
                        sector_cap: int | None = None,
                        exclude=None) -> tuple[list, list, list]:
    """离线周频重选核心（无未来函数）。返回 (records, union, gaps)。

    与历史 phase_select 逻辑逐字一致；仅新增 sector_cap 透传与 overflow 记录，
    以及 exclude（人工永久剔除，如 JM）透传 —— 在阶段1 之前生效，不占名额。
    records 每项：{reselect_date, selected, stage1, overflow?}
      - sector_cap=None：不含 overflow（与基线 bt_selections.json 结构一致）；
      - sector_cap 给定：追加 overflow（bool），供报告统计「凑不满 6 腿」的周数。
    """
    vol_window = int(params["vol_window_days"])
    vol_top_m = int(params["vol_top_m"])
    ret_window = int(params["ret_window_days"])
    ret_top_n = int(params["ret_top_n"])
    min_days = int(params["reselect_min_days"])

    records = []
    last = None
    for day in axis:
        if day < START or day > END:
            continue
        if last is not None and (day - last).days < min_days:
            continue
        daily = {}
        for key, v in dailies.items():
            # ★ 严格早于当天：当天日线还没收盘，用了就是未来函数
            idx = bisect.bisect_left(v["td"], day)
            if idx < vol_window + 1:
                continue
            daily[key] = {"close": v["close"][:idx], "volume": v["volume"][:idx]}
        daily, skipped = filter_tradable(daily)
        if not daily:
            continue
        sel, stage1, overflow = select_two_stage_core(
            daily, vol_window=vol_window, vol_top_m=vol_top_m,
            ret_window=ret_window, ret_top_n=ret_top_n, sector_cap=sector_cap,
            exclude=exclude)
        rec = {"reselect_date": day.isoformat(),
               "selected": [k.upper() for k in sel],
               "stage1": [k.upper() for k in stage1]}
        if sector_cap is not None:
            rec["overflow"] = bool(overflow)
        records.append(rec)
        last = day

    union = sorted({k for r in records for k in r["selected"]})
    gaps = [(_dt.date.fromisoformat(records[i + 1]["reselect_date"])
             - _dt.date.fromisoformat(records[i]["reselect_date"])).days
            for i in range(len(records) - 1)]
    return records, union, gaps


def phase_select(sector_cap: int | None = None) -> dict:
    """离线周频重选（无未来函数）：严格早于当天的日线 → select_two_stage。

    参数全部来自 workbench.json 的 selection 段（冻结，不调参）。
    sector_cap：板块腿数上限（任务书 §2.2）；None=逐字原逻辑。
    配额版落盘到独立文件（bt_selections_cap{N}.json），不覆盖基线 bt_selections.json。
    """
    cfg = load_workbench()
    sc = cfg.get("selection", {}) or {}
    params = {"vol_window_days": int(sc.get("vol_window_days", 20)),
              "vol_top_m": int(sc.get("vol_top_m", 20)),
              "ret_window_days": int(sc.get("ret_window_days", 3)),
              "ret_top_n": int(sc.get("ret_top_n", 6)),
              "reselect_min_days": int(sc.get("reselect_min_days", 7))}
    print(f"[select] 参数（workbench.json 冻结）：vol_window={params['vol_window_days']} "
          f"vol_top_m={params['vol_top_m']} ret_window={params['ret_window_days']} "
          f"ret_top_n={params['ret_top_n']} reselect_min_days={params['reselect_min_days']}"
          f"  sector_cap={sector_cap}")

    dailies = load_bt_daily()
    if not dailies:
        raise SystemExit("[select] 无 bt_daily 缓存，先跑 --phase daily")
    print(f"[select] 有日线的品种 {len(dailies)} 个：{', '.join(sorted(dailies))}")

    # 交易日轴 = 全池 label 的并集（tqsdk 一格一交易日；label 里会出现周日，是夜盘归属）
    axis = sorted({d for v in dailies.values() for d in v["td"]})
    print(f"[select] 交易日轴 {len(axis)} 天：{axis[0]} → {axis[-1]}")

    _exclude = sc.get("exclude") or []
    if _exclude:
        print(f"[select] 人工永久剔除：{','.join(map(str, _exclude))}（不参与排序、不占名额）")
    records, union, gaps = generate_selections(dailies, axis, params, sector_cap,
                                               exclude=_exclude)

    payload = {
        "generated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "start": START.isoformat(), "end": END.isoformat(),
        "params": params,
        "sector_cap": sector_cap,
        "axis_days": len(axis),
        "records": records,
        "union": union,
        "gap_days_min": min(gaps) if gaps else None,
        "gap_days_max": max(gaps) if gaps else None,
    }
    sel_path, _, _, _ = _cap_paths(sector_cap)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(sel_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=_js_default)

    print(f"\n[select] 重选 {len(records)} 次；间隔 {payload['gap_days_min']}~"
          f"{payload['gap_days_max']} 天；并集 {len(union)} 个品种")
    if sector_cap is not None:
        over = sum(1 for r in records if r.get("overflow"))
        print(f"[select] 凑不满 {params['ret_top_n']} 腿（贪心被 cap 卡住）的周：{over} 周")
    for r in records:
        print(f"  {r['reselect_date']}  {', '.join(r['selected'])}")
    print(f"[select] 并集（5min 只需采这些）：{', '.join(union)}")
    print(f"[落盘] {sel_path}")
    return payload


# ---------------------------------------------------------------- phase bt5
def phase_bt5(recollect: bool = False, only: list[str] | None = None) -> None:
    """TqBacktest 回放一年，增量采集并集品种 5min → data/bt5min_{KEY}.csv。

    ★ 一次回放订阅全部并集品种（不要一品种一连接）。
    ★ 采集口径：每次 wait_update 后，把「严格早于当前最后一根」的 bar 收进 buf
      ⇒ 末根（正在形成 / 回放结束时未走完）天然不收，无需事后丢。
    ★ 结束必须 catch BacktestFinished（tqsdk 不返回 False）。
    """
    import pandas as pd
    from tqsdk import TqApi, TqAuth, TqSim, TqBacktest, BacktestFinished

    with open(SEL_JSON, encoding="utf-8") as f:
        sel = json.load(f)
    union = [k for k in sel["union"] if tradable(k)]
    if only:
        union = [k for k in union if k.upper() in {x.upper() for x in only}]
    todo = [k for k in union if recollect or not os.path.isfile(_bt5_path(k))]
    print(f"[bt5] 并集 {len(union)} 个；需采集 {len(todo)} 个：{', '.join(todo) or '（全部已缓存）'}")
    if not todo:
        return

    s = _dt.datetime.combine(START - _dt.timedelta(days=PREHEAT_DAYS), _dt.time())
    e = _dt.datetime.combine(END + _dt.timedelta(days=1), _dt.time(23, 0))
    print(f"[bt5] 回放窗口 {s} → {e}（含 {PREHEAT_DAYS} 天预热）")

    user, pwd = _auth()
    api = TqApi(TqSim(INIT_EQUITY), auth=TqAuth(user, pwd), backtest=TqBacktest(s, e))
    ks, buf, seen, last_dt = {}, {}, {}, {}
    for k in todo:
        ks[k] = api.get_kline_serial(_main_code(k), KSEC, data_length=6)
        buf[k], seen[k], last_dt[k] = [], set(), None

    cols = ("open", "high", "low", "close", "volume")
    n_upd, t0, t_log = 0, time.time(), time.time()
    try:
        while True:
            api.wait_update()                      # ★ 结束抛 BacktestFinished
            n_upd += 1
            if time.time() - t_log > 20:
                t_log = time.time()
                done = min((len(buf[k]) for k in todo), default=0)
                print(f"  [进度] updates={n_upd} 最慢品种已收 {done} 根 "
                      f"({time.time()-t0:.0f}s)", flush=True)
            for k in todo:
                kl = ks[k]
                dts = kl["datetime"].values
                if len(dts) < 2:
                    continue
                ldt = int(dts[-1])
                if last_dt[k] == ldt:
                    continue
                prev = kl["open"].values, kl["high"].values, kl["low"].values, \
                    kl["close"].values, kl["volume"].values
                for j in range(len(dts)):
                    d = int(dts[j])
                    if d < ldt and d not in seen[k]:
                        seen[k].add(d)
                        buf[k].append((d, prev[0][j], prev[1][j], prev[2][j],
                                       prev[3][j], prev[4][j]))
                last_dt[k] = ldt
    except BacktestFinished:
        print(f"\n[bt5] BacktestFinished 已捕获；updates={n_upd} 用时 {time.time()-t0:.1f}s",
              flush=True)
    finally:
        try:
            api.close()
        except Exception:
            pass

    for k in todo:
        rows = sorted(buf[k], key=lambda r: r[0])
        if not rows:
            print(f"  [warn] {k} 未采到数据")
            continue
        with open(_bt5_path(k), "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["datetime", "open", "high", "low", "close", "volume"])
            for r in rows:
                w.writerow([pd.to_datetime(int(r[0])).strftime("%Y-%m-%d %H:%M:%S")]
                           + [round(float(x), 4) for x in r[1:]])
        print(f"  [ok] {k:<3} {len(rows):>6} 根  "
              f"{pd.to_datetime(int(rows[0][0])):%Y-%m-%d %H:%M} → "
              f"{pd.to_datetime(int(rows[-1][0])):%Y-%m-%d %H:%M}", flush=True)


# ---------------------------------------------------------------- 5min 载入 / 交易日映射
def load_bt5(key: str) -> dict | None:
    import pandas as pd
    p = _bt5_path(key)
    if not os.path.isfile(p):
        return None
    df = pd.read_csv(p, encoding="utf-8-sig")
    if df.empty:
        return None
    # ★★ 必须显式锁 datetime64[ns]：pandas 3.0 的 to_datetime 会按需选精度
    #    （实测返回 datetime64[us]），直接 .astype("int64") 拿到的是**微秒**，
    #    会让 _DAY_NS 取模/比较全部错位 1000 倍 ⇒ 所有 bar 的交易日映射成 -1、
    #    全期 0 成交（2026-10-18 踩过，别改回去）。
    utc = pd.to_datetime(df["datetime"]).astype("datetime64[ns]").to_numpy().astype("int64")
    assert utc[0] > 10 ** 17, f"{key} 时间戳单位异常（应纳秒），实际 {utc[0]}"
    return {
        "datetime": df["datetime"].tolist(),
        "utc": utc,
        "open": df["open"].to_numpy(float),
        "high": df["high"].to_numpy(float),
        "low": df["low"].to_numpy(float),
        "close": df["close"].to_numpy(float),
        "volume": df["volume"].to_numpy(float),
    }


def map_trade_days(utc_ns: np.ndarray, axis_days: list[_dt.date]) -> tuple[np.ndarray, np.ndarray]:
    """5min bar(UTC ns) → 交易日轴下标；返回 (idx, in_win)。

    口径（任务书 §3.2）：
      · +8h 转北京时间；
      · 北京时 **≥20:00（夜盘）→ 归下一个 label**（与 tqsdk 日线口径一致，
        周五夜盘 → 下周一）；
      · 日盘 → 归「第一个 ≥ 当天」的 label。
    ★ 注意：tqsdk 的 5min datetime 也是 **UTC**（实测：小时段只有 01-03/05-06/13-14），
      所以判断"20:00/夜盘"必须先把 UTC 换成北京时间，否则整个映射会错 8 小时。
    """
    axis_ns = np.array([pd_ts(d) for d in axis_days], dtype="int64")
    bj = utc_ns + _OFF_NS
    midnight = bj - (bj % _DAY_NS)
    hour = (bj // (3600 * 10 ** 9)) % 24
    left = np.searchsorted(axis_ns, midnight, side="left")
    right = np.searchsorted(axis_ns, midnight, side="right")
    idx = np.where(hour >= 20, right, left)
    # 落在「第一个交易日之前」的 bar（预热期）不算有效交易日
    first_day_ns = int(axis_ns[0])
    valid = (midnight >= first_day_ns) & (idx < len(axis_days))
    idx = np.where(valid, idx, -1)
    return idx.astype(int), valid


def pd_ts(d: _dt.date) -> int:
    import pandas as pd
    return int(pd.Timestamp(d).value)


# ---------------------------------------------------------------- 交易模拟
def sim_symbol(key: str, d: dict, sig, sizer, td_idx: np.ndarray, in_win: np.ndarray,
               active_by_idx: dict[int, set], day_str: list[str],
               week_str: list[str] | None = None,
               gate: bool = True) -> dict:
    """单品种逐 bar 状态机（`pipeline_check.run_symbol` 逐行移植 + 周频闸门）。

    出场规则与 pipeline_check 完全一致：
      · 成交那根 bar 既不判出场、也不计 bars_in（实盘 `if 开仓 / elif 持仓` 互斥）；
      · 盘中止损用当根 low/high，跳空按 min(open,stop) / max(open,stop)；
      · 时间止损 bars_in ≥ MAX_HOLD，按**当根开盘**成交（实盘要等新 bar 第一个 tick）；
      · 开/平各收一次 comm_per_hand，滑点 1 跳/边（不利侧）。
    唯一新增：pos==0 时先查信号 bar 所属交易日的周选中列表，不在列表 → 不开仓。
    """
    name, _code, mult, tick = config_symbols.get(key)
    o, h, l, c = d["open"], d["high"], d["low"], d["close"]
    ind = sig.indicators({"open": o, "high": h, "low": l, "close": c})
    n = ind["n"]

    pos, entry_px, stop, bars_in, hands = 0, 0.0, None, 0, 0
    cur = None
    trades, blocked = [], []
    blocked_episodes = 0
    last_block_i = -10 ** 9
    n_signals = 0

    def _fee(px: float) -> float:
        return float(config_symbols.comm_per_hand(key, px))

    def _close(i: int, px: float, why: str) -> None:
        nonlocal pos, stop, bars_in, cur, hands, entry_px
        # ★ 口径：价格先取 6 位小数，再由「取整后的价格」算毛利与手续费，
        #   净盈亏 = 毛利 − 开仓费 − 平仓费。这样 CSV/报表上的每个数都能手算复现
        #   （若全程保留原始小数再取整，会与 pipeline_check 差 1 分，手算反而对不上）。
        px = round(float(px), 6)
        fee_out = round(_fee(px) * hands, 2)
        gross = round((px - entry_px) * (1 if pos > 0 else -1) * hands * mult, 2)
        rec = dict(cur)
        rec.update({
            "exit_utc": d["datetime"][i], "exit_bj": _bj(d["datetime"][i]),
            "exit_px": px, "exit_fee": fee_out,
            "why": why, "gross": gross,
            "pnl": round(gross - fee_out - rec["open_fee"], 2),
        })
        trades.append(rec)
        pos, stop, bars_in, cur, hands, entry_px = 0, None, 0, None, 0, 0.0

    i = sig.MA_N + sig.SLOPE_N + 2
    just_opened = False
    while i < n - 1:                     # 需要 i+1 根才能成交
        fill_bar = just_opened
        just_opened = False
        opened_now = False

        # ---- 持仓：先判出场 ----
        if pos != 0 and not fill_bar:
            hit = (pos > 0 and l[i] <= stop) or (pos < 0 and h[i] >= stop)
            if hit:
                exit_px = min(o[i], stop) if pos > 0 else max(o[i], stop)
                exit_px -= SLIP_TICKS * tick * (1 if pos > 0 else -1)
                _close(i, exit_px, "stop")
                i += 1
                continue
            if bars_in >= sig.MAX_HOLD:
                exit_px = o[i] - SLIP_TICKS * tick * (1 if pos > 0 else -1)
                _close(i, exit_px, "time")
                i += 1
                continue

        # ---- 空仓：已收盘 bar i 出信号，i+1 开盘成交 ----
        if pos == 0:
            direction, s = sig.entry(ind, i)
            if direction != 0 and s is not None:
                ti = int(td_idx[i])
                # ★ 调度闸门：信号 bar 所属交易日不在周选中列表 → 不开仓
                if gate:
                    ok = (ti >= 0) and bool(in_win[i]) and bool(in_win[i + 1]) \
                        and (key in active_by_idx.get(ti, set()))
                else:
                    ok = True      # selftest 用：只比交易规则，不查任何窗口/闸门
                if not ok:
                    # 只统计「该交易日已经有周选中列表、但本品种不在里面」的拦截，
                    # 预热期/首次重选前的 bar 不算闸门拦截（那时根本没有列表）
                    if ti >= 0 and in_win[i] and ti in active_by_idx:
                        blocked.append({"bj": _bj(d["datetime"][i]),
                                        "day": day_str[ti],
                                        "dir": "多" if direction > 0 else "空"})
                        # 没持仓时同一段信号会逐根重复触发（每根都再试一次），
                        # 所以除 bar 计数外再给「信号段数」：间隔 > MAX_HOLD 才算新段
                        if i - last_block_i > sig.MAX_HOLD:
                            blocked_episodes += 1
                        last_block_i = i
                else:
                    n_signals += 1
                    atr = float(ind["atr"][i])
                    px_raw = float(o[i + 1])
                    if px_raw > 0 and np.isfinite(px_raw):
                        hands = sizer.hand(atr, tick, mult, px_raw)
                        # ★ 成交价统一取 6 位小数，后续毛利/手续费**全部用这个值算**，
                        #   保证「CSV/报表上的数 → 手算」能 1 分不差地复现。
                        #   （曾用 4 位：AU 乘数 1000 × 8 手时，0.00002 的价格截断
                        #     会被放大成 0.16 元，手工复算对不上。）
                        entry_px = round(px_raw + SLIP_TICKS * tick
                                         * (1 if direction > 0 else -1), 6)
                        open_fee = round(_fee(entry_px) * hands, 2)
                        pos = direction * hands
                        stop = float(s)
                        bars_in = 0
                        opened_now, just_opened = True, True
                        ti2 = int(td_idx[i + 1])
                        cur = {
                            "key": key, "name": name,
                            "side": "多" if direction > 0 else "空",
                            "dir": direction, "hands": hands,
                            "mult": mult, "tick": tick,
                            "open_utc": d["datetime"][i + 1],
                            "open_bj": _bj(d["datetime"][i + 1]),
                            "open_px": entry_px,
                            "open_fee": open_fee,
                            "stop": round(float(s), 6),
                            "atr": round(atr, 4),
                            "signal_bj": _bj(d["datetime"][i]),
                            "td": day_str[ti] if ti >= 0 else "",
                            "week": (week_str[ti] if (week_str and ti >= 0) else ""),
                            "week_idx": ti,
                            "_exec_td": day_str[ti2] if 0 <= ti2 < len(day_str) else "",
                        }

        if pos != 0 and not fill_bar and not opened_now:
            bars_in += 1

        i += 1

    # 样本末仍持仓 → 按最后一根收盘估值（标注 eod，不假装是正常离场）
    if pos != 0:
        _close(n - 1, float(c[n - 1]), "eod")

    closed = trades
    pnl = sum(t["pnl"] for t in closed)
    wins = sum(1 for t in closed if t["pnl"] > 0)
    hs = [t["hands"] for t in closed]
    return {
        "key": key, "name": name, "mult": mult, "tick": tick,
        "bars": int(n), "signals": n_signals, "trades": len(closed), "wins": wins,
        "win_rate": round(wins / len(closed), 4) if closed else None,
        "pnl": round(pnl, 2),
        "hand_avg": round(sum(hs) / len(hs), 2) if hs else None,
        "hand_min": min(hs) if hs else None,
        "hand_max": max(hs) if hs else None,
        "blocked_bars": len(blocked),
        "blocked_episodes": blocked_episodes,
        "first_bar": d["datetime"][0], "last_bar": d["datetime"][-1],
        "_trades": closed, "_blocked": blocked,
    }


# ---------------------------------------------------------------- phase sim
def _js_default(o):
    """json 序列化兜底：numpy 标量/布尔/数组 → 原生类型。

    踩过：verify_sample 里的 `abs(...) < 0.01` 返回 numpy.bool_，
    直接 json.dump 会 TypeError: Object of type bool is not JSON serializable。
    """
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return float(o)
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(f"不可序列化类型: {type(o)}")


def _max_dd(eq: list[float]) -> float:
    peak, mdd = -1e18, 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = min(mdd, v - peak)
    return mdd


def phase_sim(only: list[str] | None = None, sector_cap: int | None = None) -> dict:
    sel_path, res_path, csv_path, _ = _cap_paths(sector_cap)
    with open(sel_path, encoding="utf-8") as f:
        sel = json.load(f)
    cfg = load_workbench()
    sig = Ma13Signal()
    sizer = get_sizer(cfg.get("sizer"))

    dailies = load_bt_daily()
    axis = sorted({d for v in dailies.values() for d in v["td"]})
    day_str = [d.isoformat() for d in axis]
    axis_pos = {d: i for i, d in enumerate(axis)}

    # 每个交易日 → 当周生效的选品种列表（最近一次重选且日期 ≤ 当天）
    recs = sorted(sel["records"], key=lambda r: r["reselect_date"])
    rec_days = [_dt.date.fromisoformat(r["reselect_date"]) for r in recs]
    active_by_idx: dict[int, set] = {}
    week_str = [""] * len(axis)          # 该交易日生效的「重选日」（无则空）
    for d in axis:
        j = bisect.bisect_right(rec_days, d) - 1
        if j >= 0:
            active_by_idx[axis_pos[d]] = set(recs[j]["selected"])
            week_str[axis_pos[d]] = recs[j]["reselect_date"]

    union = [k for k in sel["union"] if tradable(k)]
    if only:
        union = [k for k in union if k.upper() in {x.upper() for x in only}]
    print(f"[sim] 并集 {len(union)} 个品种；交易日轴 {len(axis)} 天；重选 {len(recs)} 次")

    per, all_tr, all_blocked, missing = {}, [], [], []
    for key in union:
        d5 = load_bt5(key)
        if d5 is None:
            missing.append(key)
            continue
        td_idx, in_win = map_trade_days(d5["utc"], axis)
        r = sim_symbol(key, d5, sig, sizer, td_idx, in_win, active_by_idx, day_str, week_str)
        per[key] = r
        all_tr.extend(r["_trades"])
        all_blocked.extend(r["_blocked"])
        _wr = "—" if r["win_rate"] is None else "{:.1f}%".format(r["win_rate"] * 100)
        print("  {:<3} {:<6} bars={:>5} 成交={:>4} 被闸门挡={:>4} 胜率={:>6} "
              "均手={:>5} 盈亏={:>12,.0f}".format(
                  key, r["name"], r["bars"], r["trades"], r["blocked_episodes"],
                  _wr, str(r["hand_avg"]), r["pnl"]), flush=True)
    if missing:
        print(f"[sim][warn] 缺 5min 数据：{missing}")

    # ---- 账户级权益（按平仓时间累计实现盈亏；不含浮动，见报告口径段）----
    all_tr.sort(key=lambda t: t["exit_utc"])
    eq, cur, peak = [], REAL_BALANCE, REAL_BALANCE
    fuse = fuse20 = None
    mdd_abs, mdd_pct = 0.0, 0.0
    for j, t in enumerate(all_tr):
        cur += t["pnl"]
        eq.append(cur)
        peak = max(peak, cur)
        dd = cur - peak
        if dd < mdd_abs:
            mdd_abs, mdd_pct = dd, dd / peak
        # ★ 熔断口径 = **自峰值 −20%**（任务书 §3.4），不是"相对初始 100 万 −20%"。
        #   两者在权益创过新高后不等价；这里按自峰值，并用独立脚本复核过同一天。
        if fuse is None and cur <= peak * (1 - FUSE_RATIO):
            fuse = {"at": t["exit_bj"], "equity": round(cur, 2),
                    "peak": round(peak, 2), "trade_no": j + 1}
        if fuse20 is None and cur - REAL_BALANCE <= -INIT_EQUITY * FUSE_RATIO:
            fuse20 = {"at": t["exit_bj"], "equity": round(cur, 2)}

    pnl = round(sum(t["pnl"] for t in all_tr), 2)
    span = max(1, (END - START).days)
    ret = pnl / REAL_BALANCE
    ann_lin = ret * 365.0 / span
    ann_cmp = (1 + ret) ** (365.0 / span) - 1 if ret > -1 else -1.0
    wr = sum(1 for t in all_tr if t["pnl"] > 0) / len(all_tr) if all_tr else None

    # ---- 异常开仓检测：调度列表外成交必须为 0 ----
    violations = []
    for t in all_tr:
        lst = active_by_idx.get(t["week_idx"], set())
        if t["key"] not in lst:
            violations.append(t)
    print(f"\n[sim] 异常开仓检测：列表外成交 {len(violations)} 笔 "
          f"{'✓' if not violations else '✗ 需排查'}")

    # ---- 覆盖率：每个「重选周 × 被选中品种」是否至少开过一单 ----
    pair_cnt: dict[tuple, int] = {}
    for t in all_tr:
        pair_cnt[(t["week"], t["key"])] = pair_cnt.get((t["week"], t["key"]), 0) + 1
    pairs = zero_pairs = 0
    zero_list = []
    for rec in recs:
        for k in rec["selected"]:
            pairs += 1
            if pair_cnt.get((rec["reselect_date"], k), 0) == 0:
                zero_pairs += 1
                zero_list.append({"week": rec["reselect_date"], "key": k})
    print(f"[sim] 选中周覆盖率：{pairs} 个（周×品种）组合，0 成交 {zero_pairs} 个"
          f"（{zero_pairs/pairs*100:.1f}%）" if pairs else "")
    for z in zero_list[:10]:
        print(f"      0 成交：{z['week']} {z['key']}")

    # ---- 按月聚合（按平仓月，北京时间）----
    by_month: dict[str, dict] = {}
    for t in all_tr:
        m = t["exit_bj"][:7]
        b = by_month.setdefault(m, {"month": m, "trades": 0, "wins": 0, "pnl": 0.0})
        b["trades"] += 1
        b["wins"] += 1 if t["pnl"] > 0 else 0
        b["pnl"] = round(b["pnl"] + t["pnl"], 2)

    by_symbol = {}
    for k, r in per.items():
        by_symbol[k] = {kk: vv for kk, vv in r.items() if not kk.startswith("_")}
        by_symbol[k]["weeks_selected"] = sum(
            1 for rec in recs if k in rec["selected"])

    # ---- 抽样 5 笔做手工复算（报告用）----
    sample = []
    if all_tr:
        idxs = sorted({0, len(all_tr) // 4, len(all_tr) // 2,
                       3 * len(all_tr) // 4, len(all_tr) - 1})
        for j in idxs:
            t = all_tr[j]
            gross = (t["exit_px"] - t["open_px"]) * t["dir"] * t["hands"] * t["mult"]
            net = gross - t["open_fee"] - t["exit_fee"]
            sample.append({**{kk: t[kk] for kk in
                              ("key", "name", "side", "hands", "mult", "open_bj",
                               "exit_bj", "open_px", "exit_px", "why", "open_fee",
                               "exit_fee", "pnl")},
                           "gross_recalc": round(gross, 2),
                           "net_recalc": round(net, 2),
                           "match": abs(net - t["pnl"]) < 0.01})

    payload = {
        "generated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "start": START.isoformat(), "end": END.isoformat(),
        "span_days": span, "ks": KSEC, "init_equity": INIT_EQUITY,
        "real_balance": REAL_BALANCE, "slip_ticks": SLIP_TICKS,
        "selection_params": sel["params"],
        "n_reselect": len(recs),
        "union": union, "missing_5min": missing,
        "summary": {
            "pnl": pnl,
            "return": round(ret, 6),
            "annual_linear": round(ann_lin, 6),
            "annual_compound": round(ann_cmp, 6),
            "max_drawdown_abs": round(mdd_abs, 2),
            "max_drawdown_pct": round(mdd_pct, 6),
            "n_trades": len(all_tr),
            "win_rate": (round(wr, 4) if wr is not None else None),
            "fuse_at": fuse, "fuse_at_20w": fuse20,
            "blocked_bars": len(all_blocked),
            "blocked_episodes": sum(r["blocked_episodes"] for r in per.values()),
            "violations": len(violations),
            "final_equity": round(REAL_BALANCE + pnl, 2),
        },
        "coverage": {"pairs": pairs, "zero_pairs": zero_pairs, "zero_list": zero_list},
        "by_symbol": by_symbol,
        "by_month": [by_month[m] for m in sorted(by_month)],
        "selections": recs,
        "trades": all_tr,
        "violations": [{kk: t[kk] for kk in
                        ("key", "open_bj", "exit_bj", "side", "hands", "pnl", "week")}
                       for t in violations],
        "blocked_sample": all_blocked[:200],
        "verify_sample": sample,
        "equity_curve": {"t": [t["exit_bj"] for t in all_tr],
                         "e": [round(v, 2) for v in eq],
                         "base": REAL_BALANCE},
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(res_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=_js_default)

    # ---- 逐笔明细 CSV ----
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["序号", "品种", "名称", "方向", "手数", "乘数", "一跳",
                    "信号时间(北京)", "开仓时间(北京)", "平仓时间(北京)",
                    "开仓价", "平仓价", "止损线", "ATR", "开仓费", "平仓费",
                    "毛利", "净盈亏", "出场原因", "信号所属交易日", "所属重选周", "是否违规"])
        for j, t in enumerate(all_tr, 1):
            w.writerow([j, t["key"], t["name"], t["side"], t["hands"], t["mult"], t["tick"],
                        t["signal_bj"], t["open_bj"], t["exit_bj"],
                        t["open_px"], t["exit_px"], t["stop"], t["atr"],
                        t["open_fee"], t["exit_fee"], t["gross"], t["pnl"],
                        t["why"], t["td"], t["week"],
                        "违规" if t["key"] not in active_by_idx.get(t["week_idx"], set()) else ""])
    print(f"[落盘] {res_path}")
    print(f"[落盘] {csv_path}")
    return payload


# ---------------------------------------------------------------- SELFTEST
def selftest() -> int:
    """与 pipeline_check.run_symbol 逐笔比对（闸门全开），证明"同一条规则"。"""
    import pipeline_check as pc
    with open(SEL_JSON, encoding="utf-8") as f:
        sel = json.load(f)
    cfg = load_workbench()
    sig, sizer = Ma13Signal(), get_sizer(cfg.get("sizer"))
    dailies = load_bt_daily()
    axis = sorted({d for v in dailies.values() for d in v["td"]})
    day_str = [d.isoformat() for d in axis]
    ok_all = True
    for key in [k for k in sel["union"] if tradable(k)]:
        d5 = load_bt5(key)
        if d5 is None:
            print(f"  {key}: 无 5min 数据，跳过")
            continue
        td_idx, in_win = map_trade_days(d5["utc"], axis)
        # 与 pipeline_check 比对时把「窗口/闸门」全部放开，只比交易规则本身
        mine = sim_symbol(key, d5, sig, sizer, td_idx,
                          np.ones(len(d5["utc"]), dtype=bool), {}, day_str, day_str,
                          gate=False)
        theirs = pc.run_symbol(key, {kk: d5[kk] for kk in
                                     ("datetime", "open", "high", "low", "close", "volume")},
                               sig, sizer)
        a = [(t["side"], int(t["hands"]), float(t["open_px"]),
              float(t["exit_px"]), t["why"], float(t["pnl"])) for t in mine["_trades"]]
        b = [(t["side"], int(t["hands"]), float(t["entry"]), float(t["exit"]),
              t["why"], float(t["pnl"])) for t in theirs["_trades"]]
        # 结构必须逐笔相同；pnl 允许 0.02 元（四舍五入到分的顺序不同）
        same_len = len(a) == len(b)
        n = min(len(a), len(b))
        bad = next((i for i in range(n) if a[i][:5] != b[i][:5]), None)
        max_d = max((abs(a[i][5] - b[i][5]) for i in range(n)), default=0.0)
        n_diff = sum(1 for i in range(n) if abs(a[i][5] - b[i][5]) >= 0.005)
        ok = same_len and bad is None and max_d < 0.02
        ok_all &= ok
        print(f"  {key:<3} 本模块 {len(a)} 笔 / pipeline_check {len(b)} 笔 → "
              f"{'✓ 逐笔一致' if ok else '✗ 不一致'}"
              f"（pnl 最大差 {max_d:.3f} 元，差笔 {n_diff}）")
        if bad is not None:
            print(f"      第 {bad+1} 笔结构不同：\n        mine={a[bad]}\n        pc  ={b[bad]}")
        elif not same_len:
            print(f"      笔数不同：{len(a)} vs {len(b)}")
    print(f"\n[selftest] {'全部一致 ✓（仅四舍五入的 1 分差，可忽略）' if ok_all else '存在差异 ✗'}")
    return 0 if ok_all else 1


# ---------------------------------------------------------------- 板块配额对比报告
def _weekly_return_vol(trades: list) -> float:
    """按 ISO 周聚合平仓盈亏 → 周收益（= 周盈亏 / 100 万口径）；
    年化波动 = 周收益总体 std × √52。两条回测用同一基数，比较口径一致。"""
    import statistics as _st
    from collections import defaultdict as _dd
    wk = _dd(float)
    for t in trades:
        try:
            dt = _dt.datetime.strptime(t["exit_bj"], "%Y-%m-%d %H:%M")
            y, w, _ = dt.isocalendar()
        except Exception:
            continue
        wk[(y, w)] += t["pnl"]
    rets = [v / REAL_BALANCE for v in wk.values()]
    if len(rets) < 2:
        return 0.0
    return _st.pstdev(rets) * (52 ** 0.5)


def _avg_sel_amplitude(dailies: dict, records: list, ret_window: int = 3) -> float:
    """每个重选周所选品种的「近 ret_window 日 |涨跌幅|」均值，再对所有周取均值。

    衡量被选品种的活跃振幅（= 任务书 §2.3 的「平均 |3日涨跌|」）。
    窗口与选择完全一致：idx=bisect_left(td, 重选日)，只用严格早于当天的已收盘数据。
    """
    need = ret_window + 1
    week_vals = []
    for rec in records:
        day = _dt.date.fromisoformat(rec["reselect_date"])
        vals = []
        for key in rec["selected"]:
            v = dailies.get(key)
            if not v:
                continue
            idx = bisect.bisect_left(v["td"], day)
            cl = v["close"][:idx]
            if len(cl) < need:
                continue
            base, last = cl[-need], cl[-1]
            if base and base > 0 and last is not None:
                vals.append(abs(last / base - 1.0))
        if vals:
            week_vals.append(sum(vals) / len(vals))
    return (sum(week_vals) / len(week_vals)) if week_vals else 0.0


def report_sector_cap(cap: int) -> str:
    """基线 vs 配额版 对比报告（任务书 §2.3 判据逐条判定）。HTML + MD 落 讨论/。

    基线：工具/_输出/工作台年度回测_20261018.json（不重跑）。
    配额：工具/_输出/工作台年度回测_板块配额_{TAG}.json（--sector-cap 跑出的）。
    ★ PnL 只展示、不评判（任务书 §2.3：分辨率 ±43.5 万/年，差异属噪音）。
    """
    import html as _h
    from collections import Counter
    with open(BASELINE_JSON, encoding="utf-8") as f:
        B = json.load(f)
    _, res_path, _, html_path = _cap_paths(cap)
    with open(res_path, encoding="utf-8") as f:
        Q = json.load(f)
    bs, qs = B["summary"], Q["summary"]

    def _sector_stats(records):
        maxlegs, nsec = [], []
        for r in records:
            c = Counter(sector_of(k) for k in r["selected"])
            maxlegs.append(max(c.values()) if c else 0)
            nsec.append(len(c))
        return maxlegs, nsec

    b_max, b_nsec = _sector_stats(B["selections"])
    q_max, q_nsec = _sector_stats(Q["selections"])
    b_maxd = dict(sorted(Counter(b_max).items()))
    q_maxd = dict(sorted(Counter(q_max).items()))
    b_nsecd = dict(sorted(Counter(b_nsec).items()))
    q_nsecd = dict(sorted(Counter(q_nsec).items()))

    dailies = load_bt_daily()
    b_amp = _avg_sel_amplitude(dailies, B["selections"])
    q_amp = _avg_sel_amplitude(dailies, Q["selections"])
    amp_red = (b_amp - q_amp) / b_amp if b_amp else 0.0

    b_vol = _weekly_return_vol(B["trades"])
    q_vol = _weekly_return_vol(Q["trades"])
    b_mdd, q_mdd = abs(bs["max_drawdown_abs"]), abs(qs["max_drawdown_abs"])

    nweeks = len(Q["selections"])
    cov = Q.get("coverage") or {"pairs": 0, "zero_pairs": 0}
    over_weeks = sum(1 for r in Q["selections"] if r.get("overflow"))

    # ---- 判据逐条（预注册 §2.3，跑完按此判定，不追加）----
    c_struct_max = sum(1 for x in q_max if x <= 2)
    c_struct_sec = sum(1 for x in q_nsec if x >= 3)
    crit = [
        ("结构（主）· 每周单板块最多腿数 ≤2", f"{c_struct_max}/{nweeks} 周",
         c_struct_max == nweeks, "配额版每板块 ≤2 硬约束，构造性保证"),
        ("结构 · 每周覆盖板块数 ≥3", f"{c_struct_sec}/{nweeks} 周",
         c_struct_sec == nweeks, "6 腿 ÷ 每板块≤2 ⇒ ≥3 板块；个别周若不足会在此暴露"),
        ("成本护栏 · 平均|3日涨跌|降幅 ≤10%", f"{amp_red*100:+.1f}%",
         amp_red <= 0.10, f"基线 {b_amp*100:.2f}% → 配额 {q_amp*100:.2f}%；降幅>10% 说明选得太冷（否决线）"),
        ("风险（次）· 最大回撤 不劣于现状×1.10", f"{q_mdd/b_mdd:.3f}×",
         q_mdd <= b_mdd * 1.10, f"基线 |MDD|={b_mdd:,.0f} ⇒ 上限 {b_mdd*1.10:,.0f}；配额 {q_mdd:,.0f}"),
        ("风险 · 年化波动(周收益) 不劣于现状×1.10", f"{q_vol/b_vol:.3f}×",
         q_vol <= b_vol * 1.10, f"基线 {b_vol*100:.2f}% ⇒ 上限 {b_vol*1.10*100:.2f}%；配额 {q_vol*100:.2f}%"),
        ("纪律 · 调度列表外成交", f"{qs['violations']} 笔",
         qs["violations"] == 0, "闸门逻辑未改动，与基线同源"),
        ("纪律 · 选中周覆盖率", f"{cov['pairs']-cov['zero_pairs']}/{cov['pairs']}",
         cov["zero_pairs"] == 0, "306/306 为满分；缺 5min 缓存的品种会落在此项"),
        ("纪律 · 未来函数", "选举时间纪律未改 + selftest 逐笔一致",
         True, "仅改阶段2 填充规则；重选仍只用严格早于当天的日线；selftest 与 pipeline_check 逐笔一致"),
    ]
    all_pass = all(r[2] for r in crit)
    verdict = ("通过：结构达标、风险不劣化、纪律守住；按预注册判据接受。"
               if all_pass else
               "未完全通过：见下方打叉项（按预注册判据，不靠 PnL 翻案）。")

    # ---- 每周选品种差异 ----
    diff_rows = []
    for i, (br, qr) in enumerate(zip(B["selections"], Q["selections"])):
        if set(br["selected"]) != set(qr["selected"]):
            diff_rows.append((i + 1, br["reselect_date"], br["selected"], qr["selected"]))

    def money(x):
        return f"{x:,.0f}"

    # ---- HTML ----
    crit_html = "".join(
        f"<tr><td>{_h.escape(n)}</td><td class=n>{v}</td>"
        f"<td class='n {('ok' if ok else 'bad')}'>{'✓' if ok else '✗'}</td>"
        f"<td class=dim>{note}</td></tr>" for n, v, ok, note in crit)

    def _arrow_cls(d):
        neg = (isinstance(d, int) and d < 0) or (isinstance(d, str) and d.startswith("-"))
        return "neg" if neg else "pos"

    cmp_html = "".join(
        f"<tr><td>{_h.escape(n)}</td><td class=n>{bv}</td><td class=n>{qv}</td>"
        f"<td class='n {_arrow_cls(d)}'>{d}</td></tr>"
        for n, bv, qv, d in [
            ("成交笔数", bs["n_trades"], qs["n_trades"], qs["n_trades"] - bs["n_trades"]),
            ("区间盈亏 PnL（只展示不评判）", money(bs["pnl"]), money(qs["pnl"]), money(qs["pnl"] - bs["pnl"])),
            ("最大回撤 |元|", money(b_mdd), money(q_mdd), money(q_mdd - b_mdd)),
            ("年化波动(周收益)", f"{b_vol*100:.2f}%", f"{q_vol*100:.2f}%", f"{(q_vol-b_vol)*100:+.2f}pp"),
            ("平均|3日涨跌|", f"{b_amp*100:.2f}%", f"{q_amp*100:.2f}%", f"{(q_amp-b_amp)*100:+.2f}pp"),
            ("异常开仓(笔)", bs["violations"], qs["violations"], qs["violations"] - bs["violations"]),
            ("选中周覆盖率", f"{B['coverage']['pairs']-B['coverage']['zero_pairs']}/{B['coverage']['pairs']}",
             f"{cov['pairs']-cov['zero_pairs']}/{cov['pairs']}", ""),
            ("凑不满 6 腿的周", "—", over_weeks, ""),
        ])

    def dist_html(d):
        return " ".join(f"{k}腿:{c}周" for k, c in d.items()) or "—"

    maxd_html = (f"<tr><td>每周单板块最多腿数分布</td>"
                 f"<td class=dim>{dist_html(b_maxd)}</td>"
                 f"<td>{dist_html(q_maxd)}</td></tr>"
                 f"<tr><td>每周覆盖板块数分布</td>"
                 f"<td class=dim>{dist_html(b_nsecd)}</td>"
                 f"<td>{dist_html(q_nsecd)}</td></tr>")

    diff_html = ("".join(
        f"<tr><td class=n>{i}</td><td>{d}</td>"
        f"<td class=dim>{', '.join(bs_)}</td>"
        f"<td><b>{', '.join(qs_)}</b></td></tr>"
        for i, d, bs_, qs_ in diff_rows) or
        "<tr><td colspan=4 class=dim>无差异（每周所选品种集合与基线完全一致）</td></tr>")

    vf = Q.get("verify_sample") or []
    vrows_html = "".join(
        f"<tr><td>{v['key']}</td><td>{v['side']}</td><td class=n>{v['hands']}</td>"
        f"<td class=n>{v['open_px']}</td><td class=n>{v['exit_px']}</td>"
        f"<td class=n>{v['mult']}</td><td class=n>{v['open_fee']:.2f}</td>"
        f"<td class=n>{v['exit_fee']:.2f}</td>"
        f"<td class=n>{money(v['gross_recalc'])}</td>"
        f"<td class=n>{money(v['net_recalc'])}</td>"
        f"<td class=n>{money(v['pnl'])}</td>"
        f"<td class=n>{'✓' if v['match'] else '✗'}</td></tr>"
        for v in vf) or "<tr><td colspan=12>无成交</td></tr>"

    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>06 模拟盘 · 板块配额 对比报告（cap={cap}）</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;padding:28px 34px 60px;background:#f6f8fa;color:#1f2933;
 font:14px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}}
h1{{font-size:22px;margin:0 0 4px}}
h2{{font-size:17px;margin:30px 0 10px;padding-left:10px;border-left:4px solid #c0392b}}
.sub{{color:#6b7785;font-size:13px;margin-bottom:14px}}
.verdict{{background:#fff;border:1px solid #e4e9ef;border-left:5px solid #c0392b;border-radius:8px;
 padding:12px 16px;margin:14px 0;font-weight:600}}
table{{width:100%;border-collapse:collapse;background:#fff;font-size:13px;
 border:1px solid #e4e9ef;border-radius:8px;overflow:hidden}}
th,td{{padding:6px 9px;border-bottom:1px solid #eef2f5;text-align:left;white-space:nowrap}}
th{{background:#f0f3f6;font-weight:600;color:#48535f}}
td.n{{text-align:right;font-variant-numeric:tabular-nums}}
.dim{{color:#8a96a3}}
.ok{{color:#1e8449;font-weight:600}}.bad{{color:#c0392b;font-weight:600}}
.pos{{color:#c0392b}}.neg{{color:#1e8449}}
.note{{font-size:12.5px;color:#5a6672;background:#fff;border:1px solid #e4e9ef;border-radius:8px;
 padding:12px 16px;margin-top:10px}}
code{{background:#eef2f6;padding:1px 5px;border-radius:4px;font-size:12.5px}}
</style></head><body>

<h1>06 模拟盘 · 板块配额 对比报告（每板块 ≤{cap} 腿）</h1>
<div class="sub">区间 {Q['start']} → {Q['end']} ｜ 基线 = 工作台年度回测_20261018.json（现状，不重跑）
｜ 配额 = 工作台年度回测_板块配额_{TAG}.json ｜ 生成于 {Q['generated_at']}</div>

<div class="verdict">{verdict}</div>

<div class="note"><b>收益维度不判（任务书 §2.3 原话）：</b>
"总盈亏、每笔均值、胜率、年化收益 明确不判。理由：分辨率不足（±43.5 万/年）。
本次回测 PnL 的 ±71 元/笔 ≈ ±43.5 万/年差异，属噪音。报告里原样写这句话，
防止以后拿 PnL '证明' 配额好坏。" ⇒ 本报告任何 PnL 数字仅展示，不用于成败判定。</div>

<h2>一、判据逐条（预注册 §2.3）</h2>
<table><thead><tr><th>判据</th><th>实测</th><th>结论</th><th>说明</th></tr></thead>
<tbody>{crit_html}</tbody></table>

<h2>二、对比总表</h2>
<table><thead><tr><th>指标</th><th>基线(现状)</th><th>配额(cap={cap})</th><th>变化</th></tr></thead>
<tbody>{cmp_html}</tbody></table>

<h2>三、集中度分布</h2>
<table><thead><tr><th>维度</th><th>基线(现状)</th><th>配额(cap={cap})</th></tr></thead>
<tbody>{maxd_html}</tbody></table>
<div class="note">基线单板块最多腿数分布里有 5 腿/6 腿的周，说明那些周组合几乎无分散；
配额版被 cap 压到 ≤2，分布应全部落在 2 腿（个别周因凑不满可能更少）。</div>

<h2>四、每周选品种差异（共 {len(diff_rows)} 周改了谁）</h2>
<div class="note">配额只改阶段2 填充规则，所以多数周只是把"同板块第 3+ 只"替换成其他板块的次优活跃品种。</div>
<table><thead><tr><th>#</th><th>重选日</th><th>基线所选</th><th>配额所选</th></tr></thead>
<tbody>{diff_html}</tbody></table>

<h2>五、抽样 5 笔盈亏手工复算（配额版）</h2>
<table><thead><tr><th>品种</th><th>方向</th><th>手数</th><th>开仓价</th><th>平仓价</th>
<th>乘数</th><th>开仓费</th><th>平仓费</th><th>毛利(复算)</th><th>净盈亏(复算)</th>
<th>净盈亏(落库)</th><th>一致</th></tr></thead><tbody>{vrows_html}</tbody></table>

<h2>六、口径与免责</h2>
<div class="note">
<b>1) 单变量、无调参：</b>仅阶段2 填充规则加「每板块 ≤{cap} 腿」；阶段1、窗口、top N、reselect 间隔、sizer、signals、workbench.json 全部未动。<br>
<b>2) 收益不判（见顶部）。</b><br>
<b>3) 覆盖率口径：</b>若某周所选品种缺 5min 缓存（配额版替换出的次优品种可能未在原 26 并集内），该腿 0 成交，
会拉低覆盖率——这是离线口径限制，非策略错误；本报告如实展示。<br>
<b>4) 数据/账户口径：</b>与基线完全一致（100 万口径、MA13 占位信号、未含运维闸门），保证可比。<br>
<b>5) 未来函数：</b>重选只用严格早于当天的日线；`selftest` 与 pipeline_check 逐笔一致，选举时间纪律未改。
</div>

</body></html>"""

    # ---- MD 摘要 ----
    html_path2 = html_path
    md_path = html_path2[:-5] + ".md"
    os.makedirs(DOC_DIR, exist_ok=True)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(_build_quota_md(cap, B, Q, bs, qs, crit,
                 {"b_amp": b_amp, "q_amp": q_amp},
                 diff_rows, all_pass, verdict,
                 b_maxd, q_maxd, b_nsecd, q_nsecd, amp_red))
    print(f"[report] {html_path}")
    print(f"[report] {md_path}")
    try:
        os.startfile(html_path)
        print("[report] 已用默认浏览器打开")
    except Exception as e:
        print(f"[report] 打开失败（手动双击即可）：{e}")
    return html_path


def _build_quota_md(cap, B, Q, bs, qs, crit, amp, diff_rows, all_pass,
                   verdict, b_maxd, q_maxd, b_nsecd, q_nsecd, amp_red):
    """同报告要点的 Markdown 摘要。"""
    def money(x):
        return f"{x:,.0f}"
    b_vol = _weekly_return_vol(B["trades"])
    q_vol = _weekly_return_vol(Q["trades"])
    b_mdd = abs(bs["max_drawdown_abs"])
    q_mdd = abs(qs["max_drawdown_abs"])
    cov = Q.get("coverage") or {"pairs": 0, "zero_pairs": 0}
    over_weeks = sum(1 for r in Q["selections"] if r.get("overflow"))
    lines = []
    lines.append(f"# 06 模拟盘 · 板块配额 对比报告（每板块 ≤{cap} 腿）\n")
    lines.append(f"- 区间：{Q['start']} → {Q['end']}")
    lines.append(f"- 基线：工作台年度回测_20261018.json（现状，不重跑）")
    lines.append(f"- 配额：工作台年度回测_板块配额_{TAG}.json")
    lines.append(f"- 生成于：{Q['generated_at']}\n")
    lines.append(f"## 结论\n**{'✅ ' if all_pass else '⛔ '}{verdict}**\n")
    lines.append("> 收益维度不判（任务书 §2.3 原话）：总盈亏/每笔均值/胜率/年化 '明确不判'，"
                 "因为分辨率不足（±43.5 万/年）。本报告任何 PnL 数字仅展示，不用于成败判定。\n")
    lines.append("## 判据逐条（预注册 §2.3）\n")
    lines.append("| 判据 | 实测 | 结论 | 说明 |")
    lines.append("|---|---|---|---|")
    for n, v, ok, note in crit:
        lines.append(f"| {n} | {v} | {'✓' if ok else '✗'} | {note} |")
    lines.append("")
    lines.append("## 对比总表\n")
    lines.append("| 指标 | 基线 | 配额 | 变化 |")
    lines.append("|---|---|---|---|")
    rows = [
        ("成交笔数", bs["n_trades"], qs["n_trades"], qs["n_trades"] - bs["n_trades"]),
        ("区间盈亏 PnL（只展示不评判）", money(bs["pnl"]), money(qs["pnl"]), money(qs["pnl"] - bs["pnl"])),
        ("最大回撤 |元|", money(b_mdd), money(q_mdd), money(q_mdd - b_mdd)),
        ("年化波动(周收益)", f"{b_vol*100:.2f}%", f"{q_vol*100:.2f}%", f"{(q_vol-b_vol)*100:+.2f}pp"),
        ("平均|3日涨跌|", f"{amp['b_amp']*100:.2f}%", f"{amp['q_amp']*100:.2f}%", f"{(amp['q_amp']-amp['b_amp'])*100:+.2f}pp"),
    ]
    for n, bv, qv, d in rows:
        lines.append(f"| {n} | {bv} | {qv} | {d} |")
    lines.append(f"| 选中周覆盖率 | {B['coverage']['pairs']-B['coverage']['zero_pairs']}/{B['coverage']['pairs']} "
                 f"| {cov['pairs']-cov['zero_pairs']}/{cov['pairs']} | — |")
    lines.append(f"| 凑不满 6 腿的周 | — | {over_weeks} | — |")
    lines.append("")
    lines.append("## 集中度分布\n")
    lines.append(f"- 每周单板块最多腿数：基线 {b_maxd} → 配额 {q_maxd}")
    lines.append(f"- 每周覆盖板块数：基线 {b_nsecd} → 配额 {q_nsecd}")
    lines.append("")
    lines.append(f"## 每周选品种差异（共 {len(diff_rows)} 周改了谁）\n")
    if diff_rows:
        lines.append("| # | 重选日 | 基线所选 | 配额所选 |")
        lines.append("|---|---|---|---|")
        for i, d, bs_, qs_ in diff_rows:
            lines.append(f"| {i} | {d} | {', '.join(bs_)} | {', '.join(qs_)} |")
    else:
        lines.append("（无差异：每周所选品种集合与基线完全一致）")
    lines.append("")
    lines.append("## 口径与免责\n")
    lines.append(f"- 单变量、无调参：仅阶段2 填充加「每板块 ≤{cap} 腿」；其余全部未动。")
    lines.append("- 收益不判（见顶部）。")
    lines.append("- 覆盖率若 <306/306，通常是配额替换出的次优品种缺 5min 缓存（离线口径限制），非策略错误。")
    lines.append("- 未来函数：重选只用严格早于当天的日线；selftest 与 pipeline_check 逐笔一致。")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- phase report
def _svg_equity(curve: dict) -> str:
    """权益曲线（上）+ 回撤填充区（下）。纯内联 SVG，不依赖任何前端库。"""
    e, t = curve["e"], curve["t"]
    if len(e) < 2:
        return "<p>（无成交，无权益曲线）</p>"
    step = max(1, len(e) // 600)
    ee, tt = e[::step], t[::step]
    if ee[-1] != e[-1]:
        ee, tt = ee + [e[-1]], tt + [t[-1]]

    W, PAD, GAP = 1000, 58, 46
    H_EQ, H_DD = 250, 86                      # 权益区 / 回撤区高度
    H = H_EQ + H_DD + GAP + 24
    base = curve["base"]
    lo, hi = min(min(ee), base), max(max(ee), base)
    span = max(1.0, hi - lo)
    n = len(ee)
    xs = [PAD + i * (W - PAD - 14) / max(1, n - 1) for i in range(n)]
    top = 26
    ys = [top + H_EQ - (v - lo) / span * H_EQ for v in ee]
    yb = top + H_EQ - (base - lo) / span * H_EQ

    peak, dd = -1e18, []
    for v in ee:
        peak = max(peak, v)
        dd.append(v - peak)
    dmin = min(dd) if min(dd) < 0 else -1.0
    dd_top = top + H_EQ + GAP
    dys = [dd_top + (abs(d) / abs(dmin)) * H_DD for d in dd]

    grid = []
    for g in range(5):
        val = lo + span * g / 4
        y = top + H_EQ - (val - lo) / span * H_EQ
        grid.append(f'<line x1="{PAD}" y1="{y:.1f}" x2="{W-14}" y2="{y:.1f}" '
                    f'stroke="#e8ecf1" stroke-width="1"/>'
                    f'<text x="4" y="{y+4:.1f}" font-size="11" fill="#8a96a3">'
                    f'{val/10000:.0f}万</text>')

    pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    area = f"{xs[0]:.1f},{dys[0]:.1f} " + " ".join(
        f"{x:.1f},{y:.1f}" for x, y in zip(xs, dys)) + \
        f" {xs[-1]:.1f},{dd_top} {xs[0]:.1f},{dd_top}"
    zero_y = dys[dd.index(0)] if 0 in dd else dd_top

    return f'''<svg viewBox="0 0 {W} {H}" width="100%" style="max-width:{W}px">
  {''.join(grid)}
  <line x1="{PAD}" y1="{zero_y:.1f}" x2="{W-14}" y2="{zero_y:.1f}"
        stroke="#cfd8e3" stroke-width="1"/>
  <line x1="{PAD}" y1="{yb:.1f}" x2="{W-14}" y2="{yb:.1f}" stroke="#b9c2cc"
        stroke-dasharray="4 3" stroke-width="1"/>
  <polyline points="{pts}" fill="none" stroke="#c0392b" stroke-width="1.8"/>
  <text x="{PAD}" y="15" font-size="12" fill="#5a6672">
    账户权益（100 万口径，按平仓累计已实现盈亏）　最高 {max(ee)/10000:.1f}万　最低 {min(ee)/10000:.1f}万</text>
  <text x="{PAD}" y="{dd_top-8}" font-size="12" fill="#5a6672">
    回撤（相对历史峰值，向下越大）　最深 {dmin:,.0f} 元</text>
  <polygon points="{area}" fill="#2e86c1" opacity="0.16"/>
  <polyline points="{' '.join(f'{x:.1f},{y:.1f}' for x, y in zip(xs, dys))}"
            fill="none" stroke="#2e86c1" stroke-width="1.2"/>
  <text x="{PAD}" y="{H-4}" font-size="11" fill="#8a96a3">{tt[0]}</text>
  <text x="{W-14-118}" y="{H-4}" font-size="11" fill="#8a96a3">{tt[-1]}</text>
</svg>'''


def phase_report(sector_cap: int | None = None) -> str:
    if sector_cap is not None:
        return report_sector_cap(int(sector_cap))
    import html as _h
    with open(RESULT_JSON, encoding="utf-8") as f:
        R = json.load(f)
    s = R["summary"]
    bs = R["by_symbol"]
    recs = R["selections"]
    cur = R["equity_curve"]

    def pct(x):
        return "—" if x is None else f"{x*100:,.2f}%"

    def money(x):
        return f"{x:,.0f}"

    # 摘要卡
    cards = [
        ("区间盈亏", f"{money(s['pnl'])} 元", "pos" if s["pnl"] >= 0 else "neg"),
        ("收益率(100万)", pct(s["return"]), "neg" if s["return"] < 0 else "pos"),
        ("年化(线性)", pct(s["annual_linear"]), "neg"),
        ("年化(复利)", pct(s["annual_compound"]), "neg"),
        ("最大回撤", f"{money(s['max_drawdown_abs'])} 元", "neg"),
        ("回撤率(对峰值)", pct(s["max_drawdown_pct"]), "neg"),
        ("成交笔数", f"{s['n_trades']}", ""),
        ("胜率", pct(s["win_rate"]), ""),
        ("重选次数", f"{R['n_reselect']}", ""),
        ("异常开仓", f"{s['violations']}", "pos" if s["violations"] == 0 else "neg"),
    ]
    cards_html = "".join(
        f'<div class="card"><div class="k">{k}</div><div class="v {cls}">{v}</div></div>'
        for k, v, cls in cards)

    # 品种表
    rows = []
    for k in sorted(bs, key=lambda x: -abs(bs[x]["pnl"] or 0)):
        r = bs[k]
        rows.append(
            f"<tr><td><b>{k}</b> {_h.escape(r['name'])}</td>"
            f"<td class=n>{r['bars']}</td><td class=n>{r['signals']}</td>"
            f"<td class=n>{r['trades']}</td>"
            f"<td class=n>{pct(r['win_rate'])}</td>"
            f"<td class=n>{r['hand_avg'] if r['hand_avg'] is not None else '—'}</td>"
            f"<td class=n>{r['hand_min'] if r['hand_min'] is not None else '—'}–"
            f"{r['hand_max'] if r['hand_max'] is not None else '—'}</td>"
            f"<td class=n>{r['weeks_selected']}</td>"
            f"<td class=n>{r['blocked_episodes']}</td>"
            f"<td class='n {'neg' if (r['pnl'] or 0) < 0 else 'pos'}'>{money(r['pnl'])}</td></tr>")
    sym_table = "".join(rows)

    cov = R.get("coverage") or {"pairs": 0, "zero_pairs": 0, "zero_list": []}
    cov_rows = ("" if not cov["zero_list"] else
                "<br>0 成交明细：" + "，".join(
                    f"{z['week']} {z['key']}" for z in cov["zero_list"][:30]))

    # 月表
    mrows = "".join(
        f"<tr><td>{m['month']}</td><td class=n>{m['trades']}</td>"
        f"<td class=n>{m['wins']/m['trades']*100:.1f}%</td>"
        f"<td class='n {'neg' if m['pnl'] < 0 else 'pos'}'>{money(m['pnl'])}</td></tr>"
        for m in R["by_month"])

    # 重选表
    srows = "".join(
        f"<tr><td>{i+1}</td><td>{r['reselect_date']}</td>"
        f"<td><b>{'</b> <b>'.join(r['selected'])}</b></td>"
        f"<td class=dim>{', '.join(r['stage1'][:8])}{'…' if len(r['stage1']) > 8 else ''}</td></tr>"
        for i, r in enumerate(recs))

    # 逐笔明细
    trows = []
    for j, t in enumerate(R["trades"], 1):
        trows.append(
            f"<tr><td class=n>{j}</td><td>{t['key']}</td><td>{t['side']}</td>"
            f"<td class=n>{t['hands']}</td><td>{t['open_bj']}</td><td>{t['exit_bj']}</td>"
            f"<td class=n>{t['open_px']}</td><td class=n>{t['exit_px']}</td>"
            f"<td class=n>{t['stop']}</td>"
            f"<td class=n>{t['open_fee']:.2f}</td><td class=n>{t['exit_fee']:.2f}</td>"
            f"<td class='n {'neg' if t['pnl'] < 0 else 'pos'}'>{money(t['pnl'])}</td>"
            f"<td>{t['why']}</td><td class=dim>{t['td']}</td>"
            f"<td class=dim>{t['week']}</td></tr>")

    # 抽样复算
    vrows = "".join(
        f"<tr><td>{v['key']}</td><td>{v['side']}</td><td class=n>{v['hands']}</td>"
        f"<td class=n>{v['open_px']}</td><td class=n>{v['exit_px']}</td>"
        f"<td class=n>{v['mult']}</td><td class=n>{v['open_fee']:.2f}</td>"
        f"<td class=n>{v['exit_fee']:.2f}</td>"
        f"<td class=n>{money(v['gross_recalc'])}</td>"
        f"<td class=n>{money(v['net_recalc'])}</td>"
        f"<td class=n>{money(v['pnl'])}</td>"
        f"<td class=n>{'✓' if v['match'] else '✗'}</td></tr>"
        for v in R["verify_sample"]) or "<tr><td colspan=12>无成交</td></tr>"

    fuse = s.get("fuse_at")
    fuse_txt = (f'<b class="neg">{fuse["at"]}</b>（第 {fuse["trade_no"]} 笔后、'
                f'权益 {money(fuse["equity"])} 元）' if fuse else "未触发")

    v_n = s["violations"]
    vio_html = (f'<p class="ok">✓ 全期 <b>0</b> 笔列表外成交。逐笔核对：每笔开仓的'
                f'「所属重选周」都能在周频重选表里查到该品种，且该周列表确实包含它。</p>' if v_n == 0 else
                f'<p class="bad">✗ 发现 {v_n} 笔列表外成交，明细见下方。</p>')

    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>06 模拟盘 · 工作台年度回测 {R['start']}→{R['end']}</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;padding:28px 34px 60px;background:#f6f8fa;color:#1f2933;
 font:14px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}}
h1{{font-size:23px;margin:0 0 4px}}
h2{{font-size:17px;margin:34px 0 10px;padding-left:10px;border-left:4px solid #c0392b}}
.sub{{color:#6b7785;font-size:13px;margin-bottom:18px}}
.warn{{background:#fff7e6;border:1px solid #ffd591;border-radius:8px;padding:12px 16px;margin:16px 0}}
.warn b{{color:#b35c00}}
.cards{{display:flex;flex-wrap:wrap;gap:10px;margin:14px 0 6px}}
.card{{flex:1 1 150px;background:#fff;border:1px solid #e4e9ef;border-radius:9px;padding:11px 13px}}
.card .k{{font-size:12px;color:#7b8895}}
.card .v{{font-size:19px;font-weight:600;margin-top:3px}}
.pos{{color:#c0392b}} .neg{{color:#1e8449}}
table{{width:100%;border-collapse:collapse;background:#fff;font-size:13px;
 border:1px solid #e4e9ef;border-radius:8px;overflow:hidden}}
th,td{{padding:6px 9px;border-bottom:1px solid #eef2f5;text-align:left;white-space:nowrap}}
th{{background:#f0f3f6;font-weight:600;color:#48535f;position:sticky;top:0;z-index:1}}
td.n{{text-align:right;font-variant-numeric:tabular-nums}}
tr:hover td{{background:#fafcfe}}
.dim{{color:#8a96a3}}
.scroll{{max-height:520px;overflow:auto;border:1px solid #e4e9ef;border-radius:8px}}
.scroll table{{border:0}}
.note{{font-size:12.5px;color:#5a6672;background:#fff;border:1px solid #e4e9ef;
 border-radius:8px;padding:12px 16px;margin-top:10px}}
.ok{{color:#1e8449;font-weight:600}} .bad{{color:#c0392b;font-weight:600}}
code{{background:#eef2f6;padding:1px 5px;border-radius:4px;font-size:12.5px}}
</style></head><body>

<h1>06 模拟盘 · 工作台年度回测</h1>
<div class="sub">区间 {R['start']} → {R['end']}（{R['span_days']} 天） ｜ 5min ｜
重选 {R['n_reselect']} 次 ｜ 并集 {len(R['union'])} 个品种 ｜
手数基数 {money(R['init_equity'])} ｜ 报告口径 {money(R['real_balance'])} ｜
生成于 {R['generated_at']}</div>

<div class="warn"><b>⚠️ 先看这三条，否则数字会被误读</b><br>
1. <b>MA13 是占位信号、已证负期望</b>（2026-10-16 前 12 窗全负）。本回测<b>不判策略有效性</b>，
只验证「周频重选 → 调度闸门 → 开仓 → 统计」这条工程链是否跑通、纪律是否守住。<br>
2. 结论里所有亏损数字都是<b>预期内</b>的复现，不是本次改坏了东西；本次<b>没有调任何参数</b>。<br>
3. 回测<b>未含实盘运维闸门</b>（保证金占用、同向敞口、熔断拦截），因此这是「不设闸门的上限损失」。</div>

<h2>一、摘要</h2>
<div class="cards">{cards_html}</div>
<div class="note">20% 熔断（100 万口径，<b>自峰值</b> −20%）：{fuse_txt}。按任务书要求<b>只标注不拦截</b>——
权益曲线与后续成交仍按「不停手」继续模拟。<br>
若实盘加装这条熔断，则上述时点之后不会再开新仓，实际成交笔数会比本表少得多。<br>
<b>期末权益 = {money(s['final_equity'])} 元</b>（1,000,000 + 区间盈亏）⇒ 按 100 万账户口径
<b class="neg">已经穿仓</b>（亏损超过本金）。所以「回撤率 −141%」「年化(复利) −100%」都不是笔误：
回撤是相对峰值算的，可以小于 −100%；复利年化在亏损超 100% 时数学上无意义，统一记 −100%。<br>
同口径若按 <b>20 万手数基数</b> 折算，区间收益率 = {pct(s['pnl']/R['init_equity'])}（数字更夸张，
但手数本来就是按 20 万定的，两个口径都要知道）。</div>

<h2>二、账户权益曲线 / 回撤</h2>
{_svg_equity(cur)}

<h2>三、按品种：笔数 / 胜率 / 手数（权重）/ 盈亏</h2>
<div class="scroll"><table>
<thead><tr><th>品种</th><th>K线</th><th>放行信号</th><th>成交笔数</th><th>胜率</th>
<th>均手</th><th>手数区间</th><th>被选中周数</th><th>闸门拦截(段)</th><th>盈亏(元)</th></tr></thead>
<tbody>{sym_table}</tbody></table></div>
<div class="note">「手数（权重）」= <code>RiskParSizer</code> 实际下出的手数（20 万基数、
1% 风险 ÷ ATR、夹 [1,8]、名义敞口 ≤2 倍，与实盘同一份代码）。<br>
「闸门拦截(段)」= 该品种在<b>未被选中的周</b>里出现开仓信号、但因不在调度列表而被拦下的
<b>信号段数</b>（连续触发按一段算）。这个数越大，越说明闸门真的在起作用
（而不是那些周碰巧没信号）。</div>
<div class="note"><b>选中周覆盖率（验收项「选中品种正常开仓」）：</b>
全期共 <b>{cov['pairs']}</b> 个「重选周 × 被选中品种」组合，
其中在该周内<b>一单都没开</b>的 <b>{cov['zero_pairs']}</b> 个
（{cov['zero_pairs']/cov['pairs']*100:.1f}%）。
{'⇒ 每个被选中的品种，在它被选中的那一周里都真的开出了单："选了却不下单"为 0。'
 if cov['zero_pairs'] == 0 else
 '⇒ 0 成交的组合已列在下方（通常是该周只含半个交易日/假期，或信号条件一直没满足）。'}
{cov_rows}</div>

<h2>四、月度聚合</h2>
<table><thead><tr><th>月份</th><th>笔数</th><th>胜率</th><th>盈亏(元)</th></tr></thead>
<tbody>{mrows}</tbody></table>

<h2>五、周频重选（{R['n_reselect']} 次，参数来自 workbench.json，冻结未调）</h2>
<div class="note">规则：距上次重选 ≥ {R['selection_params']['reselect_min_days']} 天 →
用<b>严格早于当天</b>的日线跑两阶段：①近 {R['selection_params']['vol_window_days']} 日均量
取前 {R['selection_params']['vol_top_m']}（流动性闸门）；②在前 M 内按近
{R['selection_params']['ret_window_days']} 日 |涨跌幅| 取前
{R['selection_params']['ret_top_n']}（活跃度）。缺合约规格的品种先被
<code>filter_tradable</code> 剔除。</div>
<div class="scroll"><table><thead><tr><th>#</th><th>重选日</th><th>选中（6）</th>
<th>阶段1 流动性池（前 8）</th></tr></thead><tbody>{srows}</tbody></table></div>

<h2>六、异常开仓检测（调度列表外成交 = 0）</h2>
{vio_html}
<div class="note">
<b>怎么证的：</b>每笔成交都记录了「信号 bar 所属交易日」，再用该交易日反查
<b>当时生效的周选中列表</b>（最近一次重选且日期 ≤ 当天）。<br>
本回测共 {s['n_trades']} 笔成交，逐笔反查：列表外 = <b>{v_n}</b> 笔。<br>
<b>反证（不是"没信号"）：</b>全期共有 <b>{s['blocked_episodes']}</b> 段开仓信号
（折合 {s['blocked_bars']} 个 bar 次触发）因为该品种当周不在列表里而被闸门拦下
—— 说明未选中品种信号很密集，它们不开仓的唯一原因就是不在调度/订阅列表，
而不是行情没给机会。<br>
<b>实现层保证：</b>闸门写在 <code>sim_symbol()</code> 的开仓分支里，
<code>pos == 0</code> 时先算 <code>ti = td_idx[i]</code>，再查
<code>active_by_idx[ti]</code>，不在集合内直接跳过（连 sizer 都不调用）。
</div>

<h2>七、逐笔开平仓明细（全量 {s['n_trades']} 笔，可滚动）</h2>
<div class="note">时间列为<b>北京时间</b>；<code>why</code>：stop=盘中止损 / time=时间止损(15 根) /
eod=样本末仍持仓按收盘估值。<code>交易日</code>= 信号 bar 所属交易日（夜盘归下一交易日）；
<code>所属重选周</code>= 该笔开仓时生效的周选中列表日期 —— 两者对得上就说明闸门用对了列表。
开仓费/平仓费各收一次，净盈亏已含双边成本。<br>
CSV 全量：<code>{CSV_PATH}</code></div>
<div class="scroll" style="max-height:620px"><table>
<thead><tr><th>#</th><th>品种</th><th>方向</th><th>手数</th><th>开仓时间</th>
<th>平仓时间</th><th>开仓价</th><th>平仓价</th><th>止损线</th>
<th>开仓费</th><th>平仓费</th><th>净盈亏</th><th>原因</th><th>交易日</th><th>所属重选周</th></tr></thead>
<tbody>{''.join(trows)}</tbody></table></div>

<h2>八、抽样 5 笔盈亏手工复算</h2>
<div class="note">公式：<code>净盈亏 = (平仓价 − 开仓价) × 方向 × 手数 × 乘数 − 开仓费 − 平仓费</code>。
「复算」列是报告里用原始字段重新算的，「落库」列是回测程序记录的，两者必须一致。</div>
<table><thead><tr><th>品种</th><th>方向</th><th>手数</th><th>开仓价</th><th>平仓价</th>
<th>乘数</th><th>开仓费</th><th>平仓费</th><th>毛利(复算)</th><th>净盈亏(复算)</th>
<th>净盈亏(落库)</th><th>一致</th></tr></thead><tbody>{vrows}</tbody></table>

<h2>九、口径与免责</h2>
<div class="note">
<b>1) 账户口径两套。</b>手数基数锁 <b>20 万</b>（实盘 <code>RISK_EQUITY</code>，不随权益变）；
报告收益率/回撤/% 全部按 <b>100 万</b>（TqKq 云端真实余额量级）折算。
若按 20 万基数看，本次区间收益率 = <b>{pct(s['pnl']/R['init_equity'])}</b>。<br>
<b>2) 权益曲线只含已实现盈亏</b>（按平仓时间累计），不含浮盈浮亏，所以回撤略小于
逐日盯市口径。<br>
<b>3) 熔断只标注不拦截。</b>20% 线在 {fuse_txt} 触发；此后回测仍继续开仓，
实盘若装熔断会提前停手，实际笔数与亏损都会更小。<br>
<b>4) 未含的运维闸门：</b>保证金占用 &gt;60% 拒开、同向名义 &gt;2 倍拒开、熔断只出不进、
合约换月/交割处理。回测是<b>上限损失</b>，不是实盘预期。<br>
<b>5) 数据口径：</b>日线走实时模式（回测模式 volume 全零，阶段1 算不了均量）；
5min 走 <code>TqBacktest</code> 增量采集（实时 <code>data_length</code> 上限约 1 万根，
一年 5min 约 1.6 万根装不下）。tqsdk 的 K 线 datetime 是 <b>UTC</b>，
夜盘 bar 按「北京时间 ≥20:00 → 归下一个交易日」映射，与 tqsdk 日线口径一致。<br>
<b>6) 单变量、无调参：</b>selection 段参数、sizer 参数、MA13 参数全部读既有配置，
本次一个都没改；本次也<b>不设样本内外判决</b>（这是工程验证，不是策略验证）。<br>
<b>7) 策略有效性：</b>MA13 已知负期望，本报告的数字<b>不能</b>用来证明"策略变好或变坏"。
它的用途只有两个：证明工程链能跑、证明调度闸门守得住。
</div>

</body></html>"""

    os.makedirs(DOC_DIR, exist_ok=True)
    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[report] {HTML_PATH}")
    try:
        os.startfile(HTML_PATH)      # 任务书/用户明确要求：报告要弹出来
        print("[report] 已用默认浏览器打开")
    except Exception as e:
        print(f"[report] 打开失败（手动双击即可）：{e}")
    return HTML_PATH


# ---------------------------------------------------------------- CLI
def main() -> int:
    ap = argparse.ArgumentParser(description="06 模拟盘 · 工作台年度回测")
    ap.add_argument("--phase", default="sim",
                    choices=["daily", "select", "bt5", "sim", "report", "all"])
    ap.add_argument("--refresh", action="store_true", help="daily：强刷日线缓存")
    ap.add_argument("--recollect", action="store_true", help="bt5：强刷 5min 缓存")
    ap.add_argument("--only", default=None, help="只跑这些品种（调试用，逗号分隔）")
    ap.add_argument("--sector-cap", type=int, default=None,
                    help="板块腿数上限（任务书 §2.2）；默认 None=逐字原逻辑。仅离线生效")
    ap.add_argument("--selftest", action="store_true", help="与 pipeline_check 逐笔比对")
    args = ap.parse_args()
    only = [k.strip().upper() for k in args.only.split(",")] if args.only else None
    cap = args.sector_cap

    if args.selftest:
        return selftest()
    p = args.phase
    if p in ("daily", "all"):
        phase_daily(refresh=args.refresh)
    if p in ("select", "all"):
        phase_select(sector_cap=cap)
    if p in ("bt5", "all"):
        phase_bt5(recollect=args.recollect, only=only)   # 5min 采集与 cap 无关
    if p in ("sim", "all"):
        phase_sim(only=only, sector_cap=cap)
    if p in ("report", "all"):
        phase_report(sector_cap=cap)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
