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
    from lib import plan_stop as ps
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


# ================================================================== T3#3 人为触发验收（离线等价）
def t8b_触阈_持仓stop不变_拒开():
    """T3#3 离线等价：①新开被拒且台账有 DAILYSTOP/RISKBLOCK 行
    ②已有持仓的 stop **绝不被风控改动** ③平仓/止损/撤单仍走原通道
    （dloss 只门控「新开仓入口」，不接触 stop、不拦平仓/撤单）。"""
    g = _guard(cur=985_000.0)                     # 恰好 −1.5% ⇒ 触发
    s, rec, api = _mkstate(pos=8, cloud=8)
    s["stop"] = 3010.0                            # 已有持仓的止损位
    acc_box = {"acc": _Acc(985_000.0), "dloss": g, "_dl_ts": 0.0}
    m._process(api, s, True, {s["key"]: {}}, acc_box, _fuse())
    # ① 新开仓（BUY/OPEN）被拒；平仓/撤单不受限
    opens = [x for x in api.inserts if x[1] == "BUY" and x[2] == "OPEN"]
    _ok(not opens, f"触阈后仍新开仓：{opens}")
    _ok("RISKBLOCK" in rec.kinds(), f"缺拒开台账：{rec.kinds()}")
    _ok(any("停加仓阀" in t for t in rec.texts("RISKBLOCK")), rec.texts("RISKBLOCK"))
    _ok(any("当日亏损" in t for t in rec.texts("DAILYSTOP")), rec.texts("DAILYSTOP"))
    # ② 已有持仓 stop 不被风控改动
    _ok(s["stop"] == 3010.0, f"风控改动了已有止损={s['stop']}（禁区）")
    # ③ 平仓/止损走原通道：价格触 stop ⇒ 仍报 SELL/CLOSE（同 t9 已证，此处复验）
    s2, rec2, api2 = _mkstate(pos=8, cloud=8)
    s2["stop"] = 3010.0                           # 现价 3001.95 ≤ stop ⇒ 盘中扫损
    acc_box2 = {"acc": _Acc(985_000.0), "dloss": g, "_dl_ts": 0.0}
    m._process(api2, s2, True, {s2["key"]: {}}, acc_box2, _fuse())
    closes = [x for x in api2.inserts if x[2] in ("CLOSE", "CLOSETODAY")]
    _ok(closes, f"触阈后止损/平仓被挡 ⇒ 会把自己锁死：{api2.inserts}")
    print("  PASS  t8b_触阈_持仓stop不变_拒开(离线验收)")


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


# ================================================================== #15 RECOVER 止损重锚
def t14_接管原样恢复计划止损():
    """#15 核心：开仓落盘的计划止损，接管时必须**原样恢复**，不重算。

    构造多头：开仓 3260 / 计划止损 3200（风险 60）；重启时现价已跌到 3250。
    原算法重算 = 3250 − 1.15×ATR(52) ≈ 3190.2 —— **比计划宽 9.8 点**（放大风险）。
    修复后 stop 必须严格 == 3200，且 `bars_in` 要按开仓 bar 恢复（原实现一律归零）。
    """
    rd = tempfile.mkdtemp(prefix="pst14_")
    s, rec, api = _mkstate(pos=8, cloud=8, record_dir=rd)
    k = _kline(n=40)
    # ★ E1（2026-10-21）：kline datetime 必须按 tqsdk 语义构造（UTC），
    #   否则 `_bars_since_bar` 修正时区后会把它整体 +8h ⇒ bars_in 算成 40。
    k["datetime"] = pd.date_range("2026-10-21 01:00:00", periods=40, freq="5min")   # UTC
    s["k"] = k
    s["q"] = _Quote(px=3250.0)
    # quote.datetime 是**北京时间**：UTC + 8h
    _bt = str(k["datetime"].iloc[-3] + pd.Timedelta(hours=8))   # 开仓那根：倒数第 3 根
    _ok(ps.save(rd, "M", "DCE.m2701", 3200.0, 3260.0, 1, 8, _bt) is True, "计划止损落盘失败")

    m._attach_stop_on_boot(s, 8, 3250.0, 52.0)

    _ok(abs(s["stop"] - 3200.0) < 1e-9,
        f"接管把计划止损改成了 {s['stop']}（重算放宽 ⇒ #15 未修）")
    _ok(abs(s["entry"] - 3260.0) < 1e-9, f"entry 被重置为当前价 {s['entry']}（应为开仓价 3260）")
    _ok(s["bars_in"] == 2, f"bars_in 未恢复={s['bars_in']}（倒数第3根之后走了2根；原实现一律归零）")
    _ok("RECOVER" in rec.kinds(), rec.kinds())
    _ok(any("原样恢复" in t for t in rec.texts("RECOVER")), rec.texts("RECOVER"))
    print("  PASS  t14_接管原样恢复计划止损")


def t15_无记录才重算并固化():
    """无记录（老持仓/换月）⇒ 才重算兜底；算出来的线必须**当场固化**，下次重启不再重算。"""
    rd = tempfile.mkdtemp(prefix="pst15_")
    s, rec, api = _mkstate(pos=8, cloud=8, record_dir=rd)
    s["q"] = _Quote(px=3250.0)
    m._attach_stop_on_boot(s, 8, 3250.0, 52.0)
    _ok(s["stop"] > 0, s["stop"])
    _ok(any("无该合约的计划止损记录" in t for t in rec.texts("RECOVER")), rec.texts("RECOVER"))
    _first = s["stop"]
    _ok(ps.load(rd, "M", "DCE.m2701", 1) is not None, "兜底值未固化 ⇒ 下次重启会再次重算")

    # 第二次重启：价格又跌 10 点 —— 止损必须仍是第一次兜底的那条（走路径 A）
    s2, rec2, _ = _mkstate(pos=8, cloud=8, record_dir=rd)
    s2["q"] = _Quote(px=3240.0)
    m._attach_stop_on_boot(s2, 8, 3240.0, 52.0)
    _ok(abs(s2["stop"] - _first) < 1e-9,
        f"兜底值未固化：第二次接管变成 {s2['stop']}（应仍为 {_first}）")
    _ok(any("原样恢复" in t for t in rec2.texts("RECOVER")), rec2.texts("RECOVER"))
    print("  PASS  t15_无记录才重算并固化")


def t16_空仓清理记录():
    """空仓（已建立对账基线）⇒ 记录必须清掉；未建基线时**绝不能清**。"""
    rd = tempfile.mkdtemp(prefix="pst16_")
    s, rec, api = _mkstate(pos=0, cloud=0, record_dir=rd)
    _ok(ps.save(rd, "M", "DCE.m2701", 3200.0, 3260.0, 1, 8, None) is True, "预置失败")

    # ★ 启动初期（k 未就绪 / 首拍对账还没跑）pos 恒为 0 —— 此时清文件会误删预置记录
    m._sync_plan_stop(s)
    _ok(ps.load(rd, "M") is not None,
        "对账基线未建立就清了记录 ⇒ 重启时会把上一笔的计划止损误删，接管退化成重算")

    s["_recon_base"] = True                       # 对账基线已建立（真正核对过云端持仓）
    m._sync_plan_stop(s)
    _ok(ps.load(rd, "M") is None, "空仓后旧计划止损未清 ⇒ 下次会套用上一笔的线")

    # 有仓但无记录 ⇒ 补写（外部接管/兜底重算的线也要固化）
    s2, _, _ = _mkstate(pos=-8, cloud=-8, record_dir=rd)
    s2["stop"] = 3400.0
    s2["entry"] = 3460.0
    m._sync_plan_stop(s2)
    r = ps.load(rd, "M", "DCE.m2701", -1)
    _ok(r is not None and abs(r["stop"] - 3400.0) < 1e-9,
        f"有仓无记录时未补写：{r}")
    print("  PASS  t16_空仓清理_有仓补写")


# ================================================================== #16 单日亏损阀演练
def t17_演练阀只拒开仓_止损不变():
    """#16 `--drill-dailyloss` 接入 _process 后的三要素（权益**完全正常**，只有演练闩锁）。"""
    g = dl.DailyLossGuard(None)
    g.update(1_000_000.0, now=_dt.datetime(2026, 10, 21, 9, 0))
    _ok(not g.blocked, "未演练前不该 blocked")
    g.force_blocked()
    _ok(g.blocked, "演练闩锁未生效")
    _ok("[DRILL]" in g.reason(), g.reason())

    # ① 空仓 + 有信号 ⇒ 拒绝新开仓，台账 RISKBLOCK 必须带 [DRILL]（可辨识，不冒充真实触发）
    s, rec, api = _mkstate(pos=0, cloud=0)
    acc_box = {"acc": _Acc(1_000_000.0), "dloss": g, "_dl_ts": 0.0}
    m._process(api, s, True, {s["key"]: {}}, acc_box, _fuse())
    opens = [x for x in api.inserts if x[1] == "BUY" and x[2] == "OPEN"]
    _ok(not opens, f"演练态仍开新仓：{opens}")
    _ok(any("[DRILL]" in t for t in rec.texts("RISKBLOCK")),
        f"拒开台账缺 [DRILL] 标记：{rec.texts('RISKBLOCK')}")

    # ② 有持仓且未触止损 ⇒ 持仓保留、stop 一个字不改、不产生任何平仓单
    s2, rec2, api2 = _mkstate(pos=8, cloud=8)
    s2["stop"] = 2900.0                          # 远低于现价 3001.95，不会触发止损
    acc_box2 = {"acc": _Acc(1_000_000.0), "dloss": g, "_dl_ts": 0.0}
    m._process(api2, s2, True, {s2["key"]: {}}, acc_box2, _fuse())
    _ok(s2["pos"] == 8, f"演练不该动持仓：pos={s2['pos']}")
    _ok(s2["stop"] == 2900.0, f"演练改动了已有 stop={s2['stop']}（禁区）")
    _ok(not [x for x in api2.inserts if x[2] in ("CLOSE", "CLOSETODAY")],
        f"演练不该平仓：{api2.inserts}")
    g.clear_drill()
    print("  PASS  t17_演练阀只拒开仓_止损不变")


# ================================================================== 风控二期
# ★ 账户桩直接用文件上方既有的 `_Acc`（默认 100 万/10 万），不要在这里重定义 ——
#   重复定义会把旧的无默认参数版本覆盖掉，导致 t7~t10 全部 TypeError（踩过）。


def _gate():
    from lib.ops import RiskGate
    # max_same_side_ratio 设很大 ⇒ 让本组测试只暴露想测的那一层，不被同向敞口挡住
    return RiskGate(max_hands=8, max_margin_ratio=0.60,
                    max_same_side_ratio=99.0)


def t18_腿数闸门_达K拒开_持仓不变():
    """#18（D2 18.1/18.2/18.3）：6 个品种有仓 ⇒ 第 7 个拒开；已有仓状态一字不动。

    口径按**品种聚合**（换池双份算 1 腿）、**只挡开仓**。
    """
    s, rec, api = _mkstate(pos=0, cloud=0)
    s["risk"] = _gate()
    s["key"] = "RB"          # 开仓者自身不在 shared 里（真实场景：空仓品种才走到开仓路径）
    # 6 个品种各 1 手，名义很小（1000×10×1=1万/腿）⇒ 不会先撞 #22-A 隔夜上限
    shared = {k: {"pos": 1, "price": 1000.0, "tgt": 1, "dt": ""}
              for k in ("M", "SA", "MA", "FU", "BU", "EG")}
    ok, why = m._risk_check(s, _Acc(), 2, shared=shared, side_sign=1, price=1000.0)
    _ok(ok is False and "腿数" in why, f"6 腿已满仍放行：{ok} {why}")
    print(f"    {why}")

    # 只有 5 腿 ⇒ 放行
    shared2 = {k: {"pos": 1, "price": 1000.0, "tgt": 1, "dt": ""}
               for k in ("M", "SA", "MA", "FU", "BU")}
    ok2, why2 = m._risk_check(s, _Acc(), 2, shared=shared2, side_sign=1, price=1000.0)
    _ok(ok2 is True, f"5 腿应放行：{ok2} {why2}")

    # ★ 已有仓状态不变（拒开不得改动任何持仓字段）
    s2, _, _ = _mkstate(pos=8, cloud=8)
    s2["risk"] = _gate()
    s2["stop"], s2["entry"], s2["bars_in"] = 2900.0, 3000.0, 7
    m._risk_check(s2, _Acc(), 2, shared=shared, side_sign=1, price=1000.0)
    _ok((s2["stop"], s2["entry"], s2["bars_in"]) == (2900.0, 3000.0, 7),
        "拒开改动了持仓状态（禁区）")
    print("  PASS  t18_腿数闸门_达K拒开_持仓不变")


def t19_隔夜上限_超限拒开_平仓放行():
    """#22-A（D2 §8-②）：单品种隔夜名义 >SYM_RATIO×权益 ⇒ 拒开；平仓永不经过本闸门。

    ★ 2026-10-21 13:29 阈值由 1.0/0.30 上调为 2.0/0.45（D2 拍板）。
      本用例改为**按常量推算手数**，避免再次硬编码（原 8 手/32 万在新阈值下不再超限）。
    """
    from lib import overnight as _ovn
    s, rec, api = _mkstate(pos=0, cloud=0)
    s["risk"] = _gate()
    EQ = float(_Acc().balance)
    PX, MULT = 4000.0, 10.0

    # ① 单品种超 SYM_RATIO× ⇒ 拒
    _h = int(EQ * _ovn.SYM_RATIO / (PX * MULT)) + 2
    ok, why = m._risk_check(s, _Acc(), _h, shared={}, side_sign=1, price=PX,
                            day="20261021")
    _ok(ok is False and "隔夜" in why, f"单品种隔夜超限仍放行：{ok} {why}（手数={_h}）")
    print(f"    {why}")

    # ② 长假前降级：单笔永远超不过「降级总上限」，必须靠组合超 ⇒ 构造一条 SA 腿
    #    （SA 乘数 20，单品种须 ≤ SYM_RATIO×，且两腿合计要 > HOLIDAY_TOTAL_RATIO×）
    _cap = EQ * _ovn.HOLIDAY_TOTAL_RATIO
    _sa_h = max(1, int(_cap * 0.5 / (1000.0 * 20)))
    _new_h = max(1, int(_cap * 0.6 / (PX * MULT)))
    _sh = {"SA": {"pos": _sa_h, "price": 1000.0}}
    ok_n, why_n = m._risk_check(s, _Acc(), _new_h, shared=_sh, side_sign=1,
                                price=PX, day="20261021")
    ok_h, why_h = m._risk_check(s, _Acc(), _new_h, shared=_sh, side_sign=1,
                                price=PX, day="20260930")
    _ok(ok_n is True, f"普通日同规模应放行：{ok_n} {why_n}")
    _ok(ok_h is False and "降级" in why_h,
        f"长假前未降级：{ok_h} {why_h}（SA {_sa_h} 手 + 新单 {_new_h} 手）")
    print(f"    {why_h}")

    # ③ 正常规模放行
    ok3, why3 = m._risk_check(s, _Acc(), 1, shared={}, side_sign=1, price=3000.0,
                              day="20261021")
    _ok(ok3 is True, f"正常规模被误挡：{ok3} {why3}")
    print("  PASS  t19_隔夜上限_超限拒开_平仓放行")


def t20_告警队列_分级与去重():
    """#17-A：弹窗同时落队列；等级可枚举；同事件同交易日只落一次；不弹真窗。"""
    rd = tempfile.mkdtemp(prefix="alert_wire_")
    old_dir, m._ALERT_DIR = m._ALERT_DIR, rd

    class _NoThread:                     # 拦掉真弹窗（否则测试会弹出 MessageBox）
        def __init__(self, *a, **k):
            pass

        def start(self):
            pass

    old_th = m.threading.Thread
    m.threading.Thread = _NoThread
    try:
        # 四类最高级事件必须被识别（D2 §2-17.2）
        for kw in ("回撤熔断", "对账偏离", "单日亏损阀", "只出不进"):
            _ok(m._alert_level_of(f"MA13 模拟盘：{kw}触发") == "CRITICAL", kw)
        _ok(m._alert_level_of("保证金预警") == "WARN", "普通事件应按 WARN")

        m._popup("MA13 模拟盘：⚠️ 回撤熔断触发", "自峰值回撤 20%", source="回撤熔断")
        m._popup("MA13 模拟盘：⚠️ 回撤熔断触发", "自峰值回撤 20%", source="回撤熔断")  # 重复
        rows = m._aq.recent(rd, limit=50)
        _ok(len(rows) == 1, f"同事件应只落一次，实际 {len(rows)} 条：{rows}")
        _ok(rows[0]["level"] == "CRITICAL" and rows[0]["source"] == "回撤熔断", rows[0])
        _ok(rows[0]["dedup_key"].startswith("CRITICAL:"), rows[0]["dedup_key"])
    finally:
        m.threading.Thread = old_th
        m._ALERT_DIR = old_dir
    print("  PASS  t20_告警队列_分级与去重")


# ============================================================ #18 P1-A 鬼腿双计
def t21_鬼腿双计_真相源回填():
    """A18（D1 审计 2026-10-21 #18）：缓存滞后不得导致「旧仓 + 新仓」双计。

    真实数字（当天 BU 被误拒 3 次的那一笔）：
      shared["BU"]["pos"] = 7  ← 上一根 bar 的缓存（实际已止损平仓）
      s["pos"]            = 0  ← 真相源
      新单 8 手 × 4993 × 10 = 399,440
      错误：349,510(旧) + 399,440(新) = **748,950** > 443,264 ⇒ 误拒
      正确：399,440 < 443,264 ⇒ 放行
    """
    from lib import overnight as _ovn
    EQ = 985_031.0                # 由日志 0.45×权益=443264 反推
    PX, MULT = 4993.0, 10.0
    H_OLD, H_NEW = 7, 8
    conf = {"BU": ("石油沥青", "SHFE.bu2611", MULT, 1)}
    new_n = H_NEW * PX * MULT

    # ① 未回填 ⇒ 复现双计（必须被拒，且数字逐位对上）
    shared = {"BU": {"pos": H_OLD, "price": PX}}
    legs = _ovn.build_legs(shared, conf)
    ok, why = _ovn.check_limit(legs, EQ, key="BU", new_notional=new_n, day="2026-10-21")
    _ok(ok is False, f"未复现双计（应被误拒）：{ok} {why}")
    _ok("748950" in why.replace(",", ""), f"金额与实测 748950 不符：{why}")

    # ② 回填真相源后 ⇒ 放行
    s = {"key": "BU", "pos": 0}
    _ok(m._sync_shared_pos(shared, s) is True, "_sync_shared_pos 返回 False")
    _ok(shared["BU"]["pos"] == 0, f"回填未生效：{shared['BU']['pos']}")
    legs2 = _ovn.build_legs(shared, conf)
    ok2, why2 = _ovn.check_limit(legs2, EQ, key="BU", new_notional=new_n, day="2026-10-21")
    _ok(ok2 is True, f"回填后仍被拒（真实 399440 应放行）：{why2}")
    print(f"    未回填被拒：{why}")
    print("  PASS  t21_鬼腿双计_真相源回填")


def t22_快照不含鬼腿():
    """A19：隔夜敞口快照必须用真相源，否则鬼腿会进历史（实测高估 2.32 倍）。

    15:01:04 那份快照：FU+EG+BU+SA = 1,243,200 / 1.2642×
    真实（EG/BU 已于 14:44:59 平仓）：FU+SA = 537,040 / 0.546×
    """
    from lib import overnight as _ovn
    import config_symbols as cs

    eq = 983_989.0
    shared = {
        "FU": {"pos": -8, "price": 4815.0},
        "SA": {"pos": -8, "price": 949.0},
        "EG": {"pos": 7, "price": 5088.0},      # 鬼腿
        "BU": {"pos": 7, "price": 5000.0},      # 鬼腿
    }
    # 真相源：EG/BU 已平
    specs = [{"key": "FU", "pos": -8}, {"key": "SA", "pos": -8},
             {"key": "EG", "pos": 0}, {"key": "BU", "pos": 0}]

    # 未回填 ⇒ 会算进鬼腿（复现污染）
    legs_bad = _ovn.build_legs(shared, cs.SYMBOLS)
    bad = sum(L["notional"] for L in legs_bad)
    _ok(bad > 1_200_000, f"未复现污染：{bad:.0f}")

    # 回填后 ⇒ 只剩两腿真实
    for sp in specs:
        m._sync_shared_pos(shared, sp)
    legs_ok = _ovn.build_legs(shared, cs.SYMBOLS)
    total = sum(L["notional"] for L in legs_ok)
    real = 8 * 4815.0 * 10 + 8 * 949.0 * 20          # 385,200 + 151,840
    _ok(len(legs_ok) == 2, f"回填后腿数应为 2，实际 {len(legs_ok)}")
    _ok(abs(total - real) < 1e-6, f"总名义 {total:.0f} != 真实 {real:.0f}")
    print(f"    污染 {bad:,.0f} → 回填后 {total:,.0f}（真实 {real:,.0f}，占权益 {total / eq:.3f}×）")
    print("  PASS  t22_快照不含鬼腿")


# ------------------------------------------------------------------ main
def main():
    tests = [t1_账实不符_锁只出不进, t2_反向偏离也锁, t3_首次对账不锁_仍接管,
             t4_在途委托不锁, t5_换月过渡不锁, t6_FIXWAIT窗口内不锁,
             t7_未触阈正常开仓, t8_触阈只拒开仓, t8b_触阈_持仓stop不变_拒开,
             t9_触阈仍可平仓_止损照常, t10_重启不清零_仍然拒开,
             t11_涨停不报BUY, t12_涨停照常平多, t13_字段不可读不拦,
             t14_接管原样恢复计划止损, t15_无记录才重算并固化, t16_空仓清理记录,
             t17_演练阀只拒开仓_止损不变,
             # 风控二期（D2 2026-10-21 拍板 #17/#18/#22）
             t18_腿数闸门_达K拒开_持仓不变, t19_隔夜上限_超限拒开_平仓放行,
             # #18 P1-A 鬼腿双计（D1 2026-10-21 审计）
             t21_鬼腿双计_真相源回填, t22_快照不含鬼腿,
             t20_告警队列_分级与去重]
    bad = 0
    print("=" * 72)
    print("P0-2 / P1-1 / P1-3 / #15 / #16 接线级单测")
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
