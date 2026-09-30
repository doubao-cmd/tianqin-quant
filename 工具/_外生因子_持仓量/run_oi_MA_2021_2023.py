# 持仓量变化(OI-Flow) 独立信号 · 单品种速测（MA 甲醇 · 2021-2023 · 30min）
# 说明：独立回测器，不调用 engine.run_backtest（避免改冻结代码）。
#       信号规则见 预注册卡_持仓量独立信号.md §2。换月屏蔽 OI 信号（数据卫生）。
#       ⚠️ 这是用户授权的单品种速测（n=1），非正式 9 品种均值判定，不出统计结论。

import sys, json, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import numpy as np
import pandas as pd
from datetime import datetime
import tqsdk
from tqsdk import TqApi, TqSim, TqAuth
from tqsdk.backtest import TqBacktest
import config_symbols, config_account

SYMBOL = "MA"
START = datetime(2021, 1, 1)
END   = datetime(2023, 12, 31)
KSEC  = 1800          # 30min
LOOK  = 20            # 回看窗（根）
MAX_HOLD = 30         # 最大持仓根数（30分钟开仓保护：约15小时强制平）
GUARD    = 3           # 开仓后前 GUARD 根不触发价格止损（防噪音扫损）
SL_MULT  = 2.0         # 初始价格止损倍数：2×ATR（开仓即设死）
BE_TRIGGER = 2.0        # 盈利达 BE_TRIGGER×ATR 后启动保本止损
TRAIL   = 2.0           # 移动止损距离：TRAIL×ATR（保本后跟随上移）
RISK  = 2000.0        # 单笔风险（元）= 1% × 20万
INIT  = 200000.0
SLIP  = 2             # 滑点（跳）
COMM  = 2.0           # 佣金（元/手/单边，甲醇估值）

name, main_code, mult, tick = config_symbols.get(SYMBOL)
user, pwd = config_account.check()
print(f"[info] {name} {main_code} mult={mult} tick={tick} | 窗 {START.date()}~{END.date()}")

api = TqApi(TqSim(INIT), backtest=TqBacktest(START, END), auth=TqAuth(user, pwd))
k = api.get_kline_serial(main_code, KSEC, data_length=9000)
print("[info] 等待回测填充 K 线...")
import time
_end_ns = int(pd.Timestamp(END).value)
for _ in range(300000):
    try:
        upd = api.wait_update(deadline=time.time() + 30)
    except Exception:
        break
    last = k["datetime"].iloc[-1]
    if not isinstance(last, (int, float)) or last <= 0:
        continue
    if last >= _end_ns:
        break
    if not upd:
        break
api.close()
print(f"[info] K 线行数 = {len(k)}")

# 转数组
close = k["close"].to_numpy(float)
high  = k["high"].to_numpy(float)
low   = k["low"].to_numpy(float)
oi    = k["close_oi"].to_numpy(float)   # tqsdk K线持仓量字段是 close_oi/open_oi，非 open_interest
dt    = pd.to_datetime(k["datetime"].to_numpy())
under = k["underlying_symbol"].to_numpy(object) if "underlying_symbol" in k.columns else None

# 过滤到窗口内
mask = (dt >= START) & (dt <= END)
idx = np.where(mask)[0]
if len(idx) == 0:
    print("[err] 窗口内无数据"); sys.exit(1)
s, e = idx[0], idx[-1] + 1
close = close[s:e]; high = high[s:e]; low = low[s:e]; oi = oi[s:e]; dt = dt[s:e]
under = under[s:e] if under is not None else None
N = len(close)
print(f"[info] 窗口内有效 K 线 = {N}")

# ATR(20)
tr = np.empty(N)
tr[0] = high[0] - low[0]
for i in range(1, N):
    tr[i] = max(high[i]-low[i], abs(high[i]-close[i-1]), abs(low[i]-close[i-1]))
atr = np.full(N, np.nan)
for i in range(20, N):
    atr[i] = np.mean(tr[i-20:i])

# 模拟
equity = INIT
pos = 0          # 带符号手数
entry_price = 0.0
entry_cost = 0.0
entry_atr = 0.0   # 入场根 ATR（用于 2×ATR 初始止损）
stop_level = 0.0  # 当前止损价（固定→保本→移动，只向有利方向移动）
bars_in = 0
peak = INIT
prev_eq = INIT
rets = []
trades = 0
wins = 0
rollover_skips = 0
min_eq = INIT
trade_max_adverse = 0.0   # 当前持仓最大不利变动（价格点）
dd_trades = []            # 每笔交易最大浮亏回撤（比例）
dd_bar = []              # 每根 bar 权益相对峰值回撤（比例）
blown = False             # 是否爆仓（权益<=0）

for i in range(LOOK + 1, N):
    # 强平：权益<=0 即爆仓，停止交易（模拟真实账户）
    if equity <= 0:
        if pos != 0:
            pos = 0
        blown = True
        break
    rollover = (under is not None) and (under[i-1] != under[i-1 - LOOK])
    # 换月代理判定：单根 OI 跳变 >10%（主连切换时持仓参照物突变）
    if (not rollover) and oi[i-1] > 0 and oi[i-2] > 0 and abs(oi[i-1] - oi[i-2]) / oi[i-2] > 0.10:
        rollover = True
    if rollover:
        rollover_skips += 1
    # 用 bar i-1 的数据产生信号
    oi_flow = oi[i-1] - oi[i-1 - LOOK]
    ret = close[i-1] - close[i-1 - LOOK]
    desired = 0
    if (not rollover) and (not np.isnan(oi_flow)) and oi_flow > 0:
        desired = 1 if ret > 0 else -1

    # 1) 持仓中：先记账 + 判出场
    if pos != 0:
        equity += pos * (close[i] - close[i-1]) * mult
        bars_in += 1
        # 当前持仓最大浮亏（价格点）
        if pos > 0:
            trade_max_adverse = max(trade_max_adverse, entry_price - close[i])
        else:
            trade_max_adverse = max(trade_max_adverse, close[i] - entry_price)
        exit_now = False
        if oi_flow <= 0:
            exit_now = True
        if bars_in >= MAX_HOLD:
            exit_now = True
        if desired != 0 and desired == -np.sign(pos):
            exit_now = True
        # 固定2×ATR初始止损 + 保本/移动止损（前 GUARD 根不触发，防噪音扫损）
        if bars_in >= GUARD and entry_atr > 0:
            if pos > 0:
                # 盈利达 BE_TRIGGER×ATR → 启动保本，之后 trailing 跟随上移
                if close[i] >= entry_price + BE_TRIGGER * entry_atr:
                    cand = max(entry_price, close[i] - TRAIL * entry_atr)
                    stop_level = max(stop_level, cand)
                if close[i] <= stop_level:
                    exit_now = True
            else:
                if close[i] <= entry_price - BE_TRIGGER * entry_atr:
                    cand = min(entry_price, close[i] + TRAIL * entry_atr)
                    stop_level = min(stop_level, cand)
                if close[i] >= stop_level:
                    exit_now = True
        if exit_now:
            exit_cost = SLIP * tick * mult * abs(pos) + COMM * abs(pos)
            equity -= exit_cost
            pnl = pos * (close[i] - entry_price) * mult - entry_cost - exit_cost
            trades += 1
            if pnl > 0:
                wins += 1
            dd_trades.append(trade_max_adverse / entry_price if entry_price > 0 else 0.0)
            pos = 0; entry_price = 0.0; entry_cost = 0.0; entry_atr = 0.0; bars_in = 0; trade_max_adverse = 0.0

    # 2) 空仓：按信号开仓
    if pos == 0 and desired != 0 and (not rollover):
        a = atr[i-1]
        if np.isnan(a) or a <= 0:
            hand = 1
        else:
            hand = max(1, int(RISK / (a * mult)))
        entry_price = close[i]
        entry_cost = SLIP * tick * mult * hand + COMM * hand
        equity -= entry_cost
        pos = desired * hand
        entry_atr = atr[i-1]
        if desired > 0:
            stop_level = entry_price - SL_MULT * entry_atr
        else:
            stop_level = entry_price + SL_MULT * entry_atr
        bars_in = 0

    # 3) 记账
    peak = max(peak, equity)
    min_eq = min(min_eq, equity)
    if equity > 0:
        dd_bar.append((peak - equity) / peak)
    if prev_eq != 0:
        rets.append((equity - prev_eq) / prev_eq)
    prev_eq = equity

# 统计
rets = np.array(rets)
total_ret = equity / INIT - 1.0
if len(rets) > 1 and rets.std() > 0:
    sharpe = rets.mean() / rets.std() * np.sqrt(252 * 8)
else:
    sharpe = 0.0
max_dd = (peak - min_eq) / peak if peak > 0 else 0.0
avg_dd_bar = float(np.mean(dd_bar)) if dd_bar else 0.0
avg_dd_trade = float(np.mean(dd_trades)) if dd_trades else 0.0
win_rate = wins / trades if trades else 0.0

out = {
    "symbol": SYMBOL, "name": name, "window": f"{START.date()}~{END.date()}",
    "init": INIT, "final_equity": round(equity, 2),
    "total_return": round(total_ret, 4), "annual_sharpe": round(sharpe, 3),
    "max_drawdown": round(max_dd, 4), "avg_drawdown_bar": round(avg_dd_bar, 4),
    "avg_drawdown_trade": round(avg_dd_trade, 4),
    "trades": trades, "win_rate": round(win_rate, 4),
    "rollover_skips": rollover_skips, "blown": blown,
    "max_hold": MAX_HOLD, "sl_mult": SL_MULT, "guard": GUARD,
    "be_trigger": BE_TRIGGER, "trail": TRAIL,
    "cost_assumption": f"滑点{SLIP}跳+佣金{COMM}元/手/边; 单笔风险{RISK}元; 手数=ATR预算; 初始止损{SL_MULT}×ATR; 盈利{BE_TRIGGER}×ATR启动保本+移动{TRAIL}×ATR; 持仓上限{MAX_HOLD}根; 权益<=0爆仓停止",
}
print(json.dumps(out, ensure_ascii=False, indent=2))
with open("工具/_外生因子_持仓量/MA_OI_2021_2023_v3.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
