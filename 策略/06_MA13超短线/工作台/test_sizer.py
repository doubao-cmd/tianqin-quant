# -*- coding: utf-8 -*-
"""
test_sizer.py —— 可插拔仓位器单测（离线，无需 tqsdk）

验证：
  1. 与旧 live `_hand_of` 口径**逐字等价**（独立参考实现随机比对，锁死单一真相源）。
  2. ATR 无效（None/NaN/≤0）兜底返回 hand_min。
  3. 名义敞口夹取生效（低价使名义手数 < 风险手数时取名义手数）。
  4. 工厂：未知名字**抛错**，不静默回退。
  5. 闭包转发：live 风格 `hand(atr, equity, price)` 忽略 equity、正确委托 sizer。
"""

import math
import sizer as _sz_mod
from sizer import RiskParSizer, get_sizer


# ---- 旧 live `_hand_of` 口径的独立参考实现（用于等价比对） ----
def _ref_hand_of(atr, tick, mult, price,
                 risk_pct=0.01, equity=200000.0,
                 hand_min=1, hand_max=8, notional_cap=2.0):
    if atr is None or (isinstance(atr, float) and math.isnan(atr)) or (atr is not None and atr <= 0):
        return hand_min
    h = int(round((equity * risk_pct) / (atr * tick * mult)))
    h = max(hand_min, min(hand_max, h))
    if price and price > 0 and not (isinstance(price, float) and math.isnan(price)):
        名义手数 = int((equity * notional_cap) / (price * mult))
        h = min(h, max(hand_min, 名义手数))
    return h


def test_equivalence_with_legacy():
    sz = RiskParSizer()  # 默认参数 = 旧 live 常量
    rng = __import__("random").Random(42)
    mism = 0
    for _ in range(2000):
        atr = rng.uniform(1.0, 200.0)
        tick = rng.choice([0.5, 1.0, 2.0, 5.0, 10.0])
        mult = rng.choice([5, 10, 20, 50, 100])
        # price：有时为 None，有时偏离 atr 量级
        price = rng.choice([None, rng.uniform(500, 8000)])
        got = sz.hand(atr, tick, mult, price)
        exp = _ref_hand_of(atr, tick, mult, price)
        if got != exp:
            mism += 1
            if mism <= 3:
                print(f"  mismatch atr={atr} tick={tick} mult={mult} "
                      f"price={price} got={got} exp={exp}")
    assert mism == 0, f"与旧口径不一致 {mism}/2000"


def test_atr_invalid_fallback():
    sz = RiskParSizer()
    for bad in (None, 0.0, -5.0, float("nan")):
        assert sz.hand(bad, 1.0, 10, 1000.0) == 1, f"ATR 无效应返回 hand_min, got={bad}"
    # tick/mult 非法也应兜底
    assert sz.hand(10.0, 0.0, 10, 1000.0) == 1
    assert sz.hand(10.0, 1.0, 0, 1000.0) == 1


def test_notional_cap_clamps():
    # 构造：风险手数很大，但低价使名义手数很小 → 取名义手数
    sz = RiskParSizer(equity_base=200000.0, risk_pct=0.01,
                      hand_min=1, hand_max=8, notional_cap=2.0)
    # atr 极小 → 风险手数远超上限；price 极低 → 名义手数小
    atr, tick, mult, price = 0.5, 1.0, 10, 50.0
    got = sz.hand(atr, tick, mult, price)
    risk_h = int(round((200000 * 0.01) / (atr * tick * mult)))   # = 400 → 被夹到 8
    notional_h = int((200000 * 2.0) / (price * mult))            # = 800
    # 这里风险手数已被 hand_max 夹到 8，名义手数 800 不触发；换更低 price 验证夹取
    price2 = 20000.0
    notional_h2 = int((200000 * 2.0) / (price2 * mult))          # = 2
    got2 = sz.hand(atr, tick, mult, price2)
    assert got2 == max(1, min(8, notional_h2)), f"名义夹取应生效, got={got2}"
    assert got2 < 8, "低价时名义手数应小于 hand_max"


def test_factory_unknown_raises():
    try:
        get_sizer({"name": "does_not_exist"})
        assert False, "未知 sizer 应抛错"
    except ValueError as e:
        assert "does_not_exist" in str(e)
    # 默认/None 不抛，返回 RiskParSizer
    assert isinstance(get_sizer(None), RiskParSizer)
    assert isinstance(get_sizer({}), RiskParSizer)
    assert isinstance(get_sizer({"name": "risk_par"}), RiskParSizer)


def test_live_closure_forwarding():
    """模拟 live 的 `_hand_of` 闭包：签名 (atr, equity, price)，忽略 equity。"""
    sz = RiskParSizer()
    s = {"tick": 1.0, "mult": 10}
    f = lambda atr, equity=None, price=None: sz.hand(atr, s["tick"], s["mult"], price)
    # equity 即使传 999999 也应被忽略（锁死 20万）
    a, p = 15.0, 3000.0
    assert f(a, 999999, p) == sz.hand(a, 1.0, 10, p)
    assert f(a, None, p) == _ref_hand_of(a, 1.0, 10, p)
    # ATR 无效时仍返回 1
    assert f(None, 999999, p) == 1


def _run_all():
    tests = [test_equivalence_with_legacy, test_atr_invalid_fallback,
             test_notional_cap_clamps, test_factory_unknown_raises,
             test_live_closure_forwarding]
    for t in tests:
        t()
        print(f"  PASS  {t.__name__}")
    print(f"\n全部 {len(tests)} 项通过。")


if __name__ == "__main__":
    _run_all()
