# -*- coding: utf-8 -*-
"""
单日亏损上限阀（P1-1「日内停加仓阀」）—— **纯逻辑，不 import tqsdk**
================================================================================
为什么单独成模块
--------------------------------------------------------------
主文件 92KB 且**正在跑真实仓位**：逻辑写进去只能靠"重启后看结果"验证。
抽成纯函数 ⇒ 可离线单测（含"重启不清零"这种必须复现的场景）。

口径（2026-10-20 19:20 用户/D 拍板，**执行员不得改数**）
--------------------------------------------------------------
  · 基准 = **上一交易日收盘权益**（口径 B：`EQUITY` 末行）
  · 当日亏损 = 基准权益 − 当前权益（含浮动的权益，故已含未实现盈亏）
  · 阈值 = 基准权益 × **1.5%**
  · 交易日边界：夜盘 21:00 起算下一交易日（与 `live_resend.trading_day` 同一定义，
    实际取 ≥20:00 的保守边界，见该函数注释）
  · 复位：新交易日第一次快照到达时，基准切到「上一交易日的最后一次权益」并解除闩锁

它**不是亏损封顶**（写进注释，别当 bug 修）
--------------------------------------------------------------
已有持仓仍可能继续亏 —— 真正的兜底是 20% 灾难阀（`DD_FUSE`）。
本阀只做一件事：当日亏损触阈后**拒绝一切新开仓**，
不改止损、不提前平仓、不减仓（那属于已判死的「提前出场家族」，是禁区）。

为什么必须落盘
--------------------------------------------------------------
若状态只在内存，任何人重启一次就绕过风控 ⇒ 等同未实现（任务书 §二 P1-1 明令）。
状态文件与 `熔断状态.json` **并列且互不覆盖**：
  · 本阀 = 每日自动复位（`单日亏损状态.json`）
  · 熔断 = 人工删文件才复位（`熔断状态.json`）

跑法（离线单测，纯标准库）：
    python -c "from lib import daily_loss; daily_loss.selftest()"
"""
import datetime as _dt
import json
import os
import re
import time as _time

try:                                       # 包内导入（正常路径）
    from . import live_resend as _rs      # 复用同一定义，禁止第二份交易日历
except ImportError:                        # 直接 `python lib/daily_loss.py` 时
    import live_resend as _rs             # noqa: F401

# ===================== 常量（D 拍板，改动即改变风控行为）=====================
DAILY_LOSS_RATIO = 0.015      # 当日亏损阈值 = 基准权益 × 1.5%（≈ −14,900 元 @ 99.1 万）
STATE_NAME = "单日亏损状态.json"


# ---------------------------------------------------------------- 工具
def day_start(trade_day):
    """某交易日的**起点时刻**（= 前一自然日 20:00），与 `trading_day()` 严格配套。

    `trading_day()` 的规则是「≥20:00 算下一交易日 ⇒ 交易日 D 从 D−1 日 20:00 起」。
    这里返回同一个边界，用于「取上一交易日收盘权益」时切一刀。
    """
    d = _dt.datetime.strptime(str(trade_day), "%Y%m%d") - _dt.timedelta(days=1)
    return d.replace(hour=20, minute=0, second=0, microsecond=0)


def last_equity_before(csv_path, cutoff, tail_bytes=4 << 20, max_scan=200000):
    """从台账 `<品种>_成交流水.csv` 里取 **cutoff 之前最后一条 EQUITY 的权益**。

    为什么读台账而不是另存一份：`EQUITY` 行是本进程唯一"真实权益"记录，
    权益基准只能从它来（任务书 §二 P1-1 明写「`EQUITY` 末行」）。
    只读文件尾 `tail_bytes` 字节：文件可能上万行，全量扫没必要。

    返回 (equity: float|None, ts: str|None)。任何异常一律 (None, None) ——
    读不到就由调用方按"播种"处理，**绝不编造一个基准**。
    """
    if not csv_path or not os.path.exists(csv_path):
        return None, None
    try:
        with open(csv_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - int(tail_bytes)))
            raw = f.read()
        text = raw.decode("utf-8-sig", errors="ignore")
        # 丢弃首行（可能是半行）
        lines = text.splitlines()[1:] if size > tail_bytes else text.splitlines()
        hit = (None, None)
        for ln in lines[-int(max_scan):]:
            if not ln.startswith("EQUITY,"):
                continue
            parts = ln.split(",")
            if len(parts) < 2:
                continue
            ts = parts[1].strip()
            try:
                t = _dt.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
            if t >= cutoff:
                continue
            m = re.search(r"权益=(-?[\d.]+)", ln)
            if not m:
                continue
            try:
                hit = (float(m.group(1)), ts)
            except Exception:
                continue
        return hit
    except Exception:
        return None, None


# ---------------------------------------------------------------- 主体
class DailyLossGuard:
    """账户级「日内停加仓阀」。**只回答一个问题：今天还能不能开新仓。**

    用法：
        g = DailyLossGuard(path)
        g.update(bal)        # 每个节拍调；返回 True=已触阈
        if g.blocked: ...    # 拒绝新开仓（平仓/止损/撤单永不放这里判断）
    """

    def __init__(self, path=None, ratio=DAILY_LOSS_RATIO):
        self.path = path
        self.ratio = float(ratio)
        self._last_save = 0.0
        self.st = {
            "trade_day": None,        # 当前交易日 YYYYMMDD
            "base_day": None,         # 基准来自哪个交易日
            "base_equity": None,      # 基准权益（上一交易日收盘权益）
            "cur_equity": None,       # 最近一次看到的权益
            "loss": None,             # 基准 − 当前（>0 = 亏损）
            "threshold": None,        # 基准 × ratio
            "triggered": False,       # 闩锁
            "triggered_at": None,
            "base_src": None,         # tape=从台账取 / state=跨日沿用 / seed=无历史播种
            "update": None,
        }
        self._load()

    # ---------- 落盘 ----------
    def _load(self):
        if not self.path:
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                for k in self.st:
                    if k in d:
                        self.st[k] = d[k]
        except Exception:
            pass                      # 无文件/损坏 = 全新状态（不因状态问题挡启动）

    def _save(self):
        if not self.path:
            return
        try:
            self.st["update"] = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.st, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except Exception:
            pass                      # 状态存不上不能影响交易

    # ---------- 语义 ----------
    @property
    def blocked(self):
        """True ⇒ 本交易日拒绝一切新开仓。

        正常路径看落盘的 `triggered`；`_drill` 是 `--drill-dailyloss` 演练用的
        **纯内存**闩锁，不写状态文件、重启即消失 —— 演练不需要回滚任何文件。
        """
        return bool(self.st.get("triggered")) or bool(getattr(self, "_drill", False))

    def force_blocked(self, note="[DRILL] 演练：强制置为已触阀（真实亏损未达阈值）"):
        """★ 2026-10-21 #16：演练专用 —— 只在**内存**里把阀置为已触发。

        刻意**不落盘**（不调用 `_save()`、不改 `self.st`）：
          · 重启即恢复，演练结束不用手工复位任何文件；
          · 不会把 `triggered=true` 写进 `单日亏损状态.json` 污染真实风控状态。
        用途：在没有真实亏损的情况下验证「触阈后只拒新开仓、止损不动、平仓放行」。
        """
        self._drill = True
        self._drill_note = str(note)
        return True

    def clear_drill(self):
        """撤掉演练闩锁（供离线测试复位，线上从不调用）。"""
        self._drill = False
        self._drill_note = None

    def seed(self, equity, trade_day=None, src="tape"):
        """首次启动（无状态文件）时用「上一交易日收盘权益」播种。

        只在 base_equity 还是 None 时生效；已有状态一律不动（防把已触发的阀冲掉）。
        """
        if self.st.get("base_equity"):
            return False
        try:
            eq = float(equity)
        except Exception:
            return False
        if not (eq == eq and eq > 0):
            return False
        td = str(trade_day or _rs.trading_day())
        # ★ A10（2026-10-21 执行员）：base_day 必须落盘，否则跨日/重启无法自证
        #   「基准权益来自哪一天」。原实现无条件写 None，导致线上状态长期 base_day=null。
        #   · src="tape"：权益取自上一交易日收盘 ⇒ base_day = 上一交易日
        #   · 其他（无历史播种）：基准 = 当日首个快照 ⇒ base_day = 当日
        if src == "tape":
            base_day = _rs.trading_day(day_start(td) - _dt.timedelta(minutes=1))
        else:
            base_day = td
        self.st.update({"trade_day": td, "base_day": base_day, "base_equity": eq,
                        "threshold": eq * self.ratio, "base_src": src})
        self._save()
        return True

    def update(self, equity, now=None):
        """喂一次当前权益；返回是否已触阈（True=已闩锁）。

        非有限/非正权益直接忽略（读不到余额不是"没亏"）。
        落盘节流：交易日切换/触发立即写，其余最多每 60s 一次
        （权益每 tick 都在变，不节流会把磁盘写爆、也拖慢主循环）。
        """
        try:
            eq = float(equity)
        except Exception:
            return self.blocked
        if not (eq == eq) or eq <= 0:
            return self.blocked

        now_ts = _time.time()
        td = _rs.trading_day(now)
        prev_td = self.st.get("trade_day")
        if prev_td != td:
            # ---- 交易日切换：基准 = 上一交易日最后一次权益，闩锁自动解除 ----
            prev_eq = self.st.get("cur_equity") or self.st.get("base_equity")
            self.st["trade_day"] = td
            self.st["base_day"] = prev_td
            self.st["triggered"] = False
            self.st["triggered_at"] = None
            if prev_eq:
                self.st["base_equity"] = float(prev_eq)
                self.st["base_src"] = "state"
            # 无历史权益 ⇒ base 保持 None，等 seed()/首拍播种
            if self.st.get("base_equity"):
                self.st["threshold"] = float(self.st["base_equity"]) * self.ratio
            self._save()
            self._last_save = now_ts

        if not self.st.get("base_equity"):
            # 首个快照且无基准（新装机/状态被删）：就地播种，loss=0 不触发
            self.st["base_equity"] = eq
            self.st["threshold"] = eq * self.ratio
            self.st["base_src"] = self.st.get("base_src") or "seed"

        base = float(self.st["base_equity"])
        thr = float(self.st.get("threshold") or base * self.ratio)
        loss = base - eq
        self.st["cur_equity"] = eq
        self.st["loss"] = loss
        self.st["threshold"] = thr

        if not self.st.get("triggered") and thr > 0 and loss >= thr:
            self.st["triggered"] = True
            self.st["triggered_at"] = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._save()
            self._last_save = now_ts
            return True
        if now_ts - getattr(self, "_last_save", 0) >= 60:
            self._save()
            self._last_save = now_ts
        return self.blocked

    # ---------- 展示 ----------
    def summary(self):
        """一行摘要（台账/日志用）：基准/当前/差额/阈值/是否触发。"""
        g = self.st
        f = lambda v: ("nan" if v is None else f"{float(v):.0f}")   # noqa: E731
        return (f"交易日={g.get('trade_day')} 基准={f(g.get('base_equity'))}"
                f"(来自{g.get('base_day') or '上一交易日'}/{g.get('base_src') or '?'}) "
                f"当前={f(g.get('cur_equity'))} 差额={f(g.get('loss'))} "
                f"阈值={f(g.get('threshold'))}({self.ratio:.2%}) "
                f"{'已停加仓' if self.blocked else '正常'}"
                + (" [DRILL]" if getattr(self, "_drill", False) else "")
                + (f" @{g.get('triggered_at')}" if g.get("triggered_at") else ""))

    def reason(self):
        """触发原因文本（写台账用）。演练时带 `[DRILL]` 前缀，与真实触发可区分。"""
        g = self.st
        _t = (f"当日亏损 {float(g.get('loss') or 0):.0f} ≥ 阈值 "
              f"{float(g.get('threshold') or 0):.0f}"
              f"（基准 {float(g.get('base_equity') or 0):.0f} × {self.ratio:.1%}，"
              f"基准={g.get('base_day') or '?'} 收盘权益）")
        if getattr(self, "_drill", False):
            return f"{getattr(self, '_drill_note', '[DRILL] 演练')} | {_t}"
        return _t

    # ---------- A10 自证（2026-10-20 派单 T3）----------
    def self_check(self, tape_csv=None):
        """A10：单日亏损基准可自证。返回 (ok: bool, msg: str)。

        判据（派单 T3 §A10）：
          · `base_day` 非空 且 == 上一交易日（当前 `trade_day` 的前一个交易日）；
          · 若给定 `tape_csv`，则 `base_equity` 须能由该日 EQUITY 末行复现（容差 0.01）。
        只读、不写、不抛 —— 调用方（live 进程）只应记日志，不得据此改变交易行为。
        """
        g = self.st
        td = g.get("trade_day")
        bd = g.get("base_day")
        if not td:
            return False, "trade_day 为空（状态未初始化）"
        if not bd:
            return False, "base_day 为空（尚未跨日/未播种）"
        # 上一交易日 = trading_day(day_start(td) - 1min)
        prev = _rs.trading_day(day_start(str(td)) - _dt.timedelta(minutes=1))
        # 无历史播种（base_src=seed）时基准是当日首个快照 ⇒ 期望 base_day=当日，
        # 不是上一交易日（否则会把合法的退化状态误判为不自证）。
        exp = str(td) if str(g.get("base_src")) == "seed" else str(prev)
        if str(bd) != exp:
            return False, f"base_day={bd} != 期望 {exp}（base_src={g.get('base_src')}）"
        if tape_csv:
            eq, ts = last_equity_before(tape_csv, day_start(str(td)))
            if eq is None:
                return False, f"台账找不到 base_day={bd} 收盘权益（cutoff={day_start(str(td))}）"
            diff = abs(eq - float(g.get("base_equity") or 0))
            if diff > 0.01:
                return False, f"base_equity={g.get('base_equity')} 与台账 {ts}={eq} 差 {diff:.4f} > 0.01"
        return True, f"base_day={bd} 上一交易日吻合，基准可自证"


# ---------------------------------------------------------------- 自检
def selftest():
    """离线自检：阈值/跨日复位/重启不清零/边界。纯标准库。"""
    import tempfile
    print("[selftest] daily_loss.py（纯逻辑，无网络）")
    d = tempfile.mkdtemp(prefix="dloss_")
    p = os.path.join(d, STATE_NAME)

    # 1) 基准播种 + 未触阈
    g = DailyLossGuard(p)
    D1 = _dt.datetime(2026, 10, 19, 9, 0)
    g.update(1_000_000.0, now=D1)
    assert g.st["base_equity"] == 1_000_000.0
    assert g.st["threshold"] == 15_000.0, g.st["threshold"]
    assert not g.blocked
    print("  1 播种/阈值 1.5% OK")

    # 2) 亏 14,000（<1.5%）不触发
    assert g.update(986_000.0, now=D1) is False
    assert not g.blocked

    # 3) 亏到 15,000（==阈值）触发（>= 语义，判平从严）
    assert g.update(985_000.0, now=D1) is True
    assert g.blocked and g.st["triggered_at"]
    print("  2 触阈（==边界即触发）OK")

    # 4) 触发后权益反弹也不自动解除（当日闩死）
    assert g.update(1_001_000.0, now=D1) is True
    assert g.blocked, "当日权益反弹不应自动解除"
    g._save()                                 # 强制落盘：下一拍未触阈被节流跳过，保证 case5 重载一致
    print("  3 当日不可自动解除 OK")

    # 5) 重启不清零：新进程从同一文件加载，仍然 blocked
    g2 = DailyLossGuard(p)
    assert g2.blocked, "重启后闩锁丢失 = 未实现（任务书明令）"
    assert g2.st["base_equity"] == 1_000_000.0
    print("  4 重启不清零 OK")

    # 6) 跨交易日自动复位，且基准切到「上一交易日最后一次权益」
    D2 = _dt.datetime(2026, 10, 20, 9, 0)
    assert g2.update(1_001_000.0, now=D2) is False, "新交易日应自动复位"
    assert not g2.blocked
    assert g2.st["base_equity"] == 1_001_000.0, g2.st["base_equity"]
    assert g2.st["base_day"] == "20261019"
    print("  5 跨日复位 + 基准切昨收 OK")

    # 7) 夜盘 21:00 属下一交易日（边界一致）
    assert _rs.trading_day(_dt.datetime(2026, 10, 20, 21, 30)) == "20261021"
    assert day_start("20261021") == _dt.datetime(2026, 10, 20, 20, 0)
    print("  6 夜盘/交易日边界 OK")

    # 8) NaN/负数权益被忽略（读不到不是"没亏"）
    before = dict(g2.st)
    g2.update(float("nan"), now=D2)
    g2.update(-1.0, now=D2)
    assert g2.st["cur_equity"] == before["cur_equity"]
    print("  7 非法权益忽略 OK")

    # 9) seed 不覆盖已有基准
    assert g2.seed(555_000.0) is False
    assert g2.st["base_equity"] == 1_001_000.0
    print("  8 seed 不覆盖已有基准 OK")

    # 10) 从台账取「上一交易日收盘权益」
    csv = os.path.join(d, "甲醇_成交流水.csv")
    with open(csv, "w", encoding="utf-8-sig") as f:
        f.write("类型,记录时间,K线时间,方向,手数,成交价,参考价,备注\n")
        f.write("START,2026-10-19 09:00:00,,,,,,\n")
        f.write("EQUITY,2026-10-19 14:59:00,,,,,,权益=1000000.00 持仓=0\n")
        f.write("EQUITY,2026-10-19 19:59:00,,,,,,权益=998000.00 持仓=0\n")
        f.write("EQUITY,2026-10-20 09:01:00,,,,,,权益=990000.00 持仓=0\n")
    eq, ts = last_equity_before(csv, day_start("20261020"))
    assert eq == 998_000.0 and ts == "2026-10-19 19:59:00", (eq, ts)
    print("  9 台账取昨收权益 OK")

    # 11) A10 自证（独立构造，避免依赖 g2 节流的落盘状态）
    d2 = tempfile.mkdtemp(prefix="a10_")
    p2 = os.path.join(d2, STATE_NAME)
    g3 = DailyLossGuard(p2)
    g3.update(1_000_000.0, now=D1)            # 播种 base=1,000,000, td=20261019
    g3.update(1_001_000.0, now=D1)            # 当日末权益 1,001,000
    g3.update(1_001_000.0, now=D2)            # 跨日 → base_day=20261019, base=1,001,000
    assert g3.st["base_day"] == "20261019", g3.st["base_day"]
    assert g3.st["base_equity"] == 1_001_000.0, g3.st["base_equity"]
    tape = os.path.join(d2, "M_成交流水.csv")
    with open(tape, "w", encoding="utf-8-sig") as f:
        f.write("类型,记录时间,K线时间,方向,手数,成交价,参考价,备注\n")
        f.write("EQUITY,2026-10-19 14:59:00,,,,,,权益=1001000.00 持仓=0\n")
    ok, msg = g3.self_check(tape_csv=tape)
    assert ok, f"A10 自证应过：{msg}"
    # 台账篡改 ⇒ 应失败
    with open(tape, "w", encoding="utf-8-sig") as f:
        f.write("类型,记录时间,K线时间,方向,手数,成交价,参考价,备注\n")
        f.write("EQUITY,2026-10-19 14:59:00,,,,,,权益=999000.00 持仓=0\n")
    ok2, _ = g3.self_check(tape_csv=tape)
    assert not ok2, "台账权益与基准差>0.01 应判不自证"
    print("  10 A10 基准自证（台账吻合/篡改失败）OK")

    # 12) A10 播种路径 base_day：src=tape ⇒ 上一交易日（原实现无条件写 None）
    g4 = DailyLossGuard(None)
    assert g4.seed(1_000_000.0, trade_day="20261020", src="tape") is True
    assert g4.st["base_day"] == "20261019", g4.st["base_day"]
    ok4, msg4 = g4.self_check()
    assert ok4, f"A10 tape 播种自证应过：{msg4}"

    # 13) 无历史播种（seed）：基准=当日首拍 ⇒ base_day=当日（退化但自洽）
    g5 = DailyLossGuard(None)
    assert g5.seed(1_000_000.0, trade_day="20261020", src="seed") is True
    assert g5.st["base_day"] == "20261020", g5.st["base_day"]
    ok5, msg5 = g5.self_check()
    assert ok5, f"A10 seed 播种自证应过：{msg5}"
    print("  11 A10 播种路径 base_day（tape/seed）OK")

    # 12) #16 --drill-dailyloss 演练闩锁：只在内存、不落盘、重启即消失
    d3 = tempfile.mkdtemp(prefix="drill_")
    p3 = os.path.join(d3, STATE_NAME)
    g6 = DailyLossGuard(p3)
    g6.update(1_000_000.0, now=D1)             # 正常状态，远未触阈
    assert not g6.blocked
    g6.force_blocked()
    assert g6.blocked, "演练闩锁未生效"
    assert g6.st.get("triggered") is False, "演练不得改写 triggered —— 会污染真实风控状态"
    assert "[DRILL]" in g6.reason(), f"演练原因缺 [DRILL] 标记：{g6.reason()}"
    g6.update(1_000_000.0, now=D1)             # 走一次正常 update（内部可能触发节流落盘）
    g6._save()                                 # 强制落盘，验证演练态不会被写进文件
    with open(p3, "r", encoding="utf-8") as f:
        _disk = json.load(f)
    assert _disk.get("triggered") is not True, "演练态被写进状态文件 ⇒ 会污染线上风控"
    g7 = DailyLossGuard(p3)                    # 模拟重启
    assert not g7.blocked, "演练闩锁落盘了 ⇒ 重启后仍拦新开仓（演练污染线上）"
    g6.clear_drill()
    assert not g6.blocked, "撤掉演练闩锁后应恢复"
    print("  12 演练闩锁（内存/不落盘/重启消失）OK")

    print("[selftest] 全部通过")
    return True


if __name__ == "__main__":
    selftest()
