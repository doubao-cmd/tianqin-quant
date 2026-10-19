# -*- coding: utf-8 -*-
"""
sizer.py —— 可插拔仓位（手数）模块

设计目标（与 Stage 1 的 signals.py 同源思想）：
  - 把实盘 ma13_live_multi.py 里**散落的 `_hand_of` 闭包**抽成单一真相源，
    让手数算法可配置、可测试、未来可换（如波动率目标 / 等权 / Kelly）。
  - 实盘脚本通过 `ctx["sizer"]` 注入；取不到时用模块级默认兜底，
    保证 `ctx=None` 独立启动行为零变化。

★ 与 live `_hand_of` 口径严格对齐（见 ma13_live_multi.py L275-289）：
    hand = round(equity_base * risk_pct / (atr * tick * mult))，夹 [hand_min, hand_max]
    再叠加名义敞口上限：hand ≤ equity_base * notional_cap / (price * mult)
    - equity_base 锁死 20 万（RISK_EQUITY），**忽略账户实际权益**（2026-10-08 铁律：
      切 TqKq 后账户余额约 100 万，直接用它算会让名义敞口放大 5 倍且与回测脱钩）
    - tick / mult 是品种属性，由调用方传入（s["tick"] / s["mult"]），不在 sizer 内存储
    - ATR 无效（None / NaN / ≤0）时返回 hand_min（与旧逻辑一致）
"""

from __future__ import annotations
import math
from abc import ABC, abstractmethod


def _as_float(x):
    """把 scalar/numpy scalar 安全转 python float；None/NaN/非数 → None。"""
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(f):
        return None
    return f


class Sizer(ABC):
    """仓位计算器抽象基类。

    接口：hand(atr, tick, mult, price=None) -> int（手数，≥ hand_min）
      - atr   : 当前 ATR（标量；无效时返回 hand_min）
      - tick  : 每跳价位（品种属性）
      - mult  : 合约乘数（品种属性）
      - price : 当前最新价（用于名义敞口夹取；可空）
    """

    name = "base"
    hand_min: int = 1

    @abstractmethod
    def hand(self, atr, tick, mult, price=None) -> int:
        raise NotImplementedError


class RiskParSizer(Sizer):
    """风险占比手数（= 旧 live `_hand_of`，逐字复刻）。

    默认参数与 live 常量严格一致：
      RISK_PCT=0.01, RISK_EQUITY=200000, HAND_MIN=1, HAND_MAX=8, NOTIONAL_CAP=2.0
    """

    name = "risk_par"

    def __init__(self, risk_pct: float = 0.01, equity_base: float = 200_000.0,
                 hand_min: int = 1, hand_max: int = 8,
                 notional_cap: float = 2.0):
        self.risk_pct = float(risk_pct)
        self.equity_base = float(equity_base)
        self.hand_min = int(hand_min)
        self.hand_max = int(hand_max)
        self.notional_cap = float(notional_cap)

    def hand(self, atr, tick, mult, price=None) -> int:
        atr_f = _as_float(atr)
        if atr_f is None or atr_f <= 0:
            return self.hand_min
        tick_f = _as_float(tick)
        mult_f = _as_float(mult)
        if tick_f is None or tick_f <= 0 or mult_f is None or mult_f <= 0:
            return self.hand_min

        equity = self.equity_base  # ★ 锁死，不接收外部账户权益
        h = int(round((equity * self.risk_pct) / (atr_f * tick_f * mult_f)))
        h = max(self.hand_min, min(self.hand_max, h))

        price_f = _as_float(price)
        if price_f is not None and price_f > 0:
            名义手数 = int((equity * self.notional_cap) / (price_f * mult_f))
            h = min(h, max(self.hand_min, 名义手数))
        return h


# ---------------- 工厂 ----------------
_REGISTRY = {
    "risk_par": RiskParSizer,
}


def get_sizer(cfg=None) -> Sizer:
    """按配置构造仓位器。cfg 可以是 dict 或 None。

    - cfg=None / {}          → 返回默认 RiskParSizer（与旧 live 行为一致）
    - cfg={"name": "risk_par", ...参数} → 用传入参数构造
    - 未知 name              → **直接抛错**，不静默回退（防止“以为换了其实还是默认”）
    """
    cfg = cfg or {}
    if not isinstance(cfg, dict):
        cfg = {}
    name = (cfg.get("name") or "risk_par").lower()
    if name not in _REGISTRY:
        raise ValueError(
            f"未知 sizer: {name!r}（可选：{sorted(_REGISTRY)}）")
    cls = _REGISTRY[name]
    # 只把该类认识的参数透传，忽略无关键
    known = {"risk_pct", "equity_base", "hand_min", "hand_max", "notional_cap"}
    kwargs = {k: v for k, v in cfg.items() if k in known}
    return cls(**kwargs)
