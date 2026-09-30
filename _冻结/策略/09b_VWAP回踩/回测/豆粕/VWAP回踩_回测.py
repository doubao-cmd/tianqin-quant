# -*- coding: utf-8 -*-
"""
09b_VWAP回踩（裸K + VWAP 三条件精简版）· 30min 日内 B2 分段
===========================================================
T0 预注册（2026-09-30，新假设预算 5 用 1）：由 08_VWAP回撤 精简而来，
用户拍板「落地」。设计动机：五条件并联信号过稀（RB 开发窗 3 年仅 96 笔，
不够 K=172 尺子），砍掉信息冗余条件，只留三件独立的事：方向、价位、确认。

与 08 的差异（逐条）：
  - 删 fib 回撤区、confluence（VWAP 本身就是回撤锚，不需要第二个价位）
  - 删 收盘偏向 条件（并入确认K：阳线收回 VWAP 上方）
  - 删 波段时序检查（没有波段窗口了，自然消失）
  - 删 TP1 平半+保本（它依赖波段高点定义，fib 没了锚也没了）
  - 保留：止损口径 / 时间止损 / VWAP 失守全平 / B2 分段强平 / 手数地基

三条件开仓（多头版，空头镜像；全部用当根已收盘数据）：
  ① 方向：当根 VWAP > 段内 5 根前 VWAP（VWAP 上行）
  ② 价位：当根最低价 ≤ VWAP + 0.25×ATR（回踩触及锚附近）
  ③ 确认：阳线收回 close > open 且 close > VWAP
  → 下一根开盘价成交（1 跳滑点），无未来函数。

参数（定死，禁遍历禁扫参；除 PB_BUF=0.25 外全部沿用 08/MA13 旧值，无新自由度）：
  ATR_PERIOD = 14
  TIME_STOP  = 15   # 15根K时间止损
  VWAP_WARMUP = 2   # 每段前2根禁用方向条件(=VLOOK+1，随周期缩放)
  VLOOK = 1          # 方向比较回看根数：30min的1根≈5min的5根日历时长
  PB_BUF = 0.25     # 回踩容差 0.25×ATR
  止损 = 确认K低点 - 0.5×ATR，封顶 2×ATR
  手数 = 20万×1% ÷ (ATR×TICK×MULT)，夹 [1, 8]（地基口径，固定本金基数、无权益缩放）

B2 分段（任何仓位不跨休市段）：
  夜盘 21:05~22:55 强平 ｜ 早盘 09:05~11:25 强平 ｜ 午盘 13:35~14:55 强平
  每段独立累计 VWAP；ATR 用连续 bar（价格水平不因分段重置）。

tqsdk 坑（勿改回）：
  1) 回测结束 wait_update() 抛 BacktestFinished，必须捕获，否则进程静默退出；
  2) kline serial 是 DataFrame，取行必须 iloc / 布尔筛选；
  3) 段内零成交量 bar 会令 vwap=None —— 入场判定已加 None 守卫；
  4) 已知局限：主连换月跳空未平滑——检测到 underlying 变更即按当根收盘强制平仓。
"""
import os
import sys
import json
import math
import pandas as pd
from datetime import date, time as dtime

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
import config_account
import config_symbols
from tqsdk import TqApi, TqBacktest, TqSim, TqAuth
try:
    from tqsdk import BacktestFinished
except ImportError:
    from tqsdk.backtest import BacktestFinished

# ==================== 品种/账户参数 ====================
SYMBOL_KEY = "M"
NAME_CN = "豆粕"
CONT = config_symbols.get(SYMBOL_KEY)[1]        # 主连 KQ.m@SHFE.rb
_, _, MULT, TICK = config_symbols.get(SYMBOL_KEY)
PRINCIPAL = 200000.0                            # 地基口径：手数固定按本金算
RISK_PCT = 0.01
HAND_MIN, HAND_MAX = 1, 8
SLIP_TICK = 1                                   # 每次成交 1 跳滑点（对向）
COMM_RATE = 1e-4                                # 手续费 万分之一/边（近似）
INIT = 200000.0

# ==================== 策略参数（定死） ====================
ATR_PERIOD = 14
TIME_STOP = 15
VWAP_WARMUP = 2                                 # = VLOOK+1，随周期缩放(5min时为5)
VLOOK = 1                                       # 方向回看根数；30min的1根≈5min的5根日历时长
PB_BUF = 0.25                                   # 回踩容差 0.25×ATR
STOP_BUF = 0.5                                  # 止损缓冲 0.5×ATR
STOP_CAP = 2.0                                  # 止损封顶 2×ATR
# =================================================

FORCE = {"night": dtime(22, 55), "am": dtime(11, 25), "pm": dtime(14, 55)}
CUTOFF = {"night": dtime(22, 45), "am": dtime(11, 15), "pm": dtime(14, 45)}


def seg_of(t):
    """bar 收盘时刻（北京时间）→ 所属分段；None = 非交易时段。"""
    if t.hour >= 21:
        return "night"
    if t.hour in (9, 10, 11):
        return "am"
    if t.hour in (13, 14, 15):
        return "pm"
    return None


def run_backtest(start_dt=date(2016, 1, 1), end_dt=date(2019, 12, 31)):
    user, pwd = config_account.check()
    api = TqApi(TqSim(INIT), backtest=TqBacktest(start_dt=start_dt, end_dt=end_dt),
                auth=TqAuth(user, pwd))
    q_cont = api.get_quote(CONT)
    kn = api.get_kline_serial(CONT, 1800)       # 30min 主连
    api.wait_update()

    # ---- 滚动数据（连续 bar，不因分段重置）----
    C = []                                      # 收盘序列（ATR 用）
    trs = []                                    # 真实波幅序列
    # ---- 分段状态 ----
    seg, seg_bar_n = None, 0
    cum_pv, cum_v, vwap_hist = 0.0, 0.0, []
    # ---- 持仓状态 ----
    pos = 0                                     # +手数 多 / -手数 空 / 0
    entry_px = stop_px = 0.0
    bars_in, entry_trip_pnl = 0, 0.0
    pend_entry = None                           # dict(dir, stop_raw, atr)
    pend_exit = None                            # ("ALL", )
    # ---- 统计 ----
    realized, costs = 0.0, 0.0
    equity_curve, day_eq = [], {}
    trips, n_signals, n_rolls, n_force = [], 0, 0, 0
    cur_u = q_cont.underlying_symbol
    last_closed = int(kn.iloc[-1]["id"]) - 1
    nproc = 0

    def fill_cost(px, lots):
        return lots * px * MULT * COMM_RATE + lots * TICK * MULT * SLIP_TICK

    def close_all_now(px, why):
        """立即按 px 平掉全部持仓（用于分段强平/换月，含滑点与手续费）。"""
        nonlocal pos, realized, costs, entry_trip_pnl
        lots = abs(pos)
        slip = TICK * SLIP_TICK * (1 if pos > 0 else -1)
        px_fill = px - slip if pos > 0 else px + slip
        pnl = (px_fill - entry_px) * pos * MULT
        realized += pnl
        costs += fill_cost(px_fill, lots)
        entry_trip_pnl += pnl
        trips.append(entry_trip_pnl)
        pos = 0
        return why

    try:
        while True:
            api.wait_update()
            cid = int(kn.iloc[-1]["id"])
            for rid in range(last_closed + 1, cid):
                row = kn[kn["id"] == rid]
                if len(row) == 0:
                    continue
                b = row.iloc[0]
                last_closed = rid
                nproc += 1
                if nproc % 4000 == 0:
                    print(f"  ...{nproc} bars, equity≈{INIT + realized:.0f}", flush=True)

                o, h, l, c = float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"])
                v = float(b["volume"])
                dt = pd.to_datetime(int(b["datetime"]), unit="ns") + pd.Timedelta(hours=8)
                t = dt.time()
                C.append(c)

                # ---- 换月检测（主连 underlying 变更 → 当根收盘强平）----
                u = q_cont.underlying_symbol
                if u != cur_u:
                    cur_u = u
                    if pos != 0:
                        close_all_now(c, "roll")
                        n_rolls += 1
                    pend_entry = None

                # ---- 分段切换：VWAP 重置、撤未成交信号 ----
                s = seg_of(t)
                if s is None:
                    continue
                if s != seg:
                    seg, seg_bar_n = s, 0
                    cum_pv, cum_v, vwap_hist = 0.0, 0.0, []
                    pend_entry = None
                seg_bar_n += 1

                # ---- ATR（连续14根 TR 均值）----
                if len(C) >= 2:
                    pc = C[-2]
                    trs.append(max(h - l, abs(h - pc), abs(l - pc)))
                atr = (sum(trs[-ATR_PERIOD:]) / ATR_PERIOD) if len(trs) >= ATR_PERIOD else None

                # ---- VWAP（分段累计）----
                tp = (h + l + c) / 3.0
                cum_pv += tp * v
                cum_v += v
                vwap = cum_pv / cum_v if cum_v > 0 else None
                vwap_hist.append(vwap)

                # ================= 挂单执行（下一根开盘成交） =================
                if pend_entry is not None and pos == 0 and atr is not None:
                    d = pend_entry["dir"]
                    fill = o + TICK * SLIP_TICK if d > 0 else o - TICK * SLIP_TICK
                    stop_raw = pend_entry["stop_raw"]
                    if d > 0:
                        stop = max(stop_raw, fill - STOP_CAP * pend_entry["atr"])
                    else:
                        stop = min(stop_raw, fill + STOP_CAP * pend_entry["atr"])
                    hand = int(round((PRINCIPAL * RISK_PCT) /
                                     (pend_entry["atr"] * TICK * MULT)))
                    hand = max(HAND_MIN, min(HAND_MAX, hand))
                    pos = hand if d > 0 else -hand
                    entry_px = fill
                    stop_px = stop
                    bars_in, entry_trip_pnl = 0, 0.0
                    costs += fill_cost(fill, hand)
                    n_signals += 1
                    pend_entry = None

                if pend_exit is not None and pos != 0:
                    lots_all = abs(pos)
                    fill = o - TICK * SLIP_TICK if pos > 0 else o + TICK * SLIP_TICK
                    pnl = (fill - entry_px) * pos * MULT
                    realized += pnl
                    costs += fill_cost(fill, lots_all)
                    entry_trip_pnl += pnl
                    trips.append(entry_trip_pnl)
                    pos = 0
                    pend_exit = None

                # ================= 分段强平（当根收盘价） =================
                if pos != 0 and t >= FORCE[seg]:
                    close_all_now(c, "force")
                    n_force += 1

                # ================= 出场判定（用本根已收盘 close） =================
                if pos != 0 and pend_exit is None:
                    bars_in += 1
                    vwap_ok = vwap if vwap is not None else c
                    if pos > 0:
                        if c <= stop_px:
                            pend_exit = ("ALL",)
                        elif c < vwap_ok:
                            pend_exit = ("ALL",)
                        elif bars_in >= TIME_STOP:
                            pend_exit = ("ALL",)
                    else:
                        if c >= stop_px:
                            pend_exit = ("ALL",)
                        elif c > vwap_ok:
                            pend_exit = ("ALL",)
                        elif bars_in >= TIME_STOP:
                            pend_exit = ("ALL",)

                # ================= 入场判定（三条件，平仓后当根也可立即找信号） =================
                if pos == 0 and pend_entry is None and atr is not None \
                        and t < CUTOFF[seg] and seg_bar_n > VWAP_WARMUP and len(vwap_hist) >= VLOOK + 1 \
                        and vwap is not None and vwap_hist[-(VLOOK + 1)] is not None:
                    v5 = vwap_hist[-(VLOOK + 1)]
                    # ---- 多头：VWAP 上行 + 回踩触及 + 阳线收回 ----
                    if vwap > v5 and l <= vwap + PB_BUF * atr and c > o and c > vwap:
                        pend_entry = {"dir": 1, "stop_raw": l - STOP_BUF * atr, "atr": atr}
                    # ---- 空头镜像：VWAP 下行 + 反抽触及 + 阴线收回 ----
                    elif vwap < v5 and h >= vwap - PB_BUF * atr and c < o and c < vwap:
                        pend_entry = {"dir": -1, "stop_raw": h + STOP_BUF * atr, "atr": atr}

                # ---- 权益曲线（逐根 mark-to-market）----
                unreal = (c - entry_px) * pos * MULT if pos != 0 else 0.0
                equity_curve.append(INIT + realized + unreal)
                d = dt.date()
                day_eq[d] = INIT + realized + unreal

        # 回测结束：wait_update 抛 BacktestFinished，在此收尾
    except BacktestFinished:
        pass
    api.close()

    # ================= 指标计算 =================
    final_eq = INIT + realized
    total_return = final_eq / INIT - 1.0
    peak, maxdd = -1e18, 0.0
    for e in equity_curve:
        peak = max(peak, e)
        maxdd = max(maxdd, 1 - e / peak)
    days = sorted(day_eq.keys())
    rets = [(day_eq[days[i]] / day_eq[days[i - 1]] - 1.0) for i in range(1, len(days))]
    if len(rets) > 2:
        mu = sum(rets) / len(rets)
        var = sum((x - mu) ** 2 for x in rets) / (len(rets) - 1)
        sharpe = (mu / math.sqrt(var) * math.sqrt(252)) if var > 0 else 0.0
    else:
        sharpe = 0.0
    wins = sum(1 for p in trips if p > 0)
    m = {
        "品种": f"{NAME_CN}({SYMBOL_KEY}) 5min B2分段",
        "窗口": f"{start_dt}~{end_dt}",
        "初始权益": INIT, "期末权益": round(final_eq, 0),
        "收益率%": round(total_return * 100, 2),
        "最大回撤%": round(maxdd * 100, 2),
        "年化夏普(自算)": round(sharpe, 2),
        "完整回合数": len(trips),
        "胜率%": round(wins / len(trips) * 100, 1) if trips else 0.0,
        "开仓次数": n_signals, "手续费+滑点": round(costs, 0),
        "处理bar数": nproc, "换月强平次数": n_rolls, "分段强平次数": n_force,
    }
    return m


if __name__ == "__main__":
    m = run_backtest()
    for k, v in m.items():
        print(f"  {k}: {v}")
    out = os.path.join(ROOT, "工具", "vwaphc30_M_death.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=1)
    print("JSON ->", out)
