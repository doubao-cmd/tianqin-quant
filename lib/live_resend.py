# -*- coding: utf-8 -*-
"""
撤单重报策略 + 每日撤单预算 + 反手同拍净仓核对（**纯逻辑，不 import tqsdk**）
================================================================================
为什么单独成模块（不写进 ma13_live_multi.py）
--------------------------------------------------------------
  1. 主文件已 85KB 且**正在跑真实仓位**：新逻辑写在里面，只能靠"重启后看结果"
     才知道对不对。抽成纯函数 ⇒ 可离线单测，主文件只留接线。
  2. 撤单是**会被交易所惩罚**的动作（频繁报撤单会被限制交易）。预算必须可测、
     可审计、可复算，不能埋在 4 万行调用栈里。

三项职责
--------------------------------------------------------------
  ① `CancelBudget`  —— 按交易所的每日撤单计数（落盘），逼近上限即拒撤。
       ★ tqsdk 内置 RiskManager 只限**每秒**操作数（risk_rule.py:425
         `_check_rate_limit`），**不管日撤单上限** ⇒ 日计数必须自己实现。
       ★ 交易所上限按**交易日**（夜盘 21:00 属下一交易日），故用 `trading_day()`
         而非日历日，否则 21:00 后计数被错误清零。
  ② `batch_expect()` —— 反手同拍「平旧 + 开新」的净仓核对口径。
  ③ `decide()`      —— 未成交单的处置：等 / 撤 / 重报 / 放弃。

②的机制（这是本项目 2026-10-20 实测 4/4 的误报根因）
--------------------------------------------------------------
  反手在同一拍发出「平旧 8 手」+「开新 8 手」。两张单的 `expect_net` 是各自
  **累计目标**：平仓单 expect=0、开仓单 expect=±8。云端净仓最终是 ±8（新仓），
  拿平仓单的 expect=0 去比 ⇒ 必然报「持仓未变化 ⇒ 判为未成交」。
  ⇒ 正确口径：净仓应等于**同批最后一张单**的累计目标（批内提交顺序即列表顺序，
     最后一张的 expect_net 才是整批结束后的净仓）。
     同时它对"平腿真没成交"仍然有效：净仓会变成 16（旧 8 + 新 8）≠ 8 ⇒ 仍报未成交。
  ★ 上期所平仓拆成 今/昨 两张时，累计目标依次为 pos-4、pos-8，同样取最后一张 ⇒ 兼容。

跑法（离线单测，纯标准库）：
    python -c "from lib import live_resend; live_resend.selftest()"
"""
import json
import os
import datetime as _dt

# ===================== 策略常量（可调，但改动即改变交易行为）=====================
RESEND_WAIT_SEC = 45        # 未成交超过该秒数 ⇒ 进入撤单判断（须 > FIX_WAIT_SEC=30）
RESEND_TICKS = 2            # 重报价在原参考价上再让的跳数（与 LIMIT_TICKS 一致即可）
MAX_RESEND_PER_ORDER = 1    # 同一笔委托最多重报 1 次（不许无限追价）
CANCEL_DAILY_CAP = 300      # 每交易所每日撤单上限（SHFE 硬限 500，取 300 留余量）
CANCEL_MIN_GAP_SEC = 1.0    # 同交易所两次撤单最小间隔，防秒级连撤
QUOTE_STALE_SEC = 180       # 行情超过该秒数未更新 ⇒ 视为休市/断流，不撤不重报


# ===================== 交易日 =====================
def trading_day(now=None):
    """返回交易日 YYYYMMDD（字符串）。

    ★ 夜盘 21:00 起属**下一交易日** ⇒ 20:00 之后先 +1 天，周末顺延。
    ⚠️ 不含法定节假日日历：节前夜盘会多算一天（偏保守，只会少撤不会多撤）。
    """
    d = now or _dt.datetime.now()
    if d.hour >= 20:
        d = d + _dt.timedelta(days=1)
    while d.weekday() >= 5:
        d = d + _dt.timedelta(days=1)
    return d.strftime("%Y%m%d")


# ===================== 每日撤单预算 =====================
class CancelBudget:
    """按交易所的每日撤单计数，落盘持久化（跨重启不丢，防重启绕过上限）。

    path 为空 ⇒ 纯内存（单测用）。文件损坏/不可写一律**按"已用尽"处理**？
    —— 不。按"已用尽"会误锁正常交易（撤单只是补救动作，不是交易本身）。
    这里按"读不到就归零 + 记一次告警"，因为撤单额度只影响补救能力，
    误判为用尽会让该平的仓位一直挂着（更危险）。
    """

    def __init__(self, path=None, cap=CANCEL_DAILY_CAP, gap_sec=CANCEL_MIN_GAP_SEC):
        self.path = path
        self.cap = int(cap)
        self.gap_sec = float(gap_sec)
        self.day = trading_day()
        self.counts = {}        # {exch: n}
        self.last_ts = {}       # {exch: epoch}
        self.load_error = ""
        self._load()

    # ---- 落盘 ----
    def _load(self):
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                j = json.load(f)
            if str(j.get("day")) == self.day:
                self.counts = {str(k): int(v) for k, v in (j.get("counts") or {}).items()}
                self.last_ts = {str(k): float(v) for k, v in (j.get("last_ts") or {}).items()}
            # day 不同 ⇒ 新交易日，天然归零
        except Exception as e:                       # noqa: BLE001
            self.load_error = f"{type(e).__name__}: {e}"
            self.counts, self.last_ts = {}, {}

    def save(self):
        if not self.path:
            return
        try:
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"day": self.day, "counts": self.counts,
                           "last_ts": self.last_ts}, f, ensure_ascii=False)
            os.replace(tmp, self.path)               # 原子替换，不留半截文件
        except Exception:                            # noqa: BLE001
            pass                                     # 落盘失败不影响撤单判断

    def roll_if_needed(self):
        """跨交易日自动归零（进程长跑跨日而不重启）。"""
        td = trading_day()
        if td != self.day:
            self.day, self.counts, self.last_ts = td, {}, {}
            self.save()

    # ---- 判定 ----
    def used(self, exch):
        return int(self.counts.get(str(exch).upper(), 0))

    def allowed(self, exch, now=None):
        """(ok, used, cap, reason)"""
        self.roll_if_needed()
        e = str(exch).upper()
        used = self.used(e)
        if used >= self.cap:
            return False, used, self.cap, f"{e} 当日撤单已达上限 {used}/{self.cap}"
        n = now if now is not None else _now_epoch()
        gap = float(n) - float(self.last_ts.get(e, 0.0))
        if gap < self.gap_sec:
            return False, used, self.cap, f"{e} 撤单间隔 {gap:.2f}s < {self.gap_sec:.0f}s（节流）"
        return True, used, self.cap, ""

    def add(self, exch, now=None):
        self.roll_if_needed()
        e = str(exch).upper()
        self.counts[e] = self.used(e) + 1
        self.last_ts[e] = float(now if now is not None else _now_epoch())
        self.save()
        return self.counts[e]


def _now_epoch():
    import time
    return time.time()


# ===================== 反手同拍净仓口径 =====================
def batch_expect(p, batch, tol_sec=2.0):
    """同一批（同合约、下单时刻差 ≤ tol_sec）里**最后一张单**的 expect_net。

    batch 必须是**按提交顺序**的列表（调用方传 pending_fill 原列表即可）。
    找不到同批伙伴 ⇒ 退回本单自己的 expect_net（=改动前行为，向后兼容）。
    """
    own = p.get("expect_net")
    try:
        sym = p.get("symbol")
        if not sym or not batch:
            return own
        t0 = float(p.get("ts") or 0.0)
        cand = [q for q in batch
                if q is not None and q.get("symbol") == sym
                and abs(float(q.get("ts") or 0.0) - t0) <= float(tol_sec)]
        if not cand:
            return own
        last = cand[-1].get("expect_net")
        return own if last is None else last
    except Exception:                                # noqa: BLE001
        return own


# ===================== 行情新鲜度 =====================
def quote_stale(q, now_epoch=None, max_age=QUOTE_STALE_SEC):
    """行情是否停滞（休市/断流）。读不到时间 ⇒ 视为停滞（保守：不撤不重报）。

    tqsdk Quote.datetime 形如 "2026-10-20 14:59:59.700003"（交易所时间）。
    """
    try:
        dt = str(getattr(q, "datetime", "") or "")
        if len(dt) < 19:
            return True
        t = _dt.datetime.strptime(dt[:19], "%Y-%m-%d %H:%M:%S")
        now = _dt.datetime.fromtimestamp(now_epoch) if now_epoch else _dt.datetime.now()
        return (now - t).total_seconds() > float(max_age)
    except Exception:                                # noqa: BLE001
        return True


# ===================== 处置决策 =====================
def decide(p, now, budget, market_live=True, resend_wait=RESEND_WAIT_SEC):
    """未成交 pending 的处置。返回 (action, note)。

    action ∈ {"wait", "cancel", "resend", "giveup"}
      wait   —— 保持挂起，下拍再看（撤单指令已发出但未生效 / 还没到超时）
      cancel —— 现在发撤单
      resend —— 撤单已生效，按未成交量重报
      giveup —— 不再处置，交回原来的「记未成交」记账路径

    ★ 状态机记在 p["rs"] 里（dict，随 pending_fill 一起持久化到状态文件）：
        {}                 → 从未撤过
        {"cancel_ts": t}   → 撤单已发出，等 status 变 FINISHED
        {"done": True}     → 已重报过一次，不再动
    """
    rs = p.setdefault("rs", {})
    o = p.get("order")
    st = str(getattr(o, "status", "") or "")
    vleft = int(getattr(o, "volume_left", 0) or 0)

    if rs.get("done"):
        return "giveup", "已重报过一次，仍无成交"
    if rs.get("cancel_ts"):
        if st == "FINISHED" and vleft > 0:
            return "resend", f"撤单生效，未成交 {vleft} 手"
        if st == "FINISHED" and vleft == 0:
            return "giveup", "撤单前已全部成交"
        return "wait", "撤单指令待生效"
    if not market_live:
        return "giveup", "行情停滞（休市/断流），不撤单也不重报"
    if o is None:
        return "giveup", "无委托句柄（旧版本挂的单），不处置"
    if not p.get("symbol"):
        return "giveup", "缺 symbol（升级前挂的单），无从撤单，不处置"
    if st != "ALIVE":
        return "giveup", f"委托非活动态 status={st or '?'}"
    if vleft <= 0:
        return "giveup", "无可撤数量"
    try:
        elapsed = float(now) - float(p.get("ts") or 0.0)
    except Exception:                                # noqa: BLE001
        elapsed = 0.0
    if elapsed < float(resend_wait):
        return "wait", f"未成交 {elapsed:.0f}s < {resend_wait}s，继续等"
    exch = str(p.get("symbol") or "").split(".")[0].upper()
    ok, used, cap, why = budget.allowed(exch, now=now)
    if not ok:
        return "giveup", why
    return "cancel", f"未成交 {elapsed:.0f}s，撤单重报（{exch} 已撤 {used}/{cap}）"


def resend_target(p, vleft):
    """重报单的目标净仓 = 原单目标（未成交 ⇒ 目标未达成，不变）。"""
    return p.get("expect_net")


# ===================== 自检 =====================
def selftest():
    """纯标准库自检（不连网、不 import tqsdk）。"""
    import tempfile
    print("[selftest] live_resend.py")
    d = tempfile.mkdtemp(prefix="rs_")

    # 1) 交易日：20:00 之后算下一交易日，周末顺延
    assert trading_day(_dt.datetime(2026, 10, 20, 14, 0)) == "20261020"
    assert trading_day(_dt.datetime(2026, 10, 20, 21, 0)) == "20261021"
    assert trading_day(_dt.datetime(2026, 10, 23, 21, 0)) == "20261026"   # 周五夜盘→周一
    print("  1 交易日 OK")

    # 2) 预算：计数、上限、节流、落盘恢复
    bp = os.path.join(d, "budget.json")
    b = CancelBudget(bp, cap=3, gap_sec=1.0)
    assert b.allowed("SHFE", now=1000.0)[0]
    b.add("SHFE", now=1000.0); assert b.used("SHFE") == 1
    assert not b.allowed("SHFE", now=1000.5)[0]          # 节流
    assert b.allowed("SHFE", now=1002.0)[0]
    b.add("SHFE", now=1002.0); b.add("SHFE", now=1004.0)
    ok, used, cap, why = b.allowed("SHFE", now=1006.0)
    assert not ok and used == 3 and "上限" in why
    b2 = CancelBudget(bp, cap=3, gap_sec=1.0)            # 模拟重启
    assert b2.used("SHFE") == 3, "重启后计数丢失，上限可被重启绕过"
    assert not b2.allowed("SHFE", now=2000.0)[0]
    print("  2 撤单预算 OK")

    # 3) 反手同拍：净仓应比"同批最后一张"的累计目标
    pend = [
        {"symbol": "SHFE.bu2611", "ts": 100.0, "expect_net": 0, "side": "平", "dir": "多"},
        {"symbol": "SHFE.bu2611", "ts": 100.1, "expect_net": 8, "side": "开", "dir": "多"},
    ]
    assert batch_expect(pend[0], pend) == 8, "反手同拍口径错 ⇒ 平仓腿必误报未成交"
    assert batch_expect(pend[1], pend) == 8
    # 单腿平仓（无同批伙伴）⇒ 退回自身目标
    solo = [{"symbol": "DCE.m2701", "ts": 200.0, "expect_net": 0}]
    assert batch_expect(solo[0], solo) == 0
    # 上期所拆单：今4 + 昨4，累计目标 4 → 0
    split = [
        {"symbol": "SHFE.fu2611", "ts": 300.0, "expect_net": 4, "offset": "CLOSETODAY"},
        {"symbol": "SHFE.fu2611", "ts": 300.2, "expect_net": 0, "offset": "CLOSE"},
    ]
    assert batch_expect(split[0], split) == 0 and batch_expect(split[1], split) == 0
    print("  3 反手同拍口径 OK")

    # 4) 处置状态机
    class O:
        def __init__(self, status="ALIVE", left=8):
            self.status, self.volume_left, self.is_dead, self.last_msg = status, left, False, ""
    budget = CancelBudget(None, cap=300)
    base = {"symbol": "SHFE.bu2611", "ts": 1000.0, "expect_net": 0}
    p = dict(base, order=O())
    assert decide(p, 1013.0, budget)[0] == "wait"                      # 未到 45s
    assert decide(p, 1060.0, budget)[0] == "cancel"                    # 超时可撤
    p["rs"]["cancel_ts"] = 1060.0
    assert decide(p, 1062.0, budget)[0] == "wait"                      # 撤单未生效
    p["order"] = O("FINISHED", 8)
    assert decide(p, 1064.0, budget)[0] == "resend"                    # 撤成功，重报 8 手
    p["rs"] = {"done": True}
    assert decide(p, 1070.0, budget)[0] == "giveup"                    # 只重报一次
    p2 = dict(base, order=O(), rs={})
    assert decide(p2, 1070.0, budget, market_live=False)[0] == "giveup"  # 休市不撤
    # 额度用尽 ⇒ 不撤（正是红线要求的"逼近上限即停手"）
    _b1 = CancelBudget(None, cap=1)
    _b1.add("SHFE", now=1000.0)
    act, note = decide(dict(base, order=O(), rs={}), 3000.0, _b1)
    assert act == "giveup" and "上限" in note, f"额度用尽仍要撤：{act} {note}"
    print("  4 处置状态机 OK")

    # 5) 行情停滞判定
    class Q:
        def __init__(self, s):
            self.datetime = s
    now = _dt.datetime(2026, 10, 20, 15, 0, 0).timestamp()
    assert quote_stale(Q("2026-10-20 14:59:59.700003"), now) is False
    assert quote_stale(Q("2026-10-20 14:50:00.000000"), now) is True
    assert quote_stale(Q(""), now) is True
    print("  5 行情新鲜度 OK")

    print("[selftest] 全部通过")


if __name__ == "__main__":
    selftest()
