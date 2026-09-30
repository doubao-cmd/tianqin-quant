# -*- coding: utf-8 -*-
"""
==========================================================
 风险预算体检：把「单笔风险」真正算成钱
==========================================================

用法：
    python 工具/风险预算体检.py                          # 默认池 = 核心 9 个
    python 工具/风险预算体检.py --equity 400000 --risk 0.3
    python 工具/风险预算体检.py --keys RU,EB,JM
    python 工具/风险预算体检.py --stop 2.0 --kline 86400  # 2×ATR 止损、日线

它算的是什么（对应引擎 `lib/engine._risk_budget_hand`，口径逐字一致）：

        单笔风险(元) = 手数 × (止损倍数 × ATR) × 合约乘数
    =>  手数        = 权益 × 风险% / (止损倍数 × ATR × 合约乘数)

    ATR(14) = TR 的 **简单均值**（不是 Wilder 平滑）—— 与引擎 `_atr` 一致。

为什么值得单独跑一遍：
    同一笔「1% 风险」预算，落在不同品种上是完全不同的手数和完全不同的钱。
    手小、ATR 大的品种（焦煤一跳 30 元）会被算成很少的手；
    手小、跳动小的品种（苯乙烯一跳 5 元）会被算成很多的手 —— 于是撞上
    **最小开仓手数**。

三个容易忽略、但会决定「这品种做不做得起」的指标：

  ① 最低权益 = 一手风险 ÷ 风险%
     这个品种「买得起一手」需要多少本金。低于它，风险% 就是空话。

  ② 最小开仓手数（`open_min_limit_order_volume`，**是「开仓」不是「下单」**）
     交易所对开仓单的手数下限，不足会 **静默拒单**（tqsdk 只打一条 WARNING）。
     ⚠️ 它不是常数、随合约变化：2026-09-23 实测 DCE.v2605 / CZCE.MA605 = 8 手，
        2026-09-25 实测 v2701 / MA610 / 全部候选 = 1 手（碳酸锂 2 手）。
        ⇒ **换月后必须重跑本工具**，不能把某个数字写死进参数。

  ③ 开平摩擦 = (2 × 手续费 + 4 × 一跳) ÷ 一手风险
     一次完整交易的固定成本占单笔风险的比例。
     「4 跳」= 开平 2 次成交 × 每次 2 跳（价格层 ACTIVE 对价 1 跳 + 费用层 slip_ticks=1 跳）。
     ⚠️ 它等价于 **4 ÷ (ATR ÷ 一跳)** —— 也就是「止损距离有几跳」。
        止损距离越短，滑点占比越高：
            苯乙烯 ATR 82 跳 → 摩擦 5%      ｜  玉米 ATR 5.2 跳 → 摩擦 82%
     这解释了 03 布林带「毛利 +6,240 为正、1,104 笔成本把 124% 吃光」：
     成本不是手续费贵，是**止损太近、滑点相对太贵**。

⚠️ ATR 用的是【主连】K 线；换月那天会有跳空，会让该日 ATR 偏大一点。
   做仓位估算够用，做精确回测请让引擎自己算。
"""

import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
while ROOT != os.path.dirname(ROOT) and not os.path.isdir(os.path.join(ROOT, "lib")):
    ROOT = os.path.dirname(ROOT)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config_account as CA

默认池 = ["RU", "EB", "JM", "P", "M", "V", "LH", "JD", "SR"]


def 读品种(key):
    import importlib.util
    p = os.path.join(ROOT, "品种池", key + ".py")
    if not os.path.isfile(p):
        raise SystemExit(f"品种池里没有 {key}.py")
    spec = importlib.util.spec_from_file_location(f"品种池_{key}", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.品种


_主力映射 = None


def 探测品种(api, key):
    """品种池里还没建配置的候选品种 —— 直接从行情推导，省得先建文件再删。

    乘数 / 最小变动 / 手续费都是行情对象自带的实参，主连按 KQ.m@交易所.代码 拼。
    （「候选能不能进池」的初筛阶段用；要正式启用还是落一个 品种池/<key>.py。）
    """
    global _主力映射
    if _主力映射 is None:
        _主力映射 = {}
        for c in sorted(api.query_cont_quotes()):
            exch, _, rest = c.partition(".")
            prod = "".join(ch for ch in rest if not ch.isdigit())
            _主力映射.setdefault(prod.lower(), (c, exch, prod))
    命中 = _主力映射.get(key.lower())
    if not 命中:
        raise SystemExit(f"品种池里没有 {key}.py，行情里也找不到品种 {key}")
    合约, exch, prod = 命中
    q = api.get_quote(合约)
    轮询(api, lambda: float(getattr(q, "volume_multiple", 0) or 0) > 0,
        超时=20, 说明=f"取 {合约} 合约参数")
    乘数 = float(getattr(q, "volume_multiple", 0) or 0)
    跳 = float(getattr(q, "price_tick", 0) or 0)
    费 = float(getattr(q, "commission", 0) or 0)
    print(f"  [自动探测] {key} 不在品种池 ⇒ 用 {合约} 实参"
          f"（乘数 {乘数:g} / 一跳 {跳:g} / 手续费 {费:g}）", flush=True)
    return {"key": key, "中文名": prod, "主连": f"KQ.m@{exch}.{prod}",
            "交易所": exch, "乘数": 乘数, "一跳": 跳, "成本": 费}


def 轮询(api, 条件, 超时=60, 说明=""):
    end = time.time() + 超时
    while time.time() < end:
        api.wait_update(deadline=time.time() + 0.5)
        try:
            if 条件():
                return True
        except Exception:
            pass
    print(f"  [警告] 等待超时（{超时}s）：{说明}", flush=True)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keys", type=str, default="", help="逗号分隔的品种 key，默认核心 9 个")
    ap.add_argument("--equity", type=float, default=400000, help="权益基数（默认 400,000）")
    ap.add_argument("--risk", type=float, default=0.30, help="单笔风险 %（默认 0.30）")
    ap.add_argument("--stop", type=float, default=1.0, help="止损倍数 × ATR（默认 1.0）")
    ap.add_argument("--atr-period", type=int, default=14, help="ATR 周期（默认 14）")
    ap.add_argument("--kline", type=int, default=1800, help="K 线秒数（默认 1800=30min）")
    ap.add_argument("--hand-min", type=int, default=1, help="手数下限（默认 1）")
    ap.add_argument("--hand-max", type=int, default=25, help="手数上限（默认 25，v4 口径）")
    ap.add_argument("--margin-cap", type=float, default=30.0,
                    help="单品种保证金占权益上限 %%（默认 30；0=不限）")
    args = ap.parse_args()

    keys = [k.strip().upper() for k in args.keys.split(",") if k.strip()] or 默认池

    import pandas as pd
    from tqsdk import TqApi, TqAuth

    u, p = CA.check()
    api = TqApi(auth=TqAuth(u, p))
    try:
        def 配置(k):
            p_ = os.path.join(ROOT, "品种池", k + ".py")
            return 读品种(k) if os.path.isfile(p_) else 探测品种(api, k)

        品种表 = [(k, 配置(k)) for k in keys]

        ks = {}
        for k, d in 品种表:
            ks[k] = api.get_kline_serial(d["主连"], args.kline, data_length=200)
        轮询(api, lambda: all(float(x["datetime"].iloc[0]) > 0 and float(x["high"].iloc[0]) > 0
                              for x in ks.values()),
            超时=90, 说明="取主连 K 线")

        # ---- 保证金/手续费要从【真实主力合约】取：主连行情里这两个字段是空的 ----
        主连行情 = {k: api.get_quote(d["主连"]) for k, d in 品种表}
        轮询(api, lambda: all(bool(getattr(q, "underlying_symbol", "")) for q in 主连行情.values()),
            超时=30, 说明="解析各品种主力合约")
        主力代码 = {k: (getattr(q, "underlying_symbol", "") or "") for k, q in 主连行情.items()}
        主力行情 = {k: api.get_quote(u) for k, u in 主力代码.items() if u}
        轮询(api, lambda: all(float(getattr(q, "margin", 0) or 0) > 0 for q in 主力行情.values()),
            超时=30, 说明="取主力合约保证金")

        def 保金(k):
            return float(getattr(主力行情.get(k), "margin", 0) or 0)

        def 最小开仓(k):
            """交易所对【开仓单】的最小手数要求。

            ⚠️ 字段名陷阱：`min_limit_order_volume` 是「最小限价单**手数**」，
               真正的开仓下限是 `open_min_limit_order_volume` /
               `open_min_market_order_volume` —— 读错字段会得到 1，看起来毫无约束。
               实测（2026-09-25）DCE.v2701 / CZCE.MA610 的开仓下限就是这个值。

            ⚠️ 它是不夹手数之后真正的新下限：tqsdk 用它拦单，
               手数不够时是 **静默拒单**（日志一条 WARNING「剩余开仓手数 X 小于最小开仓手数 N」，
               策略会以为仓位已经开了）。09-23 甲醇 11/87 个信号就是这么丢的。
            """
            q = 主力行情.get(k)
            限 = float(getattr(q, "open_min_limit_order_volume", 0) or 0)
            市 = float(getattr(q, "open_min_market_order_volume", 0) or 0)
            return max(限, 市)

        def 手续费(k):
            v = float(getattr(主力行情.get(k), "commission", 0) or 0)
            return v if v > 0 else 品种表[k][1]["成本"]

        print(f"  主力合约：{'  '.join(f'{k}={v}' for k, v in 主力代码.items() if v)}")

        预算 = args.equity * args.risk / 100.0
        print()
        print("=" * 104)
        print(f"  风险预算体检 ｜ 权益 {args.equity:,.0f} 元 × 风险 {args.risk}%"
              f" = 单笔 {预算:,.0f} 元 ｜ 止损 {args.stop}×ATR({args.atr_period})"
              f" ｜ K线 {args.kline}s")
        print("=" * 104)
        print(f"{'品种':<10}{'ATR':>9}{'一跳':>6}{'一手风险':>10}"
              f"{'理论手':>8}{'手数':>6}{'实际风险':>10}{'占预算':>8}"
              f"{'最低权益':>10}{'开平摩擦':>10}{'最小开仓':>9}")
        print("-" * 124)

        汇总 = []
        for k, d in 品种表:
            kl = ks[k]
            high = kl["high"].astype(float)
            low = kl["low"].astype(float)
            close = kl["close"].astype(float)
            prev = close.shift(1)
            tr = pd.concat([(high - low).abs(), (high - prev).abs(),
                            (low - prev).abs()], axis=1).max(axis=1)
            a = float(tr.rolling(args.atr_period).mean().iloc[-1])

            一跳 = d["乘数"] * d["一跳"]
            一手风险 = args.stop * a * d["乘数"]      # 打掉止损亏多少（元/手）
            理论手 = (预算 / 一手风险) if 一手风险 > 0 else float("nan")

            # ① 最松：只做四舍五入，不夹
            宽松手 = max(1, int(round(理论手))) if 理论手 == 理论手 else 1
            # ② 引擎 v4 口径：四舍五入后夹 [hand_min, hand_max]
            夹后手 = min(max(int(round(理论手)), args.hand_min), args.hand_max) \
                if 理论手 == 理论手 else args.hand_min

            手 = 宽松手
            实际风险 = 手 * 一手风险
            成本 = 手续费(k)
            摩擦 = 2 * 成本 + 4 * 一跳
            # ↑ 一次完整交易 = 开 + 平 = 2 次成交；
            #   每次成交的滑点 = 价格层 1 跳（exec_price="ACTIVE" 对价单）
            #                    + 费用层 slip_ticks=1 跳 = 2 跳 ⇒ 共 4 跳。
            #   这就是 03 报告里「手续费 6,230 元吃光毛利」那句话的构成。
            汇总.append({
                "key": k, "品种": d, "ATR": a, "一跳": 一跳,
                "一手风险": 一手风险, "理论手": 理论手,
                "宽松手": 宽松手, "夹后手": 夹后手,
                "实际风险": 实际风险, "成本": 成本, "摩擦": 摩擦,
                "保金": 保金(k), "最小开仓": 最小开仓(k),
            })
            最低权益 = 一手风险 / (args.risk / 100.0) if args.risk > 0 else float("nan")
            print(f"{d['中文名']:<10}{a:>9.2f}{一跳:>6.0f}{一手风险:>10,.0f}"
                  f"{理论手:>8.2f}{手:>6}{实际风险:>10,.0f}"
                  f"{实际风险/预算*100:>7.0f}%{最低权益:>10,.0f}"
                  f"{(摩擦/一手风险*100 if 一手风险 else float('nan')):>9.1f}%"
                  f"{最小开仓(k):>9.0f}")

        有限 = [x["理论手"] for x in 汇总 if x["理论手"] == x["理论手"]]
        理论最小 = min(有限) if 有限 else float("nan")
        理论最大 = max(有限) if 有限 else float("nan")
        if 有限 and 理论最小 > 0:
            print(f"\n  理论手数跨度：{理论最小:.2f} ~ {理论最大:.2f} 手"
                  f"（同一笔预算，最费手的品种是最省手品种的 {理论最大/理论最小:.1f} 倍）")

        # ---- 关键：夹逼会把「风险预算」变成谎话 ----
        print()
        print("=" * 104)
        print(f"  如果沿用 v4 的夹逼 [{args.hand_min}, {args.hand_max}]"
              f"（旧池子是低价品种，所以夹得住；新池子未必）")
        print("=" * 104)
        print(f"{'品种':<10}{'理论手':>8}{'夹后手':>7}{'实际风险':>10}{'预算倍率':>10}"
              f"{'一手保证金':>11}{'保证金合计':>11}")
        print("-" * 104)
        恶化 = []
        for x in 汇总:
            风险夹后 = x["夹后手"] * x["一手风险"]
            倍率 = 风险夹后 / 预算 if 预算 else 0
            if abs(倍率 - 1) > 0.3:
                恶化.append((x, 倍率))
            print(f"{x['品种']['中文名']:<10}{x['理论手']:>8.2f}{x['夹后手']:>7}"
                  f"{风险夹后:>10,.0f}{倍率:>10.2f}x"
                  f"{x['保金']:>11,.0f}{x['保金']*x['夹后手']:>11,.0f}")
        if 恶化:
            print()
            print(f"  ⚠️⚠️ {len(恶化)} 个品种的实际单笔风险偏离预算超过 30%：")
            for x, 倍率 in sorted(恶化, key=lambda t: -t[1]):
                print(f"     {x['品种']['中文名']:<8}理论 {x['理论手']:.2f} 手 → "
                      f"夹成 {x['夹后手']} 手 ⇒ 实际风险 {x['夹后手']*x['一手风险']:,.0f} 元"
                      f" = 预算({预算:,.0f})的 {倍率:.2f} 倍")
            print("     ⇒ 「每笔只亏 1,200 元」这句话**已经不成立了**。"
                  "夹逼把风险预算变成了名义上的，真实风险由夹的上限决定。")

        # ---- ★ 「不夹手数」之后真正的新下限：交易所最小限价开仓手数 ----
        print()
        print("=" * 108)
        print("  不夹手数之后的新下限：交易所【最小限价开仓手数】")
        print("=" * 108)
        受制 = []
        for x in 汇总:
            m = x["最小开仓"]
            if m > 0 and x["理论手"] < m:
                实际 = m
                风险 = 实际 * x["一手风险"]
                受制.append((x, 实际, 风险))
        if not 受制:
            print("  ✅ 没有品种被最小开仓手数制约（理论手数都够）")
        else:
            print(f"  ⚠️ {len(受制)} 个品种的【理论手数 < 最小开仓手数】"
                  f"⇒ 手数下限仍然决定真实风险：")
            print(f"{'品种':<10}{'理论手':>8}{'最小开仓':>9}{'实际风险':>10}"
                  f"{'占预算':>8}{'保证金':>11}{'够格权益':>11}")
            print("-" * 108)
            for x, 实际, 风险 in sorted(受制, key=lambda t: -(t[2] / 预算)):
                够格 = x["一手风险"] * x["最小开仓"] / (args.risk / 100.0)
                print(f"{x['品种']['中文名']:<10}{x['理论手']:>8.2f}{实际:>9.0f}"
                      f"{风险:>10,.0f}{风险/预算*100:>7.0f}%"
                      f"{x['保金']*实际:>11,.0f}{够格:>11,.0f}")
            print()
            print("     「够格权益」= 最小开仓 × 一手风险 ÷ 风险% —— 这个品种要在这个本金上")
            print("     才能让「单笔 = 风险%」这句话成立。本金不够时，单笔风险由最小开仓手数决定，")
            print("     与 risk% 参数完全脱钩（这就是「风险预算变成名义上的」的完整机制）。")
            print("     ⚠️ 拒单是**静默**的：tqsdk 只打一条 WARNING，策略会以为仓位已经开了。")

        # ---- 组合层面：全部同时持仓要多少钱 ----
        print()
        print("=" * 118)
        print(f"  组合层面：{len(汇总)} 个品种同时持仓，钱够不够"
              f"（保证金按主力合约实数 ｜ 单品种上限 {args.margin_cap:g}% 权益）")
        print("=" * 118)
        print(f"{'品种':<10}{'风险手':>7}{'上限手':>7}{'实开手':>7}"
              f"{'保证金/手':>12}{'保证金合计':>12}{'占权益':>9}{'单笔风险':>10}")
        print("-" * 118)
        总保证金 = 0.0
        总实开 = 0
        for x in 汇总:
            if args.margin_cap > 0 and x["保金"] > 0:
                上限手 = int(args.equity * args.margin_cap / 100.0 / x["保金"])
            else:
                上限手 = 10 ** 9
            实开 = max(1, min(x["宽松手"], 上限手))
            合计 = x["保金"] * 实开
            总保证金 += 合计
            总实开 += 实开
            标记 = " ⚠️" if 实开 < x["宽松手"] else ""
            print(f"{x['品种']['中文名']:<10}{x['宽松手']:>7}"
                  f"{(上限手 if 上限手 < 10 ** 8 else 0):>7}{实开:>7}"
                  f"{x['保金']:>12,.0f}{合计:>12,.0f}"
                  f"{合计/args.equity*100:>8.1f}%{实开*x['一手风险']:>10,.0f}{标记}")
        print("-" * 118)
        print(f"{'合计':<10}{sum(x['宽松手'] for x in 汇总):>7}{'':>7}{总实开:>7}"
              f"{'':>12}{总保证金:>12,.0f}{总保证金/args.equity*100:>8.1f}%")
        print()
        if 总保证金 > args.equity:
            print(f"  ⚠️ 保证金合计 {总保证金:,.0f} 元 > 权益 {args.equity:,.0f} 元"
                  f"（{总保证金/args.equity:.2f} 倍）⇒ 这 9 个品种**不可能同时开满**")
        else:
            print(f"  ✅ 保证金合计占权益 {总保证金/args.equity*100:.1f}%，还留 "
                  f"{(1-总保证金/args.equity)*100:.1f}% 余地")
        print()
        print("  ⚠️ 上面是【交易所】保证金口径，期货公司通常再加 2~4 个点 ⇒ 实盘要按券商的数重算。")
    finally:
        api.close()


if __name__ == "__main__":
    main()
