# -*- coding: utf-8 -*-
"""A21 · #19-C 陈旧 bar 守卫（2026-10-22 D2 裁定）

背景（实测，P1）：核心重启后 `last_id` 为 −1，把**最后一根已收盘的 bar** 当新 bar
重放，按过期价报单。2026-10-21 18:03:41：BU+8 / EG-7 依据 14:59:59.8 的报价，在
**收盘后 3 小时**成交，实亏 −8,240（其中约 640 元属缺陷额外成本）。

修法 C（数据驱动，不做交易日历）：
    now − bar **结束**时间 > 2 × bar 周期  ⇒  该 bar 只记账，不产生任何报单
    5min ⇒ 600s；阈值从参数取（`KSEC` / `s["ksec"]`），不硬编码。

★ 判**结束**时间的原因：tqsdk `objs.py:231` 明写 `Kline.datetime` 是「K线**起点**时间」。
  拿起点当结束，每根正常 bar 一出现就已"陈旧 300s"，会把全天交易拦死。

★ 与 B 并存：B = 启动首根不处理（`skip_first`，见 A20）；C = 时间对不上就不报单。
  两者防的是不同缺口，本测试不替代 A20。
"""
import os
import sys
import tempfile
import datetime as _dt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ma13_live_multi as M   # noqa: E402

FAIL = []


def _ok(cond, msg):
    if cond:
        print(f"  PASS  {msg}")
    else:
        print(f"  FAIL  {msg}")
        FAIL.append(msg)


class _Q:
    """q 的最小替身：守卫只用它取触发价。"""
    last_price = 5000.0
    datetime = "2026-10-21 18:03:41.000000"


def _bar(start_dt, bar_id=100):
    """构造一根 bar。tqsdk 的 datetime = 起点、unix epoch 纳秒（UTC 基准）。

    这里用本地时间构造 epoch 再乘 1e9 —— 与 tqsdk 同基准（都是真 epoch 秒），
    所以 `now − 结束` 的差值不受时区影响（守卫本身就是这么算的）。
    """
    return {"id": bar_id, "datetime": int(start_dt.timestamp() * 1e9)}


def _state(rec=None, pos=0, ksec=None, last_id=-1):
    s = {"last_id": last_id, "skip_first": False, "pos": pos,
         "name": "测试品种", "q": _Q(), "rec": rec}
    if ksec is not None:
        s["ksec"] = ksec
    return s


def _old_gate(s, bar_id):
    """C 之前的判定（B 版：只有 last_id + skip_first）。用于复现 bug。"""
    try:
        bid = int(bar_id)
    except Exception:
        return False
    if bid == s.get("last_id") or bid < 0:
        return False
    s["last_id"] = bid
    if s.get("skip_first"):
        s["skip_first"] = False
        return False
    return True


def _ledger(tmp, name="测试品种"):
    p = os.path.join(tmp, name + "_成交流水.csv")
    if not os.path.exists(p):
        return ""
    with open(p, encoding="utf-8-sig") as f:
        return f.read()


print("=" * 72)
print("A21 · #19-C 陈旧 bar 守卫（阈值 = 2 × 周期）")
print("=" * 72)

NOW = _dt.datetime(2026, 10, 21, 18, 3, 41).timestamp()   # 18:03:41，真实事故时刻
P = M._bar_period_sec({})
_ok(P == 300, f"t0 周期从参数取 = {P}s（KSEC，不硬编码 600）")

tmp = tempfile.mkdtemp()
rec = M._LiveRecorder(tmp, "测试品种")

# ---- t1 新鲜 bar（差 0s）：放行 ----
s = _state(rec)
b = _bar(_dt.datetime.fromtimestamp(NOW - P), 100)        # 结束时刻 = now
r = M._bar_gate(s, 100, b, NOW)
_ok(r is True, f"t1 新鲜 bar（差 0s）放行（返回 {r}）")
_ok("STALEBAR" not in _ledger(tmp), "t1 未落 STALEBAR")

# ---- t2 差 599s（边界内）：放行 ----
s = _state(rec)
b = _bar(_dt.datetime.fromtimestamp(NOW - P - 599), 101)
r = M._bar_gate(s, 101, b, NOW)
_ok(r is True, f"t2 差 599s < 601s 阈值 ⇒ 放行（返回 {r}）")

# ---- t3 差 601s（边界外）：拦 + 落 STALEBAR ----
s = _state(rec)
b = _bar(_dt.datetime.fromtimestamp(NOW - P - 601), 102)
r = M._bar_gate(s, 102, b, NOW)
_ok(r is False, f"t3 差 601s > 600s ⇒ 拦（返回 {r}）")
_ok(s["last_id"] == 102, f"t3 但 last_id 已记账 = {s['last_id']}（陈旧 bar 被消费，不会下一拍再触发）")
_led = _ledger(tmp)
_ok("STALEBAR" in _led, "t3 STALEBAR 已落台账")
_line = [x for x in _led.splitlines() if "STALEBAR" in x]
_ok(bool(_line) and "只记账不报单" in _line[-1], "t3 台账行含「只记账不报单」")
_ok(bool(_line) and "bar_end=" in _line[-1] and "差=" in _line[-1],
    f"t3 台账行含 bar_end/差秒：{_line[-1][:120] if _line else 'NONE'}")

# ---- t4 差 3 小时（2026-10-21 18:03 真实场景）：拦 ----
s = _state(rec)
b14 = _bar(_dt.datetime(2026, 10, 21, 14, 55), 103)       # 14:55 起 / 15:00 收
r = M._bar_gate(s, 103, b14, NOW)
_st = M._stale_info(_state(), b14, NOW)
_ok(r is False, f"t4 收盘后 3 小时的真实场景 ⇒ 拦（返回 {r}）")
_ok(_st is not None and _st["diff"] > 10000,
    f"t4 差 = {_st['diff']:.0f}s（14:55 起 / 15:00 收，now 18:03）")

# ---- t5 拦的是出场：持仓 + 陈旧 bar ⇒ 拦，且不改 pos ----
s = _state(rec, pos=5)
b = _bar(_dt.datetime.fromtimestamp(NOW - P - 4000), 104)
r = M._bar_gate(s, 104, b, NOW)
_ok(r is False, f"t5 持仓时陈旧 bar 同样拦（返回 {r}）")
_ok(s["pos"] == 5, f"t5 不改 pos（仍 {s['pos']} 手）⇒ 出场未被陈旧价执行")
_ok("动作=平" in _ledger(tmp), "t5 台账标「动作=平」（持仓时的被拦动作）")

# ---- t6 周期 15min ⇒ 阈值 1800s（不硬编码）----
s = _state(rec, ksec=900)
_ok(M._bar_period_sec(s) == 900, f"t6 周期随参数变 = {M._bar_period_sec(s)}s")
b = _bar(_dt.datetime.fromtimestamp(NOW - 900 - 1500), 105)   # 差 1500s
r = M._bar_gate(s, 105, b, NOW)
_ok(r is True, f"t6 差 1500s < 2×900=1800s ⇒ 放行（返回 {r}）")
s = _state(rec, ksec=900)
b = _bar(_dt.datetime.fromtimestamp(NOW - 900 - 1801), 106)   # 差 1801s
r = M._bar_gate(s, 106, b, NOW)
_ok(r is False, f"t6 差 1801s > 1800s ⇒ 拦（返回 {r}）")

# ---- t7 用旧逻辑跑 t4：必须复现 bug（证明本测试有效）----
s_old = _state(None, last_id=-1)
r_old = _old_gate(s_old, 103)
_ok(r_old is True, f"t7 旧逻辑会把 3 小时前的 bar 放行（返回 {r_old}）⇒ bug 可复现")

# ---- 附：bar=None 时行为与 B 完全一致（A20 兼容）----
s = _state(None)
_ok(M._bar_gate(s, 200) is True, "附 bar=None ⇒ 不做陈旧判定（A20 用例不受影响）")

print("=" * 72)
if FAIL:
    print(f"失败 {len(FAIL)} 项：")
    for m in FAIL:
        print("  -", m)
    sys.exit(1)
print("7/7 通过")
