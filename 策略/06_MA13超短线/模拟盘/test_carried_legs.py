# -*- coding: utf-8 -*-
"""
test_carried_legs.py —— 离线单测：换池保护 `_adopt_carried_legs`
====================================================================
被测对象：`模拟盘/ma13_live_multi._adopt_carried_legs`

要证明的事（这是 2026-10-18 修的 P0-3）：
  卡C 每 6 天重选一次池子。**止损线只存在本进程内存里**（云端不存），
  所以「被换出池子但云端还有持仓」的腿，如果不补订阅：
    · 不被 `_sync_cloud_pos` 对账 ⇒ 进程不知道它有仓
    · 没有止损单 / 没有时间止损 ⇒ **永久裸仓，永远不会自然出场**
  本函数扫全账户持仓，把「有仓但不在本次池内」的品种补订阅接管，
  并标 `_carried=True`（只许出场、不许开新仓）。

跑法（必须用装了 tqsdk 的解释器，因为模块顶层 import tqsdk）：
    %USERPROFILE%\\.workbuddy\\binaries\\python\\envs\\tqsdk\\python.exe test_carried_legs.py
"""
from __future__ import annotations

import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = _HERE
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)
for _p in (_HERE, _ROOT, os.path.join(_ROOT, "工作台")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    import ma13_live_multi as m
except Exception as e:                       # 缺 tqsdk / 缺账号配置
    print(f"SKIP: 无法导入 ma13_live_multi（{type(e).__name__}: {e}）")
    print("      请用 tqsdk 解释器运行本测试。")
    raise SystemExit(0)


# ---------------------------------------------------------------------------
# 极简 api 桩：只实现 _adopt_carried_legs 会用到的那几个调用
# ---------------------------------------------------------------------------
class _Pos:
    def __init__(self, pos):
        self.pos = pos


class FakeApi:
    def __init__(self, pos_map):
        self._pos = {sym: _Pos(n) for sym, n in pos_map.items()}
        self.subscribed = []

    def get_position(self, symbol=None):
        if symbol is None:
            return self._pos
        return self._pos.get(symbol, _Pos(0))

    def get_quote(self, code):
        self.subscribed.append(("quote", code))
        return {"underlying_symbol": code}

    def get_kline_serial(self, code, ksec, data_length=200):
        self.subscribed.append(("kline", code))
        return {"id": []}


def _specs(keys):
    out = []
    for k in keys:
        name, cont, mult, tick = m.config_symbols.get(k)
        out.append({"key": k, "name": name, "cont": cont, "mult": mult,
                    "tick": tick, "comm": float(m.config_symbols.COST.get(k, 2.0))})
    return out


def _run(keys, pos_map):
    """跑一次 adopt，返回 (added, specs, shared, api)。"""
    specs = _specs(keys)
    shared = {k: {"dt": "", "price": 0.0, "pos": 0, "tgt": 0} for k in keys}
    tmp = tempfile.mkdtemp(prefix="carried_")
    sizer = m._SIZER if hasattr(m, "_SIZER") else m.get_sizer({"name": "risk_par"}) \
        if hasattr(m, "get_sizer") else None
    if sizer is None:
        from sizer import get_sizer
        sizer = get_sizer({"name": "risk_par"})
    api = FakeApi(pos_map)
    added = m._adopt_carried_legs(api, specs, shared, tmp, sizer, 300)
    return added, specs, shared, api


# ---------------------------------------------------------------------------
def test_adopts_out_of_pool_positions():
    """真实场景复现：池子换成 JM/FU/BU/MA/M/SA，账户却还挂着 P/SR/JD 的裸仓。"""
    added, specs, shared, api = _run(
        ["JM", "FU", "BU", "MA", "M", "SA"],
        {                       # 10-17 20:01 台账心跳的真实持仓
            "DCE.p2701": -4,
            "CZCE.SR701": 7,
            "DCE.jd2611": -8,
            "DCE.jm2601": -4,
            "DCE.m2701": -8,
            "DCE.eg2601": 0,          # 零仓不该被接管
        })
    assert sorted(added) == ["JD", "P", "SR"], f"应接管 JD/P/SR，实际 {added}"
    keys = [s["key"] for s in specs]
    for k in ("JD", "P", "SR"):
        assert k in keys, f"{k} 未补进 specs"
        st = next(s for s in specs if s["key"] == k)
        assert st.get("_carried") is True, f"{k} 未标记 _carried（会开新仓！）"
    # 池内品种不该被重复接管
    for k in ("JM", "M"):
        assert keys.count(k) == 1, f"{k} 被重复加入"
        st = next(s for s in specs if s["key"] == k)
        assert not st.get("_carried"), f"池内 {k} 被误标 _carried（会被禁开仓）"
    # 零仓品种不接管
    assert "EG" not in added, "零仓品种被无谓接管"
    # shared 必须同步补 key —— 否则 _process 开头 shared[s['key']] 直接 KeyError
    for k in ("JD", "P", "SR"):
        assert k in shared, f"{k} 未补进 shared ⇒ _process 会 KeyError"
    # 必须真的订阅了行情/K线，否则拿不到数据、止损无法判
    subs = [c for _t, c in api.subscribed]
    assert len(subs) >= 6, f"订阅数不足：{api.subscribed}"
    print(f"  PASS  test_adopts_out_of_pool_positions（接管 {sorted(added)}）")


def test_skips_unknown_spec():
    """持仓里出现无核验规格的品种 ⇒ 跳过并告警，不抛异常、不静默下单。"""
    added, specs, shared, _api = _run(
        ["JM"],
        {"DCE.jm2601": -4, "DCE.zz9999": 5})
    assert added == [], f"无规格品种不该被接管，实际 {added}"
    assert all(s["key"] == "JM" for s in specs)
    print("  PASS  test_skips_unknown_spec（无规格品种被安全跳过）")


def test_zero_positions_noop():
    """账户全空 ⇒ 什么都不做（重启后最常见的情况）。"""
    added, specs, shared, _api = _run(["JM", "M"], {"DCE.jm2601": 0, "DCE.m2701": 0})
    assert added == []
    assert len(specs) == 2
    print("  PASS  test_zero_positions_noop")


def test_carried_blocks_new_entry():
    """静态契约：`_carried` 必须在开仓分支之前把 _open_ok 置 False。

    这条是「只出不进」的守门断言——用源码位置检查，因为 _process 依赖
    tqsdk 运行时对象，不适合桩测。
    """
    src = open(os.path.join(_HERE, "ma13_live_multi.py"), encoding="utf-8").read()
    i_carried = src.find('if s.get("_carried"):')
    i_open = src.find("if pos == 0 and dir_trend != 0 and not fuse[\"halted\"] and _open_ok:")
    assert i_carried > 0, "找不到 _carried 检查"
    assert i_open > 0, "找不到开仓分支"
    assert i_carried < i_open, "★ _carried 检查在开仓分支之后 ⇒ 保护无效"
    print("  PASS  test_carried_blocks_new_entry（检查位于开仓分支之前）")


def main() -> int:
    print("=" * 70)
    print("换池保护 _adopt_carried_legs 离线单测")
    print("=" * 70)
    test_adopts_out_of_pool_positions()
    test_skips_unknown_spec()
    test_zero_positions_noop()
    test_carried_blocks_new_entry()
    print("=" * 70)
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
