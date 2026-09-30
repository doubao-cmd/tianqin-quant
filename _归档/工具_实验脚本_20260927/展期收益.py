# -*- coding: utf-8 -*-
"""机制 C｜期限结构 / 展期收益 carry —— 研究级回测器
==========================================================

机制一句话
----------
**买贴水（backwardation）的品种、空升水（contango）的品种，赚期限结构的钱。**

    carry = (P_近月 − P_远月) ÷ P_远月 ÷ 月数差 × 12      ← 年化展期收益率
      · carry > 0（近月贵 = 现货紧张）⇒ 做多
      · carry < 0（近月便宜 = 库存充裕）⇒ 做空

为什么它是本项目最该试的方向（研究路线图 §7.12 C）
--------------------------------------------------
**完全不依赖价格方向预测** —— 只赌「近期供需松紧」，与主线「走风险控制、不预测方向」
最契合。同时**天然绕开本项目最大的发现「环境 > 策略」**（三种不同逻辑的策略按年份同向涨跌）：
期限结构是横截面量，涨跌环境被多空抵消。

★ 数据口径（2026-09-26 全部实测，详见报告 §一）
------------------------------------------------
· K 线数据窗口 **2016-01-04 起**（`v1605` 82 根 = 被起点截断；`v1701` 起 244 根完整）
· **不能用 `query_quotes(expired=True)` 列历史合约**：它只返回 2020-10 之后，
  但 `v1701`/`m1701` 这类更早的合约**能直接拉到** ⇒ 必须**自己构造合约代码**
· `get_kline_serial` 单合约超时 = `25 + 60×1 = 85` 秒（`tqsdk/api.py:780`）
  ⇒ 首次请求冷数据可能超时，**必须带重试**（实测重试后 0.1 秒返回）
· CZCE 用 3 位码（年个位 + 月，**10 年一循环**）⇒ 代码与实际年份有歧义，
  ⇒ **本工具不解析代码，改用「合约最后数据日」推断年月**（见 `合约年月()`）

★ 设计一：自算夏普（Rf = 0），不调 tqsdk 的 `get_sharp`
    `tqsdk/tafunc.py:1478` 的夏普含无风险利率 2.5% ⇒ 长样本被误读成「显著为负」
    （PVC 8.77 年凭空 −4.80）。见 `工具/_输出/_日线校准_结果.md` §三。

★ 设计二：收益拆「隔夜段 + 日内段」
    信号在 `sp` 日**收盘**算出、`sp+1` 日**开盘**建仓。若按 close→close 算，
    会把 `close[sp] → open[sp+1]` 这段**我们手里没有仓位**的跳空算进去 ⇒ 收益虚高。
    见 `工具/_输出/_横截面动量_结果.md` §二.3。

★ 设计三：carry 用**具体合约**算；收益用**连续近月合约**（不是指数）
    指数 `KQ.i@` 是再平衡的连续序列，**换月价差被抹平** ⇒ 它测不到「近月向现货收敛」
    这个 carry 的收益来源。本工具改为**持有近月合约本身**：
      · near = 当日该品种**持仓量最大的合约**（= 主力，实际可交易）
      · 换月日（near 变化）**不计隔夜跳空**（新旧合约价格不可比，记为 0）
    ⇒ 这是**真实持有体验**，不是上界。

判据（**跑前写死**，见 `研究路线图.md` §7.12 C）
"""
import os
import sys
import time
import math
import numpy as np
import pandas as pd
from datetime import date, datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
sys.path.insert(0, ROOT)

# ============================================================
# 预注册参数（跑前写死，不扫描、不择优）
# ============================================================
品种默认 = ["RU", "EB", "JM", "P", "M", "V", "LH", "JD", "SR"]

交易所 = {
    "RU": ("SHFE", "ru"), "EB": ("DCE", "eb"), "JM": ("DCE", "jm"),
    "P":  ("DCE", "p"),   "M":  ("DCE", "m"),  "V":  ("DCE", "v"),
    "LH": ("DCE", "lh"),  "JD": ("DCE", "jd"), "SR": ("CZCE", "SR"),
}

# 合约宇宙（2026-09-26 实测后收窄，见报告 §一）
主力月份 = [1, 5, 9]
# ★ 预注册（2026-09-26）：「1/5/9」是这 9 个品种统一的标准主力月份格点。
#   为什么必须收窄（实测代价）：
#     · 服务器合约清单 `query_quotes(expired=True)` **只回溯到 2020-09**
#       （v 最早 v2010 / ru 最早 ru2009 / eb 最早 eb2009）⇒ 2016~2020 只能自己构造代码
#     · 构造出来的**不存在**合约，`get_kline_serial` 会**干等 85 秒超时**
#       （实测 DCE.eb1902 = 85 秒）⇒ 全月份 810 个候选最坏要十几小时
#     · CZCE 3 位码 10 年一循环（SR601 = 2016-01 还是 2026-01？）⇒ 构造老代码会
#       **静默取到 2026 年数据**，比报错更危险 ⇒ CZCE 一律只用服务器清单
#   收窄到 1/5/9 后：候选约 231 个，坏码只剩个位数，全程约 7 分钟。
#   ⚠️ 已知局限（写进报告）：1/5/9 格点下「近月」偶尔不是真实最近月，
#      但在中国商品市场这三（或四个）个月份就是主力月，near/far 仍是有效期限结构。

数据起点 = date(2016, 1, 5)      # 日线主连实测边界（与 A/B 对齐）
评估止 = date(2025, 3, 12)       # 干净样本上限（路线图 §7.12.1）
预热天数 = 150                   # 与 A/B 对齐
调仓周期H = 20                   # 每 H 个交易日调一次
每侧只数k = 2                    # 多空各 k 只
滑点跳数 = 2                     # slip_ticks=1 ⇒ 实盘 2 跳（项目口径铁律）
门槛t_ = 3.2031                  # K=169，`工具/_账本/` 汇总

年度范围 = range(16, 26)         # 2016 ~ 2025
# ★ 只取 2016~2025：carry 用的是「最近两个活跃合约」——2025-03 时是 2505 / 2509
#   ⇒ **2026 年的合约根本用不上**（且 ru2601 这类会干等 85 秒超时）。

# 各品种起始年月 ★ 必须避开「上市前」的月份，否则每个坏码要干等 85 秒：
#   EB 苯乙烯 2019-09-26 上市 ⇒ 从 1909 起（eb1901/eb1905 不存在）
#   LH 生猪   2021-01-08 上市，**首个合约是 lh2109** ⇒ 从 2109 起（2101/2105 不存在）
#   （CZCE SR 不走这里，见 全部合约()）
起始年月 = {
    "RU": (16, 1), "EB": (19, 9), "JM": (16, 1), "P": (16, 1), "M": (16, 1),
    "V": (16, 1), "LH": (21, 9), "JD": (16, 1),
}
缓存目录 = os.path.join(_HERE, "_数据")
缓存路径 = os.path.join(缓存目录, "展期_合约日线.csv")


# ============================================================
# 合约代码
# ============================================================
def 合约代码(key, y, m):
    """y = 两位年（16..25）。CZCE 用 3 位码（年个位 + 月）——本工具不实际调用它造 CZCE 码。"""
    ex, code = 交易所[key]
    if ex == "CZCE":
        return "CZCE.%s%d%02d" % (code, y % 10, m)
    return "%s.%s%02d%02d" % (ex, code, y, m)


def 代码月份(sym):
    """从合约码取月份（后两位）。用于筛 CZCE 列表里的月份。"""
    t = "".join(ch for ch in sym.split(".")[1] if ch.isdigit())
    return int(t[-2:]) if len(t) >= 2 else 0


def 全部合约(api=None):
    """返回 {合约代码: 品种key}。

    · DCE / SHFE：4 位码（yyMM）无歧义 ⇒ **自己构造**（服务器清单只回溯到 2020-09）
    · CZCE     ：3 位码 10 年一循环 ⇒ **只用服务器清单**（`expired=True`），
                 宁可少几年数据，也不要静默取到 2026 年的价格
    """
    out = {}
    for key in 品种默认:
        ex, code = 交易所[key]
        if ex == "CZCE":
            lst = list(api.query_quotes(ins_class="FUTURE", exchange_id=ex,
                                        product_id=code, expired=True))
            for s in lst:
                if 代码月份(s) in set(主力月份):
                    out[s] = key
            continue
        y0, m0 = 起始年月.get(key, (16, 1))
        for y in 年度范围:
            if y < y0:
                continue
            for m in 主力月份:
                if y == y0 and m < m0:
                    continue
                out[合约代码(key, y, m)] = key
    return out


def 合约年月(sym, 末日期):
    """★★ 不解析代码 —— 用**合约最后有数据的日期**推断年月。

    CZCE 3 位码 10 年一循环（如 "605" 可能是 2016-05 也可能是 2026-05），
    代码本身有歧义；但拿到手的 K 线一定是**某个具体合约**的，它的最后交易日
    就是到期月 ⇒ 用它反推年月，歧义自动消失。
    """
    return 末日期.year, 末日期.month


def 月数差(ym1, ym2):
    """(Y1,M1) → (Y2,M2) 的月份差，恒 ≥ 0。"""
    return (ym2[0] - ym1[0]) * 12 + (ym2[1] - ym1[1])


# ============================================================
# 品种池参数
# ============================================================
def 读品种(key):
    import importlib.util as ilu
    p = os.path.join(ROOT, "品种池", "%s.py" % key)
    spec = ilu.spec_from_file_location("_sp_%s" % key, p)
    m = ilu.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.品种


def 读K():
    import glob
    import json
    tot = 0
    for f in sorted(glob.glob(os.path.join(_HERE, "_账本", "*.json"))):
        try:
            d = json.load(open(f, encoding="utf-8"))
            tot += int(d.get("累计试验次数") or 0)
        except Exception:
            pass
    return tot


def 门槛t(K):
    return math.sqrt(2 * math.log(K)) if K > 1 else 0.0


def 判决(t, 门):
    if t > 门:
        return "显著为正"
    if t < -门:
        return "显著为负"
    return "与零无法区分"


# ============================================================
# 取数（--- --拉数 用）
# ============================================================
def _存盘(结果, 目标):
    """把 {sym: DataFrame} 合并进缓存 CSV（中途也调用，防卡死白跑）。

    ★★ 硬闸门（2026-09-26 血泪教训）：**片为空时绝不写盘**。
       空帧在这里会「假装成功」：`新` 是 0 行的空表 ⇒
       `旧[~旧["sym"].isin(空)]` 恒为 True ⇒ 旧数据**原样写回**，
       文件看上去没坏、其实一个字节新数据都没进来。
       同一次事故里并发取数返回的 643 个空帧，就是这样被静默吞掉的。
    """
    if not os.path.isdir(缓存目录):
        os.makedirs(缓存目录)
    片 = []
    for s, d in 结果.items():
        dt = (pd.to_datetime(d["datetime"] / 1e9, unit="s", utc=True)
                .dt.tz_convert("Asia/Shanghai").dt.tz_localize(None))
        片.append(pd.DataFrame({
            "key": 目标[s], "sym": s,
            "日期": dt.dt.strftime("%Y-%m-%d").values,
            "open": d["open"].values.astype(float),
            "close": d["close"].values.astype(float),
            "volume": d["volume"].values.astype(float),
            # ★ tqsdk 的 K 线用 close_oi / open_oi（**不是** open_interest，
            #   后者只在 tick 与 quote 里）。`api.py:694`：close_oi = K 线结束时刻的持仓量。
            "oi": d["close_oi"].values.astype(float),
        }))
    新 = pd.concat(片, ignore_index=True) if 片 else pd.DataFrame(
        columns=["key", "sym", "日期", "open", "close", "volume", "oi"])
    if os.path.exists(缓存路径):
        try:
            旧 = pd.read_csv(缓存路径)
            旧 = 旧[~旧["sym"].isin(新["sym"])]
            新 = pd.concat([旧, 新], ignore_index=True)
        except Exception:
            pass
    新.to_csv(缓存路径, index=False, encoding="utf-8-sig")
    return 新


def _缓存规模():
    """(合约数, 行数) —— **写后校验**用，专治「静默写回旧数据」。"""
    d = pd.read_csv(缓存路径)
    return int(d["sym"].nunique()), int(len(d))



def 拉数(每会话=20):
    """★ 主线程串行 + **会话轮换**（2026-09-26 实测，三条血的教训）。

    ▍教训一：并发是假的
        用 `ThreadPoolExecutor` 并发调 `get_kline_serial` 会**静默返回全零帧** ——
        tqsdk 的 `_ensure_serial()` 靠 `wait_update()` 等数据，而 `wait_update()`
        只在主线程有效 ⇒ 子线程拿到「刚创建、还没填」的空 frame。
        实测：并发 12 线程宣称「7 秒拉完 643 个」，**实际一个字节数据都没有**
        （计数还很好看：`d is not None` 对空帧恒真，「零根」被算成了「成功」）。
        冷热不是问题，**线程才是问题**。

    ▍教训二：一次慢请求会**污染整个会话**
        现象：前 N 个合约每个 0.2 秒，随后**所有**合约（含本来正常的）一律 85 秒超时
        （`get_kline_serial` 的 deadline = 25 + 60×合约数）。
        反证：交叉探针里 EB 2022/2023、JM/P/V/M 2016 的合约**单独拉都只要 0.13~0.18 秒**；
        三个独立会话各拉 30 个，**零超时**。
        ⇒ **不是合约慢，是会话被拖坏了。** 对策 = 一旦某次请求超过 3 秒，
          **立刻 `close()` 重开**（重开约 6 秒），不要等批末。

    ▍教训三：脏会话下的「无数据」不可信
        会话被污染后，不存在的合约也会拖满 85 秒才报错（正常时 0 秒就报）。
        所以只有**快速**（<3 秒）判定的「异常 / 0 根」才写进永久跳过名单；
        慢请求只记入本轮重试，避免把好合约永久拉黑。

    ▍三种结果分开处理（上一版最致命的漏算）
      · 有数据    ⇒ 收下
      · 抛异常    ⇒ 合约不存在（graphql `non-existent instrument`）⇒ 跳过名单
      · 返回 0 根 ⇒ 当年没挂牌 / 无数据 ⇒ 跳过名单
    """
    if not os.path.isdir(缓存目录):
        os.makedirs(缓存目录)
    import config_account
    from tqsdk import TqApi, TqAuth

    跳过路径 = os.path.join(缓存目录, "_跳过合约.txt")
    跳过 = set()
    if os.path.exists(跳过路径):
        跳过 = {x.strip() for x in open(跳过路径, encoding="utf-8") if x.strip()}

    USER, PWD = config_account.check()
    t_api = time.time()
    api = TqApi(auth=TqAuth(USER, PWD), web_gui=False)
    print("  [打点] API 就绪 %.1f 秒" % (time.time() - t_api), flush=True)

    目标 = 全部合约(api)
    已有 = set()
    if os.path.exists(缓存路径):
        try:
            已有 = set(pd.read_csv(缓存路径, usecols=["sym"])["sym"].unique())
        except Exception:
            已有 = set()
    清单 = [s for s in 目标 if s not in 已有 and s not in 跳过]
    print("  目标 %d 个 ｜ 已有 %d ｜ 跳过名单 %d ｜ 待拉 %d"
          % (len(目标), len(已有), len(跳过), len(清单)), flush=True)
    if not 清单:
        api.close()
        print("  ⇒ 无新合约可拉（全部已在缓存或跳过名单里）")
        return

    结果, 零根, 异常, 慢, 重试 = {}, [], [], [], []
    t0 = time.time()
    本会话 = 0
    换会话 = 0
    for i, s in enumerate(清单):
        t = time.time()
        有数据 = False
        try:
            ks = api.get_kline_serial(s, 86400, 700)
            d = ks[ks["datetime"] > 0]
            有数据 = len(d) > 0
        except Exception:
            d = None
        dt = time.time() - t
        脏 = dt > 3.0
        本会话 += 1
        if 脏:
            慢.append("%s %.0fs" % (s, dt))
            print("      · 脏 %.0f 秒：%s → 立刻换会话" % (dt, s), flush=True)
        if 有数据:
            结果[s] = d.copy()
        elif 脏:
            重试.append(s)              # 会话脏，判定不可信 ⇒ 本轮不拉黑
        elif d is None:
            异常.append(s)
        else:
            零根.append(s)
        if 脏 or 本会话 >= 每会话 or i + 1 == len(清单):
            if 结果:
                _存盘(结果, 目标)
                结果 = {}
            n, m = _缓存规模()
            换会话 += 1
            print("      [会话%2d] %4d/%4d ｜ 文件 %3d 合约 / %6d 行 ｜ 异常 %2d ｜ "
                  "零根 %2d ｜ 待重试 %2d ｜ %.0f 秒"
                  % (换会话, i + 1, len(清单), n, m, len(异常), len(零根),
                     len(重试), time.time() - t0), flush=True)
            本会话 = 0
            if i + 1 < len(清单):
                api.close()
                api = TqApi(auth=TqAuth(USER, PWD), web_gui=False)
    api.close()

    if 零根 or 异常:
        with open(跳过路径, "w", encoding="utf-8") as f:
            f.write("\n".join(sorted(跳过 | set(零根) | set(异常))))

    n, m = _缓存规模()
    print("  落盘 %s" % 缓存路径)
    print("  ⇒ 本次有数据 %d ｜ 零根 %d ｜ 不存在 %d ｜ 脏会话待重试 %d ｜ 会话数 %d ｜ 用时 %.0f 秒"
          % (len(清单) - len(零根) - len(异常) - len(重试), len(零根), len(异常),
             len(重试), 换会话, time.time() - t0))
    print("  ⇒ **写后校验**：文件 %d 合约 / %d 行" % (n, m))
    if 重试:
        print("  · 待重试（再跑一次 --拉数 即可）：%s"
              % " ".join(x.split(".")[1] for x in 重试[:30]))
    if 异常:
        print("  · 不存在：%s" % " ".join(x.split(".")[1] for x in 异常[:30]))
    if 零根:
        print("  · 零根：%s" % " ".join(x.split(".")[1] for x in 零根[:30]))


# ============================================================
# 面板与信号
# ============================================================
def 建面板(df):
    """{key: {O, C, I, 末}}，索引 = 统一日期轴，列 = 该品种全部合约。"""
    日期轴 = pd.DatetimeIndex(sorted(df["日期"].unique()))
    面板 = {}
    for key in 品种默认:
        sub = df[df["key"] == key]
        if len(sub) == 0:
            continue
        O = sub.pivot(index="日期", columns="sym", values="open").reindex(日期轴)
        C = sub.pivot(index="日期", columns="sym", values="close").reindex(日期轴)
        I = sub.pivot(index="日期", columns="sym", values="oi").reindex(日期轴)
        末 = sub.groupby("sym")["日期"].max()
        面板[key] = {"O": O, "C": C, "I": I, "末": 末}
    return 日期轴, 面板


def 近远与carry(日期轴, P, 最短剩余=20):
    """逐日选「持仓量最大的合约」当近月、次大当远月，算年化展期收益率。

    · 候选 = 当日有收盘价 **且** 距到期 > 最短剩余（自然日）—— 排除交割月
    · near = 持仓量第 1；far = 持仓量第 2 **且月份晚于 near**
    · carry = (P_near − P_far) ÷ P_far × 12 ÷ 月数差

    ⚠️ 全部只用**当日已收盘**数据（持仓量、收盘价），无未来函数。
    """
    C, I, 末 = P["C"], P["I"], P["末"]
    cols = list(C.columns)
    月序 = np.array([末[x].year * 12 + 末[x].month for x in cols])
    末序 = np.array([末[x].toordinal() for x in cols])
    Cv = C.values
    Iv = I.values.copy()
    Iv[np.isnan(Iv)] = -1.0
    日序 = np.array([d.toordinal() for d in 日期轴])
    T = Cv.shape[0]
    near = np.full(T, -1, dtype=int)
    far = np.full(T, -1, dtype=int)
    carry = np.full(T, np.nan)
    md = np.full(T, np.nan)
    for t in range(T):
        有效 = (~np.isnan(Cv[t])) & (末序 - 日序[t] > 最短剩余)
        idx = np.where(有效)[0]
        if len(idx) < 2:
            continue
        idx = idx[np.argsort(-Iv[t, idx], kind="stable")]
        n0 = idx[0]
        near[t] = n0
        f0 = -1
        for k in idx[1:]:
            if 月序[k] > 月序[n0]:
                f0 = k
                break
        if f0 < 0:
            continue
        far[t] = f0
        if Cv[t, f0] > 0:
            d = float(月序[f0] - 月序[n0])
            carry[t] = (Cv[t, n0] - Cv[t, f0]) / Cv[t, f0] * 12.0 / d
            md[t] = d
    return cols, near, far, carry, md


def 连续近月(P, near):
    """持有**近月合约**的隔夜段 / 日内段。

    换月日（near 变了）隔夜记 **0** —— 新合约的 open 与旧合约的 close 不可比，
    差额是换月价差不是盈亏（这正是 B 报告 §一 里「主连换月跳空 +9~31%」的同源问题）。
    日内段是同合约自身 open→close，任何一天都可比。
    """
    O, C = P["O"].values, P["C"].values
    T = C.shape[0]
    ov = np.zeros(T)
    id_ = np.zeros(T)
    for t in range(1, T):
        a = near[t]
        if a < 0:
            continue
        if O[t, a] > 0:
            id_[t] = C[t, a] / O[t, a] - 1.0
        if a == near[t - 1] and C[t - 1, a] > 0:
            ov[t] = O[t, a] / C[t - 1, a] - 1.0
    return ov, id_


# ============================================================
# 口径（与 工具/横截面动量.py 逐字一致，保证两机制可比）
# ============================================================
def 组合收益(权重, ov, id_):
    """乘法精确式：W[i-1] 赚隔夜、W[i] 赚日内（调仓在 i 的开盘完成）。"""
    W前 = np.vstack([np.zeros((1, 权重.shape[1])), 权重[:-1]])
    return (1.0 + (W前 * ov).sum(axis=1)) * (1.0 + (权重 * id_).sum(axis=1)) - 1.0


def 单边费率(价格, 品种):
    元每手 = float(品种["成本"]) + 滑点跳数 * float(品种["一跳"]) * float(品种["乘数"])
    名义 = 价格 * float(品种["乘数"])
    return 元每手 / 名义 if 名义 > 0 else 0.0


# ============================================================
# 回测主循环
# ============================================================
def 跑(日期轴, 面板, keys, 起, 止, 模式="横截面"):
    """模式 ∈ {横截面, 时序}。横截面 = 做多高 carry 前 k 只、做空后 k 只；
    时序 = 每品种按 carry 符号做多/做空（等权）。"""
    n = len(keys)
    全掩 = np.array([d.date() <= 止 for d in 日期轴])
    日期 = 日期轴[全掩]
    T = len(日期)
    日序 = np.array([d.toordinal() for d in 日期])

    # ---- 每品种：近月 / carry / 连续近月收益 ----
    ov = np.zeros((T, n))
    id_ = np.zeros((T, n))
    carryM = np.full((T, n), np.nan)
    月差M = np.full((T, n), np.nan)
    nearM = {}
    价M = np.zeros((T, n))
    诊断 = []
    for j, k in enumerate(keys):
        P = 面板.get(k)
        if P is None:
            诊断.append((k, 0, None, None))
            continue
        cols, near, far, carry, md = 近远与carry(日期轴, P)
        near = near[全掩]
        carry = carry[全掩]
        md = md[全掩]
        Cv = P["C"].reindex(日期轴).values[全掩]
        _ov, _id = 连续近月(P, 近远与carry(日期轴, P)[1])
        ov[:, j] = _ov[全掩]
        id_[:, j] = _id[全掩]
        carryM[:, j] = carry
        月差M[:, j] = md
        nearM[k] = near
        for t in range(T):
            if near[t] >= 0:
                价M[t, j] = Cv[t, near[t]]
        有效 = ~np.isnan(carry)
        诊断.append((k, int(有效.sum()),
                     float(np.nanmean(carry)) if 有效.any() else None,
                     (日期[有效][0].date(), 日期[有效][-1].date()) if 有效.any() else None))

    # ---- 费率矩阵（按当日 near 价格折算；无 near 则用中位价兜底）----
    费率 = np.zeros((T, n))
    for j, k in enumerate(keys):
        p = 读品种(k)
        ref = 价M[:, j].copy()
        好 = ref > 0
        if 好.any():
            ref[~好] = np.median(ref[好])
        else:
            ref[:] = 1.0
        for i in range(T):
            费率[i, j] = 单边费率(ref[i], p)

    # ---- 位置起点（与 A/B 对齐）----
    # ★ carry 只用**当日横截面**（当日各合约的收盘价与持仓量），**不需要任何预热**；
    #   缓存最早一天就是 2016-01-04（天勤日线数据窗口起点）⇒ 位置起 = 1 是正常的。
    #   （A/B 需要 150 根预热是因为动量/均线要回溯，carry 不需要。）
    位置起 = int(np.searchsorted(日期.values, np.datetime64(起)))
    if 位置起 < 1:
        raise SystemExit("评估起点早于数据：%d 根" % 位置起)
    if T - 位置起 < 调仓周期H * 2:
        raise SystemExit("评估区太短：%d 根" % (T - 位置起))

    # ---- 权重（信号用当日收盘；下一根开盘执行）----
    权重 = np.zeros((T, n))
    信号日 = list(range(位置起, T - 1, 调仓周期H))
    记录 = []
    上期 = np.zeros(n)
    for si, sp in enumerate(信号日):
        c = carryM[sp].copy()
        有效 = ~np.isnan(c)
        目标 = np.zeros(n)
        if 有效.sum() >= 2 * 每侧只数k:
            idx = np.where(有效)[0]
            名次 = idx[np.argsort(-c[idx], kind="stable")]
            if 模式 == "横截面":
                for j in 名次[:每侧只数k]:
                    目标[j] = +0.5 / 每侧只数k
                for j in 名次[-每侧只数k:]:
                    目标[j] = -0.5 / 每侧只数k
            else:                       # 时序：按符号，等权
                w = 1.0 / len(idx)
                for j in idx:
                    目标[j] = w if c[j] > 0 else -w
        下一生效 = 信号日[si + 1] + 1 if si + 1 < len(信号日) else T
        权重[sp + 1:下一生效] = 目标
        记录.append({
            "信号日": 日期[sp].strftime("%Y-%m-%d"),
            "生效日": 日期[sp + 1].strftime("%Y-%m-%d"),
            "多头": sorted([keys[j] for j in range(n) if 目标[j] > 0]),
            "空头": sorted([keys[j] for j in range(n) if 目标[j] < 0]),
            "换手": float(np.abs(目标 - 上期).sum()),
            "carry": {keys[j]: (round(float(c[j]), 4) if c[j] == c[j] else None)
                      for j in range(n)},
        })
        上期 = 目标

    成本 = np.zeros(T)
    for sp in 信号日:
        i = min(sp + 1, T - 1)
        前 = 权重[i - 1] if i > 0 else np.zeros(n)
        # 成本按**各品种当日实际可交易的标的**算；换月当日的费率用当日 near 价
        成本[i] = float((np.abs(权重[i] - 前) * 费率[i]).sum())

    毛 = 组合收益(权重, ov, id_)
    净 = np.maximum(毛 - 成本, -0.99)

    m = np.array([d.date() >= 起 for d in 日期])
    净e, 毛e, 成本e, 权重e = 净[m], 毛[m], 成本[m], 权重[m]
    ov_e, id_e = ov[m], id_[m]
    W前 = np.vstack([np.zeros((1, n)), 权重[:-1]])[m]

    权益 = np.cumprod(1.0 + 净e)
    毛权益 = np.cumprod(1.0 + 毛e)
    年数 = (日期[m][-1].date() - 日期[m][0].date()).days / 365.25
    sd = 净e.std(ddof=1)
    夏普 = (净e.mean() / sd) * math.sqrt(250) if sd > 0 else 0.0
    t = 夏普 * math.sqrt(年数)
    峰 = np.maximum.accumulate(权益)
    回撤 = float((权益 / 峰 - 1).min())
    分年 = {}
    年标 = np.array([d.year for d in 日期[m]])
    for y in sorted(set(年标)):
        mm = 年标 == y
        分年[int(y)] = float(np.prod(1 + 净e[mm]) - 1)

    多头腿 = float((np.where(权重e > 0, 权重e, 0) * (ov_e + id_e)).sum())
    空头腿 = float((np.where(权重e < 0, 权重e, 0) * (ov_e + id_e)).sum())

    return {
        "模式": 模式, "年数": 年数, "交易日": int(m.sum()), "调仓次数": len(信号日),
        "年化": 权益[-1] ** (1 / 年数) - 1 if 年数 > 0 else 0.0,
        "毛年化": 毛权益[-1] ** (1 / 年数) - 1 if 年数 > 0 else 0.0,
        "夏普": 夏普, "t": t, "总收益": float(权益[-1] - 1), "回撤": 回撤,
        "成本累计": float(成本e.sum()), "净累计": float(净e.sum()),
        "分年": 分年, "多头腿": 多头腿, "空头腿": 空头腿,
        "记录": 记录, "起": str(日期[m][0].date()), "止": str(日期[m][-1].date()),
        "carryM": carryM[m], "日期": 日期[m], "净": 净e,
        "诊断": 诊断, "ov": ov_e, "id": id_e,
    }


# ============================================================
# 仪器自检（不消耗 K）
# ============================================================
def 自检(日期轴, 面板, keys):
    ok = True
    print("  ① 近月/远月选择：")
    for k in keys:
        P = 面板.get(k)
        if P is None:
            print("     %-4s ⛔ 无数据" % k)
            continue
        cols, near, far, carry, md = 近远与carry(日期轴, P)
        有效 = near >= 0
        有far = far >= 0
        正 = carry[~np.isnan(carry)] > 0
        # 检查 far 月份恒晚于 near
        月序 = np.array([P["末"][x].year * 12 + P["末"][x].month for x in cols])
        错 = 0
        for t in np.where(有far)[0]:
            if 月序[far[t]] <= 月序[near[t]]:
                错 += 1
        print("     %-4s 有近月 %4d 日 ｜ 有远月 %4d 日 ｜ 有carry %4d 日 ｜ "
              "carry>0 占 %.0f%% ｜ 月序错误 %d"
              % (k, int(有效.sum()), int(有far.sum()),
                 int((~np.isnan(carry)).sum()),
                 (正.mean() * 100 if len(正) else 0), 错))
        ok = ok and 错 == 0
    print("  ② 连续近月收益：换月日隔夜必须为 0")
    for k in keys[:3]:
        P = 面板.get(k)
        if P is None:
            continue
        cols, near, far, carry, md = 近远与carry(日期轴, P)
        ov, id_ = 连续近月(P, near)
        换 = np.where((near[1:] != near[:-1]) & (near[1:] >= 0) & (near[:-1] >= 0))[0] + 1
        坏 = int(np.sum(np.abs(ov[换]) > 1e-12)) if len(换) else 0
        print("     %-4s 换月 %3d 次 ｜ 换月日隔夜非零 %d 次  %s"
              % (k, len(换), 坏, "OK" if 坏 == 0 else "**失败**"))
        ok = ok and 坏 == 0
    print("  ③ 全零权重 ⇒ 日收益必须恒为 0")
    n = len(keys)
    T = len(日期轴)
    z = float(abs(组合收益(np.zeros((T, n)), np.zeros((T, n)), np.zeros((T, n)))).max())
    print("     最大值 %.2e  %s" % (z, "OK" if z < 1e-15 else "**失败**"))
    ok = ok and z < 1e-15
    print("  ⇒ 仪器自检：%s" % ("全部通过" if ok else "**未通过 —— 禁止跑实验**"))
    return ok


# ============================================================
# 主流程
# ============================================================
def 打印结果(R, keys, 门):
    n = len(keys)
    print()
    print("=" * 104)
    print(" 【%s】结果  %s ~ %s ｜ %.2f 年 ｜ %d 个交易日 ｜ 调仓 %d 次"
          % (R["模式"], R["起"], R["止"], R["年数"], R["交易日"], R["调仓次数"]))
    print("=" * 104)
    print("  年化收益 %+8.2f%%   （毛利 %+.2f%%）｜ 累计 %+.2f%%"
          % (R["年化"] * 100, R["毛年化"] * 100, R["总收益"] * 100))
    print("  年化夏普 %+8.3f   ｜  t = %+.3f  ｜ 门槛 %.4f  ⇒ **%s**"
          % (R["夏普"], R["t"], 门, 判决(R["t"], 门)))
    print("  最大回撤 %+8.2f%%   ｜ 成本累计 %.2f%%  ｜ 净累计 %+.2f%%"
          % (R["回撤"] * 100, R["成本累计"] * 100, R["净累计"] * 100))
    print("  多空分解：多头腿 %+.2f%%  ｜ 空头腿 %+.2f%%"
          % (R["多头腿"] * 100, R["空头腿"] * 100))
    print()
    print("  分年：")
    for y, v in sorted(R["分年"].items()):
        print("    %d  %+7.2f%%  %s" % (y, v * 100, "█" * int(abs(v) * 200)))
    正年 = sum(1 for v in R["分年"].values() if v > 0)
    print("    正年 %d/%d" % (正年, len(R["分年"])))


def main():
    argv = sys.argv[1:]
    if "--拉数" in argv:
        拉数()
        return

    if not os.path.exists(缓存路径):
        print("  ✗ 无缓存，先跑：python 工具/展期收益.py --拉数")
        return 1

    df = pd.read_csv(缓存路径, parse_dates=["日期"])
    K = 读K()
    门 = 门槛t(K)
    print()
    print("=" * 104)
    print(" 机制 C｜期限结构 / 展期收益 carry   —— 研究级回测器")
    print("=" * 104)
    print("  缓存 %d 行 ｜ %d 个合约 ｜ %d 品种 ｜ 区间 %s ~ %s"
          % (len(df), df["sym"].nunique(), df["key"].nunique(),
             df["日期"].min().date(), df["日期"].max().date()))
    print("  账本 K = %d ｜ 门槛 t = %.4f" % (K, 门))

    for k in 品种默认:
        sub = df[df["key"] == k]
        if len(sub) == 0:
            print("    %-4s ⛔ 无数据" % k)
            continue
        print("    %-4s %3d 合约 ｜ %s ~ %s"
              % (k, sub["sym"].nunique(), sub["日期"].min().date(),
                 sub["日期"].max().date()))

    日期轴, 面板 = 建面板(df)

    if "--自检" in argv:
        print()
        print("=" * 104)
        print(" 仪器自检（量具校准，不消耗 K）")
        print("=" * 104)
        自检(日期轴, 面板, 品种默认)
        return 0

    # ---- carry 分布 ----
    print()
    print("=" * 104)
    print(" 【〇】展期收益率 carry 的分布（%s ~ %s）" % (数据起点, 评估止))
    print("=" * 104)
    print("  %-5s %6s %10s %10s %10s %10s %8s   %s"
          % ("品种", "有carry", "均值", "中位", "P10", "P90", "正占比", "有效区间"))
    for j, k in enumerate(品种默认):
        P = 面板.get(k)
        if P is None:
            continue
        cols, near, far, carry, md = 近远与carry(日期轴, P)
        c = carry[~np.isnan(carry)]
        if len(c) == 0:
            continue
        d = 日期轴[~np.isnan(carry)]
        msk = np.array([x.date() >= 数据起点 and x.date() <= 评估止 for x in d])
        c2 = c[msk]
        print("  %-5s %6d %9.2f%% %9.2f%% %9.2f%% %9.2f%% %7.0f%%   %s ~ %s"
              % (k, len(c2), c2.mean() * 100, np.median(c2) * 100,
                 np.percentile(c2, 10) * 100, np.percentile(c2, 90) * 100,
                 (c2 > 0).mean() * 100, d[msk][0].date(), d[msk][-1].date()))

    # ---- 主口径 ----
    R1 = 跑(日期轴, 面板, 品种默认, 数据起点, 评估止, "横截面")
    打印结果(R1, 品种默认, 门)

    R2 = 跑(日期轴, 面板, 品种默认, 数据起点, 评估止, "时序")
    打印结果(R2, 品种默认, 门)

    # ---- 落盘 ----
    出 = os.path.join(_HERE, "_输出")
    if not os.path.isdir(出):
        os.makedirs(出)
    j = {
        "门槛t": 门, "K": K,
        "横截面": {x: R1[x] for x in ("年化", "毛年化", "夏普", "t", "回撤",
                                     "成本累计", "分年", "多头腿", "空头腿",
                                     "起", "止", "年数", "调仓次数")},
        "时序": {x: R2[x] for x in ("年化", "毛年化", "夏普", "t", "回撤",
                                   "成本累计", "分年", "多头腿", "空头腿",
                                   "起", "止", "年数", "调仓次数")},
        "调仓明细": R1["记录"],
    }
    import json
    with open(os.path.join(出, "_展期收益_结果.json"), "w", encoding="utf-8") as f:
        json.dump(j, f, ensure_ascii=False, indent=1)
    print()
    print("  ⇒ 落盘 %s" % os.path.join(出, "_展期收益_结果.json"))
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
