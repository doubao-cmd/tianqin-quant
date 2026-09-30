# MA13 超短线 · 顺势回踩策略（5 分钟）
# 参数表（用户给定，写死，不调参）：
#   均线周期 MA_N=13；斜率周期 SLOPE_N=5（当前MA13 与 5根前 MA13 比较判趋势）
#   回踩容差 RETRACE_TOL=0.5×ATR（价格回到 MA13 附近 |close-MA13|<=0.5ATR 触发）
#   止损缓冲 STOP_BUFFER=0.5×ATR（止损放回踩极值外侧 0.5ATR）
#   兜底止损 STOP_CAP=2×ATR（止损宽度上限）
#   时间止损 MAX_HOLD=15 根；ATR 周期 14；滑点 1 跳；目标风险 1%；手数 1-8 手
# 禁未来函数：信号用第 i-1 根已收盘 bar，第 i 根开盘成交。
# 关键：回测中 K 线序列会被 data_length 截断，故在 wait_update 循环内逐根自收集完整数据。
# 预注册单假设：K+1、窗口只看一次、不调参。
import sys, os, json, time
import pandas as pd
import numpy as np
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from tqsdk import TqApi, TqSim, TqBacktest, TqAuth
import config_account

# ---------- 参数（写死）----------
KSEC     = 300
MA_N     = 13
SLOPE_N  = 5
ATR_N    = 14
RETRACE_TOL = 0.5
STOP_BUFFER = 0.5
STOP_CAP = 2.0
MAX_HOLD = 15
RISK     = 2000.0
SLIP     = 1
COMM     = 2
TICK     = 1
MULT     = 10
MAIN     = "KQ.m@CZCE.MA"
HAND_MIN = 1
HAND_MAX = 8
INIT     = 200000.0
START    = pd.Timestamp("2021-01-01")
END      = pd.Timestamp("2023-12-31")
ANNUAL_BARS = 11500

user, pwd = config_account.check()
api = TqApi(TqSim(INIT), backtest=TqBacktest(START, END), auth=TqAuth(user, pwd))
k = api.get_kline_serial(MAIN, KSEC, data_length=3000)
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
    if last_ns > 0 and pd.Timestamp(last_ns) >= END:
        break
    if time.time() - t0 > 900:
        print("[warn] 超时强制退出", flush=True)
        break

close = np.array(c_list, float)
open_ = np.array(o_list, float)
high  = np.array(h_list, float)
low   = np.array(l_list, float)
N = len(close)
print(f"[info] 收集到 K 线 {N} 根，覆盖 {pd.Timestamp(d_list[0])} ~ {pd.Timestamp(d_list[-1])}", flush=True)

# ATR(14)
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
bars_in = 0
trades = 0
wins = 0
peak = INIT
prev_eq = INIT
rets = []
bar_dds = []
blown = False

def hand_of(a):
    if np.isnan(a) or a <= 0:
        return HAND_MIN
    h = int(round(RISK / (a * TICK * MULT)))
    return max(HAND_MIN, min(HAND_MAX, h))

for i in range(MA_N + SLOPE_N + 1, N):
    if blown:
        break
    if np.isnan(ma13[i - 1]) or np.isnan(ma13[i - 1 - SLOPE_N]) or np.isnan(atr[i - 1]):
        continue
    slope = ma13[i - 1] - ma13[i - 1 - SLOPE_N]
    dir_trend = 1.0 if slope > 0 else (-1.0 if slope < 0 else 0.0)
    a = atr[i - 1]
    touch = abs(close[i - 1] - ma13[i - 1]) <= RETRACE_TOL * a

    exit_now = False
    exit_price = None
    if pos > 0:
        if low[i] <= stop_price:
            exit_now = True; exit_price = stop_price
        elif bars_in >= MAX_HOLD:
            exit_now = True; exit_price = open_[i]
    elif pos < 0:
        if high[i] >= stop_price:
            exit_now = True; exit_price = stop_price
        elif bars_in >= MAX_HOLD:
            exit_now = True; exit_price = open_[i]
    if exit_now and pos != 0:
        exit_cost = SLIP * TICK * MULT * abs(pos) + COMM * abs(pos)
        pnl = pos * (exit_price - entry_price) * MULT - exit_cost
        equity -= exit_cost
        trades += 1
        if pnl > 0:
            wins += 1
        pos = 0; bars_in = 0

    if pos == 0 and dir_trend != 0 and touch:
        hand = hand_of(a)
        entry_price = open_[i]
        oc = SLIP * TICK * MULT * hand + COMM * hand
        if dir_trend > 0:
            swing_low = np.min(low[max(0, i - SLOPE_N):i])
            stop_raw = swing_low - STOP_BUFFER * a
            stop_price = max(entry_price - STOP_CAP * a, stop_raw)
            pos = hand
        else:
            swing_high = np.max(high[max(0, i - SLOPE_N):i])
            stop_raw = swing_high + STOP_BUFFER * a
            stop_price = min(entry_price + STOP_CAP * a, stop_raw)
            pos = -hand
        equity -= oc
        bars_in = 0
    elif pos != 0:
        bars_in += 1

    if pos != 0:
        equity += pos * (close[i] - close[i - 1]) * MULT
    peak = max(peak, equity)
    dd = (peak - equity) / peak if peak > 0 else 0.0
    bar_dds.append(dd)
    if prev_eq != 0:
        rets.append((equity - prev_eq) / prev_eq)
    prev_eq = equity
    if equity <= 0:
        blown = True
        break

mean_ret = float(np.mean(rets)) if rets else 0.0
std_ret = float(np.std(rets)) if rets else 0.0
sharpe = (mean_ret / std_ret * (ANNUAL_BARS ** 0.5)) if std_ret > 1e-12 else 0.0
win_rate = (wins / trades) if trades else 0.0
max_dd = float(max(bar_dds)) if bar_dds else 0.0
bar_avg_dd = float(np.mean(bar_dds)) if bar_dds else 0.0
total_return = (equity - INIT) / INIT

out = {
    "symbol": MAIN, "period": "5min", "strategy": "MA13顺势回踩",
    "params": {"MA_N": MA_N, "SLOPE_N": SLOPE_N, "ATR_N": ATR_N,
               "RETRACE_TOL": RETRACE_TOL, "STOP_BUFFER": STOP_BUFFER,
               "STOP_CAP": STOP_CAP, "MAX_HOLD": MAX_HOLD, "RISK": RISK,
               "SLIP": SLIP, "hand_range": [HAND_MIN, HAND_MAX]},
    "start": str(START.date()), "end": str(END.date()),
    "bars": N,
    "trades": trades, "wins": wins, "win_rate": round(win_rate, 4),
    "sharpe": round(sharpe, 4), "final_equity": round(equity, 2),
    "total_return": round(total_return, 4), "max_drawdown": round(max_dd, 4),
    "bar_avg_drawdown": round(bar_avg_dd, 4), "blown": blown,
    "cost_assumption": f"滑点{SLIP}跳+佣金{COMM}元/手/边; 本金{INIT:.0f}; 目标风险{RISK:.0f}; 手数1-8ATR预算",
}
print(json.dumps(out, ensure_ascii=False, indent=2), flush=True)
with open(os.path.join(os.path.dirname(__file__), "MA13_5min_2021_2023.json"), "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
try:
    api.close()
except Exception:
    pass
