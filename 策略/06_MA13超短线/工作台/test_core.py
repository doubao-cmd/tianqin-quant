# -*- coding: utf-8 -*-
"""
test_core.py —— 工作台纯逻辑离线单测（不连网、不依赖 tqsdk）
运行：python test_core.py
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import universe as _u
from session_gate import SessionGate
from selection import rank_daily, Selection


def test_universe():
    assert len(_u.MAIN_CONTINUOUS) >= 50
    assert len(_u._BY_KEY) == len(_u.MAIN_CONTINUOUS)
    assert _u.seg_of("2026-01-05 09:35:00") == "morning"
    assert _u.seg_of("2026-01-05 14:00:00") == "afternoon"
    assert _u.seg_of("2026-01-05 21:30:00") == "night"
    assert _u.seg_of("2026-01-05 20:00:00") == "closed"
    sid = _u.session_id("2026-01-05 09:35:00")
    assert sid == "2026-01-05_morning"
    # 早→午
    assert _u.next_session_id(sid, "RB") == "2026-01-05_afternoon"
    # 午→夜（有夜盘 RB）
    assert _u.next_session_id("2026-01-05_afternoon", "RB") == "2026-01-05_night"
    # 午→次日早（无夜盘 C）
    assert _u.next_session_id("2026-01-05_afternoon", "C") == "2026-01-06_morning"
    # 夜→次日早
    assert _u.next_session_id("2026-01-05_night", "EB") == "2026-01-06_morning"
    print("test_universe OK")


def test_gate_basic():
    g = SessionGate(k=1.5, n_sessions=5)
    # 训练 5 个小波动 session
    for i in range(5):
        g.on_session_close("RB", f"2026-01-0{i+1}_morning", 0.01 if i % 2 else -0.01)
    atr = g.session_atr("RB")
    assert atr > 0
    # 强趋势 +5% 远超阈值
    g.on_session_close("RB", "2026-01-06_morning", 0.05)
    assert len(g.triggers) == 1
    # 封的是午盘
    assert g.can_open("RB", "2026-01-06_afternoon") is False
    # 次日早恢复
    assert g.can_open("RB", "2026-01-07_morning") is True
    # 弱波动不触发
    g2 = SessionGate(k=1.5, n_sessions=5)
    for i in range(5):
        g2.on_session_close("RB", f"2026-02-0{i+1}_morning", 0.01)
    g2.on_session_close("RB", "2026-02-06_morning", 0.012)  # 接近 atr，不触发
    assert len(g2.triggers) == 0
    print("test_gate_basic OK")


def test_gate_no_future():
    # 闸门只用已收盘 session 决定下一 session，不影响当前
    g = SessionGate(k=1.5, n_sessions=3)
    # 先积累 3 个小波动 session（训练 atr）
    for i in range(3):
        g.on_session_close("RB", f"2026-03-0{i+1}_morning", 0.01 if i % 2 else -0.01)
    # 第 4 个 session 强趋势 +6%
    g.on_session_close("RB", "2026-03-04_morning", 0.06)
    # 当前 morning 仍可开（封的是 afternoon）
    assert g.can_open("RB", "2026-03-04_morning") is True
    assert g.can_open("RB", "2026-03-04_afternoon") is False
    print("test_gate_no_future OK")


def test_rank():
    closes = {}
    for i, k in enumerate(["RB", "MA", "EG", "CU", "AU", "SR"]):
        base = 100 + i
        trend = 0.01 if i < 3 else -0.01
        closes[k] = [base * (1 + 0.002 * j + trend * j) for j in range(25)]
    force = rank_daily(closes, n=20, q=3)
    longs = [k for k, d in force.items() if d == 1]
    shorts = [k for k, d in force.items() if d == -1]
    assert len(longs) == 3 and len(shorts) == 3
    assert set(longs) == {"RB", "MA", "EG"}
    assert set(shorts) == {"CU", "AU", "SR"}
    print("test_rank OK")


def test_selection_timeseries():
    ts = Selection("timeseries", _u.all_keys(), selected=["RB", "MA", "EG"])
    assert set(ts.active_keys()) == {"RB", "MA", "EG"}
    assert ts.force_dir("RB") == 0  # 多空皆可
    print("test_selection_timeseries OK")


def test_selection_cross():
    closes = {}
    for i, k in enumerate(["RB", "MA", "EG", "CU", "AU", "SR"]):
        base = 100 + i
        trend = 0.01 if i < 3 else -0.01
        closes[k] = [base * (1 + 0.002 * j + trend * j) for j in range(25)]
    cs = Selection("cross_sectional", list(closes.keys()), cross_n=20, cross_q=3)
    cs.update_ranking(closes)
    assert cs.force_dir("RB") == 1
    assert cs.force_dir("CU") == -1
    assert cs.force_dir("RB") == 1  # 头部
    print("test_selection_cross OK")


if __name__ == "__main__":
    test_universe()
    test_gate_basic()
    test_gate_no_future()
    test_rank()
    test_selection_timeseries()
    test_selection_cross()
    print("\n全部离线单测通过 ✅")
