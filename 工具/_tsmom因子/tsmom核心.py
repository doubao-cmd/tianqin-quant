# -*- coding: utf-8 -*-
"""
TSMOM 核心（时序动量 · 月度再平衡）—— 预注册卡 `预注册卡_TSMOM.md`
独立小引擎：每品种独立信号（不排名）、月度换仓、波动率倒数加权。
跑因子见 `跑TSMOM.py`；合成数据测试见 `单元测试_TSMOM.py`。
"""
import numpy as np

# ══════════════ 卡内冻结参数（改动需先改卡）══════════════
MOM_LOOKBACK = 252     # 12 个月动量（文献标准）
VOL_WINDOW   = 20      # 波动率窗口（年化用 √243）
REBALANCE    = "monthly"
RISK_BUDGET  = 2.0     # 名义敞口 ≤ 权益 × 2（合计）
HAND_MIN, HAND_MAX = 1, 8
ANNUAL_BARS  = 243.0
# ══════════════════════════════════════════════════════


def 年化波动(close_arr, i):
    """截至 t−1 的 20 日收益年化波动率（只用 ≤ i−1，禁未来函数）。"""
    if i < VOL_WINDOW + 1:
        return np.nan
    seg = close_arr[i - VOL_WINDOW:i]          # 含 i−1，不含 i
    r = np.diff(seg) / seg[:-1]
    if len(r) < 5:
        return np.nan
    return float(np.std(r) * np.sqrt(ANNUAL_BARS))


def 动量(close_arr, i):
    """t−1 收盘的 252 日动量（只用 ≤ i−1）。"""
    if i < MOM_LOOKBACK + 1:
        return np.nan
    return float(close_arr[i - 1] / close_arr[i - 1 - MOM_LOOKBACK] - 1.0)


def 信号(close_arr, i):
    """返回 +1 / −1 / 0（nan 动量 = 0，不加仓）。"""
    m = 动量(close_arr, i)
    if m != m:
        return 0
    return 1 if m > 0 else -1


def 换仓日表(日期列表):
    """每月第一个交易日的索引列表（再平衡纪律：换仓只发生在这里）。
    日期列表元素为 'YYYY-MM-DD' 字符串。"""
    seen = set()
    out = []
    for i, d in enumerate(日期列表):
        月 = str(d)[:7]
        if 月 not in seen:
            seen.add(月)
            out.append(i)
    return out


def 目标手数(mult, price, vol, 权益, 归一分母):
    """波动率倒数加权：名义_i = 预算 × (1/vol_i) / Σ_j(1/vol_j)，夹 [HAND_MIN, HAND_MAX]。
    ★ 2026-10-10 修：第 5 参从「池数」改为「归一分母 Σ_j(1/vol_j)」。
      原实现除以池数 ⇒ 总名义 = 预算 × 均值(1/vol)，vol≈0.2 时 ≈ 10 倍杠杆
      ⇒ 权益 −201% 的直接原因（卡里写的是「归一后乘总预算」）。
    vol 为 nan 时该品种按最小手数（不参与归一分母，调用方负责把它从 Σ 里剔除）。"""
    if not price or price != price or price <= 0:
        return HAND_MIN
    if vol != vol or vol <= 0:
        return HAND_MIN
    if not 归一分母 or 归一分母 != 归一分母 or 归一分母 <= 0:
        return HAND_MIN
    名义 = RISK_BUDGET * 权益 * (1.0 / vol) / 归一分母
    手 = int(名义 / (price * mult))
    return max(HAND_MIN, min(HAND_MAX, 手))
