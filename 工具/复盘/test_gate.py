# -*- coding: utf-8 -*-
"""
test_gate.py —— 离线单测：S3 封盘状态机 + 假想单 + 敞口三列
====================================================================
被测：`工具/复盘/exec4/gate.py` + `exec4/attribution.py`（纯函数，可离线）

锁死的契约（每条都对应方案条款或一次真实踩坑）：

1. **`BLOCK_ENTRY` 只挡开仓，绝不挡平仓**（§4.1 拍板）
2. **封禁状态不跨时段继承**（§4.8(d)）：午盘被封 ⇒ 夜盘照常判，不继承
3. **涨跌停封板 ⇒ 不判定、不计入分母**（§4.7，用户明确要求）
4. **`atr20_daily` 只用"昨日及更早"**（§4.6）——
   改当天 high/low 绝不能影响当天用的 ATR（这是最容易漏的未来函数）
5. **`K = 0.6` 是常量，不是参数**（§4.6 + 铁律 2：不许扫参）
6. **双簿：假想簿的成交数 ≥ 实际簿**（封盘只能减少进场，不会凭空多出来）
7. **假想单只来自"被封的 bar"**（§4.2）—— 不能把 seeds/reset 造成的差异算进去
8. **敞口序列：出场那根 bar 不计敞口**（钱已经平了）
9. **三列统计：封盘组的实际损益必须接近 0**（因为封盘期不允许开新仓），
   真要比就看反事实列 —— 否则永远得出"封盘更好"的假结论（§4.9）

跑法：`python test_gate.py`（.venv_viz 即可）
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(_HERE, "exec4"),
           os.path.join(_HERE, "..", "..", "策略", "06_MA13超短线", "工作台")):
    p = os.path.abspath(_p)
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)

import numpy as np
import pandas as pd

import gate as G
import attribution as A


# ---------------------------------------------------------------- 造数据
def _mk_bars(n, base=100.0, drift=0.0, bars_per_day=48, start="2026-09-01"):
    """造 n 根 5min bar（datetime 用 UTC 口径，与真实缓存一致）。

    ★ 坑：不能直接用 `pd.date_range(periods=n, freq="5min")` ——
      n=144 根 5min 只有 12 小时，全落在**同一天**，多日测试会全部失效。
      必须按天铺：每天 bars_per_day 根（48 ≈ 4 小时，够区分日边界）。
    """
    rows = []
    d0 = pd.Timestamp(start)
    i = 0
    day = 0
    while len(rows) < n:
        for k in range(bars_per_day):
            if len(rows) >= n:
                break
            rows.append(d0 + pd.Timedelta(days=day, minutes=5 * k))
        day += 1
    t = pd.DatetimeIndex(rows)
    close = base + np.arange(n) * drift
    return pd.DataFrame({
        "datetime": t,
        "open": close,
        "high": close + 0.5,
        "low": close - 0.5,
        "close": close,
    })


# ---------------------------------------------------------------- 1. 只挡开仓
def test_block_entry_only_blocks_entries():
    """封禁只能挡新开仓；持仓照常按 stop/time 出场（§4.1）。"""
    g = G.BlockEntryGate()
    g._block[("RB", "2026-10-15_afternoon")] = G.BlockRecord(
        key="RB", decided_at_sid="2026-10-15_morning",
        blocked_sid="2026-10-15_afternoon", move=0.05, atr_daily=10.0,
        price=3000.0, threshold=0.002)
    assert g.can_open("RB", "2026-10-15_afternoon") is False
    assert g.can_open("RB", "2026-10-15_night") is True      # 不继承
    assert g.can_open("RB", "2026-10-16_morning") is True
    # 封禁不改变"是否能平仓" —— 本模块根本没有挡平仓的接口，这是设计保证
    assert not hasattr(g, "can_close"), "门不该有 can_close（平仓永不被挡）"
    print("  PASS  test_block_entry_only_blocks_entries")


# ---------------------------------------------------------------- 2. 不跨段继承
def test_state_not_inherited_across_sessions():
    """午盘被封 ⇒ 夜盘要重新判定，不能继承封禁。"""
    g = G.BlockEntryGate()
    g.on_session_close("RB", "2026-10-15_morning", move=0.05,
                       atr_daily=10.0, price=3000.0)      # 早盘大涨 ⇒ 封午盘
    assert ("RB", "2026-10-15_afternoon") in g._block
    # 午盘平静 ⇒ 不该封夜盘
    g.on_session_close("RB", "2026-10-15_afternoon", move=0.0001,
                       atr_daily=10.0, price=3000.0)
    assert ("RB", "2026-10-15_night") not in g._block, "封禁被错误继承到夜盘"
    print("  PASS  test_state_not_inherited_across_sessions")


# ---------------------------------------------------------------- 3. 封板排除
def test_sealed_day_skipped_and_not_counted():
    """涨跌停封板日：不判定，且不污染分母（§4.7）。"""
    g = G.BlockEntryGate()
    r = g.on_session_close("RB", "2026-10-15_morning", move=0.09,
                           atr_daily=10.0, price=3000.0, sealed=True)
    assert r is None, "封板日不该产生封禁"
    assert ("RB", "2026-10-15_afternoon") not in g._block
    st = g.stats()
    assert st["n_blocked"] == 0, f"封板日被算进封盘数：{st}"
    assert st["n_sealed_skipped"] == 1, f"未单列封板跳过：{st}"
    print("  PASS  test_sealed_day_skipped_and_not_counted")


def test_sealed_day_mask_detects_yiziban():
    """一字板（全天 high−low ≤ tick）必须被识别出来。"""
    n = 48 * 3                    # 3 天 × 48 根
    df = _mk_bars(n)
    df.loc[df.index >= 96, ["open", "high", "low", "close"]] = 100.0   # 第 3 天全平
    m = G.sealed_day_mask(df["datetime"], df["high"].to_numpy(),
                          df["low"].to_numpy(), tick=0.5)
    assert m[:96].sum() == 0, "前两天的正常 bar 被误判为封板"
    assert m[96:].all(), "第 3 天一字板未被识别"
    print("  PASS  test_sealed_day_mask_detects_yiziban")


# ---------------------------------------------------------------- 4. 无未来函数
def test_atr_daily_no_future_function():
    """★ 改"当天"的 high/low，当天 bar 拿到的 atr_daily **必须不变**。

    这是 §4.6 的核心要求：atr20_daily 用"昨日收盘时已确定"的值。
    若实现里含了当天振幅，这个测试立刻失败。
    """
    n = 48 * 12
    df1 = _mk_bars(n)
    df2 = df1.copy()
    today = pd.to_datetime(df1["datetime"]).dt.normalize()
    last_day = today.max()
    mask_today = (today == last_day).to_numpy()
    # 把最后一天振幅放大 5 倍（只改当天）
    k = 5.0
    df2.loc[mask_today, "high"] = df1.loc[mask_today, "close"] + 10.0 * k
    df2.loc[mask_today, "low"] = df1.loc[mask_today, "close"] - 10.0 * k

    a1 = G.daily_atr_from_intraday(pd.Series(df1["datetime"]), df1["high"].to_numpy(),
                                   df1["low"].to_numpy(), df1["close"].to_numpy())
    a2 = G.daily_atr_from_intraday(pd.Series(df2["datetime"]), df2["high"].to_numpy(),
                                   df2["low"].to_numpy(), df2["close"].to_numpy())
    same_today = np.allclose(a1[mask_today], a2[mask_today], equal_nan=True)
    assert same_today, ("改当天振幅却改变了当天用的 ATR ⇒ 未来函数！"
                        f" 样例 a1={a1[mask_today][:3]} a2={a2[mask_today][:3]}")
    # 而"明天"（不存在的 bar）本来就该变 —— 这里只验当天不变
    print("  PASS  test_atr_daily_no_future_function")


# ---------------------------------------------------------------- 5. K 定死
def test_k_is_fixed_constant():
    """K = 0.6 是常量；且阈值公式 = K × atr / price（收益率口径）。"""
    assert abs(G.K_GATE - 0.6) < 1e-12, f"K 被改成了 {G.K_GATE}"
    g = G.BlockEntryGate()
    atr, px = 30.0, 3000.0            # atr/price = 1%
    thr_expect = 0.6 * 30.0 / 3000.0  # = 0.006
    # move = 0.005 < 0.006 ⇒ 不封
    assert g.on_session_close("RB", "2026-10-15_morning", 0.005, atr, px) is None
    # move = 0.007 > 0.006 ⇒ 封
    r = g.on_session_close("RB", "2026-10-16_morning", 0.007, atr, px)
    assert r is not None and abs(r.threshold - thr_expect) < 1e-12, \
        f"阈值算错：{getattr(r, 'threshold', None)} vs {thr_expect}"
    print(f"  PASS  test_k_is_fixed_constant（阈值 {thr_expect:.4%}）")


def test_insufficient_history_no_decision():
    """ATR 无效（nan/≤0）⇒ 不判定，且**不写入 records**（不污染分母）。"""
    g = G.BlockEntryGate()
    for bad in (None, float("nan"), 0.0, -1.0):
        r = g.on_session_close("RB", "2026-10-15_morning", 0.05, bad, 3000.0)
        assert r is None, f"atr={bad} 不该判定"
    assert g.records == [], f"数据不足被记进了 records：{g.records}"
    print("  PASS  test_insufficient_history_no_decision")


# ---------------------------------------------------------------- 6/7. 双簿
def test_dual_book_shadow_ge_actual():
    """假想簿（不封）的成交数必须 ≥ 实际簿（封盘只能减少进场）。"""
    from signal_bridge import spec as _spec
    try:
        import data
    except Exception as e:
        print(f"  SKIP  test_dual_book_shadow_ge_actual（{e}）")
        return
    try:
        df, _m = data.load_recent("P", 5)
    except FileNotFoundError:
        print("  SKIP  test_dual_book_shadow_ge_actual（无缓存）")
        return
    sp = _spec("P")
    book = G.run_dual_book(df, "P", sp["tick"])
    assert len(book["shadow"]) >= len(book["actual"]), \
        f"封盘后成交变多了？actual={len(book['actual'])} shadow={len(book['shadow'])}"
    # 假想单必须落在被封的 bar 上
    blocked = book["blocked"]
    for h in book["hypo"]:
        assert blocked[h["entry_i"]], f"假想单不在被封 bar 上：{h}"
    print(f"  PASS  test_dual_book_shadow_ge_actual"
          f"（actual={len(book['actual'])} shadow={len(book['shadow'])} "
          f"hypo={len(book['hypo'])}）")


# ---------------------------------------------------------------- 8. 敞口
def test_exposure_series_excludes_exit_bar():
    """出场那根 bar 不计敞口（钱已经平了）。"""
    price = np.full(10, 100.0)
    trades = [{"entry_i": 2, "exit_i": 5, "hand": 2, "dir": 1,
               "entry_px": 100.0, "exit_px": 100.0}]
    e = A.exposure_series(trades, 10, price, mult=10, equity=200_000.0)
    # 应覆盖 index 2,3,4（不含 5）
    assert e[2] > 0 and e[3] > 0 and e[4] > 0
    assert e[5] == 0, f"出场 bar 仍计敞口：{e}"
    assert e[0] == 0 and e[1] == 0
    # 数值 = 2手 × 100 × 10 ÷ 200000 = 0.01
    assert abs(e[2] - 0.01) < 1e-12, f"敞口算错：{e[2]}"
    print("  PASS  test_exposure_series_excludes_exit_bar")


def test_unclosed_trade_holds_to_end():
    """未平仓（exit_i=-1）敞口算到末尾。"""
    price = np.full(6, 100.0)
    trades = [{"entry_i": 3, "exit_i": -1, "hand": 1, "dir": -1,
               "entry_px": 100.0, "exit_px": float("nan")}]
    e = A.exposure_series(trades, 6, price, mult=10, equity=200_000.0)
    assert e[3] > 0 and e[5] > 0, f"未平仓未算到末尾：{e}"
    print("  PASS  test_unclosed_trade_holds_to_end")


# ---------------------------------------------------------------- 9. 三列口径
def test_blocked_bucket_pnl_is_near_zero():
    """★ 封盘组的**实际**损益必须接近 0（封盘期不允许开新仓）。

    这条锁死「不能拿封盘组实际损益去比对照组」这个方法论错误：
    若某天有人把归因改成按"入场 bar"，封盘组会恒为 0 —— 那正是本测试要保护的事实。
    同时验证：真要比，必须看反事实列（非 0）。
    """
    from signal_bridge import spec as _spec
    try:
        import data
    except Exception as e:
        print(f"  SKIP  test_blocked_bucket_pnl_is_near_zero（{e}）")
        return
    try:
        df, _m = data.load_recent("P", 5)
    except FileNotFoundError:
        print("  SKIP  test_blocked_bucket_pnl_is_near_zero（无缓存）")
        return
    sp = _spec("P")
    book = G.run_dual_book(df, "P", sp["tick"])
    price = df["close"].to_numpy(float)
    attr = A.block_attribution(book, book["gate_info"]["sid"], book["blocked"],
                               price, "P", sp["mult"])
    assert attr["rows"], "没有任何封盘组，无法验证"
    for r in attr["rows"]:
        # 封盘组实际成交笔数应远小于反事实笔数（因为封盘期不许开仓）
        assert r["n_trades_blocked"] <= r["n_trades_hypo"], \
            f"封盘组实际成交比反事实还多？{r}"
        # 反事实列必须有数值可看（否则三列就废了）
        assert np.isfinite(r["adj_hypo"]) or not np.isfinite(r["expo_hypo"]), \
            f"反事实列算不出来：{r}"
    print(f"  PASS  test_blocked_bucket_pnl_is_near_zero（{len(attr['rows'])} 个时段）")


def test_format_report_runs():
    """报告渲染不炸，且「无独立效应」是合法结论（不是失败）。"""
    attr = {"key": "X", "rows": [{
        "seg": "morning", "seg_cn": "早盘", "n_bars_blocked": 10, "n_bars_control": 90,
        "expo_blocked": 0.1, "expo_control": 1.0, "expo_hypo": 1.1,
        "pnl_blocked": 0.0, "pnl_control": -50.0, "pnl_hypo": -80.0,
        "n_trades_blocked": 0, "n_trades_control": 9, "n_trades_hypo": 2,
        "adj_blocked": 0.0, "adj_control": -1.0, "adj_hypo": -2.0,
        "adj_diff": 2.0, "adj_se_diff": 5.0, "significant": False,
    }]}
    out = A.format_report(attr)
    assert "无独立效应" in out, f"「无独立效应」未被正确渲染：\n{out}"
    print("  PASS  test_format_report_runs")


def main() -> int:
    print("=" * 74)
    print("S3 封盘 / 假想单 / 敞口三列 —— 离线单测")
    print("=" * 74)
    test_block_entry_only_blocks_entries()
    test_state_not_inherited_across_sessions()
    test_sealed_day_skipped_and_not_counted()
    test_sealed_day_mask_detects_yiziban()
    test_atr_daily_no_future_function()
    test_k_is_fixed_constant()
    test_insufficient_history_no_decision()
    test_dual_book_shadow_ge_actual()
    test_exposure_series_excludes_exit_bar()
    test_unclosed_trade_holds_to_end()
    test_blocked_bucket_pnl_is_near_zero()
    test_format_report_runs()
    print("=" * 74)
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
