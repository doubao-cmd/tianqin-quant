# -*- coding: utf-8 -*-
"""
test_selection.py —— 卡 C 两阶段选择 + 数据层 IO 离线单测（不连网、不依赖 tqsdk）

验证：
  1. 两阶段：阶段1 成交量取前 M，阶段2 活跃度(|涨跌幅|)取前 N，结果精确。
  2. 边界：数据不足 vol_window 的品种被跳过（不崩、不误选）。
  3. 无未来函数契约：函数读取 close[-1]（末端已收盘值），故调用方必须剔除
     “未收盘 bar”，否则会把未来价混进来 → 这里正向证明它确实读末端值。
  4. Selection 卡C 集成：update_selection 写 selected，active_keys 返回它，周频重选。
  5. data_feed.load_cache 纯 IO 往返（不依赖 pandas）。
"""
from __future__ import annotations
import sys, os, csv, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import universe as _u
from selection import select_two_stage, Selection


def _make_daily(keys, vol_levels, ret_abs_map, n=30):
    """构造 {key: {"close":[..], "volume":[..]}}。

    vol_levels: {key: 量水平}；ret_abs_map: {key: 近3日 |涨跌幅| 目标值}。
    其余品种 close 平淡（ret_abs=0）、volume=1（必然落选）。
    """
    daily = {}
    for key in keys:
        vol = vol_levels.get(key, 1)
        cl = [1000.0] * n
        if key in ret_abs_map:
            r = ret_abs_map[key]
            cl[-4] = 1000.0
            cl[-3] = cl[-2] = cl[-1] = 1000.0 * (1 + r)
        daily[key] = {"close": list(cl), "volume": [float(vol)] * n}
    return daily


def test_two_stage_basic():
    keys = _u.all_keys()[:20]
    vol_levels = {k: 100 - i for i, k in enumerate(keys)}      # 阶段1 降序：前10
    ret_abs_map = {keys[i]: (10 - i) * 0.01 for i in range(10)}  # 阶段2：前6 = keys[:6]
    daily = _make_daily(keys, vol_levels, ret_abs_map)
    selected, stage1 = select_two_stage(
        daily, vol_window=20, vol_top_m=10, ret_window=3, ret_top_n=6)
    assert stage1 == keys[:10], f"stage1={stage1}"
    assert selected == keys[:6], f"selected={selected}"
    print("test_two_stage_basic OK")


def test_insufficient_data_skipped():
    keys = _u.all_keys()[:20]
    vol_levels = {k: 100 - i for i, k in enumerate(keys)}
    ret_abs_map = {keys[i]: (10 - i) * 0.01 for i in range(10)}
    daily = _make_daily(keys, vol_levels, ret_abs_map, n=30)
    # 前两个品种截短到不足 vol_window → 应被跳过
    for short in (keys[0], keys[1]):
        daily[short]["close"] = daily[short]["close"][:10]
        daily[short]["volume"] = daily[short]["volume"][:10]
    selected, stage1 = select_two_stage(
        daily, vol_window=20, vol_top_m=10, ret_window=3, ret_top_n=6)
    assert keys[0] not in stage1 and keys[0] not in selected
    assert keys[1] not in stage1 and keys[1] not in selected
    assert stage1[0] == keys[2]   # 第三个顶上
    print("test_insufficient_data_skipped OK")


def test_uses_closed_bar_endpoint():
    """契约：函数读取 close[-1]（末端已收盘），调用方须剔除未收盘 bar。"""
    keys = _u.all_keys()[:20]
    vol_levels = {k: 100 - i for i, k in enumerate(keys)}
    ret_abs_map = {keys[i]: (10 - i) * 0.01 for i in range(10)}  # keys[5]=0.05 第6名
    daily = _make_daily(keys, vol_levels, ret_abs_map, n=30)
    X = keys[5]
    sel_a, _ = select_two_stage(
        daily, vol_window=20, vol_top_m=10, ret_window=3, ret_top_n=6)
    assert X in sel_a, "X 应入选（第6名）"
    # 仅把 X 的 close[-1] 调小（模拟若喂入的末端值是更小收盘价），其它不变
    daily2 = {k: {"close": list(v["close"]), "volume": list(v["volume"])}
              for k, v in daily.items()}
    daily2[X]["close"][-1] = daily2[X]["close"][-4] * 1.005   # ret_abs 从 0.05 -> 0.005
    sel_b, _ = select_two_stage(
        daily2, vol_window=20, vol_top_m=10, ret_window=3, ret_top_n=6)
    assert X not in sel_b, "X 的 close[-1] 变小后 ret_abs 骤降应落选 → 证明读末端值"
    print("test_uses_closed_bar_endpoint OK (契约：读 close[-1]，调用方须剔除未收盘bar)")


def test_selection_integration():
    cfg_sel = {"vol_window_days": 20, "vol_top_m": 10, "ret_window_days": 3,
               "ret_top_n": 6, "reselect_min_days": 7}
    sel = Selection("liquidity", _u.all_keys(), selection_cfg=cfg_sel)
    keys = _u.all_keys()[:20]
    vol_levels = {k: 100 - i for i, k in enumerate(keys)}
    ret_abs_map = {keys[i]: (10 - i) * 0.01 for i in range(10)}
    daily = _make_daily(keys, vol_levels, ret_abs_map, n=30)
    partial = {k: daily[k] for k in keys[:12]}   # 模拟只有部分品种有缓存
    out = sel.update_selection(partial, today="2026-10-17")
    assert set(out) <= set(keys[:12])
    assert sel.active_keys() == [k.upper() for k in out]
    # 周频：刚选过，+1天不应重选；+7天应重选
    assert sel.needs_reselect("2026-10-18") is False
    assert sel.needs_reselect("2026-10-24") is True
    rep = sel.selection_report()
    assert "stage1" in rep and "selected" in rep and rep["last_reselect"] == "2026-10-17"
    print("test_selection_integration OK")


def test_data_feed_cache_roundtrip():
    import data_feed as _df
    tmp = tempfile.mkdtemp()
    old = _df.DATA_DIR
    _df.DATA_DIR = tmp
    try:
        p = _df._cache_path("RB")
        with open(p, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["datetime", "close", "volume"])
            for i in range(25):
                w.writerow([f"2026-01-{i+1:02d} 00:00:00", 3500.0 + i, 100000.0 + i * 10])
        d = _df.load_cache("RB")
        assert d is not None
        assert len(d["close"]) == 25
        assert d["close"][-1] == 3524.0
        assert d["volume"][0] == 100000.0
        assert _df.load_cache("NOPE") is None   # 无缓存返回 None
    finally:
        _df.DATA_DIR = old
    print("test_data_feed_cache_roundtrip OK")


if __name__ == "__main__":
    test_two_stage_basic()
    test_insufficient_data_skipped()
    test_uses_closed_bar_endpoint()
    test_selection_integration()
    test_data_feed_cache_roundtrip()
    print("\n卡C 选择层离线单测通过 ✅")
