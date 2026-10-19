# -*- coding: utf-8 -*-
"""
test_signals.py —— 可插拔 Signal 接口单测（离线，无需 tqsdk）

验证：
  1. 注册表 / get_signal 行为（未知名字抛错，不静默回退）。
  2. Ma13Signal.stop_price 数值用例（含跳空夹紧兜底）。
  3. 不偷看未来：entry(i) 在截掉 i 之后数据的序列上结果不变。
  4. MA13 能识别趋势方向（上涨序列出多头信号、下跌序列出空头信号，且止损在不利侧）。
  5. 可插拔：换一个 Dummy 信号，backtest_signal 调用方代码零改动即跑通。
  6. atr_at 与 indicators 的 atr 数组逐字一致（锁死"单一真相源"，防第三份 ATR）。
  7. 回踩闸门：close 远离 MA 时不触发 entry（锁死 touch 条件）。
  8. 成本模型：滑点+手续费逐笔精确核对；零成本时与旧口径逐字一致（向后兼容）。
"""
from __future__ import annotations
import math
import numpy as np
import pandas as pd

from signals import Signal, Ma13Signal, get_signal, register_signal, backtest_signal


def _df(close, high=None, low=None, open_=None):
    close = np.asarray(close, float)
    n = len(close)
    high = close + 0.5 if high is None else np.asarray(high, float)
    low = close - 0.5 if low is None else np.asarray(low, float)
    open_ = close if open_ is None else np.asarray(open_, float)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close})


def _series_up(n=80):
    x = np.arange(n)
    close = 100 + 0.2 * x + 10 * np.sin(x / 3.0)   # 上行趋势 + 摆动 → ATR 大、touch 易满足
    return _df(close)


def _series_down(n=80):
    x = np.arange(n)
    close = 200 - 0.2 * x - 10 * np.sin(x / 3.0)
    return _df(close)


# ---------------- 1. 注册表 / 工厂 ----------------
def test_registry():
    sig = get_signal("ma13")
    assert isinstance(sig, Ma13Signal), "get_signal('ma13') 应返回 Ma13Signal"
    assert sig.name == "ma13"
    try:
        get_signal("not_exist")
        raise AssertionError("未知信号应抛 KeyError")
    except KeyError:
        pass


# ---------------- 2. stop_price 与实盘逐字一致 ----------------
def test_stop_price_fidelity():
    s = Ma13Signal()
    # 多：entry=100, a=2, swing_low=95 → max(96, 94)=96；96<99 保留 → 96
    assert abs(s.stop_price(1, 100.0, 2.0, 95.0) - 96.0) < 1e-9
    # 多：极端 swing 很高 → 取 entry-2*a 兜底（stop 不能 >= entry-1）
    #   swing=200 => max(96,199)=199 >= 99 => 退回 96
    assert abs(s.stop_price(1, 100.0, 2.0, 200.0) - 96.0) < 1e-9
    # 空：entry=100, a=2, swing_high=105 → min(104,106)=104；104>101 保留 → 104
    assert abs(s.stop_price(-1, 100.0, 2.0, 105.0) - 104.0) < 1e-9
    # 空：极端 swing 很低 → min(104, 99)=99 <=101 => 退回 104
    assert abs(s.stop_price(-1, 100.0, 2.0, 80.0) - 104.0) < 1e-9
    # tick 夹紧：多 entry=100,a=0.1,swing=99.95 → max(99.8,99.9)=99.9 >=99 → 退回99.8
    assert abs(s.stop_price(1, 100.0, 0.1, 99.95) - 99.8) < 1e-9


# ---------------- 3. 不偷看未来 ----------------
def test_no_future_leak():
    s = Ma13Signal()
    df = _series_up(80)
    ind = s.indicators(df)
    n = ind["n"]
    mism = 0
    for i in range(s.MA_N + s.SLOPE_N + 2, n):
        e_full = s.entry(ind, i)
        # 截掉 i 之后的数据，重算
        trunc = df.iloc[: i + 1].reset_index(drop=True)
        ind2 = s.indicators(trunc)
        e_cut = s.entry(ind2, i)
        if e_full != e_cut:
            mism += 1
            # 至少方向/止损不能因为"未来"数据改变
    assert mism == 0, f"entry({i}) 受未来数据影响，mism={mism}"


# ---------------- 4. 趋势方向识别 + 止损在不利侧 ----------------
def _series_osc(n=120):
    x = np.arange(n)
    close = 100 + 12 * np.sin(x / 4.0)   # 无净趋势的大摆动 → MA13 斜率双向翻转
    return _df(close)


def test_direction_detection():
    s = Ma13Signal()
    df = _series_osc()
    ind = s.indicators(df)
    n = ind["n"]
    longs = shorts = 0
    for i in range(s.MA_N + s.SLOPE_N + 2, n):
        d, st = s.entry(ind, i)
        if d == 1:
            longs += 1
            assert st is not None and st < ind["close"][i]   # 多头止损在下方
        elif d == -1:
            shorts += 1
            assert st is not None and st > ind["close"][i]   # 空头止损在上方
    assert longs >= 1, "振荡序列应出现多头信号"
    assert shorts >= 1, "振荡序列应出现空头信号（方向识别双向）"


# ---------------- 5. 可插拔：换 Dummy 信号，调用方零改动 ----------------
class _DummyLong(Signal):
    name = "dummy_long"
    MAX_HOLD = 15

    def indicators(self, df):
        c = np.asarray(df["close"], float)
        return {"close": c, "n": len(c)}

    def entry(self, ind, i):
        if i < 10:
            return 0, None
        return 1, float(ind["close"][i]) - 1.0   # 永远做多，止损低 1 点

    def stop_price(self, dir_trend, entry, a, swing):
        return entry - 1.0

    def atr_at(self, df, i=None):
        return 0.0

    def exit_hit(self, ind, i, pos, stop, bars_in):
        if pos > 0 and ind["close"][i] <= stop:
            return "stop"
        return None


def test_pluggability():
    register_signal("dummy_long", _DummyLong)
    sig = get_signal("dummy_long")
    df = _series_up(80)
    res = backtest_signal(df, sig, hand=1)
    assert res["n_trades"] >= 1, "换 Dummy 信号后 backtest 应产生交易（接口透明）"
    assert isinstance(res, dict) and "equity" in res


def test_backtest_runs_with_ma13():
    s = Ma13Signal()
    res = backtest_signal(_series_up(120), s, hand=1)
    assert isinstance(res, dict) and "n_trades" in res


# ---------------- 6. atr_at 与 indicators 数组逐字一致 ----------------
def test_atr_fidelity():
    s = Ma13Signal()
    # 单调 + 固定振幅：TR 恒为 1.5（high-low=1，|high-prev_c|=1.5）
    x = np.arange(40, dtype=float)
    close = 100.0 + x
    df = _df(close)
    ind = s.indicators(df)
    # atr_at(i) 必须等于 indicators 算出的 atr[i]（单一真相源）
    for i in range(2, len(close)):
        assert abs(s.atr_at(df, i) - ind["atr"][i]) < 1e-9, f"atr_at[{i}] 与 atr 数组不一致"
    # 默认 i=None 取最后一根
    assert abs(s.atr_at(df) - ind["atr"][-1]) < 1e-9
    # 手算最后一点的 ATR = 1.5
    assert abs(ind["atr"][-1] - 1.5) < 1e-9
    # 边界：bar 数不足返回 nan
    assert s.atr_at(_df([1.0, 2.0])) != s.atr_at(_df([1.0, 2.0]))  # nan != nan → True


# ---------------- 7. 回踩闸门：远离 MA 不触发 ----------------
def test_touch_condition():
    s = Ma13Signal()
    # 强上行趋势，但 close 始终大幅偏离 MA（>> RETRACE_TOL*ATR）→ 不应触发
    x = np.arange(80, dtype=float)
    close = 100.0 + 6.0 * x
    df = _df(close)
    ind = s.indicators(df)
    n = ind["n"]
    fired = 0
    for i in range(s.MA_N + s.SLOPE_N + 2, n):
        d, st = s.entry(ind, i)
        if d != 0:
            fired += 1
            # 触发时回踩条件必成立
            a = ind["atr"][i - 1]
            assert abs(ind["close"][i - 1] - ind["ma"][i - 1]) <= s.RETRACE_TOL * a + 1e-9
    assert fired == 0, f"远离 MA 时不应触发 entry，fired={fired}"


# ---------------- 8. 成本模型：滑点 + 手续费逐笔核对 ----------------
class _DummyFee(Signal):
    """确定性信号：i>=5 起永远开多，持 1 根 bar 后时间止损 → 交易链完全可预测。"""
    name = "dummy_fee"
    MAX_HOLD = 15

    def indicators(self, df):
        c = np.asarray(df["close"], float)
        return {"close": c, "n": len(c)}

    def entry(self, ind, i):
        if i < 5 or i >= ind["n"] - 3:   # 尾部不开仓，保证结束时全部平仓、费用严格闭合
            return 0, None
        return 1, float(ind["close"][i]) - 1.0

    def stop_price(self, dir_trend, entry, a, swing):
        return entry - 1.0

    def atr_at(self, df, i=None):
        return 0.0

    def exit_hit(self, ind, i, pos, stop, bars_in):
        if pos > 0 and ind["close"][i] <= stop:
            return "stop"
        if bars_in >= 1:
            return "time"   # 持仓第 2 根 bar 出场（上行序列止损永不触发）
        return None


def test_cost_model():
    df = _series_up(60)
    sig = _DummyFee()
    E0 = backtest_signal(df, sig, hand=1)                          # 零成本
    SLIP, FEE = 0.2, 0.0005
    E1 = backtest_signal(df, sig, hand=1, slippage=SLIP, fee_rate=FEE)
    assert E0["n_trades"] >= 1, "Dummy 应产生交易"
    assert E1["n_trades"] == E0["n_trades"], "成本不应改变交易序列（信号独立于成本）"
    # 零成本向后兼容：total_fees=0，trade 字段无滑点偏移
    assert E0["total_fees"] == 0.0
    # 逐笔核对：pnl_with_cost = gross(零成本) - 2*slip*|pos| - fee*(|entry*pos|+|exit*pos|)
    fees_sum = 0.0
    for t0, t1 in zip(E0["trades"], E1["trades"]):
        pos = 1 if t1["side"] == "多" else -1
        gross0 = (t0["exit"] - t0["entry"]) * pos
        expected = gross0 - 2 * SLIP * abs(pos) \
            - FEE * (abs(t1["entry"] * pos) + abs(t1["exit"] * pos))
        assert abs(t1["pnl"] - expected) < 1e-9, f"bar {t1['i']} 成本核算不符"
        fees_sum += FEE * (abs(t1["entry"] * pos) + abs(t1["exit"] * pos))
    assert abs(E1["total_fees"] - fees_sum) < 1e-9, "total_fees 应等于逐笔手续费之和"
    # 权益恒等式：equity = start + Σ pnl
    assert abs(E1["equity"] - (100_000.0 + sum(t["pnl"] for t in E1["trades"]))) < 1e-6
    # 成本必然拖累：有交易且成本>0 ⇒ 权益严格更低
    assert E1["equity"] < E0["equity"], "滑点+手续费应严格降低权益"


def _run_all():
    tests = [test_registry, test_stop_price_fidelity, test_no_future_leak,
             test_direction_detection, test_pluggability, test_backtest_runs_with_ma13,
             test_atr_fidelity, test_touch_condition, test_cost_model]
    for t in tests:
        t()
        print(f"  PASS  {t.__name__}")
    print(f"\n全部 {len(tests)} 项通过。")


if __name__ == "__main__":
    _run_all()
