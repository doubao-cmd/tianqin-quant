# -*- coding: utf-8 -*-
"""
组合运行器：单进程 × 多品种 × 单账户
==========================================================
（2026-09-26 新建，对应研究路线图 §8.4 第三步）

★★ 定稿口径（2026-09-27 用户拍板；本条优先于本文件其它历史注释）★★
    初始资金 20 万 ｜ 每腿 RISK_PCT 1.00% ｜ HAND_MIN 1 / HAND_MAX 25（纯保护阀）
    ｜ 单品种保证金 ≤ 权益 30% ｜ 不夹手数让波动率预算说话
  为什么改（两条实测依据，别退回去）：
    ① 旧口径 40万 × 0.30% = 1,200 元/笔。要算出 8 手需一手风险 ≤ 150 元，
       而全市场最小的是玉米 208 元 ⇒ **所有品种都被下限夹死成固定手数**，
       波动率预算完全失效（这才是 09-23「两个 8 夹死手数」的真正残留形态）。
    ② 「5 万小资金」曾被我当成设计前提，是错的 —— 资金是资源、不是前提。
       定稿按「多手 + 常态预算」设计，资源不足只影响「何时上实盘」。
  20 万 / 1% 下的实际手数（不夹）：c 9.6 ／ rb 5.3 ／ hc 5.1 ／ m 4.8 ／ v 4.5
                                   ／ MA 4.4 ／ SR 3.9 ／ SA 3.2 ／ EB 2.7

【为什么需要它】
`lib/engine.py` 里每个 `run_backtest_*` 都只接**一个品种**，于是：
  · 没有进程知道别的腿 ⇒「当前在场腿数」无处可查；
  · §7.7 定的「最大同时持仓腿数 K = 6」闸门**无法生效**；
  · 9 条腿各跑各的 = 9 份独立单腿回测，**不是组合回测**。
本模块提供**单进程多品种 + 单账户**的回测器，让腿数闸门第一次真正生效，
并产出此前从未算过的**真实腿数时间序列分布**。

【与 engine 的关系：零侵入】
不改 `engine.py` 任何一个函数（它有 10 处调用方），只 import 它的**纯函数**：
    _boll / boll_decision / _atr / _dual_ma_stop_line / _dual_ma_stop_hit
    _risk_budget_hand / _risk_equity_base / _dt_str
成本口径、成交口径、手数公式、止损公式**逐字复用** ⇒ 不引入第二套标准。
单腿模式必须能与 engine 的单品种回测**逐笔对上**（见 工具/组合对照.py）。

【时点纪律（与 engine 一致，无未来函数）】
  · 信号只用**已收盘** K 线（`iloc[-2]`）
  · 下单在下一根开盘成交（TargetPosTask 对价成交）
  · 止损用已收盘那根的 high/low 判定，收盘发单、下一根成交

【腿数闸门 K（§7.7 规格逐条实现）】
  1. 开仓前查「当前在场腿数 n」；n >= K ⇒ 拒绝新开仓，已有仓位**不动**
     （不缩仓、不平仓 —— 那会破坏「单笔风险额 = 0.30%」的一致性）
  2. 拒绝判据只能是腿数，**不得**按行情好坏拒单（那是行情过滤，违反规则 2）
  3. n 的口径 = 账户里**真实持有的腿数**（本实现用目标仓位 `want != 0` 计数）
  4. K 是软的**运营参数**，不是策略参数 ⇒ 调它不需要动任何策略文件

【★ 组合层风险分配（2026-09-27 新增；参数 RISK_TOTAL_PCT）】
  问题（P3 实测，2026-09-26）：9 腿**各拿满** 0.30%、w 不归一 ⇒
    组合年化波动 13.97% ≈ √(9 × 4.5²) = 13.5%（**方差相加**）
    ⇒ 多品种此刻**只是并列，还没变成分散化收益**。
  修法：**组合总预算 F 按腿数上限 K 均分** —— 每腿 = F / K，K 同时是
    腿数闸门的上限 ⇒ Σ(在场腿预算) ≤ F，**总敞口有上界、不随腿数漂移**。
  为什么不用「当前在场腿数 N」切分：N 是事后才知道的（今天 1、明天 3），
    每腿手数会天天变，而手上已握着仓位 ⇒ **执行上做不到**；K 是先验常数。
  ⚠️ 这是**仓位数学，不是 alpha**。`E[组合] = ΣwᵢE[腿ᵢ]` 仍是线性
    ⇒ 它换来的是「**风险可控**」而不是「收益变好」。评估指标要看
    **波动的稳定性**与**回撤**，不要看收益率（看收益率必然误判）。
  ⚠️ 与 §7.7 闸门的分工：闸门管「最多几条腿」，本机制管「每条腿多少钱」；
    两者共用同一个 K ⇒ 不会出现两套口径。

【★ 一个必须写明的实现选择：两阶段结算】
同一根 bar 里可能有多条腿同时要动作（有的要平、有的要开）。若按品种顺序
逐腿「边算边下单」，闸门结果会依赖品种顺序。故拆成两阶段：
    阶段① 逐腿**只做决策**（止损 / 时间止损 / 布林信号），产出待执行清单；
    阶段② 先执行**全部平仓**（释放腿数）→ 再按固定顺序执行**开仓**并查闸门。
两阶段保证：结果与品种顺序无关、可复现。

用法（一般不用直接调，走 组合/ 层入口）：
    from lib.portfolio import 跑组合
    r = 跑组合(["V"], start, end, eval_start, 参数=P, init_balance=400000)
    r = 跑组合(["RU","EB",...], start, end, eval_start, 参数=P, 腿数上限=6)
"""

import os
import re
import sys
import time
from datetime import date as _date

import numpy as np
import pandas as pd

from lib import engine as E
import config_account
import config_symbols
from tqsdk import (
    TqApi, TqAuth, TqBacktest, TqSim, TargetPosTask, BacktestFinished
)

# ============================================================
# 默认参数（组合层会覆盖；这里只是「不给就是它」的兜底）
# ============================================================
参数默认 = {
    "BOLL_N": 20,
    "BOLL_K": 2.0,
    "ATR_PERIOD": 14,
    "ATR_STOP_MULT": 1.0,
    "TIME_STOP_BARS": 20,
    # ★ 定稿口径（2026-09-27 用户拍板）—— 详见文件头注释
    "RISK_PCT": 1.00,        # 每腿单笔风险预算 1%（常规风控档），不是旧口径 0.30%
    "RISK_EQUITY": 0,        # 0 = 按当前账户权益（复利）
    "HAND_MIN": 1,
    "HAND_MAX": 25,          # 纯保护阀：20万/1% 下理论上限 ≈10 手 ⇒ 永不触发
    "MARGIN_CAP_PCT": 30.0,
    "EXIT_OFF": 0.25,
    # ★ 组合层风险分配（2026-09-27 新增）—— 详见文件头「组合层风险分配」段
    #   >0 时生效：每腿实际预算 = RISK_TOTAL_PCT / 腿数上限 K（总预算按腿数切分）
    #   必须与 腿数上限 K 同时给出（总预算需要腿数来切分），否则抛错。
    #   0（默认）= 关闭 ⇒ 每腿仍按 RISK_PCT，**行为与改动前逐字相同**（零回归）。
    "RISK_TOTAL_PCT": 0.0,
}


# ============================================================
# 小工具
# ============================================================
def _字母段(sym):
    """取「品种字母段」（小写）。`DCE.jm2509` 与 `jm2509` 都 -> `jm`。

    两个坑：
      ① 必须取**完整字母段**而不是首字母 —— 否则 'j' 会同时匹配 jm / jd；
      ② ⚠️ tqsdk 的 `Trade.instrument_id` 与 `quote.underlying_symbol` **都不带交易所前缀**
         （实测拿到的就是 `'v2405'`），所以带前缀/不带前缀两种形式都要能处理。
         这一条是 2026-09-26 单腿对照时实测出来的（归因曾因此全表为 0）。
    """
    s = str(sym)
    if "." in s:
        s = s.split(".", 1)[1]
    m = re.match(r"^([A-Za-z]+)", s)
    return (m.group(1) if m else s).lower()


def _日期(x):
    """字符串 / date / datetime 一律转成 date（TqBacktest 要）。"""
    if isinstance(x, str):
        y, m, d = (int(v) for v in x.split("-")[:3])
        return _date(y, m, d)
    if hasattr(x, "date") and not isinstance(x, _date):
        try:
            return x.date()
        except Exception:
            return x
    return x


def _全代码(腿, sym):
    """把不带交易所前缀的合约代码补全：`v2405` -> `DCE.v2405`。

    `sim.get_commission / set_commission` 需要**完整代码**才认；
    `TargetPosTask` 则两种都吃。实测差异会直接表现成费率差 0.5 元/手。
    """
    s = str(sym or "")
    if not s:
        return s
    if "." in s:
        return s
    ex = 腿["main"].split("@")[-1].split(".")[0]
    return "%s.%s" % (ex, s)


def _最小开仓手数(api, sym):
    """读该合约的「最小限价开仓手数」（交易所逐合约下发）。读不到返回 1。

    ★ 为什么要做成**运行时自检**而不是记在文档里（2026-09-27）：
      项目里长期挂着一条「合约最小开仓手数 8 手（DCE.v2605 / CZCE.MA605 实测）」
      并据此把 HAND_MIN 写死成 8。但 2026-09-27 全市场实测（工具/最小开仓手数复核.py，
      936 个在交易合约）：
        · 只有 7 / 87 个品种 >1，且全是冷门品种（JR/PM/RI/WH/ZC/ps/lc）；
        · **DCE 全交易所 = 1**，`CZCE.MA` = 1，`DCE.v` = 1 —— 恰恰包含被点名的那两个；
        · 池内 12 品种**全部 = 1**。
      ⇒ 那条约束不成立，多手方案可落地。但静态字段**没有历史快照**，
        无法排除它在 2026-01 那批合约上真是 8 ⇒ 不能只靠「今天量过」就放心。
      ⇒ 所以改成**每次建仓/换月都读一次**：它今天是什么就以什么为准，
        一旦交易所改规则，代码会**当场报出来**，而不是静默失效。

    ⚠️ 必须读**真实合约**：主连代码（`KQ.m@DCE.v`）上这个字段是 0（未填充），
       要用 `quote.underlying_symbol` 指到的那个合约才读得到（2026-09-27 实测）。

    ⚠️ 用 `query_symbol_info` 而**不用** `get_quote`：前者是批量静态查询，
       无订阅、不需要 `wait_update`；后者在**夜盘收盘后无行情推送时会长时间阻塞**
       （2026-09-27 实测一个会话卡死 3 分钟以上）。
    """
    全 = str(sym or "")
    未补前缀 = "." not in 全
    try:
        if 未补前缀:
            return 1        # 调用方应传 `_全代码(腿, 腿["cur_sym"])`；不猜交易所，安全降级
        d = api.query_symbol_info([全])
        if d is not None and len(d):
            v = d.iloc[0].get("open_min_limit_order_volume", None)
            try:
                v = int(v)
            except Exception:
                v = 1
            return v if v > 1 else 1
    except Exception:
        pass
    return 1


def _读品种池(key):
    """读 品种池/<key>.py，取额外字段（保证金等）。取不到返回 {}。"""
    import importlib.util as ilu
    p = os.path.join(E._ROOT, "品种池", "%s.py" % key)
    if not os.path.exists(p):
        return {}
    try:
        spec = ilu.spec_from_file_location("_品种池_%s" % key, p)
        m = ilu.module_from_spec(spec)
        spec.loader.exec_module(m)
        v = getattr(m, "品种", None)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def 建腿(keys):
    """按 key 列表建立腿清单（只含静态信息，不含运行时状态）。"""
    腿们 = []
    for key in keys:
        name, main, mult, tick = config_symbols.get(key)
        cfg = _读品种池(key)
        腿们.append({
            "key": key, "name": name, "main": main,
            "mult": float(mult), "tick": float(tick),
            "保证金": float(cfg.get("保证金", 0) or 0),
            "字母": None,   # 占位，下面填
        })
    for 腿 in 腿们:
        腿["字母"] = _字母段(腿["main"].split("@")[-1])
    return 腿们


def _新跑状态(腿们):
    """给每条腿装上运行时状态。"""
    for 腿 in 腿们:
        腿.update({
            "want": 0,                       # 目标仓位（手，正=多 负=空）
            "cur_sym": None,                 # 当前真实合约
            "tp": None,                      # TargetPosTask
            "quote": None, "klines": None,
            "stop": {"active": False, "entry": 0.0, "entry_atr": 0.0},
            "bars": 0,                       # 时间止损计数（开仓信号根之后已收盘根数）
            "费率": 0.0,                     # 单边元/手（含滑点层）
            "成本base": None,                # ★ 只取一次（见 _设成本）
            "最小开仓": 1,                   # ★ 交易所最小限价开仓手数（运行时自检）
            "开仓次数": 0, "平仓次数": 0, "拒绝次数": 0,
            "在场bar数": 0,
        })
    return 腿们


# ============================================================
# 指标
# ============================================================
def 算指标(权益曲线, init_balance, 交易日年数=None):
    """从权益曲线算年化 / 夏普 / 回撤。

    夏普自算、Rf=0（不用 tqsdk 的 get_sharp —— 它内含 Rf 2.5%）。
    权益曲线 = [(日期字符串, 权益)]，按时间升序。
    """
    出 = {}
    if not 权益曲线:
        return 出
    日 = [x[0] for x in 权益曲线]
    eq = np.array([float(x[1]) for x in 权益曲线], dtype=float)
    出["起"] = 日[0]
    出["止"] = 日[-1]
    出["bar数"] = int(len(eq))
    出["初始"] = float(init_balance)
    出["终止"] = float(eq[-1])
    出["净利"] = float(eq[-1] - init_balance)
    if init_balance > 0:
        出["净收益率"] = 出["净利"] / init_balance * 100.0

    # 年数：交易日 / 250（与 功效检验.py 同口径）
    年数 = 交易日年数 if 交易日年数 else max(len(eq) - 1, 1) / 250.0
    出["年数"] = float(年数)
    if init_balance > 0 and eq[-1] > 0 and 年数 > 0:
        出["年化收益"] = ((eq[-1] / init_balance) ** (1.0 / 年数) - 1.0) * 100.0

    # 日收益（简单差分；权益曲线的间隔不全是 1 交易日，但我们按序列差分）
    if len(eq) > 2:
        r = np.diff(eq) / np.where(eq[:-1] != 0, eq[:-1], np.nan)
        r = r[np.isfinite(r)]
        if len(r) > 1:
            出["日波动"] = float(np.std(r, ddof=1))
            出["年化波动"] = 出["日波动"] * np.sqrt(250.0) * 100.0
            sd = np.std(r, ddof=1)
            # ⚠️ 必须判 sd > 极小阈值，不能判 > 0：完全线性序列的 sd 是 1e-17 级浮点噪声，
            #    判 > 0 会算出 1e14 这种假夏普（单测时踩到）。
            if sd > 1e-12:
                夏普 = float(np.mean(r) / sd * np.sqrt(250.0))
                出["夏普"] = 夏普
                出["t"] = 夏普 * np.sqrt(年数)

    # 最大回撤（权益曲线口径）
    peak = np.maximum.accumulate(eq)
    dd = np.where(peak > 0, (eq - peak) / peak, 0.0)
    出["最大回撤"] = float(dd.min()) * 100.0
    return 出


def 归因(成交列表, keys):
    """按品种把成交配对算毛利（加权均价法）。返回 {key: {...}}。

    ⚠️ 这里取的是 `instrument_id` —— tqsdk 的 Trade 类**没有 symbol 属性**
    （engine 的 `_trade_to_dict` 取的是 `t.symbol`，那是模拟盘崩溃的那个 bug）。
    """
    映射 = {}
    for key in keys:
        name, main, mult, tick = config_symbols.get(key)
        映射[_字母段(main.split("@")[-1])] = key

    状态 = {k: {"净手数": 0.0, "均价": 0.0, "毛利": 0.0,
                "成交手数": 0, "笔数": 0} for k in keys}
    for t in 成交列表:
        sym = t.get("sym") or t.get("instrument_id") or ""
        key = 映射.get(_字母段(sym))
        if key is None:
            continue
        mult = float(config_symbols.get(key)[2])
        st = 状态[key]
        vol = int(t.get("volume") or 0)
        price = float(t.get("price") or 0)
        if vol <= 0 or price <= 0:
            continue
        signed = vol if str(t.get("direction", "")).upper() == "BUY" else -vol
        st["成交手数"] += vol
        st["笔数"] += 1
        净 = st["净手数"]
        if 净 == 0 or (净 > 0) == (signed > 0):
            # 开仓 / 加仓：更新加权均价
            总 = abs(净) + vol
            st["均价"] = (st["均价"] * abs(净) + price * vol) / 总 if 总 else 0.0
            st["净手数"] = 净 + signed
        else:
            # 平仓：按净手数方向实现盈亏
            平量 = min(vol, abs(净))
            方向 = 1.0 if 净 > 0 else -1.0
            st["毛利"] += (price - st["均价"]) * 平量 * mult * 方向
            st["净手数"] = 净 + signed
            if abs(st["净手数"]) < 1e-9:
                st["净手数"] = 0.0
                st["均价"] = 0.0
            elif (st["净手数"] > 0) != (净 > 0):
                # 反手：剩下的量按新方向、以该笔价重新起算
                st["均价"] = price
    return 状态


# ============================================================
# 主回测
# ============================================================
def 跑组合(keys, start, end, eval_start=None, init_balance=400000,
           kline_sec=86400, 参数=None, 腿数上限=None, slip_ticks=1,
           策略名="布林带", 打印明细=False):
    """单进程多品种回测。

    keys      : 品种 key 列表（顺序即开盘优先顺序，决定闸门的确定性）
    start/end : 回测区间（含预热）
    eval_start: 评估起点；此前只预热、不交易（None = 全程交易）
    腿数上限  : None / <=0 = 不限；>0 = 最大同时在场腿数 K
    参数      : 覆盖 参数默认；其中 RISK_TOTAL_PCT > 0 时启用组合层风险分配
                （每腿预算 = RISK_TOTAL_PCT / 腿数上限 K，必须同时给 K）
    返回 dict：权益曲线 / 腿数序列 / 拒绝记录 / 成交 / 每腿 / 指标 / 对账
    """
    P = dict(参数默认)
    if 参数:
        P.update(参数)
    start, end = _日期(start), _日期(end)

    # ============================================================
    # ★ 组合层风险分配（2026-09-27）：总预算 F 按腿数上限 K 均分给各腿
    # ============================================================
    #   RISK_TOTAL_PCT > 0 ⇒ 每腿预算 = F / K  ⇒  Σ(在场腿预算) ≤ F
    #     ⇒ **总敞口有上界、不随腿数漂移**：腿 1 条时只用 F/K（保守），
    #       腿 K 条时才用满 F（恰好归一，Σw = 1）。
    #   RISK_TOTAL_PCT = 0 ⇒ 每腿预算 = RISK_PCT（旧行为，逐字不变）。
    #
    # 为什么用「先验常数 K」而不是「当前在场腿数 N」切分：
    #   N 是事后才知道的（今天 N=1、明天可能 N=3），每腿手数会天天变，
    #   而手上已握着仓位 —— 执行上做不到。K 是先验常数 ⇒ 可执行，
    #   且与「腿数闸门」共用同一个数，不会出现两套口径。
    #   （完整论证见 组合/布林带__EB_JD_M.py 头部「组合层风险口径」段。）
    #
    # ⚠️ 本机制**只改仓位数学，不改策略**：期望仍是线性的
    #    E[组合] = ΣwᵢE[腿ᵢ] ⇒ 只降方差、不造 alpha。看到波动变小
    #    不要当成策略变好（同 _risk_budget_hand 里那条警告）。
    每腿预算 = float(P["RISK_PCT"])
    if float(P.get("RISK_TOTAL_PCT") or 0) > 0:
        if not 腿数上限 or 腿数上限 <= 0:
            raise ValueError(
                "RISK_TOTAL_PCT=%g > 0 时必须同时给 腿数上限 K > 0"
                "（总预算要靠腿数切分；否则每腿都会拿满总预算 = 敞口放大 K 倍）"
                % float(P["RISK_TOTAL_PCT"]))
        每腿预算 = float(P["RISK_TOTAL_PCT"]) / float(腿数上限)
        组合总预算上限 = float(P["RISK_TOTAL_PCT"])
    else:
        组合总预算上限 = 每腿预算 * float(腿数上限) if (腿数上限 and 腿数上限 > 0) else None

    腿们 = _新跑状态(建腿(keys))
    user, pwd = config_account.check()
    sim = TqSim(init_balance)

    权益曲线 = []
    腿数序列 = []
    拒绝记录 = []
    信号记录 = []

    eval_ts = pd.Timestamp(eval_start) if eval_start else None
    用权式 = ("按当前账户权益（复利）" if not P.get("RISK_EQUITY")
              else "固定 %s 元" % f"{int(P['RISK_EQUITY']):,}")

    api = None
    成交 = []
    try:
        api = TqApi(sim, backtest=TqBacktest(start_dt=start, end_dt=end),
                    auth=TqAuth(user, pwd), web_gui=False)

        # ---- 订阅：9 个主连日线（实测同 tick 全部就绪、时间戳对齐）----
        for 腿 in 腿们:
            腿["klines"] = api.get_kline_serial(
                腿["main"], kline_sec, data_length=int(P["BOLL_N"]) + 200)
            腿["quote"] = api.get_quote(腿["main"])
        api.wait_update(deadline=time.time() + 30)

        # ---- 建立真实合约 + 成本（每品种独立；含换月后重设）----
        for 腿 in 腿们:
            腿["cur_sym"] = 腿["quote"].underlying_symbol
            腿["起始合约"] = 腿["cur_sym"]
            腿["最小开仓"] = _最小开仓手数(api, _全代码(腿, 腿["cur_sym"]))
            if 腿["最小开仓"] > 1:
                print("  ⚠️ %s 最小限价开仓手数 = %d（交易所规则）—— 手数不足将拒绝开仓"
                      % (腿["key"], 腿["最小开仓"]))
            腿["tp"] = TargetPosTask(api, 腿["cur_sym"],
                                     support_open_min_volume=True)
            _设成本(sim, 腿, slip_ticks)

        while True:
            try:
                api.wait_update(deadline=time.time() + 30)
            except BacktestFinished:
                break

            # ---- 换月：主连背后合约变了，先平旧再切新（带超时，不无限等）----
            for 腿 in 腿们:
                _换月(api, sim, 腿, slip_ticks)

            # ---- 本 tick 有哪些腿新收了 bar ----
            有bar = [腿 for 腿 in 腿们
                    if api.is_changing(腿["klines"].iloc[-1], "datetime")]
            if not 有bar:
                continue
            当日 = E._dt_str(腿们[0]["klines"].iloc[-2]["datetime"])

            # ---- 记权益（与 engine 同口径：bar 处理之前、含预热期）----
            try:
                权益曲线.append((当日, float(api.get_account().balance)))
            except Exception:
                pass

            # =========================================================
            # 阶段①：逐腿只做决策，产出待执行清单
            # =========================================================
            待平 = []      # (腿, 原因, 参考价)
            待开 = []      # (腿, target, 原因, atr_v, 参考价 c)

            for 腿 in 有bar:
                if 腿["want"] != 0:
                    腿["在场bar数"] += 1
                kl = 腿["klines"]
                mid_s, up_s, lo_s = E._boll(kl.close, P["BOLL_N"], P["BOLL_K"])
                m = float(mid_s.iloc[-2])
                if m != m:                       # 预热不足（布林带尚未算出）
                    continue
                u = float(up_s.iloc[-2])
                l = float(lo_s.iloc[-2])
                c = float(kl.close.iloc[-2])
                bar_dt = kl.iloc[-2]["datetime"]

                if eval_ts is not None and pd.Timestamp(bar_dt) < eval_ts:
                    continue                     # 预热期不交易

                atr_v = 0.0
                if P["ATR_STOP_MULT"] > 0 or P["RISK_PCT"] > 0:
                    try:
                        atr_v = float(E._atr(kl.high, kl.low, kl.close,
                                             P["ATR_PERIOD"]).iloc[-2])
                    except Exception:
                        atr_v = 0.0

                # ---- ① ATR 止损（优先级最高）----
                if P["ATR_STOP_MULT"] > 0 and 腿["stop"]["active"] and 腿["want"] != 0:
                    d = 1 if 腿["want"] > 0 else -1
                    线 = E._dual_ma_stop_line(腿["stop"]["entry"],
                                              腿["stop"]["entry_atr"], d,
                                              P["ATR_STOP_MULT"])
                    if E._dual_ma_stop_hit(线, d, float(kl.high.iloc[-2]),
                                           float(kl.low.iloc[-2])):
                        待平.append((腿, "ATR止损", float(线)))
                        continue

                # ---- ② 时间止损 ----
                if P["TIME_STOP_BARS"] and 腿["want"] != 0:
                    腿["bars"] += 1
                    if 腿["bars"] >= P["TIME_STOP_BARS"]:
                        待平.append((腿, "时间止损", c))
                        continue

                # ---- ③ 布林决策 ----
                target, reason = E.boll_decision(腿["want"], c, m, u, l, 1)
                if target != 腿["want"]:
                    if target == 0:
                        待平.append((腿, reason, c))
                    else:
                        待开.append((腿, int(target), reason, atr_v, c))

            # =========================================================
            # 阶段②-A：先执行全部平仓（释放腿数）
            # =========================================================
            for 腿, 原因, 参考价 in 待平:
                腿["want"] = 0
                腿["stop"] = {"active": False, "entry": 0.0, "entry_atr": 0.0}
                腿["bars"] = 0
                腿["平仓次数"] += 1
                腿["tp"].set_target_volume(0)
                信号记录.append((当日, 腿["key"], "平仓", 原因, 参考价))
                if 打印明细:
                    print("    %s %-3s 平仓%4s 参考 %.2f" % (当日, 腿["key"], 原因, 参考价))

            # =========================================================
            # 阶段②-B：再执行开仓，查腿数闸门
            # =========================================================
            for 腿, target, 原因, atr_v, c in 待开:
                n现在 = sum(1 for x in 腿们 if x["want"] != 0)
                if 腿数上限 and 腿数上限 > 0 and n现在 >= 腿数上限:
                    腿["拒绝次数"] += 1
                    拒绝记录.append((当日, 腿["key"], 原因, n现在))
                    continue

                hand = int(target)
                if 每腿预算 > 0:
                    hand = E._risk_budget_hand(
                        E._risk_equity_base(P.get("RISK_EQUITY"), init_balance,
                                            api.get_account()),
                        每腿预算, atr_v, P["ATR_STOP_MULT"], 腿["mult"],
                        P["HAND_MIN"], P["HAND_MAX"],
                        腿["保证金"], P["MARGIN_CAP_PCT"])
                if hand <= 0:
                    continue
                # ★ 最小开仓手数自检（2026-09-27）：预算算出的手数 < 交易所最小开仓手数时，
                #   TargetPosTask 会**建了任务却不发单、还判定本轮完成**（静默拒单，
                #   见 tqsdk/lib/target_pos_task.py:154）—— 项目里的「幽灵止损」就是这么来的。
                #   处理：**拒绝开仓并单独记账**，而不是抬高手数 —— 抬到最小手数就等于
                #   悄悄把单笔风险放大到预算之上，那正是我们要消灭的那类 bug。
                if hand < int(腿.get("最小开仓") or 1):
                    腿["拒绝次数"] += 1
                    拒绝记录.append((当日, 腿["key"],
                                  "手数%d<最小开仓%d" % (hand, int(腿["最小开仓"])), n现在))
                    if 打印明细:
                        print("    %s %-3s 拒绝开仓：预算 %d 手 < 交易所最小开仓 %d 手"
                              % (当日, 腿["key"], hand, int(腿["最小开仓"])))
                    continue
                腿["want"] = hand if target > 0 else -hand
                腿["stop"] = {"active": True, "entry": c, "entry_atr": atr_v}
                腿["bars"] = 0
                腿["开仓次数"] += 1
                腿["tp"].set_target_volume(腿["want"])
                信号记录.append((当日, 腿["key"], "开仓", 原因, c))
                if 打印明细:
                    print("    %s %-3s 开仓%4s %d 手 参考 %.2f" % (
                        当日, 腿["key"], 原因, 腿["want"], c))

            # ---- 末端：记录本日收盘后的在场腿数 ----
            n末 = sum(1 for x in 腿们 if x["want"] != 0)
            腿数序列.append((当日, n末))

        # ⚠️ 成交必须在 api.close() **之前**取 —— 关掉会话后 get_trade 就空了。
        #    engine 也是在 BacktestFinished 之后、close 之前调 _collect_trades。
        #    用 instrument_id 而不是 t.symbol（tqsdk 的 Trade 类没有 symbol 属性）。
        try:
            for t in api.get_trade().values():
                成交.append({
                    "sym": getattr(t, "instrument_id", "") or getattr(t, "symbol", ""),
                    "direction": getattr(t, "direction", ""),
                    "offset": getattr(t, "offset", ""),
                    "price": float(getattr(t, "price", 0) or 0),
                    "volume": int(getattr(t, "volume", 0) or 0),
                })
        except Exception:
            pass

    finally:
        if api is not None:
            try:
                api.close()
            except Exception:
                pass

    每腿 = 归因(成交, keys)

    # ---- 总成本 = Σ 各腿成交手数 × 该腿单边费率 ----
    总成本 = 0.0
    for 腿 in 腿们:
        总成本 += 每腿[腿["key"]]["成交手数"] * 腿["费率"]
    已实现毛利 = sum(v["毛利"] for v in 每腿.values())

    m = 算指标(权益曲线, init_balance)
    m["总成本"] = 总成本
    m["已实现毛利"] = 已实现毛利
    m["已实现净利"] = 已实现毛利 - 总成本
    m["成交手数"] = sum(v["成交手数"] for v in 每腿.values())
    m["成交笔数"] = sum(v["笔数"] for v in 每腿.values())
    m["拒绝次数"] = len(拒绝记录)
    m["腿数上限"] = 腿数上限
    m["每腿预算%"] = round(每腿预算, 4)
    m["组合总预算上限%"] = (round(组合总预算上限, 4)
                          if 组合总预算上限 else None)

    # 腿数分布
    分布 = {}
    for _, n in 腿数序列:
        分布[n] = 分布.get(n, 0) + 1
    m["腿数分布"] = dict(sorted(分布.items()))
    m["bar数_腿序"] = len(腿数序列)
    if 腿数序列:
        ns = [n for _, n in 腿数序列]
        m["腿数均值"] = float(np.mean(ns))
        m["腿数最大"] = int(np.max(ns))
        m["零腿占比"] = float(np.mean([1.0 if n == 0 else 0.0 for n in ns])) * 100.0

    return {
        "keys": list(keys), "参数": P, "腿数上限": 腿数上限,
        "权益曲线": 权益曲线, "腿数序列": 腿数序列,
        "拒绝": 拒绝记录, "信号": 信号记录, "成交": 成交,
        "每腿": 每腿,
        "每腿费率": {腿["key"]: 腿["费率"] for 腿 in 腿们},
        "费率明细": {腿["key"]: 腿.get("费率明细", {}) for 腿 in 腿们},
        "起始合约": {腿["key"]: 腿.get("起始合约", "") for 腿 in 腿们},
        "指标": m,
        "口径文本": {
            "手数": "波动率预算 %g%% × %s ｜ 夹[%s,%s] ｜ 保证金上限 %g%% 权益" % (
                每腿预算, 用权式, P["HAND_MIN"],
                "不夹" if not P.get("HAND_MAX") else P["HAND_MAX"],
                P["MARGIN_CAP_PCT"]),
            "组合层": ("总预算 %g%% ÷ 腿数上限 K=%s = 每腿 %g%%（总敞口有上界）"
                     % (P["RISK_TOTAL_PCT"], 腿数上限, 每腿预算)
                     if float(P.get("RISK_TOTAL_PCT") or 0) > 0 else
                     "未启用（每腿独立 %g%%，总敞口随在场腿数漂移）" % 每腿预算),
            "成本": "TqSim 对价成交（价格层 1 跳）＋ slip_ticks=%d（费用层 1 跳）"
                    "→ 等效实盘滑点假设 2 跳/次" % slip_ticks,
        },
    }


def _设成本(sim, 腿, slip_ticks, 重取=False):
    """给一条腿的当前合约设单边费率 = 交易所/配置成本 + 滑点层。

    ⚠️⚠️ 三条实测踩出来的口径（2026-09-26 单腿对照）：
      ① `underlying_symbol` **带交易所前缀**（`DCE.v2401`），而 `Trade.instrument_id`
         **不带**（`v2405`）—— 两套写法，别混。
      ② `sim.get_commission(sym)` **只对「当前主连背后的那个合约」有值**，
         给别的合约（哪怕是它自己未来的月份）一律返回 **nan**。
         ⇒ 拿不到就退回 `config_symbols.COST`（V 是 1.5，交易所实参是 2.0）。
      ③ ⇒ **base 只能取一次**（会话开始时），之后换月**只重设同一个费率**，
         不重取。engine 就是这么做的；我原先每次换月重取，结果换月那一刻
         base 从 2.0 掉回 1.5，费率被静默从 7.0 改成 6.5（18 手差 9 元）。
    """
    slip_money = slip_ticks * 腿["mult"] * 腿["tick"]
    cfg = float(getattr(config_symbols, "COST", {}).get(腿["key"], 0.0) or 0.0)
    全 = _全代码(腿, 腿["cur_sym"])
    # ★ 沿用上次的「来源标记」：本函数每次换月都会被调用（重取=False），
    #   若无条件置 None，会把首次取到的真实交易所费率来源标记抹掉，
    #   最终落盘只剩 `采用` 有值、`交易所` 恒 None（2026-09-26 体检发现）。
    交易所费率 = (腿.get("费率明细") or {}).get("交易所")
    if 腿.get("成本base") is None or 重取:
        base = cfg
        try:
            b = float(sim.get_commission(全))
            if b == b and b > 0:
                base = b
                交易所费率 = b
        except Exception:
            pass
        腿["成本base"] = base
    base = 腿["成本base"]
    腿["费率"] = base + slip_money
    腿["费率明细"] = {"合约": 全, "config": cfg, "交易所": 交易所费率,
                    "采用": base, "滑点": slip_money, "合计": 腿["费率"]}
    try:
        sim.set_commission(全, 腿["费率"])
        sim.set_commission(腿["main"], 腿["费率"])
    except Exception:
        pass
    return 腿["费率"]


def _换月(api, sim, 腿, slip_ticks):
    """主连背后换了合约：平旧 → 切新 → 恢复目标仓位。带超时，不无限等。"""
    q = 腿["quote"]
    try:
        new_sym = q.underlying_symbol
    except Exception:
        return
    if not new_sym or new_sym == 腿["cur_sym"]:
        return
    旧 = 腿["cur_sym"]
    try:
        腿["tp"].set_target_volume(0)
    except Exception:
        pass
    # ⚠️ 等待超时必须短：TqSim 在下一次 wait_update 就撮合，正常 1 次就够。
    #    写成 10 秒 × deadline 1 秒时，一次换月最多白等 10 秒；9 腿 × 2.5 年
    #    × 6 次/年换月 ≈ 135 次 ⇒ 最坏白等 22 分钟（2026-09-26 实测体检极慢的原因之一）。
    t0 = time.time()
    while time.time() - t0 < 2.0:
        try:
            if api.get_position(旧).pos == 0:
                break
        except Exception:
            break
        try:
            api.wait_update(deadline=time.time() + 0.2)
        except BacktestFinished:
            raise
        except Exception:
            break
    腿["cur_sym"] = new_sym
    # ★ 换月后必须重读：最小开仓手数是**逐合约**字段，新合约可能不同
    #   （2026-09-27 实测池内 12 品种全为 1，但别假设它永远不变）
    腿["最小开仓"] = _最小开仓手数(api, _全代码(腿, new_sym))
    if 腿["最小开仓"] > 1:
        print("  ⚠️ %s 换月后最小限价开仓手数 = %d" % (腿["key"], 腿["最小开仓"]))
    腿["tp"] = TargetPosTask(api, new_sym, support_open_min_volume=True)
    腿["tp"].set_target_volume(腿["want"])
    _设成本(sim, 腿, slip_ticks)      # 换月后必须重设，否则按交易所默认费率扣
