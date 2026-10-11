# -*- coding: utf-8 -*-
"""
诊断：回测口径 vs 实盘口径，同一份数据跑两遍看差多少（2026-10-09）
==========================================================================
★ **本文件只读+新建，不改任何生产代码**（不动 ma13_live_multi.py、不动回测脚本）。

╔════════════════════════════════════════════════════════════════════════╗
║ ⚠️⚠️ 2026-10-11 重要警告：本文件**刻意沿用旧记账口径**，请勿据此引用收益 ⚠️⚠️ ║
╠════════════════════════════════════════════════════════════════════════╣
║ 本脚本的目的是**对照「四种口径组合」的差异**，因此其中的 equity 累加     ║
║ （`equity += pos*(close[i]-close[i-1])*MULT`，见下方「盯市」段）是       ║
║ **刻意复刻当时各口径的原始写法**，以保证四格之间可横向对比。              ║
║                                                                        ║
║ 该写法**已知有 bug**（未排除开仓/平仓 bar 的边界误差，收益系统性虚高）：  ║
║ 详见 README §七问题 19 / 回测结果/06_MA13超短线/修复报告_20261011.md。   ║
║                                                                        ║
║ ⇒ 本脚本输出中的 Sharpe / 收益 / 回撤 **一律不可作为策略业绩依据**，     ║
║   只能用于「比较不同口径之间的相对差异」。                               ║
║ ⇒ **不要**为了让本脚本「记账正确」而只改其中一处 —— 那会破坏四格可比性。 ║
╚════════════════════════════════════════════════════════════════════════╝

## 为什么有这个文件
回测报 PVC 2021-2023 Sharpe 3.65 / 胜率 46.9%，模拟盘实测 16 笔胜率 19%。
两者不可能同时对。逐行对比源码后找到**四处口径差异**（见下），本脚本的
目的是**量化**每一处各自贡献了多少差异，而不是猜。

## 四处候选差异（都来自读源码，不是推测）
  D1 成交价：回测用 `open_[i]`（下一根开盘） vs 实盘记 `close`（当根收盘）
  D2 止损判据用的bar：回测 `low[i]`（本根） vs 实盘 `l = bar["low"]`（同一根，应同）
  D3 出场后同根重开：两边**都是**允许的（第 734行清pos → 第 751 行判 pos==0）
  D4 数据窗口：实盘 `data_length=200`（滚动） vs 回测全历史（但 swing 只回看 5 根，应等价）

## 怎么用
    python 工具/诊断_回测实测差异_20261009.py                # 默认 PVC 2026 探针窗（快）
    python 工具/诊断_回测实测差异_20261009.py --key V --start 2021-01-01 --end 2023-12-31
    python 工具/诊断_回测实测差异_20261009.py --only 口径A口径B   # 只跑指定组合

## 输出
一张对照表：四种口径组合下的笔数/胜率/Sharpe/总收益，直接看差异出在哪一格。
"""
import sys
import os
import time
import math
import json
import argparse

import numpy as np
import pandas as pd

# ---- 找项目根（向上直到含 lib 的目录）----
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from tqsdk import TqApi, TqSim, TqBacktest, TqAuth
import config_account
import config_symbols

# ==================== 参数（全部照抄生产代码，一个数字都不改）====================
KSEC = 300
MA_N = 13
SLOPE_N = 5
ATR_N = 14
RETRACE_TOL = 0.5
STOP_BUFFER = 0.5
STOP_CAP = 2.0
MAX_HOLD = 15
RISK_PCT = 0.01
SLIP = 1
HAND_MIN = 1
HAND_MAX = 8
NOTIONAL_CAP = 2.0
PRINCIPAL = 200000.0
INIT = 200000.0
WARMUP_DAYS = 7
# 品种相关（运行时由 main() 从 config_symbols 填入，勿在此硬编码）
name_cn = ""
main_code = ""
MULT = 1.0
TICK = 1.0
COMM = 0.0
_START_NS = None
# ===============================================================================


def _base_hand(a, equity):
    """照抄回测脚本 _base_hand（2026-10-07 复利口径）。"""
    if math.isnan(a) or a <= 0:
        return HAND_MIN
    if equity is None or math.isnan(equity) or equity <= 0:
        equity = PRINCIPAL
    h = int(round((equity * RISK_PCT) / (a * TICK * MULT)))
    return max(HAND_MIN, min(HAND_MAX, h))


def hand_plan(a, equity, price=None):
    """照抄回测脚本 hand_plan。"""
    h = _base_hand(a, equity)
    eq = equity if (equity and not math.isnan(equity) and equity > 0) else PRINCIPAL
    if price and price > 0 and not math.isnan(price):
        名义手数 = int((eq * NOTIONAL_CAP) / (price * MULT))
        h = min(h, max(HAND_MIN, 名义手数))
    return h


def _stop_price(dir_trend, entry, a, swing):
    """照抄 ma13_live_multi.py:419 _stop_price（含 10-08 bugfix 的夹紧）。"""
    tick = 1.0
    if dir_trend > 0:
        stop = max(entry - STOP_CAP * a, swing - STOP_BUFFER * a)
        if stop >= entry - tick:
            stop = entry - STOP_CAP * a
    else:
        stop = min(entry + STOP_CAP * a, swing + STOP_BUFFER * a)
        if stop <= entry + tick:
            stop = entry + STOP_CAP * a
    return stop


def 收集K线(main_code, _START, _END, timeout=1800):
    """照抄回测脚本的取数段（自收集完整序列，绕过 data_length 截断）。"""
    bt_start = _START - pd.Timedelta(days=WARMUP_DAYS)
    api = TqApi(TqSim(INIT), backtest=TqBacktest(bt_start, _END),
                auth=TqAuth(*config_account.check()))
    k = api.get_kline_serial(main_code, KSEC, data_length=3000)
    print("[info] 拉取 K 线并逐根收集...", flush=True)
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
        if time.time() - t0 > timeout:
            print("[warn] 超时强制退出", flush=True)
            break
    try:
        api.close()
    except Exception:
        pass
    return (np.array(c_list, float), np.array(o_list, float),
            np.array(h_list, float), np.array(l_list, float),
            np.array(d_list, int))


def 跑口径(close, open_, high, low, dts_arr, label,
           成交价源="开盘", 允许同根重开=True, 取整="ma"):
    """跑一遍历史。三个可切换开关就是用来隔离 D1/D3 的。

    成交价源   : "开盘" = open_[i]（回测口径）/ "收盘" = close[i]（实盘记账口径）
    允许同根重开 : False = 出场后当根不再开仓（用来验证 D3的影响）
    取整      : "ma" = 每根都判（回测）；"floor" = 只在最后一根判（实盘的 i=n-1 语义）
    """
    N = len(close)
    # 指标（照抄回测，第 146~155 行）
    prev_close = np.roll(close, 1); prev_close[0] = close[0]
    tr = np.maximum(high - low,
                    np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    atr = np.full(N, np.nan)
    for i in range(1, N):
        atr[i] = np.mean(tr[max(1, i - ATR_N):i]) if i >= 2 else tr[i]
    ma13 = np.full(N, np.nan)
    for i in range(MA_N, N):
        ma13[i] = np.mean(close[i - MA_N:i])

    equity = INIT
    pos = 0
    entry_price = stop_price = 0.0
    bars_in = 0
    cur_dir = 0
    cur_hand = 0
    cur_entry_price = 0.0
    cur_entry_dt = 0
    blown = False
    trades = wins = 0
    comm_total = 0.0
    recs = []
    peak = equity
    bar_dds = []
    rets = []
    prev_eq = equity
    eq_series, dt_series = [], []
    start_i = MA_N + SLOPE_N + 1
    start_ns = _START_NS.value

    for i in range(start_i, N):
        if blown:
            break
        if dts_arr[i] < start_ns:
            continue
        # ---- 取整：回测每根都判；实盘只看最后一根 ----
        if 取整 == "floor":
            if i != N - 1:
                # 只推进计数，不做决策
                if pos != 0:
                    bars_in += 1
                    # ⚠️ 见文件头警告：此处沿用旧口径以复现历史对照，收益不可引用
                    equity += pos * (close[i] - close[i - 1]) * MULT
                    if prev_eq != 0:
                        rets.append((equity - prev_eq) / prev_eq)
                    prev_eq = equity
                peak = max(peak, equity)
                bar_dds.append((peak - equity) / peak if peak > 0 else 0.0)
                eq_series.append(equity); dt_series.append(dts_arr[i])
                continue

        slope = ma13[i - 1] - ma13[i - 1 - SLOPE_N]
        dir_trend = 1.0 if slope > 0 else (-1.0 if slope < 0 else 0.0)
        a = atr[i - 1]
        touch = abs(close[i - 1] - ma13[i - 1]) <= RETRACE_TOL * a
        px = open_[i] if 成交价源 == "开盘" else close[i]

        # ---- 出场 ----
        exit_now = False; exit_price = 0.0; reason = ""
        if pos > 0:
            if low[i] <= stop_price:
                exit_now = True; exit_price = stop_price; reason = "止损"
            elif bars_in >= MAX_HOLD:
                exit_now = True; exit_price = px; reason = "时间止损"
        elif pos < 0:
            if high[i] >= stop_price:
                exit_now = True; exit_price = stop_price; reason = "止损"
            elif bars_in >= MAX_HOLD:
                exit_now = True; exit_price = px; reason = "时间止损"

        if exit_now and pos != 0:
            exit_cost = SLIP * TICK * MULT * abs(pos) + COMM * abs(pos)
            pnl = pos * (exit_price - cur_entry_price) * MULT - exit_cost
            equity -= exit_cost
            comm_total += COMM * abs(pos)
            trades += 1
            if pnl > 0:
                wins += 1
            recs.append({"dir": "多" if cur_dir > 0 else "空",
                         "entry_dt": pd.Timestamp(cur_entry_dt).strftime("%Y-%m-%d %H:%M"),
                         "exit_dt": pd.Timestamp(dts_arr[i]).strftime("%Y-%m-%d %H:%M"),
                         "entry": round(cur_entry_price, 2), "exit": round(exit_price, 2),
                         "hand": cur_hand, "pnl": round(pnl, 2), "reason": reason})
            pos = 0; bars_in = 0

        # ---- 开仓 ----
        can_open = (pos == 0 and dir_trend != 0 and touch)
        if exit_now and not 允许同根重开:
            can_open = False
        if can_open:
            hand = hand_plan(a, equity, px)
            entry_price = px
            oc = SLIP * TICK * MULT * hand + COMM * hand
            if dir_trend > 0:
                swing_low = float(np.min(low[max(0, i - SLOPE_N):i]))
                stop_price = _stop_price(1, entry_price, a, swing_low)
                pos = hand
            else:
                swing_high = float(np.max(high[max(0, i - SLOPE_N):i]))
                stop_price = _stop_price(-1, entry_price, a, swing_high)
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

        # ---- 盯市 ----
        if pos != 0:
            # ⚠️ 见文件头警告：此处沿用旧口径以复现历史对照，收益不可引用
            equity += pos * (close[i] - close[i - 1]) * MULT
        peak = max(peak, equity)
        dd = (peak - equity) / peak if peak > 0 else 0.0
        bar_dds.append(dd)
        eq_series.append(equity); dt_series.append(dts_arr[i])
        if prev_eq != 0:
            rets.append((equity - prev_eq) / prev_eq)
        prev_eq = equity
        if equity <= 0:
            blown = True
            break

    mean_ret = float(np.mean(rets)) if rets else 0.0
    std_ret = float(np.std(rets)) if rets else 0.0
    years = (dts_arr[-1] - dts_arr[0]) / 1e9 / (365.25 * 86400)
    annual_bars = N / years if years > 0 else 11500.0
    sharpe = (mean_ret / std_ret * math.sqrt(annual_bars)) if std_ret > 1e-12 else 0.0
    return {
        "口径": label, "笔数": trades, "胜率": round(wins / trades, 4) if trades else 0.0,
        "Sharpe": round(sharpe, 3), "总收益": round((equity - INIT) / INIT, 4),
        "最大回撤": round(float(max(bar_dds)) if bar_dds else 0.0, 4),
        "最终权益": round(equity, 2), "爆仓": blown,
        "_recs": recs, "_N": N,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", default="V", help="品种 key，默认 V(PVC)")
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--end", default="2026-09-26")
    ap.add_argument("--out", default=None, help="结果 json 落盘路径")
    ap.add_argument("--dump-recs", action="store_true", help="导出各口径成交明细")
    args = ap.parse_args()

    global MULT, TICK, COMM, _START_NS, name_cn, main_code
    name_cn, main_code, MULT, TICK = config_symbols.get(args.key)
    COMM = float(config_symbols.COST[args.key])
    _START = pd.Timestamp(args.start)
    _END = pd.Timestamp(args.end)
    _START_NS = _START

    print(f"[info] 品种={name_cn}({main_code}) 乘数={MULT} 一跳={TICK} 手续费={COMM}")
    print(f"[info] 区间 {_START.date()} ~ {_END.date()}", flush=True)

    close, open_, high, low, dts = 收集K线(main_code, _START, _END)
    N = len(close)
    print(f"[info] 收到 {N} 根 K线，覆盖 {pd.Timestamp(dts[0])} ~ {pd.Timestamp(dts[-1])}")
    if N < MA_N + SLOPE_N + 10:
        print("[error] K 线太少，检查区间/品种")
        return

    口径表 = [
        ("A原回测口径(开盘成交,每根判,允许同根重开)", dict(成交价源="开盘", 允许同根重开=True,  取整="ma")),
        ("B 实盘记账口径(收盘成交,每根判,允许同根重开)", dict(成交价源="收盘", 允许同根重开=True,  取整="ma")),
        ("C 开盘成交 + 禁同根重开",                     dict(成交价源="开盘", 允许同根重开=False, 取整="ma")),
        ("D 收盘成交 + 禁同根重开",                     dict(成交价源="收盘", 允许同根重开=False, 取整="ma")),
    ]

    rows = []
    for label, kw in 口径表:
        print(f"[run ] {label} ...", flush=True)
        r = 跑口径(close, open_, high, low, dts, label, **kw)
        rows.append(r)
        print(f"       笔数={r['笔数']:5d} 胜率={r['胜率']:6.1%} "
              f"Sharpe={r['Sharpe']:7.2f} 总收益={r['总收益']:8.1%}", flush=True)

    # ---- 对照表 ----
    print("\n" + "=" * 92)
    print(f"【{name_cn} {_START.date()} ~ {_END.date()}】回测口径 vs 实盘口径 对照")
    print("=" * 92)
    print(f"{'口径':46}{'笔数':>7}{'胜率':>8}{'Sharpe':>9}{'总收益':>10}")
    print("-" * 92)
    for r in rows:
        print(f"{r['口径']:46}{r['笔数']:>7}{r['胜率']:>8.1%}{r['Sharpe']:>9.2f}{r['总收益']:>10.1%}")

    a, b = rows[0], rows[1]
    print("-" * 92)
    print("★ D1 成交价口径（开盘 → 收盘）单独贡献：")
    print(f"    笔数 {a['笔数']} → {b['笔数']}（{b['笔数']-a['笔数']:+d}）"
          f"   胜率 {a['胜率']:.1%} → {b['胜率']:.1%}"
          f"   Sharpe {a['Sharpe']:.2f} → {b['Sharpe']:.2f}")
    c, d = rows[2], rows[3]
    print("★ D3 同根重开（允许 → 禁止）在开盘口径下贡献：")
    print(f"    笔数 {a['笔数']} → {c['笔数']}（{c['笔数']-a['笔数']:+d}）"
          f"   胜率 {a['胜率']:.1%} → {c['胜率']:.1%}"
          f"   Sharpe {a['Sharpe']:.2f} → {c['Sharpe']:.2f}")
    print("=" * 92)
    print("★ 模拟盘实测参照：16 笔/ 胜率 19% / 合计 −8,180 元（2026-10-08~09）")

    if args.out:
        out = {"品种": name_cn, "区间": f"{_START.date()}~{_END.date()}",
               "K线根数": N, "结果": [{k: v for k, v in r.items() if not k.startswith("_")}
                              for r in rows]}
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"[save] {args.out}")
    if args.dump_recs:
        for r in rows:
            p = f"工具/_输出/_差异诊断_{name_cn}_{r['口径'][:1]}.csv"
            os.makedirs(os.path.dirname(p), exist_ok=True)
            pd.DataFrame(r["_recs"]).to_csv(p, index=False, encoding="utf-8-sig")
            print(f"[save] {p}（{len(r['_recs'])} 笔）")


if __name__ == "__main__":
    main()