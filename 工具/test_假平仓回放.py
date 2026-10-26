# -*- coding: utf-8 -*-
"""
D3 批次1 离线回放 + 单测（不连网、纯标准库 + 主模块 import）
============================================================================
验证三处改动：
  改动1 RECONWAIT 超时（RECON_WAIT_MAX=180s）
  改动2 pending_fill TTL（PEND_TTL_SEC=3*FIX_WAIT_SEC=90s）
  改动3 未确认成交补 UNCONFIRMED 行

样本：今天（2026-10-26）豆粕多平 8 手——报单即本地 pos 置 0、云端仍 8 手、
      对账卡在 RECONWAIT 永不推进（事故根因层三）。

跑法：
  python 工具/test_假平仓回放.py            # 全跑（批次1 三处 + 离线回放）
  python 工具/test_假平仓回放.py --only c1  # 只跑改动1
"""
import importlib.util
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(ROOT, "策略", "06_MA13超短线", "模拟盘", "ma13_live_multi.py")

_spec = importlib.util.spec_from_file_location("ma13_live_multi", MAIN)
M = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(M)

# ---- 可控时钟：所有 time.time() 返回 fake_now[0] ----
_fake = [0.0]
M.time.time = lambda: _fake[0]


# ===================== Mock 基建 =====================
class FakeRec:
    """记录 recorder 的所有事件/行，供断言。"""
    def __init__(self):
        self.events = []          # (type, note)
        self.rows = []            # 成交流水原始行（list）

    def event(self, kind, note, dir_cn="", ref_price=""):
        self.events.append((kind, note))

    def fill(self, dir_cn, offset_cn, vol, price, symbol):
        self.rows.append(["FILL", "", "", f"{dir_cn}{offset_cn}", int(vol),
                          f"{float(price):.2f}", "", symbol])

    def unconfirmed_fill(self, dir_cn, offset_cn, vol, price, symbol):
        self.rows.append(["UNCONFIRMED", "", "", f"{dir_cn}{offset_cn}", int(vol),
                          f"{float(price):.2f}", "",
                          f"{symbol} 下单意图，未确认成交"])

    def start(self, note):
        self.events.append(("START", note))

    def stop(self, note="程序退出"):
        self.events.append(("STOP", note))

    def signal(self, *a, **k):
        pass

    def stoploss(self, *a, **k):
        pass

    def heartbeat(self, *a, **k):
        pass

    def has(self, kind):
        return any(e[0] == kind for e in self.events)

    def count(self, kind):
        return sum(1 for e in self.events if e[0] == kind)


class FakeQ:
    underlying_symbol = "DCE.m2701"
    last_price = 3200.0
    datetime = "2026-10-26 09:00:00.000000"


class HybridPos:
    """同时支持 dict 下标（_cloud_pos 用 p["pos"]）与属性访问（_cloud_fill_stats 用）。"""
    def __init__(self, net):
        self.pos = net
        self.pos_long_his = net if net > 0 else 0
        self.pos_long_today = 0
        self.pos_short_his = 0 if net > 0 else -net
        self.pos_short_today = 0
        self.position_price_long = 3000.0 if net > 0 else float("nan")
        self.position_price_short = 3000.0 if net < 0 else float("nan")
        self.float_profit_long = 0.0
        self.float_profit_short = 0.0

    def __getitem__(self, key):
        if key == "pos":
            return self.pos
        raise KeyError(key)


class FakeApi:
    def __init__(self, net):
        self.net = net

    def get_position(self, sym=None):
        if sym is None:
            return {}                 # 孤儿扫描：无持仓
        return HybridPos(self.net)    # 云端净仓 = net


def make_s(pos, pending, rec, tmp, extra=None):
    s = {
        "key": "M", "name": "豆粕", "q": FakeQ(),
        "pos": pos, "last_order_ts": -1e9,          # 足够旧，FIX_WAIT 不拦截
        "pending_fill": pending, "rec": rec, "record_dir": tmp,
        "underlying": "DCE.m2701", "last_atr": 5.0,  # >0 ⇒ 接管不重算 ATR
    }
    if extra:
        s.update(extra)
    return s


def m_pending(hands, ts, ref=3247.0, expect_net=0, symbol="DCE.m2701", side="平",
              dir_cn="多", order=None):
    return {"ts": ts, "side": side, "dir": dir_cn, "hands": hands,
            "ref_price": ref, "key": f"{symbol}|bt|{dir_cn}{side}|{hands}",
            "sig_close": ref, "expect_net": expect_net, "order": order,
            "offset": "CLOSE", "symbol": symbol, "bar_time": "bt", "rs": {}}


# 隔离：接管补止损在单测里无意义，置空避免触碰 tqsdk/计划文件
M._attach_stop_on_boot = lambda *a, **k: None


# ===================== 改动1 单测 =====================
def test_c1_timeout_takeover():
    """超时后强制对账，走接管分支，pos 回到云端净仓 8。"""
    import tempfile
    tmp = tempfile.mkdtemp(prefix="d3c1_")
    rec = FakeRec()
    p = m_pending(8, 0.0)
    s = make_s(0, [p], rec, tmp)
    # t=0：有在途委托 ⇒ wait ⇒ RECONWAIT，pos 保持 0
    _fake[0] = 0.0
    M._sync_cloud_pos(FakeApi(8), s, True)
    assert s["pos"] == 0, f"t=0 不应接管，pos={s['pos']}"
    assert rec.has("RECONWAIT"), "t=0 应写 RECONWAIT"
    assert not rec.has("RECONFORCE"), "t=0 不应 RECONFORCE"
    # t=190：超过 180s ⇒ RECONFORCE + 强制判定 ⇒ 接管 pos=8
    _fake[0] = 190.0
    M._sync_cloud_pos(FakeApi(8), s, True)
    assert rec.has("RECONFORCE"), "超时后应写 RECONFORCE"
    assert s["pos"] == 8, f"超时后应接管回 8 手，pos={s['pos']}"
    print("  [c1] 超时接管 OK：RECONWAIT→RECONFORCE→pos=8")


def test_c1_no_timeout_stays_wait():
    """未到 180s 仍返回 wait，pos 保持 0。"""
    import tempfile
    tmp = tempfile.mkdtemp(prefix="d3c1b_")
    rec = FakeRec()
    p = m_pending(8, 0.0)
    s = make_s(0, [p], rec, tmp)
    _fake[0] = 0.0
    M._sync_cloud_pos(FakeApi(8), s, True)
    _fake[0] = 100.0          # < 180
    M._sync_cloud_pos(FakeApi(8), s, True)
    assert s["pos"] == 0, f"未超时不应接管，pos={s['pos']}"
    assert not rec.has("RECONFORCE"), "未超时不应 RECONFORCE"
    print("  [c1] 未超时保持 wait OK：pos=0 无 RECONFORCE")


# ===================== 改动2 单测（驱动 pending 确认循环）=====================
class FakeOrder:
    def __init__(self, status="ALIVE", left=8, dead=False, msg=""):
        self.status = status
        self.volume_left = left
        self.is_dead = dead
        self.last_msg = msg


def _run_pending(net, pend, rec, tmp):
    """驱动 _cloud_fill_stats 跑一遍 pending 确认循环，返回结束后的 s。"""
    import tempfile
    if tmp is None:
        tmp = tempfile.mkdtemp(prefix="d3c2_")
    s = make_s(0, pend, rec, tmp)
    M._cloud_fill_stats(FakeApi(net), s)
    return s


def test_c2_filled_not_killed():
    """已成交（net==expect）且 age=200s ⇒ 走成交分支，绝不被 TTL 清掉。"""
    rec = FakeRec()
    p = m_pending(8, 0.0, expect_net=0)
    s = _run_pending(0, [p], rec, None)        # net=0==expect ⇒ filled
    assert not rec.has("PENDEXPIRE"), "已成交单不应 PENDEXPIRE"
    assert rec.has("FILLX"), "已成交应走 FILLX 记账"
    assert len(s["pending_fill"]) == 0, "已成交单应从 pending 移除"
    print("  [c2] 已成交不被 TTL 清掉 OK")


def test_c2_unfilled_expired():
    """未成交且 age=200s ⇒ PENDEXPIRE 强制终结，离开 pending。"""
    rec = FakeRec()
    p = m_pending(8, 0.0, expect_net=0)
    s = _run_pending(8, [p], rec, None)        # net=8≠0 ⇒ 未成交
    assert rec.has("PENDEXPIRE"), "超时未成交应 PENDEXPIRE"
    assert not rec.has("FILLX"), "TTL 终结不应再写 FILLX"
    assert len(s["pending_fill"]) == 0, "TTL 到期应离开 pending"
    # 改动3：TTL 分支同时补 UNCONFIRMED 行
    assert any(r[0] == "UNCONFIRMED" for r in rec.rows), "TTL 应补 UNCONFIRMED 行"
    print("  [c2] 超时未成交→PENDEXPIRE+TTL清pending OK")


def test_c2_young_kept():
    """age=10s（< FIX_WAIT=30）⇒ 原样保留，不碰 TTL。"""
    rec = FakeRec()
    # ts=190, now=200 ⇒ age=10 < 30，且远小于 TTL=90
    p = m_pending(8, 190.0, expect_net=0)
    _set_fake(200.0)
    s = _run_pending(8, [p], rec, None)
    assert len(s["pending_fill"]) == 1, "age<30 应保留 pending"
    assert not rec.has("PENDEXPIRE"), "age<30 不应 PENDEXPIRE"
    print("  [c2] 年轻 pending 保留 OK")


# ===================== 改动3 单测（拒单补 UNCONFIRMED）=====================
def test_c3_reject_unconfirmed():
    """拒单/废单（is_dead + last_msg）⇒ 补 UNCONFIRMED 行，且下游不误当成交。"""
    rec = FakeRec()
    # age=50s（过 FIX_WAIT=30，但 < TTL=90，确保走 _rej 分支而非 TTL 分支）
    p = m_pending(8, 0.0, expect_net=0, order=FakeOrder("FINISHED", 0, True, "废单"))
    _set_fake(50.0)
    s = _run_pending(8, [p], rec, None)
    assert any(r[0] == "UNCONFIRMED" for r in rec.rows), "拒单应补 UNCONFIRMED 行"
    # 下游复盘按类型白名单（START/SIGNAL/FILL/STOPLOSS）解析 ⇒ UNCONFIRMED 被跳过
    assert not any(r[0] == "FILL" for r in rec.rows), "UNCONFIRMED 不得污染 FILL 行"
    print("  [c3] 拒单补 UNCONFIRMED OK（不污染 FILL）")


# ===================== 离线回放（今天豆粕多平 8 手样本）=====================
def test_replay():
    """用今天真实场景重放：pos=0 + pending(豆粕多平8) + 云端8 + 推进200s。
    断言：s[pos]==8，事件含 RECONWAIT→RECONFORCE→接管，且出现 UNCONFIRMED。"""
    import tempfile
    tmp = tempfile.mkdtemp(prefix="d3replay_")
    rec = FakeRec()
    p = m_pending(8, 0.0)                     # 豆粕多平 8 手，报单即本地 pos 置 0
    s = make_s(0, [p], rec, tmp, extra={"_carried": True})
    api = FakeApi(8)                          # 云端仍 8 手（平仓单未成交）
    # t=0：对账 → RECONWAIT（在途委托暂缓）
    _set_fake(0.0)
    M._sync_cloud_pos(api, s, True)
    assert s["pos"] == 0, "t=0 不应接管"
    assert rec.has("RECONWAIT"), "t=0 应 RECONWAIT"
    # t=190：① 对账超时 → RECONFORCE → 接管 pos=8
    #        ② pending 循环 → TTL 到期 → PENDEXPIRE + UNCONFIRMED
    _set_fake(190.0)
    M._sync_cloud_pos(api, s, True)
    assert s["pos"] == 8, f"回放终态 pos 应为 8，实际 {s['pos']}"
    assert rec.has("RECONFORCE"), "应 RECONFORCE"
    M._cloud_fill_stats(api, s)
    assert rec.has("PENDEXPIRE"), "应 PENDEXPIRE"
    assert any(r[0] == "UNCONFIRMED" for r in rec.rows), "应出现 UNCONFIRMED 行"
    seq = [e[0] for e in rec.events]
    assert seq.index("RECONWAIT") < seq.index("RECONFORCE"), "RECONWAIT 先于 RECONFORCE"
    print("  [replay] 离线回放 OK：pos=8, RECONWAIT→RECONFORCE→接管, UNCONFIRMED 已落")


def _set_fake(t):
    _fake[0] = float(t)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    only = a.only
    tests = []
    if only in ("", "c1"):
        tests += [test_c1_timeout_takeover, test_c1_no_timeout_stays_wait]
    if only in ("", "c2"):
        tests += [test_c2_filled_not_killed, test_c2_unfilled_expired, test_c2_young_kept]
    if only in ("", "c3"):
        tests += [test_c3_reject_unconfirmed]
    if only in ("", "replay"):
        tests += [test_replay]
    ok = 0
    for t in tests:
        try:
            t()
            ok += 1
        except Exception as e:
            import traceback
            print(f"  [FAIL] {t.__name__}: {e}")
            traceback.print_exc()
            sys.exit(1)
    print(f"\n全部通过：{ok}/{len(tests)}")
