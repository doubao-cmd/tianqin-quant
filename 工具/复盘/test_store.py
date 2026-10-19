# -*- coding: utf-8 -*-
"""
test_store.py —— S4 存储层的**真实数据**属性测试（与 store._selftest 互补）

分工：
  · `exec4/store.py --selftest`  → 在**临时目录**里造数据，验逻辑（写盘/幂等/去重/失败标记）
  · 本文件                        → 在**真实 archive** 上验属性（只读，不写盘）

为什么两个都要：
  自检能证明"逻辑对"，但证明不了"我这份真实 archive 是健康的"。
  A7 验收（方案 §9）要求「连续更新 10 次，archive 行数只增不减」——
  这条只能在真实目录上持续验。

跑法：python test_store.py（.venv_viz 即可，不需要 tqsdk）
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(_HERE, "exec4")):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

import store as S


# ---------------------------------------------------------------- A7 核心
def test_archive_monotonic_vs_cache():
    """A7：archive 的行数必须 ≥ 同名 kline2026 缓存的行数。

    理由：archive 是「历次拉取的并集」，缓存只是「本次拉取结果」。
    若 archive 比缓存还少，说明 archive 没记全（或曾被人为重建）⇒ 历史正在丢。
    """
    stats = S.archive_stats()
    if not stats:
        print("  SKIP  test_archive_monotonic_vs_cache（archive 为空，先跑 --seed-archive）")
        return
    bad = []
    for st in stats:
        rp = S.recent_path(st["key"], st["period"])
        if not rp.exists():
            continue
        import pandas as pd
        n_recent = len(pd.read_csv(rp, encoding=S.ENCODING))
        if st["n"] < n_recent:
            bad.append((st["key"], st["period"], st["n"], n_recent))
    assert not bad, f"archive 比缓存还少（历史在丢）：{bad}"
    print(f"  PASS  test_archive_monotonic_vs_cache（{len(stats)} 个文件，全部 ≥ 缓存）")


def test_archive_sorted_and_unique():
    """archive 必须按 datetime 升序且无重复（否则 build/画图会错）。"""
    stats = S.archive_stats()
    if not stats:
        print("  SKIP  test_archive_sorted_and_unique（archive 为空）")
        return
    for st in stats:
        df = S.read_archive(st["key"], st["period"])
        assert df is not None and len(df) > 0
        assert df["datetime"].is_monotonic_increasing, \
            f"{st['key']}/{st['period']} 未按时间升序"
        dup = int(df["datetime"].duplicated().sum())
        assert dup == 0, f"{st['key']}/{st['period']} 有 {dup} 个重复 datetime"
    print(f"  PASS  test_archive_sorted_and_unique（{len(stats)} 个文件）")


def test_archive_has_required_columns():
    """archive 必须含 OHLC（下游 engine/pool/build 都依赖）。"""
    stats = S.archive_stats()
    if not stats:
        print("  SKIP  test_archive_has_required_columns（archive 为空）")
        return
    need = {"datetime", "open", "high", "low", "close"}
    for st in stats:
        df = S.read_archive(st["key"], st["period"])
        miss = need - set(df.columns)
        assert not miss, f"{st['key']}/{st['period']} 缺列 {miss}"
    print(f"  PASS  test_archive_has_required_columns（{len(stats)} 个文件）")


# ---------------------------------------------------------------- 新鲜度
def test_freshness_badge_states():
    """角标的三种状态都要能出现，且「从未更新」必须 stale=True。"""
    b = S.freshness_badge()
    assert set(b) >= {"ok", "hours", "text", "stale"}
    if b["hours"] is None:
        assert b["stale"] is True, "从未更新却没标 stale"
        print(f"  PASS  test_freshness_badge_states（当前：{b['text']}）")
    else:
        assert b["stale"] == (b["hours"] > 24.0)
        print(f"  PASS  test_freshness_badge_states（当前：{b['text']}）")


def test_failure_marker_roundtrip():
    """失败标记写→读→清（用当天日期，测完立刻清掉，不留垃圾）。"""
    import datetime as dt
    today = dt.date.today().isoformat()
    had = S.has_failure(today)
    if had:
        print(f"  SKIP  test_failure_marker_roundtrip（当天已有真实失败标记，不动它）")
        return
    S.write_failure(today, "测试用：模拟失败", attempts=3)
    assert S.has_failure(today)
    txt = S.failure_path(today).read_text(encoding="utf-8")
    assert "重试次数：3" in txt and "测试用" in txt
    assert S.clear_failure(today)
    assert not S.has_failure(today)
    print("  PASS  test_failure_marker_roundtrip")


# ---------------------------------------------------------------- 池层
def test_pool_screen_columns():
    """池层 CSV 的列必须完整（下游 S2/UI 按列名读）。"""
    import datetime as dt
    rows = S.read_pool_screen(dt.date.today().isoformat())
    if not rows:
        print("  SKIP  test_pool_screen_columns（今天还没生成池层表）")
        return
    got = set(rows[0].keys())
    need = set(S.POOL_SCREEN_COLS)
    assert need <= got, f"池层表缺列：{sorted(need - got)}"
    print(f"  PASS  test_pool_screen_columns（{len(rows)} 行 × {len(got)} 列）")


def test_pool_layer_size_within_budget():
    """池层体积必须远小于品种层（方案 §2.4：≈0.23 MB，不能进 HTML）。"""
    kb = S.pool_layer_size_kb()
    assert kb < 2048, f"池层 {kb} KB 太大（目标 <250KB 量级，>2MB 说明写错了东西）"
    print(f"  PASS  test_pool_layer_size_within_budget（{kb} KB）")


def main() -> int:
    print("=" * 74)
    print("S4 存储层 —— 真实 archive 属性测试（只读）")
    print(f"archive：{S.ARCHIVE_DIR}")
    print("=" * 74)
    test_archive_monotonic_vs_cache()
    test_archive_sorted_and_unique()
    test_archive_has_required_columns()
    test_freshness_badge_states()
    test_failure_marker_roundtrip()
    test_pool_screen_columns()
    test_pool_layer_size_within_budget()
    print("=" * 74)
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
