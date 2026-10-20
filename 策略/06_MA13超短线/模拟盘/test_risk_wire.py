# -*- coding: utf-8 -*-
"""
test_risk_wire.py —— 离线接线级单测：P0-2 对账硬停机 / P1-1 单日亏损阀 / P1-3 封板排除
================================================================================
为什么要有这一层：`lib/*.py` 的 selftest 只证明**判定函数**对，
证明不了主文件真的把结论执行了。本测试直接驱动
`ma13_live_multi._sync_cloud_pos()` / `._process()` / `._order()`
（真跑的唯一代码路径），用桩对象替掉 TqApi / 台账 / 账户，断言行为。

对应 交接\06模拟盘_风控增强_剩余任务书_20261020.md §三 总验收：
  R2  P0-2 对账偏离超阈即锁只出不进，过渡态不误锁        → t1~t6
  R3  P1-1 当日亏损（昨收基准）≥1.5% 只关入口，重启不清零 → t7~t10
  R5  P1-3 封板方向不报单（只读交易所字段）                → t11~t13

⚠️ 「下一交易日自动复位」由 `lib/daily_loss.selftest()` 用**注入时间**覆盖
   （接线层用真实时钟，无法在不改系统时间的前提下跨日）。

跑法（需 tqsdk 解释器，因为主模块顶层 import tqsdk）：
    %USERPROFILE%\\.workbuddy\\binaries\\python\\envs\\tqsdk\\python.exe test_risk_wire.py
"""
from __future__ import annotations

import os
import sys
import time
import tempfile
import datetime as _dt

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = _HERE
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)
for _p in (_HERE, _ROOT, os.path.join(_ROOT, "策略", "06_MA13超短线", "工作台")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    import ma13_live_multi as m
    from lib import daily_loss as dl
except Exception as e:                                   # noqa: BLE001
    print(f"SKIP: 无法导入 ma13_live_multi（{type(e).__name__}: {e}）")
    raise SystemExit(0)

import numpy as np
import pandas as pd


# ------------------------------------------------------------------ 桩
class _Quote:
    """行情桩：涨跌停字段默认不存在（模拟字段未就绪），需要时显式给。"""

    def __init__(self, symbol="DCE.m2701", px=3001.95, dt=None,
                 upper=None, lower=None):
        self.underlying_symbol = symbol
        self.last_price = px
        self.datetime = dt or _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.000000")
        if upper is not None:
            self.upper_limit = upper
        if lower is not None:
            self.lower_limit = lower


class _Pos:
    """持仓桩：同时支持 `p["pos"]`（_cloud_pos 用）与属性访问（_cloud_fill_stats 用）。"""

    def __init__(self, net=0, px=3001.95):
        self._net = int(net)
        self.pos_long_today = max(0, net)
        self.pos_long_his = 0
        self.pos_short_today = max(0, -net)
        self.pos_short_his = 0
        self.position_price_long = px if net > 0 else float("nan")
        self.position_price_short = px if net < 0 else float("nan")
        self.float_profit_long = 0.0
        self.float_profit_short = 0.0

    def __getitem__(self, k):
        if k == "pos":
            return self._net
        raise KeyError(k)


class _Order:
    def __init__(self, oid="o1"):
        self.order_id = oid
        self.status = "ALIVE"
        self.volume_left = 0
        self.volume_orign = 0
        self.last_msg = ""
        self.is_dead = False
        self.limit_price = float("nan")


class _Api:
    def __init__(self, net=0, by_symbol=None):
        self.pos = _Pos(net)
        self.by_symbol = dict(by_symbol or {})
        self.inserts = []
        self.cancels = []
        self._n = 0

    def get_position(self, symbol=None):
        if symbol is None:
            return {}                       # 账户全部持仓（孤儿扫描用）
        if self.by_symbol:
            return _Pos(self.by_symbol.get(str(symbol), 0))
        return self.pos

    def insert_order(self, symbol, direction, offset, volume, limit_price=None):
        self._n += 1
        self.inserts.append((symbol, direction, offset, volume, limit_price))
        return _Order(f"new{self._n}")

    def cancel_order(self, order_or_id, account=None):
        self.cancels.append(order_or_id)


class _Acc:
    """账户桩：`acc["balance"]` / `acc["margin"]` + 属性两种访问都要支持。"""

    def __init__(self, balance=1_000_000.0, margin=100_000.0):
        self._d = {"balance": float(balance), "margin": float(margin)}
        self.balance = float(balance)
        self.margin = float(margin)

    def __getitem__(self, k):
        return self._d[k]


class _Rec:
    def __init__(self):
        self.events = []

    def event(self, kind, text="", *a, **kw):
        self.events.append((kind, str(text)))

    def fill(self, *a, **kw):
        pass

    def signal(self, *a, **kw):
        pass

    def stoploss(self, *a, **kw):
        pass

    def kinds(self):
        return [k for k, _ in self.events]

    def texts(self, kind=None):
        return [t for k, t in self.events if kind is None or k == kind]


def _kline(n=40, base=3000.0, step=0.05):
    """合成 5min K 线：缓慢上行 ⇒ MA13 斜率>0 且价格贴着 MA13（满足回踩闸门）。"""
    close = np.array([base + step * i for i in range(n)], dtype=float)
    return pd.DataFrame({"id": np.arange(n), "open": close - 0.02,
                         "high": close + 0.5, "low": close - 0.5, "close": close})


def _mkstate(pos=0, cloud=0, record_dir=None, last_order_ts=0.0):
    rec = _Rec()
    s = {
        "key": "M", "name": "豆粕", "cont": "DCE.m2701", "mult": 10.0, "tick": 1.0,
        "comm": 2.0, "rec": rec, "q": _Quote(), "k": _kline(),
        "pos": pos, "entry": 0.0, "stop": 0.0, "bars_in": 0, "last_id": -1,
        "last_atr": 1.0, "pending_fill": [], "ledger": None,
        "underlying": "DCE.m2701", "record_dir": record_dir or tempfile.mkdtemp(prefix="rw_"),
        "last_order_ts": last_order_ts, "hand_of": lambda a, e, p: 2,
        "risk": None,
    }
    return s, rec, _Api(cloud)


def _fuse():
    # peak 用 100 万（≈真实账户）：若给 1e9，任何余额都会先触发 20% 回撤熔断，
    # 把要测的 P1-1 行为掩盖掉（t7 第一版就栽在这）。
    return {"peak": 1_000_000.0, "halted": False, "save": lambda: None, "reason": ""}


def _ok(cond, msg):
    if not cond:
        raise AssertionError(msg)


# ================================================================== R2 P0-2
def t1_账实不符_锁只出不进():
    """非首次对账 + 本地以为平 + 云端 8 手 ⇒ 必须锁，且把持仓认回本地管理。"""
    s, rec, api = _mkstate(pos=0, cloud=8)
    s["_recon_base"] = True                       # 本会话已建立对账基线
    fuse = _fuse()
    m._sync_cloud_pos(api, s, True, fuse=fuse, acc=_Acc())
    _ok(fuse["halted"] is True, "对账偏离未锁只出不进 ⇒ P0-2 未生效")
    _ok("HALT" in rec.kinds(), f"缺 HALT 台账事件：{rec.kinds()}")
    _ok(any("账实不符" in t for t in rec.texts("HALT")), rec.texts("HALT"))
    _ok(s["pos"] == 8, f"锁之后必须把云端持仓认回本地（否则无止损、脱管）：pos={s['pos']}")
    _ok(s.get("stop", 0) > 0, "认回后必须补挂止损，否则该仓裸奔")
    print("  PASS  t1_账实不符_锁只出不进")


def t2_反向偏离也锁():
    """本地以为有仓、云端已无 ⇒ 同样锁（任务书「或反之」）。"""
    s, rec, api = _mkstate(pos=8, cloud=0)
    s["_recon_base"] = True
    fuse = _fuse()
    m._sync_cloud_pos(api, s, True, fuse=fuse, acc=_Acc())
    _ok(fuse["halted"] is True, "反向偏离未锁")
    _ok(s["pos"] == 0, s["pos"])
    print("  PASS  t2_反向偏离也锁")


def t3_首次对账不锁_仍接管():
    """进程刚起（无对账基线）：云端有仓 = 正常接管，**绝不能锁**。"""
    s, rec, api = _mkstate(pos=0, cloud=8)
    fuse = _fuse()
    m._sync_cloud_pos(api, s, True, fuse=fuse, acc=_Acc())
    _ok(fuse["halted"] is False, "重启首次对账被误锁 ⇒ 每次重启都要人工复位，灾难")
    _ok("RECOVER" in rec.kinds(), f"应走接管路径：{rec.kinds()}")
    _ok(s["pos"] == 8 and s.get("_recon_base") is True, (s["pos"], s.get("_recon_base")))
    print("  PASS  t3_首次对账不锁_仍接管")


def t4_在途委托不锁():
    """有在途委托（pending_fill 非空）⇒ 本拍不下结论（wait），既不锁也不接管。"""
    s, rec, api = _mkstate(pos=0, cloud=8)
    s["_recon_base"] = True
    s["pending_fill"] = [{"ts": time.time() - 60, "side": "开", "dir": "多", "hands": 2,
                          "ref_price": 3000.0, "expect_net": 2, "order": None,
                          "offset": "OPEN", "key": "k1"}]
    fuse = _fuse()
    m._sync_cloud_pos(api, s, True, fuse=fuse, acc=_Acc())
    _ok(fuse["halted"] is False, "有在途委托时误锁")
    _ok(s["pos"] == 0, f"有在途委托时不得接管（假接管会覆盖止损）：pos={s['pos']}")
    _ok("RECONWAIT" in rec.kinds(), f"应记一次 RECONWAIT：{rec.kinds()}")
    print("  PASS  t4_在途委托不锁")


def t5_换月过渡不锁():
    """换月移交进行中（旧合约有仓、主连已切换）⇒ 不锁。

    这是真实形态：`s["underlying"]` 还是旧合约、`q.underlying_symbol` 已跳到新主连。
    此时报完旧合约平仓单后 `rollover_pending` 非空，对账**必须判 wait**
    （否则会拿"新合约 0 手"去比"本地 0 手"之外的东西，把自己刚报的换月单判成偏离而锁死）。
    """
    s, rec, api = _mkstate(pos=0, cloud=0)
    api = _Api(by_symbol={"DCE.m2611": 8})        # 旧合约 8 手；新主连 0 手
    s["q"] = _Quote(symbol="DCE.m2701")           # 主连已切到 m2701
    s["underlying"] = "DCE.m2611"                 # 本地持仓仍在旧合约
    s["_recon_base"] = True
    fuse = _fuse()
    m._sync_cloud_pos(api, s, True, fuse=fuse, acc=_Acc())
    _ok(fuse["halted"] is False, "换月过渡态被误锁 ⇒ 无法平仓")
    _ok(s.get("rollover_pending") == "DCE.m2611",
        f"应记下换月防重闸门：{s.get('rollover_pending')}")
    _ok(s["pos"] == 0, f"换月平仓后本地应归零等新仓，实际 {s['pos']}")
    old_orders = [x for x in api.inserts if x[0] == "DCE.m2611"]
    _ok(old_orders and old_orders[0][1] == "SELL",
        f"应在旧合约报平仓单（平多=SELL）：{api.inserts}")
    print("  PASS  t5_换月过渡不锁")


def t6_FIXWAIT窗口内不锁():
    """刚下过单的 FIX_WAIT 窗口内：直接跳过，既不锁也不接管。"""
    s, rec, api = _mkstate(pos=0, cloud=8, last_order_ts=time.time())
    s["_recon_base"] = True
    fuse = _fuse()
    m._sync_cloud_pos(api, s, True, fuse=fuse, acc=_Acc())
    _ok(fuse["halted"] is False, "下单后 30s 内误锁（会把刚开的仓判成偏离）")
    _ok(s["pos"] == 0, s["pos"])
    print("  PASS  t6_FIXWAIT窗口内不锁")


# ================================================================== R3 P1-1
def _guard(path=None, base=1_000_000.0, cur=1_000_000.0, ratio=dl.DAILY_LOSS_RATIO):
    g = dl.DailyLossGuard(path, ratio=ratio)
    g.seed(base)
    g.update(cur)
    return g


def t7_未触阈正常开仓():
    """对照臂：亏损未达 1.5% ⇒ 信号照常报单。"""
    g = _guard(cur=1_000_000.0)
    s, rec, api = _mkstate(pos=0, cloud=0)
    acc_box = {"acc": _Acc(1_000_000.0), "dloss": g, "_dl_ts": 0.0}
    m._process(api, s, True, {s["key"]: {}}, acc_box, _fuse())
    _ok(len(api.inserts) == 1, f"未触阈却不开仓：{api.inserts}")
    _ok(api.inserts[0][1] == "BUY" and api.inserts[0][2] == "OPEN", api.inserts)
    print("  PASS  t7_未触阈正常开仓")


def t8_触阈只拒开仓():
    """亏损 ≥1.5% ⇒ 新开仓被拒，且台账有 RISKBLOCK/DAILYSTOP 可复盘。"""
    g = _guard(cur=985_000.0)                     # 恰好 −1.5%
    _ok(g.blocked, "构造失败：guard 未触发")
    s, rec, api = _mkstate(pos=0, cloud=0)
    acc_box = {"acc": _Acc(985_000.0), "dloss": g, "_dl_ts": 0.0}
    m._process(api, s, True, {s["key"]: {}}, acc_box, _fuse())
    _ok(not api.inserts, f"触阈后仍报单 ⇒ P1-1 未生效：{api.inserts}")
    _ok("RISKBLOCK" in rec.kinds(), f"缺拒开台账：{rec.kinds()}")
    _ok(any("停加仓阀" in t for t in rec.texts("RISKBLOCK")), rec.texts("RISKBLOCK"))
    _ok(any("当日亏损" in t for t in rec.texts("DAILYSTOP")), rec.texts("DAILYSTOP"))
    print("  PASS  t8_触阈只拒开仓")


def t9_触阈仍可平仓_止损照常():
    """触阈后**平仓通道必须畅通**：持仓被止损扫掉时仍要报单。"""
    g = _guard(cur=985_000.0)
    s, rec, api = _mkstate(pos=8, cloud=8)
    s["stop"] = 3010.0                            # 现价 3001.95 ≤ 止损 ⇒ 盘中扫损
    acc_box = {"acc": _Acc(985_000.0), "dloss": g, "_dl_ts": 0.0}
    m._process(api, s, True, {s["key"]: {}}, acc_box, _fuse())
    closes = [x for x in api.inserts if x[2] in ("CLOSE", "CLOSETODAY")]
    _ok(closes, f"触阈后止损/平仓被挡 ⇒ 会把自己锁死：{api.inserts}")
    _ok(closes[0][1] == "SELL", closes)
    print("  PASS  t9_触阈仍可平仓_止损照常")


def t10_重启不清零_仍然拒开():
    """闩锁落盘：新进程从同一文件读回 ⇒ 仍然拒绝开仓（任务书硬性判据）。"""
    d = tempfile.mkdtemp(prefix="dlw_")
    p = os.path.join(d, dl.STATE_NAME)
    _guard(path=p, cur=985_000.0)                 # 触发并落盘
    g2 = dl.DailyLossGuard(p)                     # 模拟重启
    _ok(g2.blocked, "重启后闩锁丢失 ⇒ 重启即可绕过风控 = 未实现")
    s, rec, api = _mkstate(pos=0, cloud=0)
    acc_box = {"acc": _Acc(985_000.0), "dloss": g2, "_dl_ts": 0.0}
    m._process(api, s, True, {s["key"]: {}}, acc_box, _fuse())
    _ok(not api.inserts, f"重启后仍开仓：{api.inserts}")
    print("  PASS  t10_重启不清零_仍然拒开")


# ================================================================== R5 P1-3
def t11_涨停不报BUY():
    """涨停（last==upper）⇒ BUY（开多/平空）不报单。"""
    s, rec, api = _mkstate()
    s["q"] = _Quote(px=3000.0, upper=3000.0, lower=2700.0)
    ok, note, o = m._order(api, s, "DCE.m2701", "BUY", "OPEN", 1, ref_price=3000.0)
    _ok(ok is False and o is None, (ok, note))
    _ok(not api.inserts, f"涨停仍报了 BUY：{api.inserts}")
    _ok("涨停" in note, note)
    print("  PASS  t11_涨停不报BUY")


def t12_涨停照常平多():
    """涨停时 SELL（开空/平多）**可以成交**，不能一概拦住。"""
    s, rec, api = _mkstate()
    s["q"] = _Quote(px=3000.0, upper=3000.0, lower=2700.0)
    ok, note, o = m._order(api, s, "DCE.m2701", "SELL", "CLOSE", 1, ref_price=3000.0)
    _ok(ok is True, f"涨停把平多也拦了 ⇒ 该平的反而是好价：{note}")
    _ok(len(api.inserts) == 1, api.inserts)
    print("  PASS  t12_涨停照常平多")


def t13_字段不可读不拦():
    """回测/字段未就绪（nan）⇒ 不拦（宁可不拦，也不把交易卡死）。"""
    s, rec, api = _mkstate()
    s["q"] = _Quote(px=3000.0, upper=float("nan"), lower=float("nan"))
    ok, note, o = m._order(api, s, "DCE.m2701", "BUY", "OPEN", 1, ref_price=3000.0)
    _ok(ok is True and len(api.inserts) == 1, (ok, note, api.inserts))
    print("  PASS  t13_字段不可读不拦")


# ------------------------------------------------------------------ main
def main():
    tests = [t1_账实不符_锁只出不进, t2_反向偏离也锁, t3_首次对账不锁_仍接管,
             t4_在途委托不锁, t5_换月过渡不锁, t6_FIXWAIT窗口内不锁,
             t7_未触阈正常开仓, t8_触阈只拒开仓, t9_触阈仍可平仓_止损照常,
             t10_重启不清零_仍然拒开,
             t11_涨停不报BUY, t12_涨停照常平多, t13_字段不可读不拦]
    bad = 0
    print("=" * 72)
    print("P0-2 / P1-1 / P1-3 接线级单测")
    print("=" * 72)
    for t in tests:
        try:
            t()
        except Exception as e:                    # noqa: BLE001
            bad += 1
            print(f"  FAIL  {t.__name__}: {type(e).__name__}: {e}")
    print("=" * 72)
    print(f"{len(tests) - bad}/{len(tests)} 通过" + ("" if not bad else f"，{bad} 失败"))
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
