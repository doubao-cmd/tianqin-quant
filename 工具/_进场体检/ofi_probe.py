# -*- coding: utf-8 -*-
# ofi_probe.py — 盘口资金流(OFI) 进场体检探针
# ================================================================
# 维度：逐笔/L2 盘口资金流（元结论点名的唯一未证伪维度「换信息源」）。
# 数据：tqsdk 回测模式拉历史 tick（免费版 Level-1：last_price/ask1/ask_vol1/bid1/bid_vol1，
#       约 2 笔/秒），自聚 5min 桶 → 每桶算 OFI(订单流失衡) 与 OHLC。
# 信号：sign(OFI) = 方向（下一根开盘入场，沿用进场体检纪律）。
# 纪律：复用 进场体检.py 的 cost_points / _atr / cs，保证与已死 60 格同口径。
#       只能判死不能判生；免费 Level-1 太粗，FAIL 仅说明此档数据源无 edge，不能宣告维度死。
# 禁未来函数：bar i 的 OFI 只用 [bar_i 起, bar_i 收] 内的 tick，入场在下根开盘，因果成立。
import sys, os, time, json, argparse, math, importlib.util
from datetime import date, datetime, timedelta
import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
# 项目根（config_account.py 所在）——与回测脚本同源取凭证
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
# 复用 进场体检.py 的成本模型/ATR/配置（同口径，不动主线）
_spec = importlib.util.spec_from_file_location(
    "jp_mod", os.path.join(_HERE, "进场体检.py"))
jp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(jp)

SLIP       = jp.SLIP
STOP_MULT  = jp.STOP_ATR_MULT
HOLD       = jp.HOLD
ATR_N      = jp.ATR_N
cost_points = jp.cost_points
_atr        = jp._atr
cs          = jp.cs

from tqsdk import TqApi, TqBacktest, BacktestFinished, TqAuth

import config_account  # 项目根凭证（check()=回测账号，与 MA13 回测同源，不占模拟盘席位）


def _tick_to_bars(ticks, bar_sec=300):
    """纯函数：ticks = iterable of (dt_ns, last_price, ask1, bid1, volume)。
    聚成 5min 桶 → 返回 dict{o,h,l,c,ofi} ndarray + n_ticks。
    无未来函数：bar i 的 ofi 只用 [bar_i 起, bar_i 收] 内的 tick。"""
    bars = {}; order = []; prev_vol = {}; last_for_bar = {}
    n_ticks = 0
    for (dt, lp, a1, b1, v) in ticks:
        if lp is None or lp != lp or lp <= 0:
            continue
        bt = (dt // (bar_sec * 1_000_000_000)) * (bar_sec * 1_000_000_000)
        b = bars.get(bt)
        if b is None:
            b = {"o": lp, "h": lp, "l": lp, "c": lp,
                 "vol": 0.0, "ofi": 0.0, "day": dt // 86_400_000_000_000}
            bars[bt] = b; order.append(bt)
        b["h"] = max(b["h"], lp); b["l"] = min(b["l"], lp); b["c"] = lp
        day = b["day"]
        pv = prev_vol.get(day)
        dv = (v - pv) if pv is not None else 0.0
        prev_vol[day] = v
        if dv > 0:
            prev_lp = last_for_bar.get(bt)
            if a1 == a1 and b1 == b1 and a1 > 0 and b1 > 0:
                if lp >= a1:
                    s = +dv
                elif lp <= b1:
                    s = -dv
                elif prev_lp is not None:
                    s = dv if lp > prev_lp else (-dv if lp < prev_lp else 0.0)
                else:
                    s = 0.0
            elif prev_lp is not None:
                s = dv if lp > prev_lp else (-dv if lp < prev_lp else 0.0)
            else:
                s = 0.0
            b["ofi"] += s
        last_for_bar[bt] = lp
        n_ticks += 1
    if not order:
        return None
    order.sort()
    return {
        "o": np.array([bars[k]["o"] for k in order], float),
        "h": np.array([bars[k]["h"] for k in order], float),
        "l": np.array([bars[k]["l"] for k in order], float),
        "c": np.array([bars[k]["c"] for k in order], float),
        "ofi": np.array([bars[k]["ofi"] for k in order], float),
        "n_ticks": n_ticks,
    }


def _build_bars(symbol, start, end, bar_sec=300, buf=4000):
    """tqsdk 回测拉历史 tick，流式收集后交给 _tick_to_bars 聚合。
    ⚠️ tick 序列是定长滚动窗口：缓冲区填满后 len(t) 不再增长，
    必须用 datetime 游标（而非行号增量）识别新到 tick，否则整月一个都收不到。"""
    user, pwd = config_account.check()
    api = TqApi(backtest=TqBacktest(start_dt=date(*start), end_dt=date(*end)),
                auth=TqAuth(user, pwd))
    t = api.get_tick_serial(symbol, data_length=buf)
    ticks = []
    last_dt = 0          # 已处理的最大 datetime(ns)
    try:
        while True:
            api.wait_update(deadline=time.time() + 60)
            dts = t["datetime"].to_numpy("int64")
            if len(dts) == 0:
                continue
            i = int(np.searchsorted(dts, last_dt, side="right"))
            if i >= len(dts):
                continue
            seg = t.iloc[i:]
            for _, r in seg.iterrows():
                ticks.append((int(r["datetime"]), r["last_price"],
                             r["ask_price1"], r["bid_price1"], r["volume"]))
            last_dt = int(dts[-1])
    except BacktestFinished:
        pass
    finally:
        api.close()
    if not ticks:
        return None
    return _tick_to_bars(ticks, bar_sec)


def _screen(key, bars, stop_mult=STOP_MULT, hold=HOLD):
    """与 进场体检.screen_one 同口径：下一根开盘入场；L0 无止损/HOLD 持有；L1 2ATR 止损。
    signal = sign(OFI)。返回与 screen_one 同结构的 dict。"""
    o, h, l, c, ofi = bars["o"], bars["h"], bars["l"], bars["c"], bars["ofi"]
    N = len(c)
    atr = _atr(h, l, c, ATR_N)
    sig = np.sign(ofi)
    bad = np.isnan(atr)
    sig = np.where(bad, 0.0, np.nan_to_num(sig, nan=0.0))

    valid = np.arange(0, N - hold - 1)
    idx = valid[sig[valid] != 0]
    cp = cost_points(key)
    zero = {"key": key, "signal": "ofi", "n": 0, "name_cn": cs.get(key)[0],
            "per_day": 0.0, "days": 0, "cost_pts": round(cp, 3),
            "L0_gross": 0.0, "L0_t": 0.0, "L1_gross": 0.0, "L1_net": 0.0,
            "L1_t": 0.0, "L1_winrate": 0.0, "stop_share": 0.0, "ratio": 0.0}
    if len(idx) == 0:
        return zero

    sgn = sig[idx]
    entry = o[idx + 1]
    fwd = (o[idx + 1 + hold] - entry) * sgn                     # L0
    stop = np.where(sgn > 0, entry - stop_mult * atr[idx],
                    entry + stop_mult * atr[idx])               # L1
    exit_px = o[idx + 1 + hold].copy()
    hit = np.zeros(len(idx), dtype=bool)
    for j, (e_i, st, s) in enumerate(zip(idx, stop, sgn)):
        for jj in range(e_i + 1, e_i + 1 + hold):
            if s > 0:
                if l[jj] <= st:
                    exit_px[j] = min(st, o[jj]); hit[j] = True; break
            else:
                if h[jj] >= st:
                    exit_px[j] = max(st, o[jj]); hit[j] = True; break
    gross1 = (exit_px - entry) * sgn
    net1 = gross1 - cp

    def _t(x):
        sd = float(np.std(x, ddof=1)) if len(x) > 1 else 0.0
        return float(np.mean(x) / (sd / math.sqrt(len(x)))) if sd > 1e-12 else 0.0

    return {
        "key": key, "name_cn": cs.get(key)[0], "signal": "ofi",
        "n": int(len(idx)), "per_day": round(len(idx) / bars.get("days", 1), 2),
        "days": int(bars.get("days", 0)), "cost_pts": round(cp, 3),
        "L0_gross": round(float(np.mean(fwd)), 3), "L0_t": round(_t(fwd), 2),
        "L1_gross": round(float(np.mean(gross1)), 3),
        "L1_net": round(float(np.mean(net1)), 3), "L1_t": round(_t(net1), 2),
        "L1_winrate": round(float(np.mean(net1 > 0)), 4),
        "stop_share": round(float(np.mean(hit)), 4),
        "ratio": round(float(np.mean(gross1) / cp), 3) if cp > 0 else None,
        "ofi_total": round(float(np.sum(ofi)), 1),
    }


def _check(symbol, start):
    """数据质量探针：1 天 tick，打印前若干 tick 字段 + 计数，确认历史回放带 Level-1 盘口。"""
    user, pwd = config_account.check()
    api = TqApi(backtest=TqBacktest(start_dt=date(*start),
                                    end_dt=date(start[0], start[1], start[2] + 1)),
                auth=TqAuth(user, pwd))
    t = api.get_tick_serial(symbol, data_length=2000)
    cnt = 0
    samples = []
    try:
        while True:
            api.wait_update(deadline=time.time() + 60)
            n = len(t)
            if n == 0:
                continue
            seg = t.iloc[max(0, n - 50):n]   # 只看最新窗口避免刷屏
            for _, r in seg.iterrows():
                cnt += 1
                if len(samples) < 12:
                    samples.append(
                        (str(r["datetime"]), r["last_price"], r["ask_price1"],
                         r["ask_volume1"], r["bid_price1"], r["bid_volume1"], r["volume"]))
    except BacktestFinished:
        pass
    finally:
        api.close()
    print(f"[check] symbol={symbol} start={start} tick_count~={cnt}")
    print(f"{'datetime':<28}{'last':>9}{'ask1':>9}{'avol1':>7}{'bid1':>9}{'bvol1':>7}{'vol':>10}")
    for s in samples:
        print(f"{s[0]:<28}{s[1]:>9}{s[2]:>9}{s[3]:>7}{s[4]:>9}{s[5]:>7}{s[6]:>10}")
    print("[check] 若 ask1/bid1/avol1/bvol1 列有非零值 ⇒ 历史 tick 回放带 Level-1 盘口，OFI 可做。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--syms", default="RB")
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--end", default="2023-02-01")
    ap.add_argument("--check", action="store_true", help="仅跑 1 天数据质量探针")
    ap.add_argument("--hold", type=int, default=HOLD)
    ap.add_argument("--stop", type=float, default=STOP_MULT)
    a = ap.parse_args()
    hold = a.hold
    stop = a.stop

    syms = [s.strip().upper() for s in a.syms.split(",")]
    s = [int(x) for x in a.start.split("-")]
    e = [int(x) for x in a.end.split("-")]

    if a.check:
        for k in syms:
            sym = cs.get(k)[1]
            _check(sym, s)
        return

    L = []
    L.append("=" * 112)
    L.append(f"OFI 盘口资金流探针  ｜  周期=5min(自tick聚合)  ｜  HOLD={hold}  ｜  止损={stop:g}×ATR  "
             f"｜  滑点={SLIP}跳  ｜  窗口={a.start}~{a.end}（非样本外，只能判死）")
    L.append("=" * 112)
    L.append(f"{'信号':<8}{'品种':<7}{'tick数':>10}{'笔数':>7}{'成本(点)':>9}"
             f"{'L0毛':>9}{'L1毛':>9}{'L1净':>9}{'毛/成本':>9}{'胜率':>8}{'止损%':>8}{'t':>7}{'判定':>6}")
    rows = []
    for k in syms:
        sym = cs.get(k)[1]
        bars = _build_bars(sym, s, e)
        if bars is None or len(bars["c"]) < 1000:
            L.append(f"  [skip] {k} 拉不到 tick 数据")
            continue
        r = _screen(k, bars, stop_mult=stop, hold=hold)
        r["days"] = (date(*e) - date(*s)).days
        rows.append(r)
        v = "✅" if (r.get("ratio") or -9) >= 2.0 and r["L1_net"] > 0 else "❌"
        L.append(f"{'ofi':<8}{r['name_cn']:<7}{bars['n_ticks']:>10}{r['n']:>7}{r['cost_pts']:>9.2f}"
                 f"{r['L0_gross']:>9.3f}{r['L1_gross']:>9.3f}{r['L1_net']:>9.3f}"
                 f"{(r.get('ratio') or float('nan')):>9.2f}{r['L1_winrate']*100:>7.1f}%"
                 f"{r['stop_share']*100:>7.1f}%{r['L1_t']:>7.2f}{v:>6}")
        L.append(f"         (OFI 累计={r.get('ofi_total')}, 净方向 "
                 f"{'偏多' if r.get('ofi_total',0)>0 else '偏空'})")
    L.append("-" * 112)
    L.append("⚠️ 免费 Level-1 tick(2笔/秒/一档)太粗：本探针 FAIL 仅说明此档数据源无 edge，")
    L.append("   不能宣告「盘口资金流维度」死（判生/强证据需付费 L2/逐笔）。待换干净样本或付费源再验。")
    txt = "\n".join(L)
    print(txt)
    out = os.path.join(_HERE, f"OFI探针_{a.start}_{a.end}.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write(txt + "\n")
    with open(os.path.join(_HERE, f"OFI探针_{a.start}_{a.end}.json"), "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print(f"\n[done] {out}")


if __name__ == "__main__":
    main()
