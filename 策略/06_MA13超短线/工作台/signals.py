# -*- coding: utf-8 -*-
"""
signals.py —— 可插拔信号接口（工作台核心抽象，Stage 1 + A/B 收尾）

设计目标（用户 2026-10-17）：把"入场信号"从策略里抽出来做成接口，
未来找到正期望因子，只要实现 Signal 接口就能换入，选择/闸门/多腿逻辑不动。

MA13 是当前唯一实现（占位信号），逻辑与 ma13_live_multi.py 的 _process / 旧 _stop_price
**逐字对齐**。★ 单一真相源：ma13_live_multi.py 已改为调用本接口（不再内联副本）。

不偷看未来（铁律 1）：
  - entry(i) 只用已收盘的 bar i（及其之前）的信息，返回的"开仓"在下一根开盘执行；
  - stop 由 close[i] + swing(已收盘极值) 算，绝不使用 i+1 或任何未来数据。
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Optional
import numpy as np
import pandas as pd


class Signal(ABC):
    """信号接口。

    输入 K 线（open/high/low/close，时间升序，最新在末），输出每根 bar 的
    开仓方向 + 止损线；并能在持仓状态下判断该根是否触发出场。
    未来因子实现同接口即可换入。
    """
    name = "base"
    MA_N = SLOPE_N = ATR_N = 0
    RETRACE_TOL = STOP_BUFFER = STOP_CAP = 0.0
    MAX_HOLD = 0

    @abstractmethod
    def indicators(self, df: pd.DataFrame) -> dict:
        """预计算指标数组（ma/atr/...），只用已收盘 bar，无副作用。"""
        ...

    @abstractmethod
    def entry(self, ind: dict, i: int) -> tuple:
        """对刚收盘的 bar i，返回 (direction, stop)：
          direction: +1 开多 / -1 开空 / 0 无信号
          stop: float 止损线（针对"下一根开盘入场"的保护位），无信号为 None
        禁止读 i+1 或任何未来数据。"""
        ...

    @abstractmethod
    def stop_price(self, dir_trend: int, entry: float, a: float, swing: float) -> float:
        """给定入场价/ATR/swing 极值，算止损线（与开仓、恢复补挂逐字一致）。"""
        ...

    @abstractmethod
    def atr_at(self, df: pd.DataFrame, i: int = None) -> float:
        """在 bar i（默认最后一根）处算 ATR，口径与 indicators 完全一致。

        供恢复挂单等"尚无完整 ind"的场景复用，避免再写一份 ATR 公式
        （原 ma13_live_multi._now_atr 即第三份副本，已废弃）。"""
        ...

    @abstractmethod
    def exit_hit(self, ind: dict, i: int, pos: int, stop: float, bars_in: int) -> Optional[str]:
        """持仓 pos(+手/-手) 带止损 stop、已持 bars_in 根，bar i 是否触发出场。

        返回 None（不出场）/ "stop"（盘中扫损触发）/ "time"（时间止损 MAX_HOLD）。
        无未来函数：只用 bar i 的 low/high 与已记录的 stop/bars_in。"""
        ...


class Ma13Signal(Signal):
    """MA13 超短线信号（占位实现）。与 ma13_live_multi._process 逐字对齐。"""
    name = "ma13"
    MA_N, SLOPE_N, ATR_N = 13, 5, 14
    RETRACE_TOL, STOP_BUFFER, STOP_CAP, MAX_HOLD = 0.5, 0.5, 2.0, 15

    def indicators(self, df: pd.DataFrame) -> dict:
        close = np.asarray(df["close"], dtype=float)
        high = np.asarray(df["high"], dtype=float)
        low = np.asarray(df["low"], dtype=float)
        open_ = np.asarray(df["open"], dtype=float)
        n = len(close)
        prev_c = np.roll(close, 1)
        prev_c[0] = close[0]
        tr = np.maximum(high - low,
                        np.maximum(np.abs(high - prev_c), np.abs(low - prev_c)))
        atr = np.array([np.mean(tr[max(1, j - self.ATR_N):j]) if j >= 2 else tr[j]
                        for j in range(n)], dtype=float)
        ma = np.array([np.mean(close[j - self.MA_N:j]) for j in range(self.MA_N, n)], dtype=float)
        ma = np.concatenate([np.full(self.MA_N, np.nan), ma])
        return {"open": open_, "close": close, "high": high, "low": low,
                "atr": atr, "ma": ma, "n": n}

    def entry(self, ind: dict, i: int):
        if i < self.MA_N + self.SLOPE_N + 2:
            return 0, None
        close = ind["close"]
        ma = ind["ma"]
        atr = ind["atr"]
        a = atr[i - 1]
        if not (a == a and a > 0):
            return 0, None
        slope = ma[i - 1] - ma[i - 1 - self.SLOPE_N]
        dir_trend = 1 if slope > 0 else (-1 if slope < 0 else 0)
        touch = abs(close[i - 1] - ma[i - 1]) <= self.RETRACE_TOL * a
        if dir_trend != 0 and touch:
            low = ind["low"]
            high = ind["high"]
            if dir_trend > 0:
                swing = float(np.min(low[max(0, i - self.SLOPE_N):i]))
            else:
                swing = float(np.max(high[max(0, i - self.SLOPE_N):i]))
            stop = self.stop_price(dir_trend, float(close[i]), a, swing)
            return dir_trend, stop
        return 0, None

    def stop_price(self, dir_trend: int, entry: float, a: float, swing: float) -> float:
        tick = 1.0
        if dir_trend > 0:
            stop = max(entry - self.STOP_CAP * a, swing - self.STOP_BUFFER * a)
            if stop >= entry - tick:
                stop = entry - self.STOP_CAP * a
        else:
            stop = min(entry + self.STOP_CAP * a, swing + self.STOP_BUFFER * a)
            if stop <= entry + tick:
                stop = entry + self.STOP_CAP * a
        return stop

    def atr_at(self, df: pd.DataFrame, i: int = None) -> float:
        high = np.asarray(df["high"], dtype=float)
        low = np.asarray(df["low"], dtype=float)
        close = np.asarray(df["close"], dtype=float)
        n = len(close)
        if n < 3:
            return float("nan")
        prev_c = np.roll(close, 1)
        prev_c[0] = close[0]
        tr = np.maximum(high - low,
                        np.maximum(np.abs(high - prev_c), np.abs(low - prev_c)))
        i = (n - 1) if i is None else i
        if i < 2 or i >= n:
            return float("nan")
        return float(np.mean(tr[max(1, i - self.ATR_N):i]))

    def exit_hit(self, ind: dict, i: int, pos: int, stop: float, bars_in: int) -> Optional[str]:
        if pos == 0 or stop is None:
            return None
        if pos > 0 and ind["low"][i] <= stop:
            return "stop"
        if pos < 0 and ind["high"][i] >= stop:
            return "stop"
        if bars_in >= self.MAX_HOLD:
            return "time"
        return None


# ---------- 工厂 & 注册表 ----------
_REGISTRY = {"ma13": Ma13Signal}


def register_signal(name: str, cls) -> None:
    """注册新信号（未来正期望因子在此挂入）。"""
    _REGISTRY[name.lower()] = cls


def get_signal(name: str | None = None, **params) -> Signal:
    """按 entry.signal 名字返回信号实例。

    ★ 未知名字**抛错**而非静默回退到 MA13——避免"以为换了因子其实还是 MA13"
    的隐性 bug（这正是过拟合/误判的常见来源）。
    """
    name = (name or "ma13").lower()
    if name not in _REGISTRY:
        raise KeyError(f"未知信号 '{name}'；已注册：{sorted(_REGISTRY)}")
    return _REGISTRY[name](**params)


# ---------- 通用回测驱动（不依赖 tqsdk，仅管线自检用） ----------
def backtest_signal(df: pd.DataFrame, signal: Signal,
                    start_equity: float = 100_000.0, hand: int = 1,
                    slippage: float = 0.0, fee_rate: float = 0.0) -> dict:
    """极简逐 bar 回测：entry 在信号 bar 收盘成交（近似下一根开盘），
    exit 的"扫损/时间止损"判定**统一交给 signal.exit_hit**（无未来函数）。

    成本模型（2026-10-17 补，Tier 2）：
      - slippage：每边滑点（价格点）。多头买贵卖便宜、空头反过来，双边各收一次。
      - fee_rate ：单边手续费率（占名义价值比例，name义 = 价格×手数，乘数按 1）。
        开、平各收一次。fees 全额从权益扣除。
      - 默认均为 0 ⇒ 结果与旧版（不计费）逐字一致，向后兼容。
    ⚠️ 这是工程自检驱动，不是真实回测引擎；真实 P&L 由正式回测/模拟盘负责。
    但**任何因子是否"正期望"的初筛必须带成本**，否则结论系统性偏乐观。
    """
    ind = signal.indicators(df)
    n = ind["n"]
    equity = float(start_equity)
    pos = 0
    entry = 0.0        # 含滑点的实际入场价
    stop = None
    bars_in = 0
    trades = []
    total_fees = 0.0
    entry_fee = 0.0
    for i in range(1, n):
        if pos != 0:
            why = signal.exit_hit(ind, i, pos, stop, bars_in)
            if why is not None:
                px = stop if why == "stop" else float(ind["close"][i])
                exit_fill = px - slippage * (1 if pos > 0 else -1)  # 不利侧滑点
                gross = (exit_fill - entry) * pos
                fee_out = fee_rate * abs(exit_fill * pos)
                total_fees += fee_out
                pnl = gross - fee_out - entry_fee   # 该笔净 pnl 含双边手续费
                equity += pnl
                trades.append({"i": i, "side": "多" if pos > 0 else "空",
                               "entry": entry, "exit": exit_fill, "pnl": pnl,
                               "why": why})
                pos = 0
                stop = None
                bars_in = 0
            else:
                bars_in += 1
        if pos == 0:
            direction, s = signal.entry(ind, i - 1)  # 用上一根收盘算信号
            if direction != 0 and s is not None:
                pos = hand if direction > 0 else -hand
                entry = float(ind["close"][i - 1]) + slippage * (1 if pos > 0 else -1)
                entry_fee = fee_rate * abs(entry * pos)
                total_fees += entry_fee
                # entry_fee 不在此处扣权益：统一在平仓时结转进该笔 pnl（避免双扣）
                stop = s
                bars_in = 0
    return {"equity": equity, "trades": trades, "n_trades": len(trades),
            "total_fees": total_fees}
