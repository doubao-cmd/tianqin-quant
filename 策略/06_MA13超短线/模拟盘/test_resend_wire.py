# -*- coding: utf-8 -*-
"""
test_resend_wire.py —— 离线单测：P0-1 撤单重报 + 反手同拍净仓口径（**接线级**，不是纯函数）
================================================================================
为什么要有这一层：`lib/live_resend.py` 的 selftest 只证明决策函数对，
**证明不了主文件真的把它的结论执行了**。本测试直接驱动
`ma13_live_multi._cloud_fill_stats()`（撮合确认 + 撤单 + 重报的唯一代码路径），
用桩对象替掉 TqApi / 台账，断言"台账里出现了什么动作"。

覆盖 6 条（对应 交接\06模拟盘_风控增强任务书_20261019.md §五 总验收 R1 / 红线 §六-1）：
  1. 反手同拍「平旧+开新」⇒ 平仓腿**不得**再报「判为未成交」（2026-10-20 实测 4/4 的根因）
  2. 单腿平仓超时未成交、委托仍 ALIVE ⇒ 发一次撤单，pending 保留，预算 +1
  3. 撤单生效（FINISHED + 剩余量）⇒ 按未成交量重报一次，新单标记 rs.done（只重报一次）
  4. 已重报过的单仍无成交 ⇒ 不再撤，交原路径记未成交
  5. 休市/断流（行情停滞）⇒ **不撤单**（避免白烧撤单额度）
  6. 日撤单额度用尽 ⇒ **不撤单**（红线：逼近交易所上限即停手）

跑法（需 tqsdk 解释器，因为主模块顶层 import tqsdk）：
    %USERPROFILE%\\.workbuddy\\binaries\\python\\envs\\tqsdk\\python.exe test_resend_wire.py
"""
from __future__ import annotations

import os
import sys
import time
import datetime as _dt
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = _HERE
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)
for _p in (_HERE, _ROOT, os.path.join(_ROOT, "策略", "06_MA13超短线", "工作台")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    import ma13_live_multi as m
    from lib import live_resend as rs
except Exception as e:                                   # noqa: BLE001
    print(f"SKIP: 无法导入 ma13_live_multi（{type(e).__name__}: {e}）")
    raise SystemExit(0)


# ------------------------------------------------------------------ 桩
class _Pos:
    def __init__(self, long_n=0, short_n=0, px_long=float("nan"), px_short=float("nan")):
        self.pos_long_today = long_n
        self.pos_long_his = 0
        self.pos_short_today = short_n
        self.pos_short_his = 0
        self.position_price_long = px_long
        self.position_price_short = px_short
        self.float_profit_long = 0.0
        self.float_profit_short = 0.0


class _Order:
    def __init__(self, status="ALIVE", left=8, msg="", dead=False, oid="o1"):
        self.order_id = oid
        self.status = status
        self.volume_left = left
        self.volume_orign = 8
        self.last_msg = msg
        self.is_dead = dead
        self.limit_price = float("nan")


class _Quote:
    def __init__(self, symbol="SHFE.fu2611", px=3000.0, dt=None):
        self.underlying_symbol = symbol
        self.last_price = px
        self.datetime = dt or _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.000000")


class _Api:
    def __init__(self, pos=None):
        self.pos = pos or _Pos()
        self.cancels = []
        self.inserts = []          # (symbol, direction, offset, volume, limit_price)
        self._n = 0

    def get_position(self, symbol=None):
        return self.pos

    def cancel_order(self, order_or_id, account=None):
        self.cancels.append(getattr(order_or_id, "order_id", order_or_id))

    def insert_order(self, symbol, direction, offset, volume, limit_price=None):
        self._n += 1
        self.inserts.append((symbol, direction, offset, volume, limit_price))
        return _Order("ALIVE", volume, oid=f"new{self._n}")


class _Rec:
    def __init__(self):
        self.events = []           # (kind, text)

    def event(self, kind, text="", *a, **kw):
        self.events.append((kind, str(text)))

    def fill(self, *a, **kw):
        pass

    def signal(self, *a, **kw):
        pass

    def kinds(self):
        return [k for k, _ in self.events]

    def texts(self, kind=None):
        return [t for k, t in self.events if kind is None or k == kind]


def _mkstate(pend, quote=None, record_dir=None):
    rec = _Rec()
    s = {
        "key": "FU", "name": "燃料油", "rec": rec,
        "q": quote or _Quote(),
        "pending_fill": pend, "last_cloud_pos": None,
        "record_dir": record_dir or tempfile.mkdtemp(prefix="rsw_"),
        "ledger": None,
    }
    return s, rec


def _fresh_budget(cap=300, gap=0.0):
    """每个用例独立的撤单预算（进程级单例，测试间必须重置）。"""
    m._RS_BUDGET = rs.CancelBudget(None, cap=cap, gap_sec=gap)
    return m._RS_BUDGET


def _ok(cond, msg):
    if not cond:
        raise AssertionError(msg)


# ------------------------------------------------------------------ 用例
def t1_反手同拍不再误报未成交():
    now = time.time()
    o_close = _Order("FINISHED", 0, oid="c1")      # 平仓腿：撮合窗口内已成交
    o_open = _Order("FINISHED", 0, oid="o1")
    pend = [
        {"symbol": "SHFE.bu2611", "ts": now - 60, "side": "平", "dir": "多", "hands": 8,
         "ref_price": 4906.0, "expect_net": 0, "order": o_close, "offset": "CLOSETODAY",
         "key": "k-close"},
        {"symbol": "SHFE.bu2611", "ts": now - 59.9, "side": "开", "dir": "多", "hands": 8,
         "ref_price": 4906.0, "expect_net": 8, "order": o_open, "offset": "OPEN",
         "key": "k-open"},
    ]
    api = _Api(_Pos(long_n=8, px_long=4907.0))     # 反手后净仓仍为 +8（新仓）
    s, rec = _mkstate(pend, _Quote("SHFE.bu2611", 4906.0))
    _fresh_budget()
    m._cloud_fill_stats(api, s)
    bad = [t for t in rec.texts("FILLX") if "判为未成交" in t]
    _ok(not bad, f"反手同拍仍误报未成交：{bad}")
    _ok(not api.cancels, f"反手同拍不该撤单：{api.cancels}")
    _ok(len(s["pending_fill"]) == 0, "已确认的 pending 应清空")


def t2_未成交撤单():
    now = time.time()
    o = _Order("ALIVE", 8, oid="c9")
    pend = [{"symbol": "SHFE.fu2611", "ts": now - 60, "side": "平", "dir": "空", "hands": 8,
             "ref_price": 3000.0, "expect_net": 0, "order": o, "offset": "CLOSE", "key": "k9"}]
    api = _Api(_Pos(short_n=8, px_short=3000.0))   # 仓位没动 ⇒ 真未成交
    s, rec = _mkstate(pend, _Quote("SHFE.fu2611", 3000.0))
    bg = _fresh_budget()
    m._cloud_fill_stats(api, s)
    _ok(api.cancels == ["c9"], f"应发一次撤单，实际 {api.cancels}")
    _ok(bg.used("SHFE") == 1, f"撤单预算应 +1，实际 {bg.used('SHFE')}")
    _ok(len(s["pending_fill"]) == 1, "撤单后应保留 pending 等下拍看重报")
    _ok("RSCANCEL" in rec.kinds(), f"缺 RSCANCEL 台账事件：{rec.kinds()}")
    _ok(not [t for t in rec.texts("FILLX") if "判为未成交" in t],
        "撤单这一拍不应同时写未成交（否则台账自相矛盾）")


def t3_撤单生效后重报一次():
    now = time.time()
    o = _Order("FINISHED", 8, oid="c9")            # 撤单已生效：终结 + 剩余 8 手
    pend = [{"symbol": "SHFE.fu2611", "ts": now - 60, "side": "平", "dir": "空", "hands": 8,
             "ref_price": 3000.0, "expect_net": 0, "order": o, "offset": "CLOSE",
             "key": "k9", "rs": {"cancel_ts": now - 3}}]
    api = _Api(_Pos(short_n=8, px_short=3000.0))
    s, rec = _mkstate(pend, _Quote("SHFE.fu2611", 3000.0))
    _fresh_budget()
    m._cloud_fill_stats(api, s)
    _ok(len(api.inserts) == 1, f"应重报 1 张单，实际 {api.inserts}")
    sym, d, off, vol, lim = api.inserts[0]
    _ok((sym, d, off, vol) == ("SHFE.fu2611", "BUY", "CLOSE", 8),
        f"重报参数错（平空=BUY）：{(sym, d, off, vol)}")
    _ok(lim is not None and lim > 3000.0,
        f"SHFE 重报必须是让价限价（平空=BUY ⇒ 向上让 2 跳），实际 {lim}")
    _ok(len(s["pending_fill"]) == 1 and s["pending_fill"][0].get("rs", {}).get("done"),
        "重报单应标记 rs.done（只重报一次）")
    _ok("RSEND" in rec.kinds(), f"缺 RSEND 台账事件：{rec.kinds()}")


def t4_已重报过的单不再撤():
    now = time.time()
    o = _Order("ALIVE", 4, oid="c7")
    pend = [{"symbol": "SHFE.fu2611", "ts": now - 120, "side": "平", "dir": "空", "hands": 8,
             "ref_price": 3000.0, "expect_net": 0, "order": o, "offset": "CLOSE",
             "key": "k7", "rs": {"done": True}}]
    api = _Api(_Pos(short_n=8, px_short=3000.0))
    s, rec = _mkstate(pend, _Quote("SHFE.fu2611", 3000.0))
    _fresh_budget()
    m._cloud_fill_stats(api, s)
    _ok(not api.cancels, f"已重报过的单不该再撤：{api.cancels}")
    _ok([t for t in rec.texts("FILLX") if "判为未成交" in t], "放弃处置后应记未成交")


def t5_休市不撤单():
    now = time.time()
    o = _Order("ALIVE", 8, oid="c5")
    pend = [{"symbol": "SHFE.fu2611", "ts": now - 600, "side": "平", "dir": "空", "hands": 8,
             "ref_price": 3000.0, "expect_net": 0, "order": o, "offset": "CLOSE", "key": "k5"}]
    api = _Api(_Pos(short_n=8, px_short=3000.0))
    stale = _Quote("SHFE.fu2611", 3000.0,
                   dt=(_dt.datetime.now() - _dt.timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S.000000"))
    s, rec = _mkstate(pend, stale)
    _fresh_budget()
    m._cloud_fill_stats(api, s)
    _ok(not api.cancels, f"行情停滞时不该撤单：{api.cancels}")
    _ok([t for t in rec.texts("FILLX") if "判为未成交" in t], "应记未成交并说明")


def t6_额度用尽不撤单():
    now = time.time()
    o = _Order("ALIVE", 8, oid="c3")
    pend = [{"symbol": "SHFE.fu2611", "ts": now - 60, "side": "平", "dir": "空", "hands": 8,
             "ref_price": 3000.0, "expect_net": 0, "order": o, "offset": "CLOSE", "key": "k3"}]
    api = _Api(_Pos(short_n=8, px_short=3000.0))
    s, rec = _mkstate(pend, _Quote("SHFE.fu2611", 3000.0))
    _fresh_budget(cap=0)                            # 额度已耗尽
    m._cloud_fill_stats(api, s)
    _ok(not api.cancels, f"额度用尽仍撤单（违红线）：{api.cancels}")
    _ok([t for t in rec.texts("FILLX") if "判为未成交" in t], "应记未成交")


def main():
    tests = [t1_反手同拍不再误报未成交, t2_未成交撤单, t3_撤单生效后重报一次,
             t4_已重报过的单不再撤, t5_休市不撤单, t6_额度用尽不撤单]
    fail = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            fail += 1
            print(f"  FAIL  {t.__name__}  →  {e}")
        except Exception as e:                       # noqa: BLE001
            fail += 1
            print(f"  ERR   {t.__name__}  →  {type(e).__name__}: {e}")
    print(f"\n{len(tests) - fail}/{len(tests)} 通过")
    raise SystemExit(1 if fail else 0)


if __name__ == "__main__":
    main()
