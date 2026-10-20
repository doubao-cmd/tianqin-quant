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
        """True ⇒ 本交易日拒绝一切新开仓。"""
        return bool(self.st.get("triggered"))

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
        self.st.update({"trade_day": td, "base_day": None, "base_equity": eq,
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
                f"{'已停加仓' if g.get('triggered') else '正常'}"
                + (f" @{g.get('triggered_at')}" if g.get("triggered_at") else ""))

    def reason(self):
        """触发原因文本（写台账用）。"""
        g = self.st
        return (f"当日亏损 {float(g.get('loss') or 0):.0f} ≥ 阈值 "
                f"{float(g.get('threshold') or 0):.0f}"
                f"（基准 {float(g.get('base_equity') or 0):.0f} × {self.ratio:.1%}，"
                f"基准={g.get('base_day') or '?'} 收盘权益）")


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

    print("[selftest] 全部通过")
    return True


if __name__ == "__main__":
    selftest()
