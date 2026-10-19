# -*- coding: utf-8 -*-
"""
exec4/gate.py —— 四期 S3：封盘状态机（`BLOCK_ENTRY`）+ 双簿（实际 / 假想）

四期方案 §4 的落地。核心不是"封盘"这个动作，而是**让封盘规则可被评估**。

一、状态机（§4.1 拍板，2026-10-18 19:20）
    OPEN         : 新开仓 ✅ / 平仓 ✅ / 加仓 ✅
    BLOCK_ENTRY  : 新开仓 ❌ / 平仓 ✅ / 加仓 ❌   ← 四期**唯一**实现的封禁态
    `BLOCK_ALL`  : 四期不实现，只保留枚举值（那是交易所强制场景，走独立流程）

    ★ 用户原话：「封开仓，正在持仓的正常按照信号走，封单个品种的，可以增加空仓时间」
      追加确认：「封仓遇到反手信号平仓」
    ⇒ 封盘期遇到反手信号：**平掉旧仓 ✅ 但不开新仓 ❌ ⇒ 变成空仓**
      （不是"保持原仓位"）。这个选择必然导致封盘期平均敞口下降，
       是 §4.9 敞口混杂陷阱的来源，所以必须配 §4.9 的三列统计。

二、判定公式（§4.6，阈值一次定死，禁止扫参）
    move      = (该 session 末根 close − 首根 open) / 首根 open
    threshold = K × atr20_daily / price
    |move| >= threshold  ⇒  封禁**紧接着的下一个** session
    K = 0.6（早盘走完 0.6 个日均 ATR 算"强趋势"）

    ★ atr20_daily 必须用**昨日收盘时已确定**的值 —— 不能用当日盘中数据（否则未来函数）。
      本模块用 `daily_atr_from_intraday()` 从 5min 聚合，且**严格只取前一天及更早**。

三、生命周期（§4.8(d)）：**每个时段独立判定，状态不跨时段继承**
    早盘收 → 判午盘；午盘收 → 判夜盘；夜盘收 → 判次日早盘。
    无夜盘品种（JD/LH 等）：午盘收后直接判"次日早盘"，跳过夜盘。

四、涨跌停封板排除（§4.7，用户明确要求）
    封板时量价失真（成交极少、振幅≈0），判"强趋势"会严重误判
    ⇒ 当日**不出信号**，且**不计入"被封机会"统计**（否则污染分母）。

    ⚠️ 两点口径差异：
      · `upper_limit`/`lower_limit` 只有**实盘行情模式**才有值（回测为 nan，见 §2.1）
        ⇒ 依赖它们的「收盘封板」判定只能在实盘用。
      · 但「**一字板**」可以纯从 K 线判：`high - low <= tick` 且全天如此。
        本模块默认用这个（回测也能用），limit 价判定留出接口。

五、假想单（§4.2）—— 这是本节最重要的东西
    只显示"封盘后没亏钱"永远证明不了封盘有用，因为"没亏"也可能只是"没做"。
    ⇒ 双簿并行：Book A = 实际（带封盘）；Book B = 假想（完全不带封盘）。
      两者的差就是"封盘干了什么"。假想单用**同一套成交模型**（同一个 engine.run）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

# 拍板 §4.6：K 定死 0.6，不许扫参（铁律 2）
K_GATE = 0.6

# §4.6：atr20_daily
ATR_DAILY_N = 20

# §4.8(b)：早盘口径 09:00–11:30（不含夜盘）。`universe.session_id` 已按此切段。
SEG_CN = {"morning": "早盘", "afternoon": "午盘", "night": "夜盘", "closed": "休市"}


# ---------------------------------------------------------------------------
# 1) 日均 ATR（严格只用"昨日及更早"，防未来函数）
# ---------------------------------------------------------------------------
def daily_atr_from_intraday(dt_bj: pd.Series, high: np.ndarray, low: np.ndarray,
                            close: np.ndarray, n: int = ATR_DAILY_N
                            ) -> np.ndarray:
    """把 5min bar 聚合成日线，算 ATR(n)，再**逐 bar 广播**成 atr 数组。

    ★ 关键：第 d 天所有 bar 拿到的 atr，只用 **d-1 天及更早**的日线算 —— 绝不含当天。
      理由（§4.6）：atr20_daily 必须用"昨日收盘时已确定"的值。若含当天，
      等于用当天的振幅去判当天早盘是否"强趋势"，是未来函数。

    返回长度 = len(close) 的 float 数组，前若干根为 nan（历史不足）。
    """
    n_bar = len(close)
    if n_bar == 0:
        return np.array([], dtype=float)
    d = pd.Series(pd.to_datetime(dt_bj)).dt.normalize().to_numpy()
    # 按日聚合 OHLC
    dfe = pd.DataFrame({"d": d, "high": high, "low": low, "close": close})
    g = dfe.groupby("d", sort=True).agg(high=("high", "max"),
                                        low=("low", "min"),
                                        close=("close", "last"))
    days = g.index.to_numpy()
    hh = g["high"].to_numpy(float)
    ll = g["low"].to_numpy(float)
    cc = g["close"].to_numpy(float)
    nd = len(days)

    # 日线 TR / ATR
    atr_daily = np.full(nd, np.nan)
    tr = np.zeros(nd)
    for i in range(nd):
        if i == 0:
            tr[i] = hh[i] - ll[i]
        else:
            tr[i] = max(hh[i] - ll[i], abs(hh[i] - cc[i - 1]), abs(ll[i] - cc[i - 1]))
    for i in range(nd):
        lo = max(1, i - n + 1)
        if i - lo + 1 >= 2:                      # 至少 2 天才有"均值"可言
            atr_daily[i] = tr[lo:i + 1].mean()

    # 逐 bar 广播：第 d 天用 atr_daily[d-1]
    day_index = {v: i for i, v in enumerate(days)}
    out = np.full(n_bar, np.nan)
    for j in range(n_bar):
        i = day_index.get(d[j])
        if i is None or i == 0:
            continue                             # 首日无"昨日" ⇒ nan
        out[j] = atr_daily[i - 1]                # ★ 只用前一天及更早
    return out


def sealed_day_mask(dt_bj: pd.Series, high: np.ndarray, low: np.ndarray,
                    tick: float) -> np.ndarray:
    """「一字板」日掩码（§4.7）：当天**所有** bar 都 `high-low <= tick`。

    为什么用这个而不是涨跌停价：
      `quote.upper_limit/lower_limit` 只有实盘行情模式才有值，回测是 nan（§2.1）。
      而"一字板"（全天振幅≈0）可以直接从 K 线识别，回测也能用。

    返回长度 = len(high) 的 bool 数组：True = 该 bar 属于封板日。
    """
    n_bar = len(high)
    if n_bar == 0:
        return np.array([], dtype=bool)
    d = pd.Series(pd.to_datetime(dt_bj)).dt.normalize().to_numpy()
    rng = np.asarray(high, dtype=float) - np.asarray(low, dtype=float)
    flat = rng <= float(tick) + 1e-12
    dfe = pd.DataFrame({"d": d, "flat": flat})
    # 当天**全部** bar 都 flat 才算一字板（有一根不 flat 就不是）
    all_flat = dfe.groupby("d", sort=False)["flat"].all()
    sealed_days = set(all_flat[all_flat].index)   # 只有"全天都 flat"的日才进集合
    return np.array([dd in sealed_days for dd in d], dtype=bool)


# ---------------------------------------------------------------------------
# 2) 状态机
# ---------------------------------------------------------------------------
@dataclass
class BlockRecord:
    """一次封盘决策的记录（诊断用；不是交易）。"""
    key: str
    decided_at_sid: str        # 在哪一段收盘时判定
    blocked_sid: str           # 被封的是哪一段
    move: float                # 那一段的收益率
    atr_daily: float           # 判定用的日均 ATR（昨日已确定）
    price: float               # 判定时的价格
    threshold: float           # K × atr_daily / price（收益率阈值）
    sealed_skipped: bool = False   # 是否因封板而跳过判定（不计入统计分母）


@dataclass
class BlockEntryGate:
    """`BLOCK_ENTRY` 状态机（§4.1 拍板）。每个 (key, sid) 独立，状态不跨段继承。

    用法（由 engine 在 session 边界驱动）：
        gate.on_session_close(key, sid, move, atr_daily, price, sealed)
            → 判定是否封禁 next_session_id
        gate.can_open(key, sid) → bool
        gate.consume(key, sid)  → 取走一次性封禁标记（进段时调用）
    """
    k: float = K_GATE
    records: list = field(default_factory=list)
    _block: dict = field(default_factory=dict)      # (key, sid) -> BlockRecord
    _sealed_days: set = field(default_factory=set)  # (key, date) 封板日（当日不出信号）

    # ---- 判定 ----
    def on_session_close(self, key: str, sid: str, move: Optional[float],
                         atr_daily: Optional[float], price: Optional[float],
                         next_sid: Optional[str] = None,
                         sealed: bool = False) -> Optional[BlockRecord]:
        """某 session 收盘时调用：按 §4.6 判定是否封禁**紧接着的下一段**。

        move       : 该 session 收益率（末 close / 首 open − 1）
        atr_daily  : **昨日**日均 ATR（价格单位）；nan/≤0 ⇒ 不判（历史不足）
        price      : 判定时价格（用来把 ATR 归一成收益率）
        sealed     : 该日是否涨跌停封板（§4.7）⇒ **直接不判，且不计入分母**

        返回：真封了 ⇒ BlockRecord；没封/无法判 ⇒ None。
        ★ "无法判"（历史不足）**不写 records** —— 否则"数据不足"会被当成
          "判定过但没封"塞进分母，把封盘率算低。
        """
        from universe import next_session_id

        nxt = next_sid or next_session_id(sid, key)
        if sid in ("closed", "", None) or nxt in ("closed", ""):
            return None
        if sealed:
            # §4.7：封板日量价失真 ⇒ 不判、不计分母（单列一类记着，便于核对）
            self.records.append(BlockRecord(
                key=key, decided_at_sid=sid, blocked_sid=nxt,
                move=float("nan") if move is None else float(move),
                atr_daily=float("nan") if atr_daily is None else float(atr_daily),
                price=float("nan") if price is None else float(price),
                threshold=float("nan"), sealed_skipped=True))
            return None
        if (move is None or price is None or price <= 0
                or atr_daily is None or not np.isfinite(atr_daily) or atr_daily <= 0):
            return None                      # 历史不足 ⇒ 不判、不入分母

        thr = self.k * float(atr_daily) / float(price)
        if abs(float(move)) < thr:
            return None                      # 判过但没触发 ⇒ 不写 records（只统计"封了几次"）
        rec = BlockRecord(key=key, decided_at_sid=sid, blocked_sid=nxt,
                          move=float(move), atr_daily=float(atr_daily),
                          price=float(price), threshold=thr)
        self._block[(key, nxt)] = rec
        self.records.append(rec)
        return rec

    # ---- 查询 ----
    def can_open(self, key: str, sid: str) -> bool:
        if sid in ("closed", "", None):
            return True                     # 休市不交易，谈不上封
        if (key, str(sid)[:10]) in self._sealed_days:
            return False                    # 封板日不出信号（§4.7）
        return (key, sid) not in self._block

    def is_sealed_day(self, key: str, sid: str) -> bool:
        return (key, str(sid)[:10]) in self._sealed_days

    def mark_sealed_day(self, key: str, date_str: str) -> None:
        self._sealed_days.add((key, str(date_str)[:10]))

    def blocked_sessions(self, key: Optional[str] = None) -> list:
        out = [(k, s, r) for (k, s), r in self._block.items()]
        if key:
            out = [x for x in out if x[0] == key]
        return out

    def stats(self) -> dict:
        """决策层统计。

        ★ 口径（易错，写清楚）：
          · `n_blocked`   = records 里真封了的（sealed_skipped=False）
          · `n_sealed`    = 因封板跳过判定的（不计入封盘率分母）
          · `block_rate`  = n_blocked / (n_blocked + n_decided_free)
                            —— n_decided_free 由调用方在驱动时累加，
                            因为"判过但没触发"本模块不落 records（见 on_session_close）
        """
        blocked = [r for r in self.records if not r.sealed_skipped]
        sealed = [r for r in self.records if r.sealed_skipped]
        return {
            "n_blocked": len(blocked),
            "n_sealed_skipped": len(sealed),
            "blocked_sessions": [(r.key, r.blocked_sid) for r in blocked],
        }


# ---------------------------------------------------------------------------
# 3) 预计算封禁集合（把状态机从逐 bar 循环里解耦）
# ---------------------------------------------------------------------------
def precompute_blocks(dt_bj: pd.Series, open_, high, low, close, key: str,
                      tick: float, gate: BlockEntryGate,
                      n_atr: int = ATR_DAILY_N) -> dict:
    """扫一遍 bar，在 session 边界驱动状态机，返回逐 bar 的封禁掩码。

    返回
    ----
    {
      "sid":       [str] 每根 bar 的 session_id,
      "sealed":    np.ndarray[bool] 封板日掩码（§4.7）,
      "atr_daily": np.ndarray[float] 逐 bar 的"昨日"日均 ATR,
      "blocked":   np.ndarray[bool] True = 该 bar 所在 session 被封禁（含封板日）,
      "n_sessions": int 有判定的 session 段数（分母，含未触发的）,
      "n_decided":  int 实际做过判定的次数（排除封板日跳过的）,
      "segments":  [(sid, i0, i1)] 便于归因,
    }

    为什么预计算而不是在 engine 循环里调：
      封禁判定只依赖「某 session 的首根 open 与末根 close」，与持仓/成交完全无关。
      预先把整段算完，engine 只需一个 `can_open(j)` 回调 ⇒ 两簿（实际/假想）
      共用同一份封禁集合，避免"两簿各自算一遍导致判定不一致"。
    """
    import numpy as _np
    from universe import session_id as _sid_of

    n = len(close)
    sids = [_sid_of(t) for t in pd.to_datetime(dt_bj)]
    sealed = sealed_day_mask(dt_bj, high, low, tick)
    atr_d = daily_atr_from_intraday(dt_bj, high, low, close, n=n_atr)

    # ---- 1) 分段：连续相同 sid 归一段 ----
    segs = []          # [(sid, i0, i1)]
    i = 0
    while i < n:
        sid = sids[i]
        j = i
        while j + 1 < n and sids[j + 1] == sid:
            j += 1
        segs.append((sid, i, j))
        i = j + 1

    # ---- 2) 逐相邻段判定：前一段收盘 ⇒ 决定后一段封不封 ----
    n_decided = 0
    for (psid, pi0, pi1), (nsid, _ni0, _ni1) in zip(segs, segs[1:]):
        if psid == "closed" or nsid == "closed":
            continue
        p_open = float(open_[pi0])
        p_close = float(close[pi1])
        mv = (p_close - p_open) / p_open if p_open > 0 else None
        # ATR 取"该段末 bar"的昨日 ATR（pi1 这一根）
        rec = gate.on_session_close(
            key, psid, mv,
            float(atr_d[pi1]) if np.isfinite(atr_d[pi1]) else None,
            p_close, next_sid=nsid, sealed=bool(sealed[pi1]))
        if rec is not None and not rec.sealed_skipped:
            n_decided += 1
        elif rec is None:
            # 判过但没触发（不是封板、也不是数据不足）也要计入分母
            ok = (mv is not None and p_close > 0
                  and np.isfinite(atr_d[pi1]) and atr_d[pi1] > 0)
            if ok:
                n_decided += 1

    # ---- 3) 生成逐 bar 掩码 ----
    blocked = np.zeros(n, dtype=bool)
    blocked_sids = {sid for _k, sid in
                    [(r.key, r.blocked_sid) for r in gate.records if not r.sealed_skipped]}
    for sid, i0, i1 in segs:
        if sid in blocked_sids:
            blocked[i0:i1 + 1] = True
    blocked |= sealed           # §4.7 封板日整日不出信号

    return {"sid": sids, "sealed": sealed, "atr_daily": atr_d,
            "blocked": blocked, "n_sessions": len(segs), "n_decided": n_decided,
            "segments": segs}


# ---------------------------------------------------------------------------
# 4) 双簿：实际（带封盘） vs 假想（完全不带封盘）
# ---------------------------------------------------------------------------
def run_dual_book(df: pd.DataFrame, key: str, tick: float,
                  gate: Optional[BlockEntryGate] = None,
                  signal=None, sizer=None,
                  intrabar_stop: bool = False,
                  reset_bars=None, seeds=None) -> dict:
    """跑两遍同一个 engine：Book A 带封盘、Book B 不带封盘。

    为什么是「跑两遍」而不是「在循环里记假想单」：
      §4.2 要求假想单用**同一套成交模型**算。最可靠的"同一套"就是**同一份代码**。
      在循环里分叉记假想单，等于维护两套状态（真仓/影子仓），迟早不一致。
      跑两遍的代价是慢一倍，但 6 品种 × 500 笔是毫秒级，完全不必优化。

    ★ 一个必须说清的口径（否则 §4.8(c) 会被误读）：
      §4.8(c) 那张表里有「持多 + 出现反手做空信号 ⇒ 平掉多单」。
      但 **MA13 的出场只有 stop / time，没有"反手平仓"** —— 它持多时根本不看空信号。
      ⇒ 在 06 上 §4.8(c) 退化为：**封盘只影响"开新仓"，不影响任何出场**。
         封盘期的敞口下降，纯粹来自"平掉后不再进"，而不是"被强制平仓"。
      这仍然会造成 §4.9 的敞口混杂（空仓时间变长），所以三列统计照样必须做。

    返回
    ----
    {
      "actual":  [trade...]  带封盘的成交,
      "shadow":  [trade...]  不带封盘的成交（= 反事实基线）,
      "blocked": np.ndarray  逐 bar 封禁掩码,
      "gate_info": {...}     判定统计,
      "hypo":    [dict...]   假想单：Book A 被封掉、而 Book B 真的开了的那些
    }
    """
    import numpy as _np
    try:
        from data import to_beijing
    except Exception:                      # 独立运行时兜底
        def to_beijing(s):
            return pd.to_datetime(s) + pd.Timedelta(hours=8)

    from engine import run as _run

    dt_bj = pd.Series(to_beijing(df["datetime"])).reset_index(drop=True)
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)

    g = gate or BlockEntryGate()
    info = precompute_blocks(dt_bj, o, h, l, c, key, tick, g)

    blocked = info["blocked"]
    # Book A：被封的 bar 一律不许开新仓（复用 engine 的 halted_guard 钩子）
    actual = _run(df, key, signal=signal, sizer=sizer,
                  intrabar_stop=intrabar_stop, reset_bars=reset_bars, seeds=seeds,
                  halted_guard=lambda j: bool(blocked[j]))
    # Book B：完全不封（反事实）
    shadow = _run(df, key, signal=signal, sizer=sizer,
                  intrabar_stop=intrabar_stop, reset_bars=reset_bars, seeds=seeds)

    # ---- 假想单：Book B 里有、Book A 里没有的开仓 ----
    def _sig(t):
        return (int(t["entry_i"]), int(t["dir"]))

    a_keys = {}
    for t in actual:
        a_keys[_sig(t)] = t
    hypo = []
    for t in shadow:
        k2 = _sig(t)
        if k2 in a_keys:
            continue
        j = t["entry_i"]
        # 只算"被封掉"的那些（不是被 seeds/reset 影响的）
        if not (0 <= j < len(blocked)) or not blocked[j]:
            continue
        hypo.append({
            "key": key,
            "sid": info["sid"][j],
            "entry_i": j,
            "dir": t["dir"],
            "hand": t["hand"],
            "hypo_entry_px": t["entry_px"],
            "hypo_exit_px": t["exit_px"],
            "hypo_pnl": _pnl_yuan(t, key),
            "reason": t["reason"],
        })

    return {"actual": actual, "shadow": shadow, "blocked": blocked,
            "gate_info": info, "hypo": hypo, "records": list(g.records)}


def _pnl_yuan(t, key: str) -> float:
    """单笔毛利（元）= (出场价 − 入场价) × 方向 × 手数 × 乘数。"""
    try:
        from signal_bridge import spec as _spec
        mult = float(_spec(key)["mult"])
    except Exception:
        mult = 10.0
    ep, xp = t.get("entry_px"), t.get("exit_px")
    if ep is None or xp is None:
        return float("nan")
    try:
        ep, xp = float(ep), float(xp)
    except (TypeError, ValueError):
        return float("nan")
    if not (ep == ep and xp == xp):
        return float("nan")
    return (xp - ep) * int(t["dir"]) * int(t["hand"]) * mult
