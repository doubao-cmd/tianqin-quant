# -*- coding: utf-8 -*-
"""截面持仓量因子引擎（候选 A：增仓滞涨）—— 2026-10-09

══════ 本文件严格实现预注册卡，不含任何额外逻辑 ══════
卡片：`工具/_截面因子_持仓量/预注册卡_截面持仓量因子.md`（注册时间 2026-10-09 20:47）
★ **任何参数都不得在此处调整**。要改先改卡、再改代码，并记入卡的变更记录。

══════ 与时序策略的本质区别（这是为什么必须新写，不能复用 MA13）══════
MA13：每品种各自判断 → 8个品种 8 个独立决策
本引擎：**同一根 bar 上横向比较 12 个品种 → 排序 → 取最强的 N 个**
        ⇒ 决策依赖「别人」，所以是截面。

══════ 因子定义（卡内冻结）══════
    oi_chg = close_oi[t] / close_oi[t-LOOKBACK] - 1
    px_chg = close[t]   / close[t-LOOKBACK]   - 1
    顶部信号（做空）：oi_chg > +5%  且 px_chg < +1%
    底部信号（做多）：oi_chg< −5%  且px_chg > −1%
    截面聚合：12 品种各算信号强度，取最强 TOP_N=3
    平仓：反向信号出现，或最长持有 MAX_HOLD=20 根

══════ 关键实现约束（已由 2026-10-09 第0步验证）══════
    ★ open_oi ≡ close_oi（天勤只填收盘持仓量）⇒ 因子只能用 close_oi
    ★ 持仓量变化率无异常跳变（>15% 的 bar = 0 个）⇒ 不需要清洗换月

══════ 禁未来函数（规则 1）══════
    信号用第 t−1 根已收盘 bar 的持仓量/收盘价，成交在第 t 根开盘价。
"""
import numpy as np
import pandas as pd

# ══════════════ 卡内冻结参数（改动需先改卡）══════════════
LOOKBACK   = 20      # 持仓量回看根数
OI_THRESH  = 0.05    # 持仓量变化阈值 ±5%
PX_THRESH  = 0.01    # 价格变化阈值 ±1%
TOP_N      = 3       # 12 品种取信号最强的 3 个
MAX_HOLD   = 20      # 最长持有根数
KSEC       = 1800    # 30 分钟
INIT       = 200000.0
SLIP       = 1# 滑点（跳）
HAND_MIN   = 1
HAND_MAX   = 8
# ══════════════════════════════════════════════════════

SIGNAL_NONE = 0
SIGNAL_LONG = 1     # 底部信号：持仓量减 + 价格跌 ⇒ 底部，做多
SIGNAL_SHORT = -1   # 顶部信号：持仓量增 + 价格滞涨 ⇒ 顶部，做空


def 信号强度(oi_chg, px_chg):
    """返回该品种此刻的信号强度（用于截面排序）。

    强度 = 背离程度 = |持仓量变化| − |价格变化| 的归一化值。
    ⇒ 背离越明显，强度越高；同向变动（健康趋势）强度为负。

    ⚠️ 不用「乘以符号」是为了让多空两腿可比（判定标准第4 条要看两腿分别贡献）。
    """
    背离 = abs(oi_chg) - abs(px_chg)
    if oi_chg > OI_THRESH and px_chg < PX_THRESH:
        return -背离      # 顶部：强度为正（做空），取 Top_N 里的强者
    if oi_chg < -OI_THRESH and px_chg > -PX_THRESH:
        return 背离       # 底部：强度为正（做多）
    return 0.0            # 无信号


def 方向(强度):
    """强度 → 方向。强度为负=顶部(做空)，为正=底部(做多)。"""
    if 强度 > 0:
        return SIGNAL_LONG
    if 强度 < 0:
        return SIGNAL_SHORT
    return SIGNAL_NONE


class 截面引擎:
    """多品种截面引擎（独立模块，不改 lib/portfolio.py）。

    与 portfolio 的区别：portfolio 让每条腿自己出信号；本引擎
    每根 bar 上先汇总全部 12 品种的信号强度，**排序后再决定做哪几个**。
    """

    def __init__(self, 品种列表, 手数函数=None):
        """品种列表: [(中文名, key, MULT, TICK, COMM), ...]"""
        self.腿 = list(品种列表)
        self.手数函数 = 手数函数      # 手数(atr, equity, price) -> int
        # 每条腿的状态
        self.st = {}
        for 名, key, mult, tick, comm in self.腿:
            self.st[key] = {
                "名": 名, "key": key, "mult": mult, "tick": tick, "comm": comm,
                "pos": 0, "手": 0, "entry": 0.0, "bars_in": 0,
                "oi": None, "close": None, "close_arr": None, "oi_arr": None,
            }

    # ---------- 数据装载 ----------
    def 装数据(self, key, oi_arr, close_arr):
        """把某品种的持仓量/收盘价序列塞进来（numpy 数组，长度一致）。"""
        s = self.st[key]
        s["oi_arr"] = np.asarray(oi_arr, float)
        s["close_arr"] = np.asarray(close_arr, float)

    def 就绪(self):
        return all(s["oi_arr"] is not None and len(s["oi_arr"]) > 0
                   for s in self.st.values())

    def 长度(self):
        return min(len(s["oi_arr"]) for s in self.st.values()) if self.腿 else 0

    # ---------- 单品种信号（只用 t-1 及之前的数据）----------
    def 腿信号(self, key, i):
        """第 i 根 bar 收盘后、**基于 i-1 及之前**能算出的信号强度。

        ⚠️禁未来函数：这里返回的是「在 i 根开盘前」已知的强度，
        真正的成交在第 i 根开盘价。
        """
        s = self.st[key]
        oi = s["oi_arr"]; px = s["close_arr"]
        j = i - 1                      # ★ 只用到 i-1
        if j < LOOKBACK or j < 0:
            return 0.0, 0.0, 0.0
        oi_prev = oi[j - LOOKBACK]; px_prev = px[j - LOOKBACK]
        if not np.isfinite(oi_prev) or oi_prev <= 0:
            return 0.0, 0.0, 0.0
        if not np.isfinite(px_prev) or px_prev <= 0:
            return 0.0, 0.0, 0.0
        oi_chg = oi[j] / oi_prev - 1.0
        px_chg = px[j] / px_prev - 1.0
        强 = 信号强度(oi_chg, px_chg)
        return 强, oi_chg, px_chg

    # ---------- 截面：同一 bar 上排序 ----------
    def 截面选(self, i):
        """返回 [(key, 方向, 强度), ...]，取信号最强的 TOP_N 个。"""
        候选 = []
        for 名, key, *_ in self.腿:
            强, oi_chg, px_chg = self.腿信号(key, i)
            d = 方向(强)
            if d != SIGNAL_NONE:
                候选.append((key, d, 强, oi_chg, px_chg))
        if not 候选:
            return []
        # ★ 排序：按 |强度| 从大到小（强弱对比），只留 TOP_N
        候选.sort(key=lambda x: abs(x[2]), reverse=True)
        return 候选[:TOP_N]

    # ---------- 组合级状态机 ----------
    def 走一根(self, i, 开盘价表):
        """在第 i 根 bar 上推进一次。开盘价表: {key: open_[i]}。

        返回本根发生的交易事件列表（供记账与硬校验）。
        """
        事件 = []
        if i < LOOKBACK + 1:
            return 事件

        选 = self.截面选(i)
        选中 = {k: (d, abs(s)) for k, d, s, _, _ in 选}

        # --- 1) 先处理已有持仓：反向信号 或 持仓超期 → 平仓 ---
        for 名, key, *_ in self.腿:
            s = self.st[key]
            if s["pos"] == 0:
                continue
            反向 = False
            if key in 选中 and 选中[key][0] != s["pos"]:
                反向 = True
            if not 反向 and s["bars_in"] >= MAX_HOLD:
                反向 = True
            if 反向:
                px = 开盘价表.get(key)
                if px and np.isfinite(px) and px > 0:
                    pnl = s["pos"] * s["手"] * (px - s["entry"]) * s["mult"]
                    事件.append({
                        "key": key, "名": 名, "act": "平",
                        "dir": s["pos"], "手": s["手"],
                        "entry": s["entry"], "px": px,
                        "pnl": pnl - SLIP * s["tick"] * s["mult"] * s["手"],
                        "bars": s["bars_in"], "i": i,
                    })
                    s["pos"] = 0; s["手"] = 0; s["bars_in"] = 0

        # --- 2) 再开新仓：只开「还没持仓」且被选中的品种 ---
        已持有 = {k for 名, k, *_ in self.腿 if self.st[k]["pos"] != 0}
        for key, d, 强, oi_chg, px_chg in 选:
            if key in 已持有:
                continue
            s = self.st[key]
            px = 开盘价表.get(key)
            if not px or not np.isfinite(px) or px <= 0:
                continue
            手 = self._手数(key, px)
            if 手 < HAND_MIN:
                手 = HAND_MIN
            手 = min(手, HAND_MAX)
            s["pos"] = d; s["手"] = 手; s["entry"] = px; s["bars_in"] = 0
            事件.append({
                "key": key, "名": s["名"], "act": "开",
                "dir": d, "手": 手, "entry": px, "px": px,
                "oi_chg": oi_chg, "px_chg": px_chg, "强": 强, "i": i,
            })

        # --- 3) 持仓计数+1（只对仍在持仓的） ---
        for 名, key, *_ in self.腿:
            if self.st[key]["pos"] != 0:
                self.st[key]["bars_in"] += 1
        return 事件

    def _手数(self, key, price):
        if self.手数函数 is None:
            return HAND_MIN
        return self.手数函数(self.st[key], price)


def 空表(腿):
    """构造一个 {key: open} 的空开盘价表。"""
    return {k: np.nan for 名, k, *_ in 腿}
