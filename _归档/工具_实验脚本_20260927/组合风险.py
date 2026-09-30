# -*- coding: utf-8 -*-
"""
==========================================================
 组合风险：N 条腿同时持仓，总风险是多少
==========================================================

用法：
    python 工具/组合风险.py                      # 核心 9 池，单笔 0.30%
    python 工具/组合风险.py --f 0.3 --equity 400000
    python 工具/组合风险.py --pos RU,EB,JM       # 只看某个子集
    python 工具/组合风险.py --roll 60            # 滚动窗口天数（默认 60）
    python 工具/组合风险.py --no-net             # 不联网，用缓存

它回答的问题：
    单个品种「单笔风险 0.30%」很好懂。但 9 个组合文件各自都是 0.30%，
    如果它们【同时】持仓，账户那一刻的风险敞口是多少？

    ——这是仓位管理从「单笔」跨到「组合」时必须回答的第一个问题。

五块输出：
  【一】分散化数学：N 腿总风险 = f × sqrt(N + N(N-1)·ρ̄)
       完全相关 → N×f  ｜ 完全独立 → √N×f  ｜ 真实矩阵 → 介于两者之间
  【二】尾部相关：平均相关是个「太平时期」的数字。
       2a 滚动窗口跑一遍看它最坏能坏到哪；2b 下行 vs 上行半相关。
  【三】同日共振：比相关系数更贴近「止损同时被打」。
       取每个品种最差的 5% 交易日，看当天其他品种也在跌的比例。
  【四】保证金硬约束：钱不够，数学再漂亮也开不出来。
  【五】反推：若规定组合总风险上限 X%，最多能同时持几条腿
       —— 并讨论「每腿固定 f」vs「总预算固定 F、腿间切分」到底该选哪个。

⚠️ 三个必须知道的边界：
  ① 这里的「风险」把 f 当成波动的代理（f 本身就是「止损被打时亏多少」）。
     相关矩阵合成的是**波动**的相关，不是**止损事件**的相关。
  ② 止损是**尾部事件**，而皮尔逊相关在尾部通常**低估**联动。
     所以【三】同日共振才是「同时被打」的主证据，不要只看【二】。
  ③ 指数合约（KQ.i@）的收盘价是各月合约的加权，**不含换月跳空**，
     比主连更适合算相关（主连换月那天的假跳空会污染相关性）。

⚠️ 数据来源：`KQ.i@` 指数日线 260 根。样本约一年，**统计噪声不小**：
   260 天日收益、n≈250 时，相关系数的标准误约 ±0.06。
   看到 0.22 vs 0.19 这种差值**不要当结论**（见 `品种池/_候选池_2026-09-25.md`）。
"""

import argparse
import itertools
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


def 轮询(api, 条件, 超时=60, 说明=""):
    """带 deadline 的等待：绝不允许无限阻塞（tqsdk 不传 deadline 会卡死）。"""
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


def 读品种(key):
    import importlib.util
    p = os.path.join(ROOT, "品种池", key + ".py")
    if not os.path.isfile(p):
        return None
    spec = importlib.util.spec_from_file_location(f"品种池_{key}", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.品种


def 拉收益(keys):
    """返回 (收益率 DataFrame, 元数据 dict, 保证金 dict)。列 = 品种 key。"""
    import pandas as pd
    from tqsdk import TqApi, TqAuth

    u, p = CA.check()
    api = TqApi(auth=TqAuth(u, p))
    try:
        ks, 元 = {}, {}
        for k in keys:
            d = 读品种(k)
            if not d:
                print(f"  [跳过] 品种池里没有 {k}.py", flush=True)
                continue
            元[k] = d
            # ⚠️ 指数合约的代码大小写必须与交易所一致（SHFE.ru 小写 / CZCE.SR 大写），
            #    从「主连」里原样拆出来最稳，别用品种池的 key（它是大写规范化的）。
            tar = d["主连"].split("@")[-1]          # 'SHFE.ru'
            exch, _, prod = tar.partition(".")
            idx = f"KQ.i@{exch}.{prod}"
            try:
                ks[k] = api.get_kline_serial(idx, 86400, data_length=260)
            except Exception as e:
                print(f"  [跳过] {k} 订阅指数日线失败：{e}", flush=True)

        轮询(api, lambda: all(float(x["datetime"].iloc[0]) > 0 and
                              float(x["close"].iloc[0]) > 0 for x in ks.values()),
            超时=90, 说明="取指数日线（必须等最早一根也就绪，否则前段会被当成缺失）")

        数据 = {}
        for k, kl in ks.items():
            s = pd.Series(kl["close"].to_numpy(), index=kl["datetime"].to_numpy())
            s = s[s > 0]
            if len(s) > 30:
                数据[k] = s.pct_change()
        df = pd.DataFrame(数据)
        有效 = df.dropna()
        if 有效.empty:
            raise SystemExit("  取到的有效数据为空")

        t0 = pd.to_datetime(有效.index[0], unit="ns")
        t1 = pd.to_datetime(有效.index[-1], unit="ns")
        print(f"\n  样本：{len(有效)} 个交易日（{t0:%Y-%m-%d} ~ {t1:%Y-%m-%d}）"
              f"  ｜  品种 {len(有效.columns)} 个", flush=True)

        # 保证金：从真实主力合约取（主连行情里这个字段是空的）
        保金 = {}
        try:
            行情 = {k: api.get_quote(d["主连"]) for k, d in 元.items()}
            轮询(api, lambda: all(bool(getattr(q, "underlying_symbol", "")) for q in 行情.values()),
                超时=30, 说明="解析主力合约")
            主力 = {k: getattr(q, "underlying_symbol", "") for k, q in 行情.items()}
            mq = {k: api.get_quote(v) for k, v in 主力.items() if v}
            轮询(api, lambda: all(float(getattr(q, "margin", 0) or 0) > 0 for q in mq.values()),
                超时=30, 说明="取保证金")
            for k in 元:
                保金[k] = float(getattr(mq.get(k), "margin", 0) or 0) or float(元[k].get("保证金", 0) or 0)
        except Exception as e:
            print(f"  [警告] 保证金取不到（{e}），退回品种池配置值", flush=True)
            for k in 元:
                保金[k] = float(元[k].get("保证金", 0) or 0)

        return 有效, 元, 保金
    finally:
        api.close()


def 组合因子(C, idx):
    """sqrt(1' C 1) —— N 腿等风险权重下的组合风险倍数。

    每腿风险都是 f（=权益×风险%），故组合风险 = f × sqrt(ΣᵢΣⱼ ρᵢⱼ)。
    完全相关时 C 全 1 ⇒ sqrt(N²) = N；完全独立时 C = I ⇒ sqrt(N)。
    """
    import numpy as np
    n = len(idx)
    ones = np.ones(n)
    sub = C[np.ix_(idx, idx)]
    return float((ones @ sub @ ones) ** 0.5)


def 打印一(df, f, 权益, 手数表=None):
    import numpy as np
    cols = list(df.columns)
    C = df.corr().to_numpy()
    N = len(cols)

    print()
    print("=" * 96)
    print(f"  【一】分散化数学：N 条腿同时持仓，组合风险不是 N × 单笔")
    print("=" * 96)
    print(f"  单笔风险 f = {f}% × 权益 {权益:,.0f} = {f/100*权益:,.0f} 元/腿")
    print(f"  公式：组合风险 = f × √(N + N(N-1)·ρ̄)      完全相关→N×f  独立→√N×f")
    print()
    print(f"  {'腿数 N':>7}{'完全相关 N×f':>15}{'真实矩阵':>12}{'完全独立 √N×f':>15}"
          f"{'省下':>9}  说明")
    print("-" * 96)
    for n in range(1, N + 1):
        idx = list(range(n))
        因真实 = 组合因子(C, idx)
        因相关 = float(n)
        因独立 = n ** 0.5
        省 = (1 - 因真实 / 因相关) * 100
        注 = "（单腿）" if n == 1 else f"{len(cols[:n])} 腿等风险"
        print(f"  {n:>7}{因相关*f/100*权益:>14,.0f}元{因真实*f/100*权益:>11,.0f}元"
              f"{因独立*f/100*权益:>14,.0f}元{省:>8.0f}%  {注}")
    print()
    因全 = 组合因子(C, list(range(N)))
    print(f"  ⇒ 全 {N} 腿同时持仓（用真实相关矩阵）：组合风险 "
          f"{因全*f/100*权益:,.0f} 元 = 权益的 {因全*f/100*权益/权益*100:.2f}%"
          f" = 单笔的 {因全:.2f} 倍")
    print(f"     若按「完全相关」估算会是 {N*f:.2f}%（虚高 {N/因全:.2f} 倍）")
    print(f"     若按「完全独立」估算会是 {N**0.5*f:.2f}%（虚低 {因全/N**0.5:.2f} 倍）"
          f"  ←★ 这是最常被犯的错")

    # 腿数子集：最集中 vs 最分散
    if N >= 4:
        for n in (3, 4):
            if n > N:
                continue
            组 = [(组合因子(C, list(c)), c) for c in itertools.combinations(range(N), n)]
            组.sort()
            最散, 最集 = 组[0], 组[-1]
            print()
            print(f"  {n} 腿子集（共 {len(组)} 种）—— 选品种的差距：")
            print(f"     最分散：{'/'.join(cols[i] for i in 最散[1]):<20}"
                  f" 组合风险 {最散[0]*f/100*权益:>9,.0f} 元 ({最散[0]*f:.2f}%)")
            print(f"     最集中：{'/'.join(cols[i] for i in 最集[1]):<20}"
                  f" 组合风险 {最集[0]*f/100*权益:>9,.0f} 元 ({最集[0]*f:.2f}%)"
                  f"  = 最分散的 {最集[0]/最散[0]:.2f} 倍")

    return C


def 平均相关(df, 取绝对值=True):
    """全样本平均 pairwise 相关。取绝对值=True 时忽略方向，只看"联不联动"。"""
    pairs = list(itertools.combinations(df.columns, 2))
    if not pairs:
        return float("nan")
    vals = [df[a].corr(df[b]) for a, b in pairs]
    vals = [abs(v) for v in vals] if 取绝对值 else vals
    return sum(vals) / len(vals)


def 打印二(df, f, 权益, 窗口):
    cols = list(df.columns)
    全期 = 平均相关(df)

    print()
    print("=" * 96)
    print("  【二】相关性不是常数：它随时间和方向变")
    print("=" * 96)
    print(f"  全期平均|相关| = {全期:.3f}   ← 这个数字你会看到很多次，但它只是一个平均")

    # ---- 2a 滚动窗口：窗口越短，峰值越高 ----
    print()
    print("  ▸ 2a 滚动窗口：相关性是**会动的**（下面看它最坏能坏到什么程度）")
    print(f"  {'窗口':>8}{'中位':>10}{'95分位':>10}{'峰值':>10}{'峰值/中位':>12}")
    print("-" * 96)
    窗口表 = [20, 60, 120]
    if 窗口 not in 窗口表:
        窗口表.append(窗口)
    for w in sorted(窗口表):
        if len(df) < w + 5:
            continue
        各对 = {(a, b): df[a].rolling(w).corr(df[b])
               for a, b in itertools.combinations(cols, 2)}
        R = pd_DataFrame(各对).dropna()
        if R.empty:
            continue
        均 = R.mean(axis=1)
        峰, 中 = 均.max(), 均.median()
        标 = "  ← --roll 指定" if w == 窗口 else ""
        print(f"  {w:>7}日{中:>10.3f}{均.quantile(0.95):>10.3f}{峰:>10.3f}"
              f"{(峰/中 if 中 else float('nan')):>11.2f}x{标}")
    print()
    print("  ⇒ 窗口越长峰值越低 —— 不是因为风险变小了，是**被平均掉了**。")
    print("     相关性的爆发是短促的（几天到几周），用 60/120 日窗口看不见它。")

    # ---- 2b 下行 vs 上行：用「半相关」，不是「条件筛选」 ----
    #
    # ⚠️⚠️ 这里是一个真实的统计陷阱，本工具第一版就掉进去了：
    #    「只取市场下跌的那些日子，再算相关」—— 看起来天经地义，其实是错的。
    #    因为「市场下跌」= 「各品种收益之和为负」这个条件，
    #    会自动让各品种之间呈现**负相关**（一个更负，另一个就得没那么负才能满足条件）。
    #    N 个相互独立的品种，条件在「均值为负」上，pairwise 相关会变成约 -1/(N-1)。
    #    9 个品种 ⇒ 偏差约 -0.125。第一版实测：全样本 0.220（绝对值口径），
    #    「下跌日」子样本 0.118 ⇒ 看起来"下跌时联动更弱"，正好是偏差制造的假象。
    #    ⇒ **做条件子样本的相关分析，先问一句「这个条件本身是否含了共同的项」。**
    #
    #    正确做法之一：**半相关**（semi-correlation）—— 不做筛选，改做变换：
    #    把正收益置 0 得「下行序列」、把负收益置 0 得「上行序列」，
    #    再分别算相关。这样样本量不变，不存在选择偏差。
    print()
    print("  ▸ 2b 下行 vs 上行：腿在**下跌时**是否抱得更紧")
    上行 = df.clip(lower=0)          # 只留涨的部分
    下行 = df.clip(upper=0)          # 只留跌的部分
    上v, 下v = 平均相关(上行), 平均相关(下行)
    有正 = 平均相关(df, 取绝对值=False)
    print(f"  {'口径':<24}{'平均相关':>12}  说明")
    print("-" * 96)
    print(f"  {'全样本（有符号平均）':<24}{有正:>12.3f}  正负相抵后的净联动")
    print(f"  {'全样本（绝对值平均）':<24}{平均相关(df):>12.3f}  ★ 忽略方向，只看「动不动一起」")
    print(f"  {'上行半相关（涨的部分）':<24}{上v:>12.3f}")
    print(f"  {'下行半相关（跌的部分）':<24}{下v:>12.3f}")
    if 上v > 0:
        print()
        print(f"  ⇒ 下行 {下v:.3f} vs 上行 {上v:.3f}"
              f" ⇒ 下跌时的联动是上涨时的 **{下v/上v:.2f} 倍**")
    print()
    print("  ⚠️ 注意：本池在这 259 天里**没有**表现出教科书式的「下跌联动更强」。")
    print("     两种可能：① 池子确实分散得好 ② 样本只有 259 天、且以震荡行情为主，")
    print("     还没经历过一次真正的全市场暴跌（真正的尾部样本是缺失的）。")
    print("     ⇒ **不要把「没观测到」当成「不会发生」**。真实的多腿组合，")
    print("       相关性在危机中冲向 1 是所有从业者的共同经验，代价由没见过它的人支付。")


def pd_DataFrame(d):
    import pandas as pd
    return pd.DataFrame(d)


def 打印三(df, 姓名):
    """同日共振：比相关系数更贴近「止损同时被打」。"""
    cols = list(df.columns)
    print()
    print("=" * 96)
    print("  【三】同日共振：某品种大跌那天，其他腿在干什么（这才是「止损同时被打」）")
    print("=" * 96)
    print("  做法：取每个品种自己最差的 5% 交易日，看当天其他品种也**收跌**的比例。")
    print("        比例接近 100% ⇒ 那天你的多条腿会一起触发止损。")
    print()
    print(f"  {'品种':<10}{'自身最差日':>10}{'其他腿也跌':>12}  共振最狠的 3 条腿")
    print("-" * 96)
    汇总 = []
    for a in cols:
        thr = df[a].quantile(0.05)
        坏 = df.index[df[a] <= thr]
        if len(坏) == 0:
            continue
        比例 = {}
        for b in cols:
            if b == a:
                continue
            比例[b] = float((df.loc[坏, b] < 0).mean())
        均 = sum(比例.values()) / len(比例)
        汇总.append((均, a, 比例))
        最狠 = sorted(比例.items(), key=lambda t: -t[1])[:3]
        print(f"  {姓名.get(a,a):<10}{len(坏):>10}{均:>11.0%}  "
              + "  ".join(f"{姓名.get(b,b)} {v:.0%}" for b, v in 最狠))
    print("-" * 96)
    总体 = sum(x[0] for x in 汇总) / len(汇总)
    print(f"  {'平均':<10}{'':>10}{总体:>11.0%}")
    print()
    print(f"  ⇒ 一个品种暴跌时，平均 {总体:.0%} 的其他腿也在跌。")
    print(f"     这不是「相关性 0.2」的样子 —— 因为**止损是尾部事件**，")
    print(f"     平时各走各的，出事时一起出事（皮尔逊相关在尾部系统性低估联动）。")
    最 = max(汇总)
    print(f"     最抱团的是【{姓名.get(最[1],最[1])}】：它暴跌时平均 {最[0]:.0%} 的其他腿也在跌。")
    return 总体


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keys", type=str, default="", help="逗号分隔品种 key，默认核心 9 个")
    ap.add_argument("--pos", type=str, default="", help="只看某个子集（同 --keys 的别名）")
    ap.add_argument("--f", type=float, default=0.30, help="单笔风险 %%（默认 0.30）")
    ap.add_argument("--equity", type=float, default=400000, help="权益（默认 400,000）")
    ap.add_argument("--roll", type=int, default=60, help="滚动相关窗口天数（默认 60）")
    args = ap.parse_args()

    原始 = args.pos or args.keys
    keys = [k.strip().upper() for k in 原始.split(",") if k.strip()] or 默认池
    keys = [k for k in dict.fromkeys(keys)]        # 去重保序

    df, 元, 保金 = 拉收益(keys)
    姓名 = {k: 元[k].get("中文名", k) for k in df.columns}
    cols = list(df.columns)

    C = 打印一(df, args.f, args.equity, None)
    打印二(df, args.f, args.equity, args.roll)
    共振 = 打印三(df, 姓名)

    # ---- 【四】保证金与腿数上限 ----
    import numpy as np
    print()
    print("=" * 96)
    print("  【四】保证金是硬约束：钱不够，数学再漂亮也开不出来")
    print("=" * 96)
    print(f"  {'品种':<10}{'保证金/手':>12}{'理论手':>9}{'占用':>12}{'占权益':>9}")
    print("-" * 96)
    总 = 0.0
    for k in cols:
        m = 保金.get(k, 0)
        # 理论手数由「单笔风险 = 权益×f」反推；这里用品种池里的 一手风险 会不准
        # （一手风险依赖 ATR，本工具不拉 ATR），故只报「1 手占用」的下限口径。
        占 = m * 1
        总 += 占
        print(f"  {姓名.get(k,k):<10}{m:>12,.0f}{1:>9}{占:>12,.0f}{占/args.equity*100:>8.1f}%")
    print("-" * 96)
    print(f"  {'各开 1 手':<10}{'':>12}{len(cols):>9}{总:>12,.0f}{总/args.equity*100:>8.1f}%")
    print()
    print("  ⚠️ 上表是【各开 1 手】的下限口径（真实手数按 ATR 反推，通常 >1 手）。")
    print("     真实占用请跑 `python 工具/风险预算体检.py`（它会拉 ATR 并给出实际手数）。")
    print(f"     交易所口径 ≈ {总/args.equity:.2f} 倍权益？"
          f"{'是 ⇒ 不可能同时开满' if 总 > args.equity else '否'}；期货公司通常再加 2~4 个点。")

    # ---- 【五】腿数上限反推 ----
    print()
    print("=" * 96)
    print("  【五】反推：如果规定「组合总风险上限 = X%」，最多能同时持几条腿")
    print("=" * 96)
    因子 = 组合因子(C, list(range(len(cols))))
    每腿因子 = 因子 / len(cols)
    print(f"  {'组合风险上限':>14}{'可持腿数':>10}   说明")
    print("-" * 96)
    for 上限 in (1.0, 1.5, 2.0, 3.0, 5.0):
        n = int(上限 / (args.f * 每腿因子))
        注 = "（按真实矩阵的边际增量 ≈ {:.3f}%/腿）".format(args.f * 每腿因子)
        print(f"  {上限:>13.1f}%{n:>10}   {注}")
    print()
    print(f"  ⇒ 单笔 {args.f}% 时，每多一条腿，组合风险约 +{args.f*每腿因子:.3f}%"
          f"（≈单笔的 {每腿因子:.2f} 倍，因为腿之间不是完全相关）。")
    print(f"     注意这是【等风险权重】口径：每腿都下 {args.f}%。")
    print()
    print(f"  ▸ 那要不要改成「总预算固定、腿间切分」（每腿 f = F/N）？")
    print(f"    数学上两种切法**夏普完全相同**（夏普对尺度不变），差别只在绝对敞口：")
    print(f"      每腿固定 {args.f}%  ⇒ 总风险额随 N 线性增长（{args.f}% → {args.f*因子:.2f}%）")
    print(f"      总预算固定 F      ⇒ 总风险额恒定，每腿被 N 稀释")
    print(f"    但 f = F/N 有两个**执行层面**的硬伤：")
    print(f"      ① 腿数在时间轴上浮动 ⇒ 每腿手数天天变（今天 6 手、明天来一条新腿要改成 5 手，")
    print(f"         可你手上已经握着 6 手）—— 它要求「预知未来同时有几条腿在场」。")
    print(f"      ② 腿少时单笔过重（N=1 时 f = F，一次止损就亏掉账户 F）；")
    print(f"         腿多时每腿过小（若 F={args.f*因子:.2f}%，9 腿时每腿仅 {args.f*因子/9:.3f}%，")
    print(f"         对 RU 这类一手风险 ≈1,000 元的品种连 1 手都开不出）。")
    print(f"    ⇒ 可执行的做法：**每腿固定 {args.f}%（方案 A）** + 组合层加「最大同时持仓腿数」上限。")
    print(f"      「用腿数封顶」，而不是「用缩小每腿预算来封顶」。")
    print()
    print("  ⚠️ 以上全部是【同时持仓】口径。真实运行时腿数在 1 腿和 N 腿之间来回跳。")
    print("  ⚠️ 恒等式「总期望 = k × 总风险额」⇒ 加腿在放大期望的同时也放大亏损，")
    print("     前提是**每条腿的期望为正**。判断某条腿期望是否为正 = 模块 3 的问题。")


if __name__ == "__main__":
    main()
