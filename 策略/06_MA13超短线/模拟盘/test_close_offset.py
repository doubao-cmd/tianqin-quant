# -*- coding: utf-8 -*-
"""
test_close_offset.py —— 离线单测：上期所/能源「平今 vs 平昨」拆单
=================================================================
事故复盘（2026-10-19 实盘，真实发生过）

  FU（SHFE）10:44 当天开空 6 手 ⇒ pos_short_today=6, pos_short_his=0。
  14:04 时间止损、14:55 止损触发，两次平仓都发 offset="CLOSE"。
  tqsdk/api.py:1201 官方定义：「上期所和上期能源分平今/平昨，**平今用 CLOSETODAY**，
  平昨用 CLOSE」⇒ 发 CLOSE 等于报「平昨 6 手」，而昨仓为 0 ⇒ 交易所废单。
  废单是**异步回报**，insert_order 照常返回 True，代码先写 FILL，
  30 秒后才靠「持仓没变」倒推成一句「未成交」，原因完全不可见；仓位裸奔到收盘。

本测试锁死三件事：
  1. 非上期所/能源 ⇒ 仍然只有一张 CLOSE（行为逐字不变，不许误伤 DCE/CZCE）
  2. 上期所/能源 ⇒ 按今/昨仓拆成 CLOSETODAY + CLOSE，手数守恒
  3. 拆单后每张单的 expect_net 各自正确（否则成交核对会误判）

跑法（需 tqsdk 解释器，因为模块顶层 import tqsdk）：
    %USERPROFILE%\\.workbuddy\\binaries\\python\\envs\\tqsdk\\python.exe test_close_offset.py
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = _HERE
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)
for _p in (_HERE, _ROOT, os.path.join(_ROOT, "策略", "06_MA13超短线", "工作台")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    import ma13_live_multi as m
except Exception as e:
    print(f"SKIP: 无法导入 ma13_live_multi（{type(e).__name__}: {e}）")
    raise SystemExit(0)


# ------------------------------------------------------------------ 桩
class _Pos:
    def __init__(self, long_today=0, long_his=0, short_today=0, short_his=0):
        self.pos_long_today = long_today
        self.pos_long_his = long_his
        self.pos_short_today = short_today
        self.pos_short_his = short_his


class _Order:
    """模拟 tqsdk Order 的三个只读字段（objs.py:494-498）。"""
    def __init__(self):
        self.status = "ALIVE"
        self.last_msg = ""
        self.is_dead = False


class _Api:
    def __init__(self, pos=None, raise_on_pos=False):
        self.pos = pos
        self.raise_on_pos = raise_on_pos
        self.calls = []          # (symbol, direction, offset, volume, limit_price)

    def get_position(self, symbol):
        if self.raise_on_pos:
            raise RuntimeError("position not ready")
        return self.pos if self.pos is not None else _Pos()

    def insert_order(self, symbol, direction, offset, volume, limit_price=None):
        self.calls.append((symbol, direction, offset, volume, limit_price))
        return _Order()


class _Rec:
    def __init__(self):
        self.fills = []

    def fill(self, dir_cn, side, hands, price, symbol):
        self.fills.append((dir_cn, side, int(hands), float(price), symbol))


class _Ledger:
    def __init__(self):
        self.sent_keys = []

    def sent(self, key, symbol, dir_cn, offset_cn, hands, ref_price=0.0):
        self.sent_keys.append((key, offset_cn, int(hands)))
        return True


def _mk_s(key="FU", rec=None, ledger=None):
    return {"key": key, "rec": rec or _Rec(), "ledger": ledger or _Ledger(),
            "pending_fill": [], "last_order_ts": 0.0}


# ------------------------------------------------------------------ 1. 拆单计划
def test_plan_non_shfe_unchanged():
    """DCE/CZCE/INE 之外：永远只有一张 CLOSE —— 旧行为逐字不变。"""
    api = _Api(_Pos(long_his=8, short_his=8))
    for sym in ("CZCE.MA701", "DCE.m2701", "CZCE.SA701"):
        assert m._close_plan(api, sym, "BUY", 8) == [("CLOSE", 8)], sym
        assert m._close_plan(api, sym, "SELL", 8) == [("CLOSE", 8)], sym
    print("  PASS  test_plan_non_shfe_unchanged")


def test_plan_shfe_today_only():
    """★ 事故场景：当日开仓 ⇒ 只有今仓 ⇒ 必须 CLOSETODAY。"""
    api = _Api(_Pos(short_today=6))
    assert m._close_plan(api, "SHFE.fu2611", "BUY", 6) == [("CLOSETODAY", 6)]
    api2 = _Api(_Pos(long_today=4))
    assert m._close_plan(api2, "SHFE.bu2611", "SELL", 4) == [("CLOSETODAY", 4)]
    print("  PASS  test_plan_shfe_today_only")


def test_plan_shfe_yesterday_only():
    """隔夜仓 ⇒ 昨仓 ⇒ 仍是 CLOSE（不能一律改 CLOSETODAY，否则同样废单）。"""
    api = _Api(_Pos(short_his=6))
    assert m._close_plan(api, "SHFE.fu2611", "BUY", 6) == [("CLOSE", 6)]
    print("  PASS  test_plan_shfe_yesterday_only")


def test_plan_shfe_split():
    """今昨混合 ⇒ 拆两张，手数守恒，且**先平今后平昨**。"""
    api = _Api(_Pos(short_today=2, short_his=4))
    plan = m._close_plan(api, "INE.sc2612", "BUY", 6)
    assert plan == [("CLOSETODAY", 2), ("CLOSE", 4)], plan
    assert sum(v for _, v in plan) == 6
    # 只平今仓那部分：不许多报昨仓
    assert m._close_plan(api, "INE.sc2612", "BUY", 2) == [("CLOSETODAY", 2)]
    print("  PASS  test_plan_shfe_split")


def test_plan_shfe_pos_unreadable():
    """持仓查询滞后/异常 ⇒ 先按当日开当日平试 CLOSETODAY，绝不同时发两张。"""
    api = _Api(raise_on_pos=True)
    assert m._close_plan(api, "SHFE.fu2611", "BUY", 6) == [("CLOSETODAY", 6)]
    api2 = _Api(_Pos())          # 读到但全 0（撮合未回填）
    assert m._close_plan(api2, "SHFE.fu2611", "BUY", 6) == [("CLOSETODAY", 6)]
    print("  PASS  test_plan_shfe_pos_unreadable")


# ------------------------------------------------------------------ 2. 下单落账
def test_send_close_shfe_today_limit_price():
    """事故场景端到端：FU 今仓 6 手，实际发出的委托必须是 CLOSETODAY + 让价限价。"""
    s = _mk_s("FU")
    api = _Api(_Pos(short_today=6))
    ok, note, sent = m._send_close(api, s, "SHFE.fu2611", "BUY", "空", 6,
                                   5745.0, "2026-10-19 14:05:01.5", -6)
    assert ok, note
    assert api.calls == [("SHFE.fu2611", "BUY", "CLOSETODAY", 6, 5747.0)], api.calls
    assert sent == [("CLOSETODAY", 6)]
    assert len(s["rec"].fills) == 1
    assert s["pending_fill"][0]["expect_net"] == 0
    print("  PASS  test_send_close_shfe_today_limit_price")


def test_send_close_split_bookkeeping():
    """拆两张单时：幂等键必须不同，且每张单的 expect_net 各自正确。"""
    s = _mk_s("FU")
    api = _Api(_Pos(short_today=2, short_his=4))
    ok, note, sent = m._send_close(api, s, "SHFE.fu2611", "BUY", "空", 6,
                                   5800.0, "2026-10-19 14:55:00.5", -6)
    assert ok, note
    assert [c[2] for c in api.calls] == ["CLOSETODAY", "CLOSE"], api.calls
    assert sent == [("CLOSETODAY", 2), ("CLOSE", 4)]
    # 空 6 手：平今 2 后应为 -4，平昨 4 后应为 0
    assert [p["expect_net"] for p in s["pending_fill"]] == [-4, 0], s["pending_fill"]
    keys = [k for k, _, _ in s["ledger"].sent_keys]
    assert len(set(keys)) == 2, f"拆单幂等键重复 ⇒ 台账串号：{keys}"
    assert len(s["rec"].fills) == 2 and [f[2] for f in s["rec"].fills] == [2, 4]
    print("  PASS  test_send_close_split_bookkeeping")


def test_send_close_non_shfe_single_key():
    """非上期所：单张 CLOSE、无后缀、市价单（不带 limit_price）。"""
    s = _mk_s("MA")
    api = _Api(_Pos(short_his=8))
    ok, note, sent = m._send_close(api, s, "CZCE.MA701", "BUY", "空", 8,
                                   3151.0, "2026-10-19 14:35:00.0", -8)
    assert ok, note
    assert api.calls == [("CZCE.MA701", "BUY", "CLOSE", 8, None)], api.calls
    assert list(s["ledger"].sent_keys[0][0].split("|"))[-1] == "8", "不该有 #今/#昨 后缀"
    assert s["pending_fill"][0]["expect_net"] == 0
    print("  PASS  test_send_close_non_shfe_single_key")


def test_order_returns_three_tuple():
    """_order 必须把 Order 对象交出来 —— 否则废单原因永远查不到（本次事故的第二个坑）。"""
    s = _mk_s("FU")
    api = _Api(_Pos(short_today=6))
    r = m._order(api, s, "SHFE.fu2611", "BUY", "CLOSETODAY", 6, ref_price=5745.0)
    assert len(r) == 3, f"_order 返回值不是三元组：{r}"
    ok, note, o = r
    assert ok and hasattr(o, "last_msg") and hasattr(o, "is_dead")
    # 缺参考价 ⇒ ok=False 且 order=None（调用方必须跳过记账）
    ok2, note2, o2 = m._order(api, s, "SHFE.fu2611", "BUY", "CLOSE", 6, ref_price=None)
    assert ok2 is False and o2 is None, (ok2, note2, o2)
    print("  PASS  test_order_returns_three_tuple")


# ------------------------------------------------------------------ 3. 静态红线
def test_source_redlines():
    """三处平仓不许再直接硬编码 CLOSE；心跳不许再用缓存仓位。"""
    src = open(os.path.join(_HERE, "ma13_live_multi.py"), encoding="utf-8").read()
    assert src.count("_send_close(") >= 3, "平仓没有全部走 _send_close（应为 定义1 + 调用2）"
    assert '_order(api, s, q.underlying_symbol, _side, "CLOSE"' not in src, \
        "出场处仍硬编码 CLOSE ⇒ 上期所平今会再次废单"
    assert 'sh["pos"], sh["tgt"])' not in src, \
        "心跳仍在用缓存仓位 ⇒ RECOVER 后持仓列会与 EQUITY 不一致"
    assert "int(s.get(\"pos\", sh[\"pos\"]))" in src, "心跳未改用 s[\"pos\"] 真相源"
    print("  PASS  test_source_redlines")


def main() -> int:
    print("=" * 72)
    print("上期所/能源 平今 vs 平昨 拆单 —— 离线单测")
    print("=" * 72)
    test_plan_non_shfe_unchanged()
    test_plan_shfe_today_only()
    test_plan_shfe_yesterday_only()
    test_plan_shfe_split()
    test_plan_shfe_pos_unreadable()
    test_send_close_shfe_today_limit_price()
    test_send_close_split_bookkeeping()
    test_send_close_non_shfe_single_key()
    test_order_returns_three_tuple()
    test_source_redlines()
    print("=" * 72)
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
