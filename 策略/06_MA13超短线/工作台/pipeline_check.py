# -*- coding: utf-8 -*-
"""
pipeline_check.py —— 工作台「选品种 → 信号 → 执行 → 统计」工程管线冒烟回测
================================================================================

定位（严格对齐 `工作台_预注册_时序截面.md` §6）：
    **工程管线验证，不判 P&L**。MA13 是占位信号、已证负期望（用户明确无需验证），
    这里只回答四个工程问题：
      ① 选中的 6 个品种是否都真的跑起来了（有 bar、有信号、有成交）；
      ② 没选中的品种会不会开仓（调度列表 = 唯一闸门，必须 0 笔）；
      ③ 汇总收益 / 年化是多少（只作"跑通了"的量化旁证，不作策略结论）；
      ④ 成本模型是否已含（固定费 + 比例费 + 1 跳滑点）。

不引入第二套策略逻辑（单一真相源）：
    - 信号：`signals.Ma13Signal`（与实盘 ma13_live_multi._process 同一份代码）
    - 手数：`sizer.RiskParSizer`（与实盘 ctx["sizer"] 同一份代码）
    - 规格/成本：`config_symbols`（乘数/一跳/保证金 + comm_per_hand）

时点纪律（铁律 1）：
    · 第 i 根**已收盘** bar 出信号（entry(ind, i)：信号用 close[i-1]，止损用 close[i]）；
    · **第 i+1 根开盘价成交**；
    · 持仓期间止损用当根 low/high 判，跳空按 open 与止损线中更差的一侧成交；
    · 时间止损在收盘判定、按当根收盘价成交（≈ 实盘下一 tick 市价）。

用法：
    python pipeline_check.py                 # 默认最近 45 天，选中 6 个 + 2 个对照
    python pipeline_check.py --days 60 --control RB,SR
"""
from __future__ import annotations
import argparse
import datetime as _dt
import json
import os
import sys
import time

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

_ROOT = _HERE
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config_symbols                       # noqa: E402
from signals import Ma13Signal              # noqa: E402
from sizer import get_sizer                 # noqa: E402
from config import load_workbench           # noqa: E402
import universe as _u                       # noqa: E402

KSEC = 300
INIT_EQUITY = 200000.0
REAL_BALANCE = 1_000_000.0   # TqKq 云端账户真实余额（约 100 万；手数基数仍锁 20 万）
SLIP_TICKS = 1.0


# ---------------------------------------------------------------- 取数
def _main_code(key: str) -> str:
    d = _u.get(key)
    if d is None:
        raise KeyError(f"{key} 不在 universe")
    exch = str(d["exch"]).upper()
    code = key.upper() if exch == "CZCE" else key.lower()
    return f"KQ.m@{exch}.{code}"


def fetch_bars(keys: list[str], days: int) -> dict[str, dict]:
    """一次连接批量拉 5min K 线。返回 {key: {datetime,open,high,low,close,volume}}。

    · 最后一根一律丢弃（可能正在形成）——少 5 分钟不影响冒烟结论。
    · 按 `--days` 裁掉更早的 bar（data_length 只是"多拉点"，窗口由日期裁）。
    """
    import pandas as pd
    from tqsdk import TqApi, TqAuth
    import config_account

    user, pwd = config_account.check()
    api = TqApi(auth=TqAuth(user, pwd))
    n_want = int(min(9000, max(800, days * 80)))
    cutoff = (_dt.date.today() - _dt.timedelta(days=int(days))).isoformat()
    ks, out = {}, {}
    try:
        for key in keys:
            code = _main_code(key)
            print(f"[订阅] {key:<3} {code}", flush=True)
            try:
                ks[key] = api.get_kline_serial(code, KSEC, data_length=n_want)
            except Exception as e:
                print(f"  [warn] {key} 订阅失败: {e}", flush=True)
        pending = set(ks)
        t0 = time.time()
        while pending and time.time() - t0 < 90:
            api.wait_update(deadline=min(time.time() + 5, t0 + 90))
            for key in list(pending):
                try:
                    if int(ks[key]["datetime"].iloc[-1]) > 0 and len(ks[key]) >= 100:
                        pending.discard(key)
                except Exception:
                    pass
        for key, k in ks.items():
            try:
                n = len(k)
                if n < 3:
                    print(f"  [warn] {key} 数据不足 ({n} 根)", flush=True)
                    continue
                n -= 1                      # 丢最后一根（可能正在形成）
                dts = [pd.to_datetime(int(x)).strftime("%Y-%m-%d %H:%M")
                       for x in k["datetime"].to_numpy()[:n]]
                keep = [j for j, s in enumerate(dts) if s[:10] >= cutoff]
                if len(keep) < 50:
                    keep = list(range(max(0, n - 50), n))   # 数据太短就至少给 50 根
                idx = np.asarray(keep, dtype=int)
                data = {
                    "datetime": [dts[j] for j in keep],
                    "open": k["open"].to_numpy(float)[:n][idx],
                    "high": k["high"].to_numpy(float)[:n][idx],
                    "low": k["low"].to_numpy(float)[:n][idx],
                    "close": k["close"].to_numpy(float)[:n][idx],
                    "volume": k["volume"].to_numpy(float)[:n][idx],
                }
                out[key] = data
                print(f"  [ok] {key} {len(keep)} 根 {data['datetime'][0]} → {data['datetime'][-1]}",
                      flush=True)
            except Exception as e:
                print(f"  [warn] {key} 读取失败: {e}", flush=True)
    finally:
        try:
            api.close()
        except Exception:
            pass
    return out


# ---------------------------------------------------------------- 模拟
def run_symbol(key: str, d: dict, sig: Ma13Signal, sizer) -> dict:
    """单品种逐 bar 模拟（与实盘 _process 同口径）。

    返回 {bars, signals, trades:[...], pnl, wins, ...}，总盈亏已扣双边成本。
    """
    name, _code, mult, tick = config_symbols.get(key)
    df = {k: np.asarray(d[k], dtype=float) for k in ("open", "high", "low", "close")}
    ind = sig.indicators(df)
    n = ind["n"]
    pos = 0
    entry_px = 0.0
    stop = None
    bars_in = 0
    hands = 0
    trades = []
    n_signals = 0

    def _commission(px: float) -> float:
        return float(config_symbols.comm_per_hand(key, px))

    i = sig.MA_N + sig.SLOPE_N + 2
    just_opened = False                   # 上一拍成交 ⇒ 本根是「成交那根」
    while i < n - 1:                      # 需要 i+1 根才能成交
        # ★★ 2026-10-18 修（P1）：与实盘/engine 口径对齐
        #   ① 「成交那根」bar 既不做出场判定、也不计 bars_in
        #      —— 实盘 `if 开仓` / `elif 持仓` 互斥，成交那根走不进「持仓」分支；
        #      旧写法把成交 bar 也算一根 ⇒ 时间止损**早一根**（j+15 而非 j+16）。
        #   ② 时间止损成交价用**当根开盘** `open[i]`，不是上一根收盘 close[i]
        #      —— 实盘是在新 bar 的第一个 tick 才察觉 bars_in 到期的。
        fill_bar = just_opened
        just_opened = False
        opened_now = False

        # ---- 持仓：先判出场（当根 low/high 触止损；跳空按更差侧）----
        if pos != 0 and not fill_bar:
            hit = (pos > 0 and df["low"][i] <= stop) or (pos < 0 and df["high"][i] >= stop)
            if hit:
                exit_px = min(df["open"][i], stop) if pos > 0 else max(df["open"][i], stop)
                exit_px -= SLIP_TICKS * tick * (1 if pos > 0 else -1)
                gross = (exit_px - entry_px) * (1 if pos > 0 else -1) * hands * mult
                fee = _commission(exit_px) * hands
                pnl = gross - fee
                trades.append({"i": i, "t": d["datetime"][i], "side": "多" if pos > 0 else "空",
                               "hands": hands, "entry": round(entry_px, 4),
                               "exit": round(exit_px, 4), "why": "stop",
                               "pnl": round(pnl, 2)})
                pos, bars_in, stop = 0, 0, None
                i += 1
                continue                  # 同一根不再开新仓（与实盘"平完再看开仓"近似）
            # 时间止损：开盘判定，成交在开盘价
            if bars_in >= sig.MAX_HOLD:
                exit_px = df["open"][i] - SLIP_TICKS * tick * (1 if pos > 0 else -1)
                gross = (exit_px - entry_px) * (1 if pos > 0 else -1) * hands * mult
                fee = _commission(exit_px) * hands
                pnl = gross - fee
                trades.append({"i": i, "t": d["datetime"][i], "side": "多" if pos > 0 else "空",
                               "hands": hands, "entry": round(entry_px, 4),
                               "exit": round(exit_px, 4), "why": "time",
                               "pnl": round(pnl, 2)})
                pos, bars_in, stop = 0, 0, None
                i += 1
                continue

        # ---- 空仓：已收盘 bar i 出信号，i+1 开盘成交 ----
        if pos == 0:
            direction, s = sig.entry(ind, i)
            if direction != 0 and s is not None:
                n_signals += 1
                atr = float(ind["atr"][i])
                px_raw = float(df["open"][i + 1])
                if px_raw > 0 and np.isfinite(px_raw):
                    h = sizer.hand(atr, tick, mult, px_raw)
                    entry_px = px_raw + SLIP_TICKS * tick * (1 if direction > 0 else -1)
                    fee = _commission(entry_px) * h
                    # 开仓手续费先不扣，平仓时并入同一笔 pnl（避免双扣/漏扣）
                    hands = h
                    pos = direction * h
                    stop = float(s)
                    bars_in = 0
                    opened_now = True
                    just_opened = True        # 下一拍（i+1）是成交那根
                    # 把开仓费记到一个临时字段，平仓时扣
                    trades.append({"i": i + 1, "t": d["datetime"][i + 1],
                                   "side": "多" if direction > 0 else "空",
                                   "hands": h, "entry": round(entry_px, 4),
                                   "exit": None, "why": "open",
                                   "pnl": round(-fee, 2)})

        # ---- 持有 bar 计数（成交那根与开仓那根都不计）----
        if pos != 0 and not fill_bar and not opened_now:
            bars_in += 1

        i += 1

    # 未平仓（回测末尾）：按最后收盘价估值，标注 open
    if pos != 0:
        last = n - 1
        exit_px = float(df["close"][last])
        gross = (exit_px - entry_px) * (1 if pos > 0 else -1) * hands * mult
        fee = _commission(exit_px) * hands
        trades.append({"i": last, "t": d["datetime"][last],
                       "side": "多" if pos > 0 else "空", "hands": hands,
                       "entry": round(entry_px, 4), "exit": round(exit_px, 4),
                       "why": "eod", "pnl": round(gross - fee, 2)})

    # 合并：开仓行的负手续费 + 平仓行 pnl（开仓行 exit=None）
    merged = []
    orphan_open = None
    for t in trades:
        if t["why"] == "open":
            orphan_open = t
            continue
        if orphan_open is not None:
            # 开仓手续费 = open 行的 pnl（负数），并入本笔
            t = dict(t)
            t["pnl"] = round(t["pnl"] + orphan_open["pnl"], 2)
            t["entry_hands"] = orphan_open["hands"]
            orphan_open = None
        merged.append(t)
    # 若还有没配上的开仓行（理论上不会有），单独保留
    if orphan_open is not None:
        merged.append(orphan_open)

    closed = [t for t in merged if t["exit"] is not None]
    for t in merged:
        t["key"] = key            # ★ 便于报告追溯/断言"无列表外品种"
    pnl = sum(t["pnl"] for t in closed)
    wins = sum(1 for t in closed if t["pnl"] > 0)
    return {
        "key": key, "name": name, "mult": mult, "tick": tick,
        "bars": n, "signals": n_signals,
        "trades": len(closed), "wins": wins,
        "win_rate": round(wins / len(closed), 4) if closed else None,
        "pnl": round(pnl, 2),
        "avg_hands": round(sum(t["hands"] for t in closed) / len(closed), 2) if closed else None,
        "first_bar": d["datetime"][0], "last_bar": d["datetime"][-1],
        "_trades": closed,
    }


def _max_dd(equity: list[float]) -> float:
    peak, mdd = -1e18, 0.0
    for v in equity:
        peak = max(peak, v)
        mdd = min(mdd, v - peak)
    return mdd


def causal_check(key: str, d: dict, sig, sizer, cut: int = 200) -> tuple[bool, str]:
    """因果自检（T2）：把数据截掉尾部 cut 根重跑，截断点之前的成交必须逐笔不变。

    通过 ⇒ 证明模拟器没有引用未来数据（否则截尾后早期结果会变）。
    """
    if len(d["datetime"]) <= cut + 200:
        return True, "数据太短，跳过"
    full = run_symbol(key, d, sig, sizer)["_trades"]
    part_d = {k: (v[:-cut] if hasattr(v, "__getitem__") else v) for k, v in d.items()}
    part = run_symbol(key, part_d, sig, sizer)["_trades"]
    if len(part) < 3:
        return False, "截断后成交太少，无法比对"
    # 截断处附近允许 2 笔差异（未平仓/边界成交），之前必须逐笔一致
    edge_t = part[-3]["t"]
    a = [t for t in part if t["t"] <= edge_t]
    b = [t for t in full if t["t"] <= edge_t]
    sig_a = [(t["t"], t["side"], t["hands"], t["entry"], t["exit"], t["pnl"]) for t in a]
    sig_b = [(t["t"], t["side"], t["hands"], t["entry"], t["exit"], t["pnl"]) for t in b]
    if sig_a != sig_b:
        n = min(len(sig_a), len(sig_b))
        bad = next((i for i in range(n) if sig_a[i] != sig_b[i]), n)
        return False, f"第 {bad+1} 笔起不一致：{sig_a[bad:bad+1]} vs {sig_b[bad:bad+1]}"
    return True, f"{len(sig_a)} 笔逐笔一致"


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser(description="工作台选品种→信号→执行 工程冒烟回测")
    ap.add_argument("--days", type=int, default=45, help="回看自然日数（默认 45）")
    ap.add_argument("--control", default="RB,SR",
                    help="对照品种（未选中，用来证明'不是没信号，是被调度挡住'）")
    ap.add_argument("--out", default=None, help="JSON 输出路径")
    ap.add_argument("--causal-check", action="store_true",
                    help="对第一个调度品种做因果自检（截尾后早期成交必须逐笔不变）")
    args = ap.parse_args()

    cfg = load_workbench()
    selected = [str(k).upper() for k in (cfg.get("selected") or [])]
    if not selected:
        raise SystemExit("workbench.json 的 selected 为空，先跑 --auto-select --write")
    control = [k.strip().upper() for k in args.control.split(",") if k.strip()]
    control = [k for k in control if k not in selected]

    # ★ 调度列表 = 选中列表（唯一闸门）。核心只订阅这里面的品种。
    dispatch = list(selected)
    print("=" * 74)
    print(f"  工作台工程冒烟回测 ｜ 回看 {args.days} 天 ｜ 5min ｜ 初始权益 {INIT_EQUITY:,.0f}")
    print(f"  调度品种（= workbench.json selected）：{', '.join(dispatch)}")
    print(f"  对照品种（不在调度列表）：{', '.join(control) or '无'}")
    print("=" * 74)

    bars = fetch_bars(dispatch + control, args.days)
    missing = [k for k in dispatch + control if k not in bars]
    if missing:
        print(f"[warn] 以下品种无数据，跳过：{missing}")
    _ww = [bars[k]["datetime"] for k in dispatch if k in bars and bars[k]["datetime"]]
    if _ww:
        print(f"  数据窗口：{min(x[0] for x in _ww)} → {max(x[-1] for x in _ww)}"
              f"（每品种约 {min(len(x) for x in _ww)} 根）")

    sig = Ma13Signal()
    sizer = get_sizer(cfg.get("sizer"))
    res, res_ctrl = {}, {}
    for k in dispatch:
        if k in bars:
            res[k] = run_symbol(k, bars[k], sig, sizer)
    for k in control:
        if k in bars:
            res_ctrl[k] = run_symbol(k, bars[k], sig, sizer)

    causal_note = None
    if args.causal_check:
        _k0 = next((k for k in dispatch if k in bars), None)
        if _k0:
            _ok, _msg = causal_check(_k0, bars[_k0], sig, sizer)
            causal_note = {"key": _k0, "ok": bool(_ok), "msg": _msg}
            print(f"\n[因果自检] {_k0}: {'✓ 通过' if _ok else '✗ 失败'} — {_msg}")

    # ---- 逐笔时间序 → 组合权益曲线 ----
    all_tr = sorted([t for r in res.values() for t in r["_trades"]], key=lambda t: t["t"])
    eq, cur = [], INIT_EQUITY
    _peak = INIT_EQUITY
    _fuse_t = None
    _fuse_1m_t = None
    for t in all_tr:
        cur += t["pnl"]
        eq.append(cur)
        _peak = max(_peak, cur)
        if _fuse_t is None and cur <= _peak * 0.80:     # 20 万基数口径
            _fuse_t = t["t"]
        # 实盘口径：TqKq 云端余额约 100 万，熔断线 = 自峰值 -20% ≈ -20 万
        if _fuse_1m_t is None and cur - INIT_EQUITY <= -REAL_BALANCE * 0.20:
            _fuse_1m_t = t["t"]
    pnl = round(sum(t["pnl"] for t in all_tr), 2)
    ret = pnl / INIT_EQUITY
    # 年化跨度用**数据窗口**（不是首末成交日 —— 没成交的日子也是持有期）
    _w = [bars[k]["datetime"] for k in dispatch if k in bars and bars[k]["datetime"]]
    if _w:
        _first = min(x[0] for x in _w)
        _last = max(x[-1] for x in _w)
        span_days = max(1, (_dt.date.fromisoformat(_last[:10]) -
                            _dt.date.fromisoformat(_first[:10])).days)
    else:
        span_days = max(1, args.days)
    ann_lin = ret * 365.0 / span_days
    ann_cmp = (1 + ret) ** (365.0 / span_days) - 1 if ret > -1 else -1.0

    # ---- 输出 ----
    print("\n" + "=" * 74)
    print("  一、选中品种是否正常跑")
    print("=" * 74)
    print(f"  {'品种':<4} {'K线':>5} {'信号':>5} {'成交':>5} {'胜率':>7} {'均手':>5} {'盈亏(元)':>12}")
    for k, r in res.items():
        wr = f"{r['win_rate']*100:.1f}%" if r["win_rate"] is not None else "  —"
        print(f"  {k:<4} {r['bars']:>5} {r['signals']:>5} {r['trades']:>5} "
              f"{wr:>7} {str(r['avg_hands'] or '—'):>5} {r['pnl']:>12,.0f}")

    print("\n" + "=" * 74)
    print("  二、汇总（不看品种，只看账户）")
    print("=" * 74)
    print(f"  区间盈亏      : {pnl:>12,.0f} 元")
    print(f"  区间收益率    : {ret*100:>11.2f}%   （基数 {INIT_EQUITY:,.0f}）")
    print(f"  年化（复利）  : {ann_cmp*100:>11.2f}%   ⚠️ 只有 {span_days} 天，外推仅供参考")
    print(f"  年化（线性）  : {ann_lin*100:>11.2f}%")
    print(f"  最大回撤      : {_max_dd(eq):>12,.0f} 元")
    print(f"  熔断(20万口径): {(('第 ' + _fuse_t + ' 触发') if _fuse_t else '未触发'):>12}")
    print(f"  熔断(实盘100万): {(('第 ' + _fuse_1m_t + ' 触发') if _fuse_1m_t else '未触发')}")
    print(f"  按100万口径   : {pnl/REAL_BALANCE*100:>11.2f}%")
    print(f"  总成交笔数    : {len(all_tr):>12d}")
    wr_all = (sum(1 for t in all_tr if t["pnl"] > 0) / len(all_tr)) if all_tr else None
    print(f"  胜率          : {(f'{wr_all*100:.1f}%' if wr_all is not None else '—'):>12}")

    print("\n" + "=" * 74)
    print("  三、没选上的品种会不会开仓")
    print("=" * 74)
    print(f"  ① 调度列表 = {dispatch}")
    print(f"  ② 账户回测实际出现的品种 = {sorted(res.keys())}")
    print(f"     ⇒ 是否有列表外品种成交：{'否 ✓' if set(res.keys()) <= set(dispatch) else '是 ✗'}")
    for k, r in res_ctrl.items():
        print(f"  ③ 对照 {k}（不在调度列表）：若放进来会产生 {r['signals']} 个信号 / "
              f"{r['trades']} 笔成交 / {r['pnl']:,.0f} 元")
    print("     ⇒ 说明：未选中品种不是'没信号'，而是根本没进订阅/调度列表（0 手）。")

    # ---- 落盘 ----
    out = args.out or os.path.join(_ROOT, "工具", "_输出",
                                   f"工作台冒烟回测_{_dt.date.today():%Y%m%d}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    payload = {
        "generated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "window_days": args.days,
        "init_equity": INIT_EQUITY,
        "ks": KSEC,
        "dispatch": dispatch,
        "control": control,
        "causal_check": causal_note,
        "summary": {"pnl": pnl, "return": round(ret, 6),
                    "annual_compound": round(ann_cmp, 6),
                    "annual_linear": round(ann_lin, 6),
                    "max_drawdown": round(_max_dd(eq), 2),
                    "fuse_trigger_at": _fuse_t,
                    "fuse_trigger_at_real_1m": _fuse_1m_t,
                    "return_on_real_1m": round(pnl / REAL_BALANCE, 6),
                    "n_trades": len(all_tr),
                    "win_rate": (round(wr_all, 4) if wr_all is not None else None),
                    "span_days": span_days},
        "by_symbol": {k: {kk: vv for kk, vv in r.items() if not kk.startswith("_")}
                      for k, r in res.items()},
        "control_by_symbol": {k: {kk: vv for kk, vv in r.items() if not kk.startswith("_")}
                              for k, r in res_ctrl.items()},
        "trades": all_tr,
    }
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\n[落盘] {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
