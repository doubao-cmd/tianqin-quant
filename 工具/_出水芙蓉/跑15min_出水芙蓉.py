# -*- coding: utf-8 -*-
"""出水芙蓉/断头铡刀 · 15min 真回测驱动（周期 86400→900 单变量变更重启）

与日频版 `跑出水芙蓉.py` 的**唯一变量**是周期（900 代替 86400）；
信号/四级出场/止损夹紧/记账逻辑严格照抄原回测()，口径零偏差。

★ 数据模式 = 实时模式（用户 2026-10-11 拍板方案 A）。原因（探针实证）：
  回测模式主连 15min 的 volume 在 **2018-01 起全为 0**（2016-2017 真实、零值率 0.2%；
  2018-2023 零值率 100%），与日频「回测模式 volume 全 0」同源缺陷。
  放量条件 `v >= 3*volma` 在 volume=0 区退化为 `0>=0` 恒真 ⇒ 信号爆炸 ⇒ 结果作废。
  故只能用实时模式（volume 真实），代价是历史被限 data_length=8964 根
  ⇒ MA 覆盖 2025-02-25 ~ 2026-10-09（约 19.5 个月），窗口据此重切。

用法：
    python 跑15min_出水芙蓉.py --窗 IS      # 2025-03 ~ 2026-02
    python 跑15min_出水芙蓉.py --窗 OOS     # 2026-03 ~ 2026-09
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
import 跑出水芙蓉 as YF                                 # noqa: E402  复用其 指标()（循环版，口径零偏差）
import config_symbols as cs                             # noqa: E402
import config_account                                   # noqa: E402
from tqsdk import TqApi, TqAuth, TqSim                 # noqa: E402

池 = ["MA", "M", "RB", "V", "JD", "JM", "P", "RU", "SR"]
# 实时模式 data_length=8964 上限 ⇒ 实得 ~2025-02 ~ 2026-10，窗口按此重切
窗 = {"IS": ("2025-03-01", "2026-02-28"),
      "OOS": ("2026-03-01", "2026-09-30")}
DUR = 900                                              # 15min
DL = 8964                                              # 实时模式 data_length 上限
年化根 = float(np.sqrt(243.0 * 16))                    # 15min：243 交易日 × 16 根/日
最少笔数 = {"IS": 50, "OOS": 30}


def 收集_实时(keys, dur):
    """实时模式一次拉全部品种的历史 15min（volume 真实，日频版同款做法）。"""
    user, pwd = config_account.check()
    api = TqApi(TqSim(200000.0), auth=TqAuth(user, pwd))
    out = {}
    try:
        ks = {}
        for k in keys:
            _名, code, mult, tick = cs.get(k)
            ks[k] = (api.get_kline_serial(code, dur, data_length=DL), mult, tick, float(cs.COST[k]))
        # 等数据到位：连续两次 wait_update 且各品种首根时间戳不再变化
        prev = None
        for _ in range(10):
            api.wait_update(deadline=time.time() + 60)
            firsts = tuple(int(kk["datetime"].to_numpy()[0]) if len(kk) else 0
                           for kk, _, _, _ in ks.values())
            if all(f > 0 for f in firsts) and firsts == prev:
                break
            prev = firsts
        for k, (kk, mult, tick, comm) in ks.items():
            d = kk["datetime"].to_numpy().astype(np.int64)
            o = kk["open"].to_numpy(float)
            h = kk["high"].to_numpy(float)
            l = kk["low"].to_numpy(float)
            c = kk["close"].to_numpy(float)
            v = kk["volume"].to_numpy(float)
            oi = kk["close_oi"].to_numpy(float)
            m = np.isfinite(c) & (c > 0) & (d > 0)
            out[k] = dict(dates=d[m], o=o[m], h=h[m], l=l[m], c=c[m], v=v[m], oi=oi[m],
                          mult=mult, tick=tick, comm=comm)
    finally:
        try:
            api.close()
        except Exception:
            pass
    return out


def 回测单品种(k, 数据, 起, 止):
    """逐品种独立回测（信号/出场/止损/记账口径照抄 跑出水芙蓉.回测 的逐品种体）。

    窗口切片：指标在全序列上算（窗口起点处指标已预热），主循环只在 [起,止] 的 index 范围内跑，
    持仓状态自窗口起点起为 0（该窗口内独立评估）。
    """
    o, c, h, l, v, oi = 数据["o"], 数据["c"], 数据["h"], 数据["l"], 数据["v"], 数据["oi"]
    mult, tick, comm = 数据["mult"], 数据["tick"], 数据["comm"]
    dts = 数据["dates"]
    N = len(c)
    ma5, ma10, ma20, atr, volma, hh, ll = YF.指标(c, h, l, v)

    # 换月日标记（卡 16:30 变更）：close_oi 跳变 >20% 或前值≤0 ⇒ 主力切换日，剔除该日信号
    换月 = np.zeros(N, bool)
    for i in range(1, N):
        if oi[i - 1] <= 0:
            换月[i] = True
        elif np.isfinite(oi[i]) and abs(oi[i] / oi[i - 1] - 1.0) > 0.20:
            换月[i] = True

    # 窗口 index 范围（窗口起之前的数据仅用于指标预热，不产生交易）
    起ns = pd.Timestamp(起).value
    止ns = pd.Timestamp(止).value
    i0 = int(np.searchsorted(dts, 起ns, side="left"))
    i1 = int(np.searchsorted(dts, 止ns, side="right")) - 1
    if i1 < i0:
        return [], np.array([K.RISK_EQUITY])

    pos = 0
    entry = 0.0
    stop = 0.0
    tp = 0.0
    hand = 0
    e_i = -1
    s_i = -1
    开成本 = 0.0
    realized = 0.0
    trades = []
    待开 = None
    待平 = None
    eq = np.full(i1 - i0 + 1, K.RISK_EQUITY)

    def 平仓(px, reason, ii):
        nonlocal pos, hand, realized, entry, stop, tp, e_i, s_i, 开成本
        d = pos
        hh_ = hand
        成本 = (K.SLIP_TICKS * tick * mult + comm) * hh_
        pnl = d * hh_ * (px - entry) * mult - 成本
        realized += pnl
        trades.append(dict(key=k, dir=int(d), hand=int(hh_),
                           entry=float(entry), exit=float(px),
                           entry_i=int(e_i), exit_i=int(ii), signal_i=int(s_i),
                           reason=reason, pnl=float(pnl), bars=int(ii - e_i),
                           开成本=float(开成本)))
        pos = 0
        hand = 0

    for i in range(i0, i1 + 1):
        up, dn = K.均线群上下沿(ma5[i], ma10[i], ma20[i])
        # 1) 次开盘离场（昨日收盘确认）—— 同根不重进
        if pos != 0 and 待平 is not None:
            px = o[i] - pos * K.SLIP_TICKS * tick
            平仓(px, 待平, i)
            待平 = None
            continue
        # 2) 挂单入场（昨日信号，A1：t+1 开盘市价）
        if pos == 0 and 待开 is not None:
            d, stop_raw, tp_px, si, a_prev = 待开
            ep = o[i]
            if np.isfinite(ep) and ep > 0:
                st = K.夹紧止损(stop_raw, ep, a_prev, tick, d)
                h_ = K.目标手数(ep, st, mult, tick)
                pos = d
                entry = ep
                stop = st
                tp = tp_px
                hand = h_
                e_i = i
                s_i = si
                开成本 = (K.SLIP_TICKS * tick * mult + comm) * h_
                realized -= 开成本
            待开 = None
        # 3) 止损 → 止盈（同根先止损）
        if pos != 0:
            d = pos
            px = None
            reason = None
            if d > 0 and l[i] <= stop:
                px = K.止损成交多(stop, o[i])
                reason = "止损"
            elif d < 0 and h[i] >= stop:
                px = K.止损成交空(stop, o[i])
                reason = "止损"
            elif d > 0 and np.isfinite(tp) and h[i] >= tp:
                px = K.止盈成交多(tp, o[i], tick)
                reason = "前高止盈"
            elif d < 0 and np.isfinite(tp) and l[i] <= tp:
                px = K.止盈成交空(tp, o[i], tick)
                reason = "前低止盈"
            if reason:
                平仓(px, reason, i)
        # 4) 收盘确认 → 排队次开盘平
        if pos != 0 and 待平 is None:
            d = pos
            if d > 0 and np.isfinite(up) and c[i] < up:
                待平 = "跌回均线群"
            elif d > 0 and np.isfinite(ma10[i]) and c[i] < ma10[i]:
                待平 = "MA10移动止盈"
            elif d < 0 and np.isfinite(dn) and c[i] > dn:
                待平 = "跌回均线群"
            elif d < 0 and np.isfinite(ma10[i]) and c[i] > ma10[i]:
                待平 = "MA10移动止盈"
        # 5) 信号（t 收盘确认；flat 且无排队才有效；换月日剔除）
        if (pos == 0 and 待开 is None and 待平 is None and not 换月[i]):
            if i >= 1:
                sig = 0
                if K.是多头信号(o[i], c[i], v[i], up, atr[i], volma[i]):
                    sig = 1
                elif K.是空头信号(o[i], c[i], v[i], dn, atr[i], volma[i]):
                    sig = -1
                if sig != 0 and np.isfinite(atr[i]):
                    极值 = l[i] if sig > 0 else h[i]
                    tp_px = hh[i] if sig > 0 else ll[i]
                    if np.isfinite(tp_px):
                        待开 = (sig, K.原始止损(极值, tick, sig), tp_px, i, atr[i])
        # 逐根记账
        if pos != 0:
            浮 = pos * hand * (c[i] - entry) * mult
        else:
            浮 = 0.0
        eq[i - i0] = K.RISK_EQUITY + realized + 浮

    # 期末强平
    if pos != 0:
        px = c[i1] - pos * K.SLIP_TICKS * tick
        平仓(px, "期末强平", i1)
    eq[-1] = K.RISK_EQUITY + realized
    return trades, eq


def 统计与判定(per_k, 窗名):
    全部 = []
    for k, (ts, eq) in per_k.items():
        全部 += ts
    实盘笔 = [t for t in 全部 if t["reason"] != "期末强平"]
    硬 = {}
    止损笔 = [t for t in 全部 if t["reason"] == "止损"]
    硬["1_止损组盈利笔"] = sum(1 for t in 止损笔 if t["pnl"] > 0)
    分组 = {}
    for t in 全部:
        分组[t["reason"]] = 分组.get(t["reason"], 0) + 1
    硬["2_分组和==总数"] = (sum(分组.values()) == len(全部))
    期末 = sum(ts_eq[1][-1] for ts_eq in per_k.values())
    硬["3_开仓成本记账"] = abs((K.RISK_EQUITY * len(per_k)
                                + sum(t["pnl"] - t["开成本"] for t in 全部))
                               - 期末) < 1.0
    硬["4_无未来函数"] = all(t["entry_i"] == t["signal_i"] + 1 for t in 实盘笔)
    硬["5_止损价不利侧"] = all((t["exit"] < t["entry"]) if t["dir"] > 0
                              else (t["exit"] > t["entry"]) for t in 止损笔)
    硬["6_样本量"] = len(实盘笔) >= 最少笔数[窗名]

    明细 = []
    夏普集 = []
    for k, (ts, eq) in per_k.items():
        rets = np.diff(eq) / eq[:-1]
        if (eq > 0).all() and rets.std() > 1e-12:
            sharpe = float(rets.mean() / rets.std() * np.sqrt(年化根))
        else:
            sharpe = float("nan")
        峰 = np.maximum.accumulate(eq)
        dd = float(((eq - 峰) / 峰).min())
        wins = sum(1 for t in ts if t["pnl"] > 0)
        明细.append(dict(key=k, 收益=round((eq[-1] / K.RISK_EQUITY - 1) * 100, 1),
                          夏普=None if sharpe != sharpe else round(sharpe, 3),
                          回撤=round(dd * 100, 1), 笔数=len(ts),
                          胜率=round(100 * wins / len(ts), 1) if ts else None,
                          多=sum(1 for t in ts if t["dir"] > 0),
                          空=sum(1 for t in ts if t["dir"] < 0)))
        if 明细[-1]["夏普"] is not None:
            夏普集.append(明细[-1]["夏普"])
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
    print("出水芙蓉/断头铡刀 · 15min(实时模式) · %s · %s ~ %s · 9 品种固定池 · 锁20万" %
          (args.窗, 起, 止), flush=True)
    print("=" * 78, flush=True)

    原始 = 收集_实时(池, DUR)
    per_k = {}
    for k in 池:
        if k not in 原始 or not len(原始[k]["dates"]):
            print("    ⚠️ %s 无数据，跳过" % k, flush=True)
            continue
        n = len(原始[k]["dates"])
        vz = int((原始[k]["v"] <= 0).sum())
        头 = pd.Timestamp(int(原始[k]["dates"][0])).strftime("%Y-%m-%d")
        尾 = pd.Timestamp(int(原始[k]["dates"][-1])).strftime("%Y-%m-%d")
        print("    %s: %d 根 15min, volume零值=%d(%.1f%%), 覆盖 %s ~ %s, 乘数%.0f 跳%.1f"
              % (k, n, vz, 100 * vz / max(n, 1), 头, 尾,
                 原始[k]["mult"], 原始[k]["tick"]), flush=True)
        ts, eq = 回测单品种(k, 原始[k], 起, 止)
        per_k[k] = (ts, eq)

    明细, 硬, 判定, 分组 = 统计与判定(per_k, args.窗)

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
    print("等权平均夏普 = %s   正夏普 %s/%s   实盘笔数 %d" %
          (判定["等权平均夏普"], 判定["正夏普品种"], 判定["品种数"], 判定["实盘笔数"]))
    print()
    print("硬校验:")
    for kk, vv in 硬.items():
        # 1_止损组盈利笔 是计数(期望恰为0)，其余是布尔；分别取「是否通过」再显示
        if kk == "1_止损组盈利笔":
            通过 = (vv == 0)
            print("  [%s] %s = %s" % ("✅" if 通过 else "❌", kk, vv))
        else:
            通过 = bool(vv)
            print("  [%s] %s = %s" % ("✅" if 通过 else "❌", kk, 通过))
    print()
    print("★ 判定（等权夏普>1.0 且 正夏普≥2/3 且 回撤<25% 且 硬校验全过）: " +
          ("✅ 通过" if 判定["通过"] else "❌ 不通过 ⇒ 无效收档"))
    print()
    print("出场分布:", 分组)

    out = dict(窗=args.窗, 周期="15min", 数据模式="实时模式", 起=起, 止=止,
               年化根=round(年化根, 2), 明细=明细, 硬校验={kk: vv for kk, vv in 硬.items()},
               判定=判定, 出场分布=分组)
    p = os.path.join(HERE, "出水芙蓉15min_%s_结果.json" % args.窗)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=float)
    print("已落盘:", p)


if __name__ == "__main__":
    main()
