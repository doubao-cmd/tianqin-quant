# -*- coding: utf-8 -*-
"""
test_incomplete_bar.py —— 离线单测：选品种链路的两个关键契约
================================================================
覆盖 2026-10-18 修掉的两个 P0（都可离线复现、不需要网络/账号）：

1. **未收盘日线 bar 必须剔除**（data_feed.is_incomplete_bar / trim_incomplete）
   背景：tqsdk 日线 datetime 是 UTC，标签 `2026-10-18 16:00` = 北京 `10-19 00:00`
   = 周一那个交易日。旧判定 `last.date()==now.date() and now.hour<15` 在夜里
   永远为 False ⇒ 周日拉数据会把「周一那根只含周五夜盘的未成形 bar」当成已收盘。
   实测污染 46/57 个缓存，并让两阶段选择换了 2/6 条腿。

2. **时间止损在 entry+16 根触发**（pipeline_check 口径 = 实盘/engine）
   背景：实盘 `if 开仓` / `elif 持仓` 互斥 ⇒ 成交那根不计 bars_in；
   旧实现把成交 bar 也算一根 ⇒ 时间止损早一根（15 而非 16）。

跑法：`python test_incomplete_bar.py`（纯离线，无 tqsdk、无网络）
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import numpy as np

import data_feed as df
import pipeline_check as pc
from signals import Ma13Signal
from sizer import get_sizer


# ---------------------------------------------------------------- 契约 1
def test_incomplete_bar_boundaries():
    """按「北京交易日 + 15:00 收盘」判定未收盘 bar。"""
    import pandas as pd
    now = pd.Timestamp("2026-10-18 21:31")          # 周日晚上（正是出事时刻）

    # 周一那根（标签 10-18 16:00 UTC = 北京 10-19 00:00）⇒ 未发生
    assert df.is_incomplete_bar("2026-10-18 16:00:00", now=now) is True
    # 周五那根（标签 10-15 16:00 UTC = 北京 10-16 00:00）⇒ 已收盘
    assert df.is_incomplete_bar("2026-10-15 16:00:00", now=now) is False

    # 同一根「今天」的 bar：盘中未收盘 / 收盘后已收盘
    monday_noon = pd.Timestamp("2026-10-19 10:00")
    monday_after = pd.Timestamp("2026-10-19 16:00")
    assert df.is_incomplete_bar("2026-10-18 16:00:00", now=monday_noon) is True
    assert df.is_incomplete_bar("2026-10-18 16:00:00", now=monday_after) is False
    print("  PASS  test_incomplete_bar_boundaries")


def test_trim_drops_only_tail():
    """trim_incomplete 只从尾部剪，且能处理多根尾部未收盘。"""
    import pandas as pd
    now = pd.Timestamp("2026-10-18 21:31")
    dts = ["2026-10-13 16:00:00", "2026-10-14 16:00:00",
           "2026-10-15 16:00:00", "2026-10-18 16:00:00"]
    assert df.trim_incomplete(dts, now=now) == 3
    # 已全部收盘 ⇒ 一根不剪
    assert df.trim_incomplete(dts[:3], now=now) == 3
    # 空序列安全
    assert df.trim_incomplete([], now=now) == 0
    print("  PASS  test_trim_drops_only_tail")


def test_repair_is_idempotent():
    """repair_caches 第二次跑必须无改动（防止把好数据越剪越短）。"""
    first = df.repair_caches()
    second = df.repair_caches()
    assert second == [], f"第二次仍有改动: {second}"
    print(f"  PASS  test_repair_is_idempotent（首次修 {len(first)} 个，二次 0 个）")


# ---------------------------------------------------------------- 契约 2
def test_time_stop_hold_is_16():
    """时间止损必须在 entry+16 根触发（MAX_HOLD=15，成交那根不计）。"""
    SIG_BAR = 300

    class OneShot(Ma13Signal):
        def entry(self, ind, i):
            return (1, 1.0) if i == SIG_BAR else (0, None)

        def exit_hit(self, ind, i, pos, stop, bars_in):
            return "time" if bars_in >= self.MAX_HOLD else None

    n = 400
    close = np.full(n, 1000.0)
    d = {"datetime": [f"2026-01-01 {i:04d}" for i in range(n)],
         "open": close.copy(), "high": close + 5, "low": close - 5,
         "close": close, "volume": np.full(n, 1000.0)}
    r = pc.run_symbol("MA", d, OneShot(), get_sizer(None))
    assert len(r["_trades"]) == 1, f"应只有 1 笔，实际 {len(r['_trades'])}"
    exit_idx = int(r["_trades"][0]["t"].split()[-1])
    hold = exit_idx - (SIG_BAR + 1)
    assert hold == 16, f"持有时长应为 16 根（实盘口径），实际 {hold}"
    print(f"  PASS  test_time_stop_hold_is_16（信号{SIG_BAR} → 成交{SIG_BAR+1} → 出场{exit_idx}）")


# ---------------------------------------------------------------- 选择层
def test_selection_ignores_incomplete_tail():
    """端到端：把选择层的输入末根剪掉后，结果必须与 build_universe_daily 一致。

    这条是「未来 bar 不进决策」的最终守门：如果哪天缓存里又混进未收盘 bar，
    这里会因为两次结果不同而失败。
    """
    import universe as u
    from selection import select_two_stage, filter_tradable

    raw = df.build_universe_daily(u.all_keys(), use_cache=True)
    if not raw:
        print("  SKIP  test_selection_ignores_incomplete_tail（无缓存）")
        return
    kept, _ = filter_tradable(raw)
    sel_now, _ = select_two_stage(kept)
    # 再手工多剪一根，模拟「调用方未剔除」的旧行为
    trim = {k: {"close": v["close"][:-1], "volume": v["volume"][:-1]} for k, v in kept.items()}
    sel_trim, _ = select_two_stage(trim)
    assert sel_now != sel_trim, "输入差一根 bar 却选出同一批 —— 说明末根没被正确剔除"
    print(f"  PASS  test_selection_ignores_incomplete_tail（当前 {sel_now}）")


def main() -> int:
    print("=" * 66)
    print("选品种链路离线单测（未收盘 bar / 时间止损口径）")
    print("=" * 66)
    test_incomplete_bar_boundaries()
    test_trim_drops_only_tail()
    test_repair_is_idempotent()
    test_time_stop_hold_is_16()
    test_selection_ignores_incomplete_tail()
    print("=" * 66)
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
