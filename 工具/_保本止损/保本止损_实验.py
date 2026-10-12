# -*- coding: utf-8 -*-
"""
06 MA13 超短线 · 「保本止损」单变量实验（预注册卡：预注册卡_保本止损_06MA13.md）
================================================================================
唯一改动（相对 策略/06_MA13超短线/回测/<品种>/MA13超短线_回测.py）：
    新增 BREAKEVEN_ATR —— 持仓期间「最大有利偏移 best_fav」≥ BREAKEVEN_ATR × 开仓时ATR
    后，把止损线移到「开仓成交价」（多单上抬、空单下压），此后不再移动。
    其余每一行（信号/手数/成本/跳空成交/时间止损/记账）与 06 原脚本**逐字一致**。

口径来源：lib/engine.py `_dual_ma_stop_line()`（2026-09-18「阶段B」），
    用【已收盘】K 线极值维护 best_fav；移到开仓基准价为止，不含跟踪止损。

禁未来函数：
    信号用第 i-1 根已收盘 bar，第 i 根开盘成交；
    best_fav 在本根（第 i 根）**处理完之后**用 high[i]/low[i] 更新 ⇒ 只影响第 i+1 根及以后。

硬校验（跑完自动断言）：
    ① 记账对账：equity == INIT + Σpnl（继承 06 原脚本）
    ② 保本组（出场原因=「保本止损」）逐笔净盈亏必须 < 0 ——
       保本线在入场价不利侧，成交价 ≤ 入场价（多）/ ≥ 入场价（空）⇒ 毛盈亏 ≤ 0，扣成本后必为负。
       若出现 ≥0 即为「落有利侧凭空盈利」的资金曲线bug（项目血泪条款）。
    ③ 保本线必须夹紧入场价不利侧。

用法（环境变量驱动）：
    MA13_SYMBOL=JM MA13_BREAKEVEN=1.0 python 保本止损_实验.py
"""
import sys
import os
import json
import time
import math

import numpy as np
import pandas as pd

# ---- 找项目根（向上直到含 lib 的目录）----
_ROOT = os.path.dirname(os.path.abspath(__file__))
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from tqsdk import TqApi, TqSim, TqBacktest, TqAuth
import config_account
import config_symbols

# ==================== 参数（与 06 原脚本逐字一致，除 BREAKEVEN_ATR）====================
SYMBOL_KEY = os.environ.get("MA13_SYMBOL", "JM").upper()
KSEC       = 300
MA_N       = 13
SLOPE_N    = 5
ATR_N      = 14
RETRACE_TOL = 0.5
STOP_BUFFER = 0.5
STOP_CAP    = 2.0
MAX_HOLD    = 15
RISK_PCT    = 0.01
SLIP        = 1
HAND_MIN    = 1
HAND_MAX    = 8
NOTIONAL_CAP = 2.0
INIT        = 200000.0
GAP_FILL   = True
COMPOUND   = False

# ★★ 本实验的**唯一**自变量（其余全不动）
BREAKEVEN_ATR = float(os.environ.get("MA13_BREAKEVEN", "0.0"))

START = pd.Timestamp(os.environ.get("MA13_START", "2021-01-01"))
END   = pd.Timestamp(os.environ.get("MA13_END", "2023-12-31"))
WARMUP_DAYS = 7

name_cn, main_code, MULT, TICK = config_symbols.get(SYMBOL_KEY)
COMM = float(config_symbols.COST[SYMBOL_KEY])

PRINCIPAL = 200000.0


def _base_hand(a, equity):
    if math.isnan(a) or a <= 0:
        return HAND_MIN
    if equity is None or math.isnan(equity) or equity <= 0:
        equity = PRINCIPAL
    h = int(round((equity * RISK_PCT) / (a * TICK * MULT)))
    return max(HAND_MIN, min(HAND_MAX, h))


def hand_plan(a, equity, price=None):
    equity = PRINCIPAL if not COMPOUND else equity
    h = _base_hand(a, equity)
    eq = equity if (equity and not math.isnan(equity) and equity > 0) else PRINCIPAL
    if price and price > 0 and not math.isnan(price):
        名义手数 = int((eq * NOTIONAL_CAP) / (price * MULT))
        h = min(h, max(HAND_MIN, 名义手数))
    return h


def run_backtest(start=None, end=None):
    _START = start if start is not None else START
    _END = end if end is not None else END
    user, pwd = config_account.check()
    bt_start = _START - pd.Timedelta(days=WARMUP_DAYS)
    api = TqApi(TqSim(INIT), backtest=TqBacktest(bt_start, _END), auth=TqAuth(user, pwd))
    k = api.get_kline_serial(main_code, KSEC, data_length=3000)
    print(f"[info] {name_cn}({SYMBOL_KEY}) BREAKEVEN_ATR={BREAKEVEN_ATR:g} 拉取 K 线...", flush=True)
    t0 = time.time()

    c_list, o_list, h_list, l_list, d_list = [], [], [], [], []
    last_ns = 0
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
                d_list.append(dts[j]); c_list.append(cl[j]); o_list.append(op[j])
                h_list.append(hi[j]); l_list.append(lo[j]); last_ns = dts[j]
        if last_ns > 0 and pd.Timestamp(last_ns) >= _END:
            break
        if time.time() - t0 > 1500:
            print("[warn] 超时强制退出", flush=True)
            break

    close = np.array(c_list, float)
    open_ = np.array(o_list, float)
    high = np.array(h_list, float)
    low = np.array(l_list, float)
    dts_arr = np.array(d_list, int)
    N = len(close)
    print(f"[info] 收集到 K 线 {N} 根，覆盖 {pd.Timestamp(d_list[0])} ~ {pd.Timestamp(d_list[-1])}", flush=True)

    prev_close = np.roll(close, 1); prev_close[0] = close[0]
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    atr = np.full(N, np.nan)
    for i in range(1, N):
        atr[i] = np.mean(tr[max(1, i - ATR_N):i]) if i >= 2 else tr[i]

    ma13 = np.full(N, np.nan)
    for i in range(MA_N, N):
        ma13[i] = np.mean(close[i - MA_N:i])

    # ---------- 回测循环（与 06 原脚本同结构；★ 仅新增 best_fav / 保本线三处）----------
    equity = INIT
    eq_mark = INIT
    peak = INIT
    pos = 0
    entry_price = 0.0
    stop_price = 0.0
    cur_dir = 0.0
    cur_entry_price = 0.0
    cur_entry_dt = 0
    cur_hand = 0
    cur_open_cost = 0.0
    bars_in = 0
    # ★★ 本实验新增状态
    cur_entry_atr = 0.0     # 开仓那一刻锁定的 ATR（与 06 止损同刻度）
    best_fav = 0.0          # 持仓期间最大有利偏移（点），只用已收盘极值更新
    be_armed = False        # 是否已把止损移到保本位
    trades = 0
    wins = 0
    prev_eq = INIT
    rets = []
    bar_dds = []
    eq_series = []
    dt_series = []
    trades_rec = []
    blown = False
    comm_total = 0.0

    start_i = MA_N + SLOPE_N + 1
    start_ns = _START.value

    for i in range(start_i, N):
        if blown:
            break
        in_window = dts_arr[i] >= start_ns

        if in_window:
            slope = ma13[i - 1] - ma13[i - 1 - SLOPE_N]
            dir_trend = 1.0 if slope > 0 else (-1.0 if slope < 0 else 0.0)
            a = atr[i - 1]
            touch = abs(close[i - 1] - ma13[i - 1]) <= RETRACE_TOL * a

            exit_now = False
            exit_price = None
            reason = ""
            if pos > 0:
                if low[i] <= stop_price:
                    exit_now = True
                    exit_price = min(stop_price, open_[i]) if GAP_FILL else stop_price
                    reason = "保本止损" if be_armed else "止损"
                elif bars_in >= MAX_HOLD:
                    exit_now = True; exit_price = open_[i]; reason = "时间止损"
            elif pos < 0:
                if high[i] >= stop_price:
                    exit_now = True
                    exit_price = max(stop_price, open_[i]) if GAP_FILL else stop_price
                    reason = "保本止损" if be_armed else "止损"
                elif bars_in >= MAX_HOLD:
                    exit_now = True; exit_price = open_[i]; reason = "时间止损"

            if exit_now and pos != 0:
                exit_cost = SLIP * TICK * MULT * abs(pos) + COMM * abs(pos)
                pnl = pos * (exit_price - entry_price) * MULT - cur_open_cost - exit_cost
                equity += pnl
                eq_mark = equity
                comm_total += COMM * abs(pos)
                trades += 1
                if pnl > 0:
                    wins += 1
                trades_rec.append({
                    "dir": "多" if cur_dir > 0 else "空",
                    "entry_dt": pd.Timestamp(cur_entry_dt).strftime("%Y-%m-%d %H:%M"),
                    "exit_dt": pd.Timestamp(dts_arr[i]).strftime("%Y-%m-%d %H:%M"),
                    "entry": round(cur_entry_price, 2),
                    "exit": round(exit_price, 2),
                    "hand": cur_hand,
                    "pnl": round(pnl, 2),
                    "reason": reason,
                    "best_fav_pts": round(best_fav, 2),
                    "entry_atr": round(cur_entry_atr, 2),
                })
                pos = 0; bars_in = 0; cur_open_cost = 0.0
                best_fav = 0.0; be_armed = False

            if pos == 0 and dir_trend != 0 and touch:
                hand = hand_plan(a, equity, open_[i])
                entry_price = open_[i]
                oc = SLIP * TICK * MULT * hand + COMM * hand
                if dir_trend > 0:
                    swing_low = np.min(low[max(0, i - SLOPE_N):i])
                    stop_raw = swing_low - STOP_BUFFER * a
                    stop_price = max(entry_price - STOP_CAP * a, stop_raw)
                    if stop_price >= entry_price - TICK:
                        stop_price = entry_price - STOP_CAP * a
                    pos = hand
                else:
                    swing_high = np.max(high[max(0, i - SLOPE_N):i])
                    stop_raw = swing_high + STOP_BUFFER * a
                    stop_price = min(entry_price + STOP_CAP * a, stop_raw)
                    if stop_price <= entry_price + TICK:
                        stop_price = entry_price + STOP_CAP * a
                    pos = -hand
                cur_open_cost = oc
                eq_mark = equity
                comm_total += COMM * hand
                bars_in = 0
                cur_dir = dir_trend
                cur_entry_price = entry_price
                cur_entry_dt = dts_arr[i]
                cur_hand = hand
                # ★★ 新增：锁定开仓 ATR，重置保本状态
                cur_entry_atr = a
                best_fav = 0.0
                be_armed = False
            elif pos != 0:
                bars_in += 1

            # ★★ 新增（唯一逻辑增量）：用【已收盘】的本根极值更新最大有利偏移，
            #    达标则把止损线移到开仓成交价（此后不再动）。只影响下一根及以后。
            if pos != 0:
                fav = (high[i] - cur_entry_price) if pos > 0 else (cur_entry_price - low[i])
                if fav > best_fav:
                    best_fav = fav
                if BREAKEVEN_ATR > 0 and (not be_armed) and best_fav >= BREAKEVEN_ATR * cur_entry_atr:
                    if pos > 0:
                        stop_price = max(stop_price, cur_entry_price)
                        assert stop_price <= cur_entry_price + 1e-9, "保本线必须在入场价不利侧(多)"
                    else:
                        stop_price = min(stop_price, cur_entry_price)
                        assert stop_price >= cur_entry_price - 1e-9, "保本线必须在入场价不利侧(空)"
                    be_armed = True

        if pos != 0:
            eq_mark = equity + pos * (close[i] - cur_entry_price) * MULT
        else:
            eq_mark = equity
        if eq_mark <= 0:
            blown = True
            break
        peak = max(peak, eq_mark)
        dd = (peak - eq_mark) / peak if peak > 0 else 0.0
        bar_dds.append(dd)
        eq_series.append(eq_mark)
        dt_series.append(dts_arr[i])
        if prev_eq != 0:
            rets.append((eq_mark - prev_eq) / prev_eq)
        prev_eq = eq_mark

    # ---------- 硬校验①：记账对账 ----------
    _pnl_sum = sum(r["pnl"] for r in trades_rec)
    _diff = equity - INIT - _pnl_sum
    _tol = max(1.0, len(trades_rec) * 0.01)
    if abs(_diff) > _tol:
        print(f"[FATAL] 对账失败: equity={equity:.2f} INIT={INIT:.2f} "
              f"sum(pnl)={_pnl_sum:.2f} diff={_diff:.2f}", flush=True)
        raise AssertionError("记账对账失败")
    print(f"[check] 对账通过: equity={equity:.2f} = INIT {INIT:.0f} + Σpnl {_pnl_sum:.2f}", flush=True)

    # ---------- 硬校验②：保本组不得出现非负净盈亏 ----------
    _be = [r for r in trades_rec if r["reason"] == "保本止损"]
    _be_bad = [r for r in _be if r["pnl"] > 1e-9]
    if _be_bad:
        print(f"[FATAL] 保本组出现非负盈亏 {len(_be_bad)} 笔，样例={_be_bad[:3]}", flush=True)
        raise AssertionError("保本止损组出现 ≥0 净盈亏 —— 疑似落有利侧凭空盈利")
    print(f"[check] 保本组 {len(_be)} 笔，净盈亏全为负 ✅", flush=True)

    try:
        api.close()
    except Exception:
        pass

    # ---------- 指标（与 06 同口径）----------
    mean_ret = float(np.mean(rets)) if rets else 0.0
    std_ret = float(np.std(rets)) if rets else 0.0
    years = (dts_arr[-1] - dts_arr[0]) / 1e9 / (365.25 * 86400)
    annual_bars = N / years if years > 0 else 11500.0
    sharpe = (mean_ret / std_ret * math.sqrt(annual_bars)) if std_ret > 1e-12 else 0.0
    win_rate = (wins / trades) if trades else 0.0
    max_dd = float(max(bar_dds)) if bar_dds else 0.0
    total_return = (equity - INIT) / INIT

    # 出场原因分布 + 分组统计
    from collections import Counter
    cnt = Counter(r["reason"] for r in trades_rec)
    grp = {}
    for rs in ("止损", "保本止损", "时间止损"):
        g = [r for r in trades_rec if r["reason"] == rs]
        if g:
            grp[rs] = {
                "n": len(g),
                "pnl_sum": round(sum(x["pnl"] for x in g), 2),
                "pnl_avg": round(sum(x["pnl"] for x in g) / len(g), 2),
                "win_n": sum(1 for x in g if x["pnl"] > 0),
            }

    out = {
        "symbol": main_code, "symbol_cn": name_cn, "symbol_key": SYMBOL_KEY,
        "period": "5min", "strategy": "MA13超短线_顺势回踩",
        "BREAKEVEN_ATR": BREAKEVEN_ATR,
        "INIT": INIT,
        "params": {"MA_N": MA_N, "SLOPE_N": SLOPE_N, "ATR_N": ATR_N,
                   "RETRACE_TOL": RETRACE_TOL, "STOP_BUFFER": STOP_BUFFER,
                   "STOP_CAP": STOP_CAP, "MAX_HOLD": MAX_HOLD, "RISK_PCT": RISK_PCT,
                   "SLIP": SLIP, "hand_range": [HAND_MIN, HAND_MAX], "KSEC": KSEC,
                   "INIT": INIT, "NOTIONAL_CAP": NOTIONAL_CAP,
                   "GAP_FILL": GAP_FILL, "COMPOUND": COMPOUND},
        "start": str(_START.date()), "end": str(_END.date()),
        "bars": N, "annual_bars": round(annual_bars, 1),
        "trades": trades, "wins": wins, "win_rate": round(win_rate, 4),
        "sharpe": round(sharpe, 4),
        "final_equity": round(equity, 2),
        "total_return": round(total_return, 4), "max_drawdown": round(max_dd, 4),
        "blown": bool(blown), "commission": round(comm_total, 2),
        "exit_reason_cnt": dict(cnt),
        "exit_reason_stats": grp,
        "blown_at_trade": trades,
    }
    return out, trades_rec


def main():
    out, trades_rec = run_backtest()
    out_dir = os.path.join(_ROOT, "工具", "_保本止损", "结果")
    os.makedirs(out_dir, exist_ok=True)
    tag = f"{out['symbol_key']}_BE{out['BREAKEVEN_ATR']:g}"
    jp = os.path.join(out_dir, tag + ".json")
    tp = os.path.join(out_dir, tag + "_逐笔.txt")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    with open(tp, "w", encoding="utf-8") as f:
        f.write("方向,开仓时间,平仓时间,开仓价,平仓价,手数,盈亏,离场原因,最大有利偏移,开仓ATR\n")
        for t in trades_rec:
            f.write(f"{t['dir']},{t['entry_dt']},{t['exit_dt']},{t['entry']},{t['exit']},"
                    f"{t['hand']},{t['pnl']},{t['reason']},{t['best_fav_pts']},{t['entry_atr']}\n")
    print("[done] " + jp, flush=True)
    print(json.dumps({k: v for k, v in out.items() if k not in ("params",)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
