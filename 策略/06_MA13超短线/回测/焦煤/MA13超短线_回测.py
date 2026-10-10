# -*- coding: utf-8 -*-
"""
MA13 超短线 · 顺势回踩策略 —— 回测入口（转正版，自包含）
==========================================================
逻辑（用户给定参数表，写死，不调参）：
  1. 计算 MA13 与其「前 5 根」的差值判断趋势方向（斜率）；
  2. 价格回踩到 MA13 附近（|close-MA13| <= 0.5×ATR）且趋势明确时，沿趋势方向开仓；
  3. 止损 = 回踩区间极值外侧 0.5×ATR，封顶 2×ATR；时间止损 = 持仓满 15 根 K 线；
  4. 手数 = 本金 20 万算基准块（1%÷ATR，恒定保本），封顶 8 手（安全地基，不上杠杆）。

禁未来函数：信号用第 i-1 根已收盘 bar，第 i 根开盘成交。
样本纪律：本脚本默认跑「开发窗 2021-2023（已看）」；前向验证窗（2024/2025）待另跑，
  不在此脚本里复用同一段数据。

【与 lib/engine.py 的关系】
  本项目 5 个正式策略的买卖逻辑都在 lib/engine.py 里。本策略**不接入** engine
  （engine 标注「❌ 别动」，且 MA13 是独立逻辑），故自包含实现。但为让报告「长相」
  与 01~05 一致，本文件**原样搬入**了 engine 里 4 个纯报告函数（_kline_svg /
  _monthly_table_html / _drawdown_svg / _equity_svg），只读复用、未改动 engine 本体。
  参数口径、成本模型、禁未来函数规则与正式策略完全一致。

怎么用：
  改顶部「可调参数」→ 双击同目录 回测.bat → 跑完弹出 HTML 报告（同目录）。
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

# ==================== 可调参数（写死，不调参）====================
SYMBOL_KEY = "JM"          # 品种缩写（config_symbols 里查主连/乘数/一跳/手续费）
KSEC       = 300           # 5 分钟 K 线
MA_N       = 13            # 均线周期
SLOPE_N    = 5             # 斜率判断周期（当前 MA13 与 5 根前 MA13 比较判趋势）
ATR_N      = 14            # ATR 周期
RETRACE_TOL = 0.5          # 回踩容差（×ATR）
STOP_BUFFER = 0.5          # 止损缓冲（×ATR，放回踩极值外侧）
STOP_CAP    = 2.0          # 兜底止损宽度（×ATR，止损上限）
MAX_HOLD    = 15           # 时间止损（根）
RISK_PCT    = 0.01        # 每笔目标风险 = 当前权益 × 1%（动态复利口径，与 engine 对齐）
SLIP        = 1            # 滑点（跳）
HAND_MIN    = 1
HAND_MAX    = 8     # 封顶 8 手（风险预算封顶）
NOTIONAL_CAP = 2.0  # 名义敞口上限（倍权益）：手数 ≤ 权益×2÷每手名义价值。2026-10-07 加——8手对各品种含义差10倍，需按名义而非固定手数分档
INIT        = 200000.0     # 初始资金（元）
# ---- ★ 口径开关（2026-10-09 用户拍板；只影响成交价与手数基数，不影响信号）----
# GAP_FILL：止损成交价是否模拟跳空。
#   True  = 保守侧（多头 min(止损线,开盘价) / 空头 max(止损线,开盘价)）
#          ⇒ 对齐模拟盘实测（成交价可差止损线 −22.11 点）
#   False = 旧口径（按止损线理想价成交，零滑点零跳空）⇒ 系统性虚高，仅供对照
GAP_FILL   = True
# COMPOUND：手数预算基数。
#   False = 锁死 20 万（对齐模拟盘 RISK_EQUITY，★用户 2026-10-09 拍板方案 A）
#          ⇒ 保住单腿收益可加性，E[组合]=ΣwᵢE[腿ᵢ] 成立
#   True  = 复利（预算基数 = 当前权益）⇒ 与模拟盘不一致
COMPOUND   = False
# ---- 开发窗（已看）：2021-2023；回测起点前移 7 天预热，预热期不交易 ----
START = pd.Timestamp("2021-01-01")
END   = pd.Timestamp("2023-12-31")
WARMUP_DAYS = 7
# ===============================================================

name_cn, main_code, MULT, TICK = config_symbols.get(SYMBOL_KEY)
COMM = float(config_symbols.COST[SYMBOL_KEY])


# ---- 动态仓位（2026-10-07 改：预算基数 固定本金20万 → 当前权益（复利）。唯一改动，其余不动）----
PRINCIPAL  = 200000.0   # 兜底：权益取不到时回落到 20 万（正常路径不会用到）
def _base_hand(a, equity):
    """波动率手数：预算 = 当前权益 ×1% ÷ 每手风险（ATR 口径）。
    改动史：原为固定 PRINCIPAL=20 万保本块（扁平，与浮盈无关）；
    2026-10-07 用户拍板改为按当前权益（复利）⇒ 赚钱自动加注、亏钱自动减注，
    代价=收益丧失单品种可加性。权益为决策时点（上一根收盘标记）——无未来函数。"""
    if math.isnan(a) or a <= 0:
        return HAND_MIN
    if equity is None or math.isnan(equity) or equity <= 0:
        equity = PRINCIPAL
    h = int(round((equity * RISK_PCT) / (a * TICK * MULT)))
    return max(HAND_MIN, min(HAND_MAX, h))

def hand_plan(a, equity, price=None):
    """手数 = 预算基数×1%÷(ATR×一跳×乘数)，夹 [1,8]；再夹名义敞口 ≤ 预算基数×NOTIONAL_CAP。
    price = 决策时点成交价（本脚本传入场当根开盘价 open_[i]），不是未来数据。
    注意：名义上限不足 1 手时仍保底 1 手（与既有 HAND_MIN 行为一致，已知代价：实际风险>1%）。
    ★ COMPOUND=False 时预算基数 = PRINCIPAL(20万)，与模拟盘 RISK_EQUITY 严格一致。"""
    equity = PRINCIPAL if not COMPOUND else equity
    h = _base_hand(a, equity)
    eq = equity if (equity and not math.isnan(equity) and equity > 0) else PRINCIPAL
    if price and price > 0 and not math.isnan(price):
        名义手数 = int((eq * NOTIONAL_CAP) / (price * MULT))
        h = min(h, max(HAND_MIN, 名义手数))
    return h


def run_backtest(start=None, end=None):
    """start/end 可注入（前向验证用）；缺省沿用模块顶层 START/END（开发窗）。"""
    _START = start if start is not None else START
    _END = end if end is not None else END
    user, pwd = config_account.check()
    bt_start = _START - pd.Timedelta(days=WARMUP_DAYS)
    api = TqApi(TqSim(INIT), backtest=TqBacktest(bt_start, _END), auth=TqAuth(user, pwd))
    k = api.get_kline_serial(main_code, KSEC, data_length=3000)
    print("[info] 拉取 K 线并逐根收集...", flush=True)
    t0 = time.time()

    # 自收集完整序列（绕过 data_length 截断）
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

    # ATR(14) —— 简单滚动均值（与实验版一致，保证可复现）
    prev_close = np.roll(close, 1); prev_close[0] = close[0]
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    atr = np.full(N, np.nan)
    for i in range(1, N):
        atr[i] = np.mean(tr[max(1, i - ATR_N):i]) if i >= 2 else tr[i]

    # MA13
    ma13 = np.full(N, np.nan)
    for i in range(MA_N, N):
        ma13[i] = np.mean(close[i - MA_N:i])

    # ---------- 回测循环 ----------
    equity = INIT
    pos = 0
    entry_price = 0.0
    stop_price = 0.0
    cur_dir = 0.0
    cur_entry_price = 0.0
    cur_entry_dt = 0
    cur_hand = 0
    bars_in = 0
    trades = 0
    wins = 0
    peak = INIT
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
                    # ★ GAP_FILL：跳空低开时成交在开盘价（保守），否则在止损线
                    exit_price = min(stop_price, open_[i]) if GAP_FILL else stop_price
                    reason = "止损"
                elif bars_in >= MAX_HOLD:
                    exit_now = True; exit_price = open_[i]; reason = "时间止损"
            elif pos < 0:
                if high[i] >= stop_price:
                    exit_now = True
                    exit_price = max(stop_price, open_[i]) if GAP_FILL else stop_price
                    reason = "止损"
                elif bars_in >= MAX_HOLD:
                    exit_now = True; exit_price = open_[i]; reason = "时间止损"

            if exit_now and pos != 0:
                exit_cost = SLIP * TICK * MULT * abs(pos) + COMM * abs(pos)
                pnl = pos * (exit_price - entry_price) * MULT - exit_cost
                equity -= exit_cost
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
                    "pnl_per_hand": round(pnl / (cur_hand * MULT), 2),
                    "reason": reason,
                })
                pos = 0; bars_in = 0

            if pos == 0 and dir_trend != 0 and touch:
                hand = hand_plan(a, equity, open_[i])
                entry_price = open_[i]
                oc = SLIP * TICK * MULT * hand + COMM * hand
                if dir_trend > 0:
                    swing_low = np.min(low[max(0, i - SLOPE_N):i])
                    stop_raw = swing_low - STOP_BUFFER * a
                    stop_price = max(entry_price - STOP_CAP * a, stop_raw)
                    # 【2026-10-08 bugfix】止损价必须落在入场价不利侧（至少留一跳）。
                    # 跳空低开时 open[i] 可能低于 swing_low => max() 会选中 swing_low 支，
                    # 使 stop > entry，下一根 low<=stop 立刻成立 ⇒ 以不存在的价格平仓、凭空盈利。
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
                equity -= oc
                comm_total += COMM * hand
                bars_in = 0
                cur_dir = dir_trend
                cur_entry_price = entry_price
                cur_entry_dt = dts_arr[i]
                cur_hand = hand
            elif pos != 0:
                bars_in += 1

        if pos != 0:
            equity += pos * (close[i] - close[i - 1]) * MULT
        peak = max(peak, equity)
        dd = (peak - equity) / peak if peak > 0 else 0.0
        bar_dds.append(dd)
        eq_series.append(equity)
        dt_series.append(dts_arr[i])
        if prev_eq != 0:
            rets.append((equity - prev_eq) / prev_eq)
        prev_eq = equity
        if equity <= 0:
            blown = True
            break

    try:
        api.close()
    except Exception:
        pass

    # ---------- 指标 ----------
    mean_ret = float(np.mean(rets)) if rets else 0.0
    std_ret = float(np.std(rets)) if rets else 0.0
    years = (dts_arr[-1] - dts_arr[0]) / 1e9 / (365.25 * 86400)
    annual_bars = N / years if years > 0 else 11500.0
    sharpe = (mean_ret / std_ret * math.sqrt(annual_bars)) if std_ret > 1e-12 else 0.0
    sharpe_11500 = (mean_ret / std_ret * math.sqrt(11500.0)) if std_ret > 1e-12 else 0.0
    win_rate = (wins / trades) if trades else 0.0
    max_dd = float(max(bar_dds)) if bar_dds else 0.0
    bar_avg_dd = float(np.mean(bar_dds)) if bar_dds else 0.0
    total_return = (equity - INIT) / INIT

    out = {
        "symbol": main_code, "symbol_cn": name_cn, "period": "5min",
        "strategy": "MA13超短线_顺势回踩",
        "INIT": INIT,
        "params": {"MA_N": MA_N, "SLOPE_N": SLOPE_N, "ATR_N": ATR_N,
                   "RETRACE_TOL": RETRACE_TOL, "STOP_BUFFER": STOP_BUFFER,
                   "STOP_CAP": STOP_CAP, "MAX_HOLD": MAX_HOLD, "RISK_PCT": RISK_PCT,
                   "SLIP": SLIP, "hand_range": [HAND_MIN, HAND_MAX],
                   "KSEC": KSEC, "INIT": INIT},
        "start": str(_START.date()), "end": str(_END.date()),
        "bars": N, "annual_bars": round(annual_bars, 1),
        "trades": trades, "wins": wins, "win_rate": round(win_rate, 4),
        "sharpe": round(sharpe, 4), "sharpe_11500": round(sharpe_11500, 4),
        "final_equity": round(equity, 2),
        "total_return": round(total_return, 4), "max_drawdown": round(max_dd, 4),
        "bar_avg_drawdown": round(bar_avg_dd, 4), "blown": bool(blown),
        "commission": round(comm_total, 2),
        # ★ 2026-10-10 修：标签不再硬编码（原标签恒写「当前权益×1%」=复利，
        #   但 COMPOUND=False 时实际基数=锁死20万 ⇒ 标签与实现不符，误导所有读 json 的人）。
        #   现在由两个开关动态生成，含标定日期，任何人一眼能对上代码。
        "cost_assumption": (
            f"滑点{SLIP}跳+佣金{COMM}元/手/边; 本金{INIT:.0f}; "
            + (f"手数=锁死{PRINCIPAL:.0f}(对齐模拟盘RISK_EQUITY)×{RISK_PCT*100:.0f}%÷ATR风险夹[1,8]"
               if not COMPOUND else
               f"手数=当前权益×{RISK_PCT*100:.0f}%÷ATR风险夹[1,8]")
            + f"+名义敞口≤权益×{NOTIONAL_CAP:.0f}倍; "
            + ("止损=GAP_FILL保守成交(多头min(止损线,开盘)/空头max)" if GAP_FILL
               else "止损=理想价成交(零滑点零跳空,系统性虚高,仅供对照)")
            + f"; 乘数{MULT}; 一跳{TICK}; 标定2026-10-10"
        ),
    }
    kline_history = [{"dt": int(d_list[i]), "open": float(o_list[i]), "high": float(h_list[i]),
                     "low": float(l_list[i]), "close": float(c_list[i]), "volume": 0.0}
                    for i in range(N)]
    return out, eq_series, dt_series, trades_rec, kline_history


# ============ 以下 4 个纯函数从 lib/engine.py 原样搬入（只读复用，未改 engine）============
# 作用：让 06 报告的 K线图/回撤/月度表与 01~05 视觉一致，且零外部依赖。

def _kline_svg(history, signals, max_marks=50):
    """画 K 线图 + 买卖点标记。history: [{dt,open,high,low,close,volume}, ...]"""
    if not history:
        return ""
    sigs = list(signals or [])
    if max_marks and len(sigs) > max_marks:
        if max_marks <= 1:
            sigs = sigs[-1:]
        else:
            sigs = [sigs[int(round(i * (len(sigs) - 1) / (max_marks - 1)))]
                    for i in range(max_marks)]
    n = len(history)
    step = 1
    ds = history
    if n > 600:
        step = (n + 599) // 600
        ds = []
        for i in range(0, n, step):
            chunk = history[i:i + step]
            ds.append({
                "dt": chunk[0]["dt"],
                "open": chunk[0]["open"],
                "close": chunk[-1]["close"],
                "high": max(b["high"] for b in chunk),
                "low": min(b["low"] for b in chunk),
                "volume": sum(b["volume"] for b in chunk),
            })
    m = len(ds)
    if m == 0:
        return ""
    W, H, pad = 800, 320, 36
    lo = min(b["low"] for b in ds)
    hi = max(b["high"] for b in ds)
    rng = (hi - lo) or 1
    cw = (W - 2 * pad) / m
    body_w = max(1.0, cw * 0.6)

    candles = ""
    for i, b in enumerate(ds):
        x = pad + (i + 0.5) * cw
        y_o = H - pad - (b["open"] - lo) / rng * (H - 2 * pad)
        y_c = H - pad - (b["close"] - lo) / rng * (H - 2 * pad)
        y_h = H - pad - (b["high"] - lo) / rng * (H - 2 * pad)
        y_l = H - pad - (b["low"] - lo) / rng * (H - 2 * pad)
        color = "#d4380d" if b["close"] >= b["open"] else "#198755"
        top = min(y_o, y_c)
        hgt = max(1.0, abs(y_c - y_o))
        candles += (f'<line x1="{x:.1f}" y1="{y_h:.1f}" x2="{x:.1f}" y2="{y_l:.1f}" stroke="{color}" stroke-width="1"/>')
        candles += (f'<rect x="{x - body_w / 2:.1f}" y="{top:.1f}" width="{body_w:.1f}" height="{hgt:.1f}" fill="{color}"/>')

    # 快速定位：dt -> 下标（history 按 dt 升序）
    dt_to_idx = {b["dt"]: j for j, b in enumerate(history)}

    markers = ""
    for sig in sigs:
        idx = dt_to_idx.get(sig["dt"])
        if idx is None:
            lo_i, hi_i = 0, n - 1
            while lo_i < hi_i:
                mid = (lo_i + hi_i) // 2
                if history[mid]["dt"] < sig["dt"]:
                    lo_i = mid + 1
                else:
                    hi_i = mid
            idx = lo_i if lo_i < n and history[lo_i]["dt"] >= sig["dt"] else None
        if idx is None:
            continue
        di = idx // step
        if di >= m:
            di = m - 1
        b = ds[di]
        x = pad + (di + 0.5) * cw
        _rs = str(sig.get("reason", "") or "")
        _lb = _rs if _rs else ("买入" if sig["dir"] == "BUY" else "卖出")
        if sig["dir"] == "BUY":
            y = H - pad - (b["low"] - lo) / rng * (H - 2 * pad) + 14
            _col = "#e8890c" if _rs else "#d4380d"
            markers += (f'<path d="M{x:.1f},{y:.1f} l-5,-9 l10,0 z" fill="{_col}">'
                        f'<title>{_lb} {sig["dt"]}</title></path>')
        else:
            y = H - pad - (b["high"] - lo) / rng * (H - 2 * pad) - 14
            _col = "#e8890c" if _rs else "#198755"
            markers += (f'<path d="M{x:.1f},{y:.1f} l-5,9 l10,0 z" fill="{_col}">'
                        f'<title>{_lb} {sig["dt"]}</title></path>')

    labels = (f'<text x="{W - pad - 70}" y="{pad - 6:.0f}" font-size="11" fill="#99a">高 {hi:,.0f}</text>'
              f'<text x="{W - pad - 70}" y="{H - pad + 14:.0f}" font-size="11" fill="#99a">低 {lo:,.0f}</text>')
    return f"""
    <svg viewBox="0 0 {W} {H}" width="100%" preserveAspectRatio="xMidYMid meet"
         style="background:#fbfcfe;border:1px solid #eef2f7;border-radius:8px;">
      {candles}
      {markers}
      {labels}
    </svg>
    <p class="note">红涨绿跌 ｜ 红=买入信号 绿=卖出信号 橙=止损平仓 ｜ K线过多已自动降采样；买卖点最多显示 {max_marks} 个</p>"""


def _monthly_table_html(equity_curve, init_balance):
    """按月聚合权益曲线，生成月度收益表 HTML。"""
    if not equity_curve:
        return '<p class="note">无权益曲线数据，无法生成月度收益。</p>'
    try:
        month_end = {}
        for dt_str, bal in equity_curve:
            key = str(dt_str)[:7]
            month_end[key] = float(bal)
        months = sorted(month_end.keys())
        if not months:
            return '<p class="note">无有效月度数据。</p>'
        base = float(init_balance) if init_balance else month_end[months[0]]
        rows = ""
        prev = base
        for m in months:
            end = month_end[m]
            ret = (end / prev - 1) if prev else 0.0
            cum = (end / base - 1) if base else 0.0
            pnl = end - prev
            cls = "pos" if ret >= 0 else "neg"
            sign = "+" if pnl >= 0 else ""
            rows += (f"<tr><td>{m}</td><td>{end:,.0f}</td>"
                     f"<td class='{cls}'>{ret * 100:+.2f}%</td>"
                     f"<td class='{cls}'>{sign}{pnl:,.0f}</td>"
                     f"<td class='{cls}'>{cum * 100:+.2f}%</td></tr>")
            prev = end
        return f"""
      <div class="scroll">
      <table>
        <thead><tr><th>月份</th><th>月末权益(元)</th><th>月度收益</th>
        <th>月度盈亏(元)</th><th>累计收益</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
      </div>"""
    except Exception as e:
        return f'<p class="note">月度收益计算失败：{e}</p>'


def _drawdown_svg(series):
    """基于权益序列画回撤曲线（红色填充向下）。"""
    if not series:
        return '<p class="note">（无权益数据）</p>'
    vals = [float(v) for _, v in series]
    W, H, pad = 800, 200, 30
    rm = vals[0]
    dds = []
    for v in vals:
        rm = max(rm, v)
        dds.append((rm - v) / rm if rm else 0)
    max_dd = max(dds) if dds else 0
    rng = max_dd or 1
    n = len(vals)
    pts = []
    for i, dd in enumerate(dds):
        x = pad + (i / (n - 1)) * (W - 2 * pad) if n > 1 else W / 2
        y = pad + dd / rng * (H - 2 * pad)
        pts.append((x, y))
    poly = f"{pad},{H - pad} " + " ".join(f"{x:.1f},{y:.1f}" for x, y in pts) + f" {W - pad},{H - pad}"
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    label = (f'<text x="{W - pad - 130}" y="{pad + 12:.0f}" font-size="11" fill="#d4380d">'
             f'最大回撤 {max_dd * 100:.2f}%</text>')
    return f"""
    <svg viewBox="0 0 {W} {H}" width="100%" preserveAspectRatio="xMidYMid meet"
         style="background:#fbfcfe;border:1px solid #eef2f7;border-radius:8px;">
      <polygon fill="rgba(212,56,13,0.16)" points="{poly}"/>
      <polyline fill="none" stroke="#d4380d" stroke-width="1.5" points="{line}"/>
      {label}
    </svg>"""


def _equity_svg(series, init_balance):
    """把权益序列画成自包含 SVG。series: [(dt_str, balance), ...]"""
    if not series:
        return '<p class="note">（本次回测无权益序列数据）</p>'
    W, H, pad = 800, 280, 46
    vals = [v for _, v in series]
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    n = len(series)
    pts = []
    for i, (_, v) in enumerate(series):
        x = pad + (i / (n - 1)) * (W - 2 * pad) if n > 1 else W / 2
        y = H - pad - (v - lo) / rng * (H - 2 * pad)
        pts.append((x, y))
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    y0 = H - pad - (init_balance - lo) / rng * (H - 2 * pad)
    y0 = max(pad, min(H - pad, y0))
    labels = ""
    labels += (f'<text x="{pad}" y="{pts[0][1] - 8:.0f}" font-size="11" fill="#4c8bf5">'
               f'起 {vals[0]:,.0f}</text>')
    labels += (f'<text x="{W - pad - 90}" y="{pts[-1][1] - 8:.0f}" font-size="11" fill="#d4380d">'
               f'终 {vals[-1]:,.0f}</text>')
    labels += (f'<text x="{W - pad - 40}" y="{pad - 8:.0f}" font-size="11" fill="#99a">'
               f'高 {hi:,.0f}</text>')
    labels += (f'<text x="{W - pad - 40}" y="{H - pad + 14:.0f}" font-size="11" fill="#99a">'
               f'低 {lo:,.0f}</text>')
    return f"""
    <svg viewBox="0 0 {W} {H}" width="100%" preserveAspectRatio="xMidYMid meet"
         style="background:#fbfcfe;border:1px solid #eef2f7;border-radius:8px;">
      <line x1="{pad}" y1="{y0:.0f}" x2="{W - pad}" y2="{y0:.0f}"
            stroke="#cdd6e3" stroke-width="1" stroke-dasharray="5,4"/>
      <polyline fill="none" stroke="#4c8bf5" stroke-width="2" points="{poly}"/>
      {labels}
    </svg>"""


def build_html(out, eq_series, dt_series, trades_rec, kline_history, sample_mode="IS"):
    main_code = out["symbol"]
    name_cn = out["symbol_cn"]

    # 权益曲线（与 engine 同格式：[(dt_str, balance)]）
    equity_curve = [(pd.Timestamp(dt_series[i]).strftime("%Y-%m-%d %H:%M:%S"), float(eq_series[i]))
                    for i in range(len(eq_series))]

    # 信号事件：由逐笔记录推导（entry=开仓方向；exit=平仓反向 + 离场原因）
    signal_events = []
    for t in trades_rec:
        d = t["dir"]
        bdir = "BUY" if d == "多" else "SELL"
        sdir = "SELL" if d == "多" else "BUY"
        try:
            edt = int(pd.Timestamp(t["entry_dt"]).value)
            xdt = int(pd.Timestamp(t["exit_dt"]).value)
        except Exception:
            edt = xdt = 0
        signal_events.append({"dt": edt, "dir": bdir, "reason": ""})
        if t.get("reason"):
            signal_events.append({"dt": xdt, "dir": sdir, "reason": t["reason"]})

    kline_svg = _kline_svg(kline_history, signal_events) if kline_history else ""
    equity_svg = _equity_svg(equity_curve, out["INIT"])
    drawdown_svg = _drawdown_svg(equity_curve)
    monthly_html = _monthly_table_html(equity_curve, out["INIT"])

    # 指标卡
    def _card(label, val, color="#222"):
        return (f'<div class="card"><div class="k">{label}</div>'
                f'<div class="v" style="color:{color}">{val}</div></div>')

    tr_color = "#d4380d" if out["total_return"] >= 0 else "#198755"
    sh_color = "#d4380d" if out["sharpe"] >= 0 else "#198755"
    win_color = "#d4380d" if out["win_rate"] >= 0.5 else "#222"
    blown_color = "#d4380d" if out["blown"] else "#198755"

    core_cards = "".join([
        _card("收益率", f"{out['total_return'] * 100:.1f}%", tr_color),
        _card("夏普(实测年化)", f"{out['sharpe']:.2f}", sh_color),
        _card("最大回撤", f"{out['max_drawdown'] * 100:.1f}%", "#d4380d"),
        _card("笔数", f"{out['trades']}"),
        _card("胜率", f"{out['win_rate'] * 100:.1f}%", win_color),
        _card("期末权益", f"{out['final_equity']:,.0f}"),
        _card("爆仓", "是" if out["blown"] else "否", blown_color),
        _card("年化根数", f"{out['annual_bars']:,.0f}"),
    ])

    # 扩展指标
    comm = out.get("commission", 0) or 0
    win_streak = loss_streak = cur_w = cur_l = 0
    for t in trades_rec:
        if t["pnl"] >= 0:
            cur_w += 1; cur_l = 0
            win_streak = max(win_streak, cur_w)
        else:
            cur_l += 1; cur_w = 0
            loss_streak = max(loss_streak, cur_l)
    years = (dt_series[-1] - dt_series[0]) / 1e9 / (365.25 * 86400) if dt_series else 0
    annual = ((1 + out["total_return"]) ** (1 / years) - 1) if (years > 0 and out["total_return"] > -1) else 0
    calmar = (annual / out["max_drawdown"]) if out["max_drawdown"] > 1e-9 else 0
    trading_days = len({pd.Timestamp(dt_series[i]).date() for i in range(len(dt_series))}) if dt_series else 0

    ext_cards = "".join([
        _card("总手续费", f"{comm:,.0f}"),
        _card("开仓次数", f"{out['trades']}"),
        _card("平仓次数", f"{out['trades']}"),
        _card("最大连胜", f"{win_streak}", "#d4380d"),
        _card("最大连亏", f"{loss_streak}", "#198755"),
        _card("卡玛比率", f"{calmar:.2f}"),
        _card("索提诺比率", "—"),
        _card("交易天数", f"{trading_days}"),
    ])

    # 逐笔表（最多 300 行）
    show = trades_rec[:300]
    rows = ""
    for t in show:
        pnl_color = "#d4380d" if t["pnl"] >= 0 else "#198755"
        rows += (f'<tr><td>{t["dir"]}</td><td>{t["entry_dt"]}</td><td>{t["exit_dt"]}</td>'
                 f'<td>{t["entry"]}</td><td>{t["exit"]}</td><td>{t["hand"]}</td>'
                 f'<td style="color:{pnl_color}">{t["pnl"]:,.0f}</td>'
                 f'<td>{t["pnl_per_hand"]}</td><td>{t["reason"]}</td></tr>')
    more = "" if len(trades_rec) <= 300 else f'<p class="note">（仅显示前 300 笔，共 {len(trades_rec)} 笔）</p>'

    params = out["params"]
    param_rows = "".join(
        f"<tr><td>{k}</td><td>{v}</td></tr>"
        for k, v in [("均线周期 MA_N", params["MA_N"]), ("斜率周期 SLOPE_N", params["SLOPE_N"]),
                     ("ATR 周期", params["ATR_N"]), ("回踩容差", f'{params["RETRACE_TOL"]}×ATR'),
                     ("止损缓冲", f'{params["STOP_BUFFER"]}×ATR'), ("兜底止损", f'{params["STOP_CAP"]}×ATR'),
                     ("时间止损", f'{params["MAX_HOLD"]} 根'), ('目标风险/仓位', f'当前权益×{RISK_PCT*100:.0f}%÷ATR风险，夹[1,8]；另夹名义敞口≤权益×{NOTIONAL_CAP:.0f}倍'),
                     ("滑点", f'{params["SLIP"]} 跳'), ("手数约束", "名义上限不足1手时仍保底1手（实际风险>1%，已知代价）"),
                     ("周期", "5 分钟"), ("初始资金", f'{params["INIT"]:,.0f} 元')]
    )

    cost_note = out["cost_assumption"]
    if sample_mode == "OOS":
        window_tag = "前向窗"
        warn_html = (
            '<div class="warn">'
            f'<b>⚠️ 样本状态：前向验证窗（未看 / OOS）。</b> 本结果是 <b>{out["start"]}~{out["end"]}</b> '
            '一段我们从未看过、未调过参的干净窗上的回测，仅跑一次、跑完即作废。'
            '判定线 <b>MDES ≈ 1.39</b>（K=1，T=2 年）：夏普 ≥ 1.39 且为正 ⇒ <b>有 edge 证据</b>；'
            '否则样本内（2021-2023）结果作废，绝不回头调参。'
            '</div>'
        )
        extra_note = (
            "样本状态：前向验证窗（未看/OOS），参数与开发窗完全一致（未调参）。"
            "判定线 MDES≈1.39（K=1, T=2年）：夏普≥1.39且正⇒有edge证据；"
            "低于线⇒样本内运气/特定行情，策略暂不成立。"
            "成本口径仍为滑点1跳（乐观），须阶段三抬滑点压力测试确认。"
        )
    else:
        window_tag = "开发窗"
        warn_html = (
            '<div class="warn">'
            '<b>⚠️ 样本状态：开发窗（已看）。</b> 本结果是 2021-2023 一段窗口上的回测，'
            '我们已看过并使用它来判定策略是否值得继续——它属于<b>样本内 / 已看</b>数据，'
            '<b>不是前向验证</b>。策略转正 ≠ 验证有效。须在未看过的干净窗（2024 或 2025）'
            '上跑一次前向验证，通过才算有 edge。'
            '</div>'
        )
        extra_note = ("样本状态：开发窗（已看）/ 样本内，非前向验证。指标口径为脚本自算"
                      f"（夏普按实测年化 {out['annual_bars']:,.0f} 根/年），与 engine 审计口径不完全一致，"
                      "数字暂不与 01~05 横比；须在未看过的干净窗（2024/2025）跑一次前向验证方算有 edge。")

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{name_cn} MA13 超短线 回测报告</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, "Microsoft YaHei", sans-serif;
         background:#f5f7fa; color:#222; margin:0; padding:28px; }}
  .wrap {{ max-width:920px; margin:0 auto; background:#fff;
          border:1px solid #e3e8ef; border-radius:12px; padding:28px 32px; }}
  h1 {{ font-size:22px; margin:0 0 4px; }}
  .sub {{ color:#788; font-size:13px; margin-bottom:20px; }}
  .cards {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin:14px 0 6px; }}
  .card {{ background:#f8fafc; border:1px solid #eef2f7; border-radius:10px;
          padding:14px 12px; text-align:center; }}
  .card .k {{ font-size:12px; color:#8a94a6; margin-bottom:6px; }}
  .card .v {{ font-size:19px; font-weight:700; }}
  .pos {{ color:#d4380d; }} .neg {{ color:#198755; }}
  .buy {{ color:#d4380d; font-weight:600; }} .sell {{ color:#198755; font-weight:600; }}
  h2 {{ font-size:16px; margin:24px 0 10px; border-left:4px solid #4c8bf5;
        padding-left:10px; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th,td {{ border-bottom:1px solid #eef2f7; padding:7px 8px; text-align:left; }}
  th {{ background:#f8fafc; color:#667; font-weight:600; }}
  .note {{ color:#99a; font-size:13px; background:#f8fafc; padding:10px 12px;
           border-radius:8px; }}
  .scroll {{ max-height:340px; overflow:auto; border:1px solid #eef2f7; border-radius:8px; }}
  .params {{ font-size:13px; color:#556; line-height:1.9; }}
  .params b {{ color:#222; }}
  details {{ margin-top:8px; }}
  details summary {{ cursor:pointer; color:#4c8bf5; font-size:13px; }}
  .foot {{ margin-top:26px; font-size:12px; color:#aab; border-top:1px solid #eef2f7;
           padding-top:12px; }}
  .warn {{ background:#fff7e6; border:1px solid #ffd591; color:#874d00;
           border-radius:10px; padding:12px 14px; font-size:13px; line-height:1.6; margin-bottom:18px; }}
  .warn b {{ color:#ad4e00; }}
</style>
</head>
<body>
  <div class="wrap">
    <h1>{name_cn}（{main_code}）· MA13 超短线 · 顺势回踩 —— 回测报告</h1>
    <div class="sub">合约 {main_code} ｜ 周期 5 分钟 ｜ {window_tag} {out['start']} ~ {out['end']} ｜ 生成于本地回测</div>
    <div class="sub" style="margin-top:-14px;color:#b45309">成本口径：{cost_note}</div>

    {warn_html}

    <h2>核心指标</h2>
    <div class="cards">{core_cards}</div>
    <h2>扩展指标</h2>
    <div class="cards">{ext_cards}</div>

    {"<h2>K线图与买卖点</h2>" + kline_svg if kline_svg else ""}

    <h2>资金权益曲线</h2>
    {equity_svg}

    <h2>回撤曲线</h2>
    {drawdown_svg}

    <h2>月度收益</h2>
    {monthly_html}

    <div class="params">
      <h2>定稿参数（用户给定，写死，不调参）</h2>
      <table><tbody>{param_rows}</tbody></table>
    </div>

    <h2>逐笔交易明细（共 {len(trades_rec)} 笔）</h2>
    <div class="scroll">
    <table>
      <thead><tr><th>方向</th><th>开仓时间</th><th>平仓时间</th><th>开仓价</th>
      <th>平仓价</th><th>手数</th><th>盈亏(元)</th><th>每手(点)</th><th>离场原因</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
    </div>
    {more}

    <h2>补充说明</h2>
    <p class="note">{extra_note}</p>

    <div class="foot">MA13 超短线 · 自包含回测（不接入 lib/engine.py）｜ 禁未来函数：信号用第 i-1 根已收盘 bar，第 i 根开盘成交。</div>
  </div>
</body></html>"""
    return html


def main():
    out, eq_series, dt_series, trades_rec, kline_history = run_backtest()
    print(json.dumps({k: v for k, v in out.items() if k != "cost_assumption"},
                     ensure_ascii=False, indent=2), flush=True)

    res_dir = os.path.join(_ROOT, "回测结果", "06_MA13超短线", name_cn)
    os.makedirs(res_dir, exist_ok=True)
    base = f"{name_cn}_MA13超短线_5min_开发窗{out['start']}_{out['end']}"
    html_path = os.path.join(res_dir, base + ".html")
    txt_path = os.path.join(res_dir, base + ".txt")
    json_path = os.path.join(res_dir, base + "_参数.json")

    html = build_html(out, eq_series, dt_series, trades_rec, kline_history)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    # 逐笔 txt
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(f"# 甲醇 MA13 超短线 逐笔成交 开发窗{out['start']}~{out['end']}\n")
        f.write(f"# 笔数={out['trades']} 胜率={out['win_rate']*100:.1f}% 夏普={out['sharpe']:.2f} "
                f"收益率={out['total_return']*100:.1f}% 最大回撤={out['max_drawdown']*100:.1f}%\n")
        f.write("方向,开仓时间,平仓时间,开仓价,平仓价,手数,盈亏(元),每手(点),离场原因\n")
        for t in trades_rec:
            f.write(f"{t['dir']},{t['entry_dt']},{t['exit_dt']},{t['entry']},{t['exit']},"
                    f"{t['hand']},{t['pnl']:.0f},{t['pnl_per_hand']},{t['reason']}\n")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"\n[done] 报告：{html_path}", flush=True)
    print(f"[done] 逐笔：{txt_path}", flush=True)
    print(f"[done] 参数：{json_path}", flush=True)

    # 双击 bat 时自动弹出 HTML；批量后台跑（环境变量 MA13_BATCH=1）时不弹
    if os.environ.get("MA13_BATCH") != "1":
        try:
            os.startfile(html_path)
        except Exception:
            pass


if __name__ == "__main__":
    main()
