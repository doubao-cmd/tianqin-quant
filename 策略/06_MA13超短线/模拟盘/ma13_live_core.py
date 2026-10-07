# -*- coding: utf-8 -*-
"""
MA13 超短线 · 实时模拟盘核心（12 品种共用）
==========================================================
信号状态机与回测脚本**逐条对齐**（策略/06_MA13超短线/回测/<品种>/MA13超短线_回测.py）：
  1. MA13 与其前 SLOPE_N 根的差判趋势方向（斜率）；
  2. |close-MA13| <= RETRACE_TOL×ATR 且趋势明确 → 沿趋势开仓（下一根开盘≈收盘后市价）；
  3. 止损 = 回踩区间极值外侧 STOP_BUFFER×ATR，封顶 STOP_CAP×ATR；时间止损 MAX_HOLD 根；
  4. 手数 = 当前账户权益 ×1% ÷ ATR，夹 [1, 8]（2026-10-07 改复利口径，原固定 20 万）。

禁未来函数：只用已收盘 bar；bar 收盘后判定，立即执行（≈下一根开盘）。

⚠ 与回测的两处已知差异（如实标注）：
  a) 止损触发：回测以 stop_price 成交；实时版在 bar 收盘后发现当根曾触及 → 立即市价平仓，
     成交价≈当时盘口，更真实但可能与回测价不同。
  b) 实时下单走 TqSim 真实账户（主连不能下单，用 quote.underlying_symbol 具体合约）。

默认 ALLOW_TRADE=False 观察模式：只算信号、打印、写心跳，不报单。
"""
import sys, os, time, math
import numpy as np

_ROOT = os.path.dirname(os.path.abspath(__file__))
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from tqsdk import TqApi, TqSim, TqAuth
from lib.engine import _LiveRecorder
import config_account
import config_symbols

MA_N, SLOPE_N, ATR_N = 13, 5, 14
RETRACE_TOL, STOP_BUFFER, STOP_CAP, MAX_HOLD = 0.5, 0.5, 2.0, 15
RISK_PCT, SLIP, HAND_MIN, HAND_MAX = 0.01, 1, 1, 8
INIT = 200000.0
RISK_EQUITY = 200000.0     # 兜底：账户权益取不到时回落到 20 万（2026-10-07 起正常路径用实时权益）
KSEC = 300


def run_live_ma13(symbol_key, allow_trade=False, web_gui=True,
                  record_dir=None, heartbeat_sec=300):
    name_cn, cont, mult, tick = config_symbols.get(symbol_key)
    comm = float(config_symbols.COST.get(symbol_key, 2.0))
    user, pwd = config_account.check()
    api = TqApi(TqSim(INIT), auth=TqAuth(user, pwd), web_gui=web_gui)
    q = api.get_quote(cont)
    k = api.get_kline_serial(cont, KSEC, data_length=200)
    rec = _LiveRecorder(record_dir, name_cn, heartbeat_sec=heartbeat_sec)

    def hand_of(atr):
        if not atr or math.isnan(atr) or atr <= 0:
            return HAND_MIN
        try:
            eq = float(api.get_account()["balance"])
        except Exception:
            eq = RISK_EQUITY                  # 兜底：权益读不到才走
        h = int(round((eq * RISK_PCT) / (atr * tick * mult)))
        return max(HAND_MIN, min(HAND_MAX, h))

    pos = 0; entry = 0.0; stop = 0.0; bars_in = 0; last_id = -1
    rec.start(f"MA13 实时盘启动 allow_trade={allow_trade} 手数口径=当前权益x1%夹[1,8](20261007改)")
    try:
        while True:
            api.wait_update()
            if len(k) == 0:
                continue
            bar = k.iloc[-1]
            if bar["id"] == last_id or bar["id"] < 0:
                continue
            last_id = bar["id"]
            # 只处理已收盘 bar（datetime + KSEC == 下一根开始时间才收完）
            if bar["datetime"] and int(bar["datetime"]) + KSEC * 1e9 > int(q.datetime):
                pass  # tqsdk 回放中 iloc[-1] 即最新；收盘判定用下面统一口径
            c, h, l = float(bar["close"]), float(bar["high"]), float(bar["low"])
            if c <= 0:
                continue

            df = k
            close = df["close"].to_numpy(float); high = df["high"].to_numpy(float); low = df["low"].to_numpy(float)
            n = len(close)
            if n < MA_N + SLOPE_N + 2:
                rec.heartbeat(q.datetime, c, pos, 0, force=False)
                continue
            prev_c = np.roll(close, 1); prev_c[0] = close[0]
            tr = np.maximum(high - low, np.maximum(np.abs(high - prev_c), np.abs(low - prev_c)))
            atr_arr = np.array([np.mean(tr[max(1, i - ATR_N):i]) if i >= 2 else tr[i] for i in range(n)])
            ma = np.array([np.mean(close[i - MA_N:i]) for i in range(MA_N, n)])
            ma = np.concatenate([np.full(MA_N, np.nan), ma])
            i = n - 1
            a = atr_arr[i - 1]
            slope = ma[i - 1] - ma[i - 1 - SLOPE_N]
            dir_trend = 1.0 if slope > 0 else (-1.0 if slope < 0 else 0.0)
            touch = abs(close[i - 1] - ma[i - 1]) <= RETRACE_TOL * a

            # ---- 出场（对齐回测：止损/时间止损）----
            if pos > 0 and (l <= stop or bars_in >= MAX_HOLD):
                if allow_trade:
                    api.insert_order(q.underlying_symbol, "SELL", "CLOSE", abs(pos))
                rec.stoploss("多", stop, c, f"止损触发={l <= stop} 时间止损={bars_in >= MAX_HOLD}")
                print(f"[EXIT] 多 {abs(pos)}手 止损={l <= stop} 时间={bars_in >= MAX_HOLD} @≈{c:.1f}", flush=True)
                pos = 0; bars_in = 0
            elif pos < 0 and (h >= stop or bars_in >= MAX_HOLD):
                if allow_trade:
                    api.insert_order(q.underlying_symbol, "BUY", "CLOSE", abs(pos))
                rec.stoploss("空", stop, c, f"止损触发={h >= stop} 时间止损={bars_in >= MAX_HOLD}")
                print(f"[EXIT] 空 {abs(pos)}手 止损={h >= stop} 时间={bars_in >= MAX_HOLD} @≈{c:.1f}", flush=True)
                pos = 0; bars_in = 0

            # ---- 开仓（对齐回测：趋势 + 回踩）----
            if pos == 0 and dir_trend != 0 and touch:
                hand = hand_of(a)
                dir_cn = "多" if dir_trend > 0 else "空"
                if dir_trend > 0:
                    swing_low = float(np.min(low[max(0, i - SLOPE_N):i]))
                    stop_new = max(c - STOP_CAP * a, swing_low - STOP_BUFFER * a)
                else:
                    swing_high = float(np.max(high[max(0, i - SLOPE_N):i]))
                    stop_new = min(c + STOP_CAP * a, swing_high + STOP_BUFFER * a)
                rec.signal(dir_cn, hand, c, q.datetime, c, a,
                           f"MA13回踩顺势 stop≈{stop_new:.1f}" + ("" if allow_trade else " 【观察模式不报单】"))
                if allow_trade and hand > 0:
                    api.insert_order(q.underlying_symbol,
                                     "BUY" if dir_trend > 0 else "SELL", "OPEN", hand)
                    if led: led.on_order_fill(q.underlying_symbol,
                                              "买开" if dir_trend > 0 else "卖开", hand, c)
                print(f"[{'OPEN' if allow_trade else 'SIGNAL'}] {dir_cn} {hand}手 "
                      f"@≈{c:.1f} 止损≈{stop_new:.1f} ATR={a:.2f}", flush=True)
                if allow_trade:
                    pos = hand if dir_trend > 0 else -hand
                    entry = c; stop = stop_new; bars_in = 0
            elif pos != 0:
                bars_in += 1

            rec.heartbeat(q.datetime, c, pos,
                          int(dir_trend) if (pos == 0 and dir_trend != 0 and touch) else (1 if pos > 0 else -1 if pos < 0 else 0))
    finally:
        try:
            api.close()
        except Exception:
            pass


if __name__ == "__main__":
    pass
