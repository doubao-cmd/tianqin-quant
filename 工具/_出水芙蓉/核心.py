# -*- coding: utf-8 -*-
"""出水芙蓉/断头铡刀 —— 纯函数核心（预注册卡冻结：A1/B1/C1，日频）

卡：工具/_出水芙蓉/预注册卡_出水芙蓉.md（2026-10-10 16:10 用户拍板冻结）
实现口径澄清（写入卡变更记录）：MA / 量均线均用截至 t−1 的窗口（不含当日），
ATR 同 —— 避免大阳线自抬均线/均量导致阈值自失效。
"""
import numpy as np

# ---- 卡内冻结参数（改动需先改卡） ----
MA_FAST, MA_MID, MA_SLOW = 5, 10, 20
VOL_MA_N      = 20     # 量均线窗口
VOL_MULT      = 3.0    # 放量倍数
ATR_N         = 20     # ATR 窗口
BODY_ATR      = 1.5    # 实体 ≥ 1.5×ATR
DONCHIAN_N    = 60     # 前高/前低窗口（不含当日）
STOP_BUF_TICKS = 1    # 止损放在芙蓉K极值 ∓1跳
STOP_CAP_ATR   = 2.0  # 止损夹紧时的兜底宽度
TP_SLIP_TICKS  = 1    # 前高限价止盈成交 −1跳（卡 B1）
SLIP_TICKS     = 1    # 开仓/普通平仓每边滑点（跳）
HAND_MIN, HAND_MAX = 1, 8
RISK_EQUITY = 200000.0  # 锁死 20 万（对齐模拟盘，不复利）
RISK_PCT    = 0.01


def 均线群上下沿(ma5, ma10, ma20):
    """群上沿 = 三者最大；群下沿 = 三者最小。NaN 参与时结果为 NaN（由信号函数判 False）。"""
    return (np.maximum(np.maximum(ma5, ma10), ma20),
            np.minimum(np.minimum(ma5, ma10), ma20))


def 是多头信号(o, c, v, 群上沿, atr, volma):
    """出水芙蓉 5 条件，全部用 t 及以前数据（无未来函数）。任一 NaN → False。"""
    for x in (o, c, 群上沿, atr, volma, v):
        if not np.isfinite(x):
            return False
    return (o <= 群上沿 and c > 群上沿 and c > o
            and (c - o) >= BODY_ATR * atr
            and v >= VOL_MULT * volma)


def 是空头信号(o, c, v, 群下沿, atr, volma):
    """断头铡刀：完全镜像。"""
    for x in (o, c, 群下沿, atr, volma, v):
        if not np.isfinite(x):
            return False
    return (o >= 群下沿 and c < 群下沿 and c < o
            and (o - c) >= BODY_ATR * atr
            and v >= VOL_MULT * volma)


def 原始止损(极值, tick, 方向):
    """多：芙蓉K最低 −1跳；空：芙蓉K最高 +1跳。"""
    return 极值 - 方向 * STOP_BUF_TICKS * tick


def 夹紧止损(止损价, 入场价, atr, tick, 方向):
    """★10-08 铁律：止损必须在入场价不利侧。贴身/有利侧时收紧到 2×ATR。"""
    if 方向 > 0:
        if not (止损价 < 入场价 - tick):
            return 入场价 - STOP_CAP_ATR * atr
    else:
        if not (止损价 > 入场价 + tick):
            return 入场价 + STOP_CAP_ATR * atr
    return 止损价


def 止损成交多(止损线, 开盘):
    """GAP_FILL：跳空低开穿越止损线 → 按开盘（更差）；否则按线。"""
    return min(止损线, 开盘)


def 止损成交空(止损线, 开盘):
    return max(止损线, 开盘)


def 止盈成交多(前高, 开盘, tick):
    """B1 限价挂在前高：开盘已越过 → 按开盘；盘中触及 → 按前高。再 −1跳保守。"""
    return max(前高, 开盘) - TP_SLIP_TICKS * tick


def 止盈成交空(前低, 开盘, tick):
    return min(前低, 开盘) + TP_SLIP_TICKS * tick


def 目标手数(入场价, 止损价, mult, tick):
    """按止损距离定风险：20万×1% ÷ 单手最大亏损，夹[1,8]。"""
    距 = abs(入场价 - 止损价)
    if not np.isfinite(距) or 距 < tick:
        return HAND_MIN
    单手亏 = 距 * mult
    if 单手亏 <= 0:
        return HAND_MAX
    手 = int(RISK_EQUITY * RISK_PCT / 单手亏)
    return max(HAND_MIN, min(HAND_MAX, 手))
