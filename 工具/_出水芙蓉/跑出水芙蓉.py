# -*- coding: utf-8 -*-
"""出水芙蓉/断头铡刀 回测驱动（卡冻结 A1/B1/C1 日频；窗 16:20 变更版）

用法：
    python 跑出水芙蓉.py --窗 IS     # 2016-2020（干净 5 年）
    python 跑出水芙蓉.py --窗 VAL    # 2023-2025（已污染，声明）
    python 跑出水芙蓉.py --窗 OOS    # 2026-01~10（已污染，声明）
"""
import os
import sys
import time
import json
import argparse

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))          # 工具/_出水芙蓉 → 仓库根
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "lib"))
sys.path.insert(0, HERE)

import 核心 as K                                        # noqa: E402
import config_symbols as cs                             # noqa: E402
import config_account                                   # noqa: E402
from tqsdk import TqApi, TqAuth, TqSim  # noqa: E402

池 = ["MA", "M", "RB", "V", "JD", "JM", "P", "RU", "SR"]
窗 = {"IS": ("2016-01-01", "2020-12-31"),
      "VAL": ("2023-01-01", "2025-12-31"),
      "OOS": ("2026-01-01", "2026-10-01")}
WARMUP_DAYS = 150          # 自然日，够 MA20/ATR20/量MA20/前高60
最少笔数 = {"IS": 50, "VAL": 30, "OOS": 15}
年化根 = 243.0


def 批量收集(keys, 起, 止):
    """★ 实时模式一次拉全部品种的历史日线（无 TqBacktest）。

    为什么不用回测模式：①实测回测模式主连日线 volume 全 0（实时模式真实），
    放量条件会退化成 0≥0 恒真；②日线 5.5 年仅 ~2600 根 < 8964 上限，不需要回测模式；
    ③一次 wait_update 全部到位，无逐根流式。周六休市照样可取（历史数据）。"""
    user, pwd = config_account.check()
    api = TqApi(TqSim(200000.0), auth=TqAuth(user, pwd))
    out = {}
    try:
        ks = {}
        for k in keys:
            _名, code, mult, tick = cs.get(k)
            ks[k] = (api.get_kline_serial(code, 86400, data_length=3000),
                     mult, tick, float(cs.COST[k]))
        # 等数据到位：连续两次 wait_update 且首根时间戳不再变化
        prev_first = None
        for _ in range(8):
            api.wait_update(deadline=time.time() + 60)
            firsts = tuple(int(kk["datetime"].to_numpy()[0]) if len(kk) else 0
                           for kk, _, _, _ in ks.values())
            if all(f > 0 for f in firsts) and firsts == prev_first:
                break
            prev_first = firsts
        lo_dt = (pd.Timestamp(起) - pd.Timedelta(days=WARMUP_DAYS)).value
        hi_dt = pd.Timestamp(止).value
        for k, (kk, mult, tick, comm) in ks.items():
            d = kk["datetime"].to_numpy()
            o = kk["open"].to_numpy(float); h = kk["high"].to_numpy(float)
            l = kk["low"].to_numpy(float); c = kk["close"].to_numpy(float)
            v = kk["volume"].to_numpy(float); oi = kk["close_oi"].to_numpy(float)
            m = (d >= lo_dt) & (d <= hi_dt) & np.isfinite(c) & (c > 0)
            out[k] = dict(dates=d[m].astype(np.int64),
                          o=o[m], h=h[m], l=l[m], c=c[m], v=v[m], oi=oi[m],
                          mult=mult, tick=tick, comm=comm)
    finally:
        try:
            api.close()
        except Exception:
            pass
    return out


def 指标(c, h, l, v):
    """全部用截至 t−1 的窗口（不含当日，卡 16:20 澄清）；前高/前低不含当日。"""
    N = len(c)
    ma5 = np.full(N, np.nan); ma10 = np.full(N, np.nan); ma20 = np.full(N, np.nan)
    for i in range(K.MA_SLOW, N):
        ma5[i] = c[i - K.MA_FAST:i].mean()
        ma10[i] = c[i - K.MA_MID:i].mean()
        ma20[i] = c[i - K.MA_SLOW:i].mean()
    pc = np.roll(c, 1); pc[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    atr = np.full(N, np.nan)
    for i in range(K.ATR_N + 1, N):
        atr[i] = tr[i - K.ATR_N:i].mean()
    volma = np.full(N, np.nan)
    for i in range(K.VOL_MA_N, N):
        volma[i] = v[i - K.VOL_MA_N:i].mean()
    hh = np.full(N, np.nan); ll = np.full(N, np.nan)
    for i in range(K.DONCHIAN_N, N):
        hh[i] = h[i - K.DONCHIAN_N:i].max()
        ll[i] = l[i - K.DONCHIAN_N:i].min()
    return ma5, ma10, ma20, atr, volma, hh, ll


def 回测(原始, 起):
    """对齐 → 指标 → 主循环（A1 入场 / 四级出场 / 期末强平）→ 权益逐日记账。"""
    日期集 = None
    for k in 池:
        s = set(pd.Timestamp(int(d)).strftime("%Y-%m-%d") for d in 原始[k]["dates"])
        日期集 = s if 日期集 is None else (日期集 & s)
    dates = sorted(日期集)
    N = len(dates)
    idx = {k: {pd.Timestamp(int(d)).strftime("%Y-%m-%d"): j
               for j, d in enumerate(原始[k]["dates"])} for k in 池}
    A = {k: dict(o=np.array([原始[k]["o"][idx[k][d]] for d in dates], float),
                 h=np.array([原始[k]["h"][idx[k][d]] for d in dates], float),
                 l=np.array([原始[k]["l"][idx[k][d]] for d in dates], float),
                 c=np.array([原始[k]["c"][idx[k][d]] for d in dates], float),
                 v=np.array([原始[k]["v"][idx[k][d]] for d in dates], float),
                 oi=np.array([原始[k]["oi"][idx[k][d]] for d in dates], float),
                 mult=原始[k]["mult"], tick=原始[k]["tick"], comm=原始[k]["comm"])
        for k in 池}
    # 换月日标记：close_oi 日跳变 >20%（或前值 ≤0）⇒ 主力切换日（卡 16:35 补充）
    换月 = {}
    for k in 池:
        oi = A[k]["oi"]
        r = np.zeros(N, bool)
        for i in range(1, N):
            if oi[i - 1] <= 0:
                r[i] = True
            elif np.isfinite(oi[i]) and abs(oi[i] / oi[i - 1] - 1.0) > 0.20:
                r[i] = True
        换月[k] = r
    IND = {k: 指标(A[k]["c"], A[k]["h"], A[k]["l"], A[k]["v"]) for k in 池}

    pos = {k: 0 for k in 池}
    entry = {k: 0.0 for k in 池}; stop = {k: 0.0 for k in 池}
    tp = {k: 0.0 for k in 池}; hand = {k: 0 for k in 池}
    e_i = {k: -1 for k in 池}; s_i = {k: -1 for k in 池}
    开成本 = {k: 0.0 for k in 池}
    realized = {k: 0.0 for k in 池}
    待开 = {k: None for k in 池}; 待平 = {k: None for k in 池}
    trades = []
    跳过换月 = {"n": 0}
    eq = {k: np.full(N, K.RISK_EQUITY) for k in 池}

    def 平仓(k, i, px, reason):
        d, h = pos[k], hand[k]
        成本 = (K.SLIP_TICKS * A[k]["tick"] * A[k]["mult"] + A[k]["comm"]) * h
        pnl = d * h * (px - entry[k]) * A[k]["mult"] - 成本
        realized[k] += pnl
        trades.append(dict(key=k, dir=int(d), hand=int(h),
                           entry=float(entry[k]), exit=float(px),
                           entry_dt=dates[e_i[k]], exit_dt=dates[i],
                           signal_i=int(s_i[k]), entry_i=int(e_i[k]), exit_i=int(i),
                           reason=reason, pnl=float(pnl), bars=int(i - e_i[k]),
                           开成本=float(开成本[k])))
        pos[k] = 0; hand[k] = 0

    for i in range(K.DONCHIAN_N + 1, N):
        for k in 池:
            tick, mult, comm = A[k]["tick"], A[k]["mult"], A[k]["comm"]
            ma5, ma10, ma20, atr, volma, hh, ll = IND[k]
            up, dn = K.均线群上下沿(ma5[i], ma10[i], ma20[i])
            # 1) 次开盘离场（昨日收盘确认）—— 同根不重进
            if pos[k] != 0 and 待平[k] is not None:
                px = A[k]["o"][i] - pos[k] * K.SLIP_TICKS * tick
                平仓(k, i, px, 待平[k])
                待平[k] = None
                continue
            # 2) 挂单入场（昨日信号，A1：t+1 开盘市价）
            if pos[k] == 0 and 待开[k] is not None:
                d, stop_raw, tp_px, si, a_prev = 待开[k]
                ep = A[k]["o"][i]
                if np.isfinite(ep) and ep > 0:
                    st = K.夹紧止损(stop_raw, ep, a_prev, tick, d)
                    h_ = K.目标手数(ep, st, mult, tick)
                    pos[k] = d; entry[k] = ep; stop[k] = st
                    tp[k] = tp_px; hand[k] = h_; e_i[k] = i; s_i[k] = si
                    开成本[k] = (K.SLIP_TICKS * tick * mult + comm) * h_
                    realized[k] -= 开成本[k]
                待开[k] = None
            # 3) 止损 → 止盈（同根先止损）
            if pos[k] != 0:
                d = pos[k]; px = None; reason = None
                if d > 0 and A[k]["l"][i] <= stop[k]:
                    px = K.止损成交多(stop[k], A[k]["o"][i]); reason = "止损"
                elif d < 0 and A[k]["h"][i] >= stop[k]:
                    px = K.止损成交空(stop[k], A[k]["o"][i]); reason = "止损"
                elif d > 0 and np.isfinite(tp[k]) and A[k]["h"][i] >= tp[k]:
                    px = K.止盈成交多(tp[k], A[k]["o"][i], tick); reason = "前高止盈"
                elif d < 0 and np.isfinite(tp[k]) and A[k]["l"][i] <= tp[k]:
                    px = K.止盈成交空(tp[k], A[k]["o"][i], tick); reason = "前低止盈"
                if reason:
                    平仓(k, i, px, reason)
            # 4) 收盘确认 → 排队次开盘平
            if pos[k] != 0 and 待平[k] is None:
                d = pos[k]
                if d > 0 and np.isfinite(up) and A[k]["c"][i] < up:
                    待平[k] = "跌回均线群"
                elif d > 0 and np.isfinite(ma10[i]) and A[k]["c"][i] < ma10[i]:
                    待平[k] = "MA10移动止盈"
                elif d < 0 and np.isfinite(dn) and A[k]["c"][i] > dn:
                    待平[k] = "跌回均线群"
                elif d < 0 and np.isfinite(ma10[i]) and A[k]["c"][i] > ma10[i]:
                    待平[k] = "MA10移动止盈"
            # 5) 信号（t 收盘确认；flat 且无排队才有效；换月日剔除；★不早于 IS 起点）
            if (pos[k] == 0 and 待开[k] is None and 待平[k] is None
                    and not 换月[k][i] and pd.Timestamp(dates[i]) >= pd.Timestamp(起)):
                if i >= 1:
                    sig = 0
                    if K.是多头信号(A[k]["o"][i], A[k]["c"][i], A[k]["v"][i],
                                    up, atr[i], volma[i]):
                        sig = 1
                    elif K.是空头信号(A[k]["o"][i], A[k]["c"][i], A[k]["v"][i],
                                      dn, atr[i], volma[i]):
                        sig = -1
                    if sig != 0 and np.isfinite(atr[i]):
                        极值 = A[k]["l"][i] if sig > 0 else A[k]["h"][i]
                        tp_px = hh[i] if sig > 0 else ll[i]
                        if np.isfinite(tp_px):
                            待开[k] = (sig, K.原始止损(极值, tick, sig),
                                       tp_px, i, atr[i])
        # 逐日记账（当日全部动作后，按收盘 mark-to-market）
        for k in 池:
            浮 = 0.0
            if pos[k] != 0:
                浮 = pos[k] * hand[k] * (A[k]["c"][i] - entry[k]) * A[k]["mult"]
            eq[k][i] = K.RISK_EQUITY + realized[k] + 浮

    # 期末强平（保证 INIT+Σpnl==期末 精确对账）
    for k in 池:
        if pos[k] != 0:
            px = A[k]["c"][N - 1] - pos[k] * K.SLIP_TICKS * A[k]["tick"]
            平仓(k, N - 1, px, "期末强平")
        eq[k][N - 1] = K.RISK_EQUITY + realized[k]
    return trades, dates, eq, 跳过换月


def 统计与判定(trades, eq, 窗名):
    实盘笔 = [t for t in trades if t["reason"] != "期末强平"]
    硬 = {}
    # 校验1：止损组胜率恒 0
    止损笔 = [t for t in trades if t["reason"] == "止损"]
    硬["1_止损组盈利笔"] = sum(1 for t in 止损笔 if t["pnl"] > 0)
    # 校验2：分组和 == 总数
    分组 = {}
    for t in trades:
        分组[t["reason"]] = 分组.get(t["reason"], 0) + 1
    硬["2_分组和==总数"] = (sum(分组.values()) == len(trades))
    # 校验3：INIT×9 + Σ(pnl − 开成本) == 期末（开成本在入场时已扣进 realized）
    期末 = sum(eq[k][-1] for k in 池)
    硬["3_开仓成本记账"] = abs((K.RISK_EQUITY * len(池)
                                + sum(t["pnl"] - t["开成本"] for t in trades))
                               - 期末) < 1.0
    # 校验4：入场日 == 信号日 + 1
    硬["4_无未来函数"] = all(t["entry_i"] == t["signal_i"] + 1
                            for t in 实盘笔)
    # 校验5：止损成交价在不利侧
    硬["5_止损价不利侧"] = all((t["exit"] < t["entry"]) if t["dir"] > 0
                              else (t["exit"] > t["entry"])
                              for t in 止损笔)
    # 校验6/7：样本量闸门
    硬["6_样本量"] = len(实盘笔) >= 最少笔数[窗名]

    # 每品种指标
    明细 = []
    for k in 池:
        e = eq[k]
        rets = np.diff(e) / e[:-1]
        if (e > 0).all() and rets.std() > 1e-12:
            sharpe = float(rets.mean() / rets.std() * np.sqrt(年化根))
        else:
            sharpe = float("nan")
        峰 = np.maximum.accumulate(e)
        dd = float(((e - 峰) / 峰).min())
        ts = [t for t in 实盘笔 if t["key"] == k]
        wins = sum(1 for t in ts if t["pnl"] > 0)
        明细.append(dict(key=k, 收益=round((e[-1] / K.RISK_EQUITY - 1) * 100, 1),
                        夏普=None if sharpe != sharpe else round(sharpe, 3),
                        回撤=round(dd * 100, 1), 笔数=len(ts),
                        胜率=round(100 * wins / len(ts), 1) if ts else None,
                        多=sum(1 for t in ts if t["dir"] > 0),
                        空=sum(1 for t in ts if t["dir"] < 0)))
    夏普集 = [m["夏普"] for m in 明细 if m["夏普"] is not None]
    判定 = dict(
        等权平均夏普=round(float(np.mean(夏普集)), 3) if 夏普集 else None,
        正夏普品种=sum(1 for s in 夏普集 if s > 0),
        品种数=len(明细),
        最差单品种回撤=min(m["回撤"] for m in 明细),
        实盘笔数=len(实盘笔),
        通过=bool(硬["1_止损组盈利笔"] == 0 and 硬["2_分组和==总数"]
                  and 硬["3_开仓成本记账"] and 硬["4_无未来函数"]
                  and 硬["5_止损价不利侧"] and 硬["6_样本量"]
                  and (not 夏普集 or (float(np.mean(夏普集)) > 1.0
                                      and sum(1 for s in 夏普集 if s > 0) >= len(明细) * 2 / 3
                                      and min(m["回撤"] for m in 明细) > -25.0)))
        if 夏普集 else False)
    return 明细, 硬, 判定, 分组


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--窗", required=True, choices=list(窗))
    args = ap.parse_args()
    起, 止 = 窗[args.窗]
    print("=" * 78, flush=True)
    print("出水芙蓉/断头铡刀 · %s · %s ~ %s · 9 品种固定池 · 锁20万" %
          (args.窗, 起, 止), flush=True)
    print("=" * 78, flush=True)
    # 数据缓存（pickle）：回测逻辑调试期免重拉（收集约 6 分钟）
    缓存 = os.path.join(HERE, "_数据缓存_%s.pkl" % args.窗)
    if os.path.isfile(缓存) and os.path.getmtime(缓存) > time.mktime(time.strptime(止, "%Y-%m-%d")):
        import pickle
        with open(缓存, "rb") as f:
            原始 = pickle.load(f)
        print("已用缓存:", 缓存, flush=True)
    else:
        原始 = 批量收集(池, 起, 止)
        for k in 池:
            末 = pd.Timestamp(原始[k]["dates"][-1]).date() if len(原始[k]["dates"]) else "-"
            缺 = (pd.Timestamp(止) - pd.Timestamp(末)).days if len(原始[k]["dates"]) else 999
            vz = int((原始[k]["v"] <= 0).sum())
            print("    %s: %d 根, 末根 %s%s, volume零值=%d" % (
                k, len(原始[k]["dates"]), 末,
                "  ⚠️距窗口终点%d天" % 缺 if 缺 > 7 else "", vz), flush=True)
        import pickle
        with open(缓存, "wb") as f:
            pickle.dump(原始, f)

    trades, dates, eq, 跳过换月 = 回测(原始, 起)
    明细, 硬, 判定, 分组 = 统计与判定(trades, eq, args.窗)

    print()
    print("%-6s %9s %8s %8s %6s %7s %5s %5s" %
          ("品种", "收益%", "夏普", "回撤%", "笔数", "胜率%", "多", "空"))
    print("-" * 66)
    for m in 明细:
        print("%-6s %8.1f%% %8s %7.1f%% %6d %6.1f%% %5d %5d" %
              (m["key"], m["收益"],
               "—" if m["夏普"] is None else "%.3f" % m["夏普"],
               m["回撤"], m["笔数"], m["胜率"] or 0, m["多"], m["空"]))
    print("-" * 66)
    print("等权平均夏普 = %s   正夏普 %s/%s   实盘笔数 %d   跳过换月信号 %d" %
          (判定["等权平均夏普"], 判定["正夏普品种"], 判定["品种数"],
           判定["实盘笔数"], 跳过换月["n"]))
    print()
    print("硬校验:")
    for kk, vv in 硬.items():
        vv = bool(vv)
        print("  [%s] %s = %s" % ("✅" if vv else "❌", kk, vv))
    print()
    print("★ 判定（等权夏普>1.0 且 正夏普≥2/3 且 回撤<25% 且 硬校验全过）: " +
          ("✅ 通过" if 判定["通过"] else "❌ 不通过 ⇒ 无效收档"))
    print()
    print("出场分布:", 分组)

    out = dict(窗=args.窗, 起=起, 止=止, 明细=明细,
               硬校验={kk: vv for kk, vv in 硬.items()},
               判定=判定, 出场分布=分组, 跳过换月=跳过换月["n"],
               trades=trades)
    p = os.path.join(HERE, "出水芙蓉_%s_结果.json" % args.窗)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=float)
    print("已落盘:", p)


if __name__ == "__main__":
    main()
