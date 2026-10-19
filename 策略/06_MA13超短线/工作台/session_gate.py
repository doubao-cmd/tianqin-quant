# -*- coding: utf-8 -*-
"""
session_gate.py —— 时段闸门（纯状态机，无网络依赖，可离线单测）

逻辑（对应预注册卡 A / 复盘工具"用途二"）：
  某品种在某 session 走出「强趋势」→ 封掉紧接着的下一个 session 的新开仓。
  「强」的定义（逻辑定死，禁止回测调参）：
      |该 session 收益率| > K × session_ATR
  其中 session_ATR = 该品种最近 N 个 session |收益率| 的滚动均值（典型波动）。

无未来函数：只用「已收盘 session」的收益率判定，影响的是「下一 session」的开仓，
不触碰任何未收盘数据。

状态机由调用方在边界处驱动：
  - 当某品种进入新 session 的第一根 bar 时，调用 on_session_open(key, sid)
        → 返回该 session 是否允许新开仓（依据上一 session 是否封禁）。
  - 当某品种某 session 的最后一根 bar 被处理完、检测到切换到新 session 时，
    先调用 on_session_close(key, prev_sid, ret_pct) 记录收益率并可能封禁下一 session。

封禁集合以 (key, next_sid) 为键；on_session_open 命中即拒绝，并在返回后清除该键
（封禁只针对那一个 session，不会延续）。
"""
from __future__ import annotations
from collections import deque
import universe as _u


class SessionGate:
    def __init__(self, k: float = 1.5, n_sessions: int = 20):
        """
        k          : 强趋势阈值倍数（默认 1.5 = 比平时波动猛 50% 以上）。
        n_sessions : 滚动统计 session 收益率的样本窗（默认 20）。
        """
        self.k = float(k)
        self.n = int(n_sessions)
        # key -> deque(|session ret|)，滚动窗口
        self._hist: dict[str, deque[float]] = {}
        # (key, session_id) -> 封禁标记
        self._block: dict[tuple[str, str], bool] = {}
        # 调试/台账可见的触发记录
        self.triggers: list[dict] = []

    # ---------------- 驱动接口 ----------------
    def on_session_close(self, key: str, sid: str, ret_pct: float) -> None:
        """记录一个已收盘 session 的收益率，并按需封禁下一 session。"""
        if sid in ("closed", "", None):
            return
        h = self._hist.setdefault(key, deque(maxlen=self.n))
        h.append(abs(float(ret_pct)))
        if len(h) < 2:
            return  # 至少要有 1 个历史才谈得上"偏离"
        atr = sum(h) / len(h)
        if atr <= 0:
            return
        if abs(ret_pct) > self.k * atr:
            nxt = _u.next_session_id(sid, key)
            if nxt not in ("closed", ""):
                self._block[(key, nxt)] = True
                self.triggers.append({
                    "key": key, "from": sid, "block": nxt,
                    "ret": round(ret_pct, 4), "atr": round(atr, 4), "k": self.k,
                })

    def on_session_open(self, key: str, sid: str) -> bool:
        """新 session 开始时调用：返回该 session 是否允许新开仓。"""
        allowed = not self._block.pop((key, sid), False)
        return allowed

    def can_open(self, key: str, sid: str) -> bool:
        """开仓前查询：该 (key, session) 当前是否允许开新仓。"""
        if sid in ("closed", "", None):
            return True  # 休市不交易，自然不开
        return not self._block.get((key, sid), False)

    # ---------------- 查询 ----------------
    def session_atr(self, key: str) -> float:
        h = self._hist.get(key)
        return (sum(h) / len(h)) if h else 0.0

    def pending_blocks(self) -> list[tuple[str, str]]:
        return [k for k in self._block.keys()]


if __name__ == "__main__":
    g = SessionGate(k=1.5, n_sessions=5)
    # 前 5 个 session 稳定小波动（训练 atr≈1%）
    for i in range(5):
        g.on_session_close("RB", f"2026-01-0{i+1}_morning", ret_pct=0.01 if i % 2 else -0.01)
    print("atr(RB)≈", round(g.session_atr("RB"), 4))
    # 早盘突发 +5%（远超 1.5×1%）
    g.on_session_close("RB", "2026-01-06_morning", ret_pct=0.05)
    print("触发记录:", g.triggers)
    # 该封禁应落在 2026-01-06_afternoon
    print("午盘 can_open =", g.can_open("RB", "2026-01-06_afternoon"), "(应 False)")
    print("次日早 can_open =", g.can_open("RB", "2026-01-07_morning"), "(应 True)")
