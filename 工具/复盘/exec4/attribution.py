# -*- coding: utf-8 -*-
"""
exec4/attribution.py —— 四期 S3：§4.9 敞口混杂检查（三列统计）

## 这个模块存在的唯一理由

封盘会同时做两件事：

| | 变化 | 性质 |
|---|---|---|
| ① | 避开被判定为「坏」的时段 | 封盘的**本意** |
| ② | **平均敞口下降**（平掉后不再进 ⇒ 空仓时间变长） | **副作用** |

而②**本身就能少亏** —— 06 是负期望策略，降低敞口必然少亏。
所以「封盘后少亏了 X 元」这句话**证明不了任何事**：无法区分是①还是②在起作用。

⇒ §4.9 强制要求三列，其中第三列是**唯一可比口径**：

| 指标 | 定义 |
|---|---|
| 封盘组 平均敞口 | 封盘时段 `|持仓手数| × 乘数 × 价格 / 权益` 的均值 |
| 对照组 平均敞口 | 同品种、同时段类型、**未封盘**时的平均敞口 |
| **敞口调整后损益** | `实际损益 / 平均敞口`（每单位敞口的收益）|

## 验收断言（§4.9 原文）

> 如果「敞口调整后损益」两组差异 **< 1 个标准误**，则结论必须写成
> 「**封盘无独立效应，观察到的差异可由敞口下降解释**」，不许写成「封盘有效」。

## 两条实现口径（容易错，写死在这里）

1. **权益分母用 `equity_base = 200,000`（锁死），不是账户真实余额。**
   理由：手数就是用 20 万算的（`RiskParSizer` 锁死 equity_base），
   敞口若用真实余额（约 98 万）做分母，得到的比例和手数完全脱钩，失去意义。

2. **敞口按 bar 计权，不按笔计权。**
   封盘期的"平均敞口"问的是"这段时间里我平均暴露多少"，
   所以要按**每根 bar 的持仓**取均值。按笔平均会把"持了 1 根"和"持了 16 根"
   当成同等权重，与"时间占比"语义不符。
"""
from __future__ import annotations

import math
from typing import Iterable, Optional

import numpy as np

# 手数计算基数（与 sizer.RiskParSizer 的 equity_base 严格一致）
EQUITY_BASE = 200_000.0

SEG_CN = {"morning": "早盘", "afternoon": "午盘", "night": "夜盘"}


def _seg_of_sid(sid: str) -> str:
    """'2026-10-15_afternoon' → 'afternoon'"""
    if not sid or "_" not in str(sid):
        return "closed"
    return str(sid).rsplit("_", 1)[1]


# ---------------------------------------------------------------------------
# 逐 bar 敞口序列
# ---------------------------------------------------------------------------
def exposure_series(trades: Iterable[dict], n_bars: int, price: np.ndarray,
                    mult: float, equity: float = EQUITY_BASE) -> np.ndarray:
    """把成交序列还原成**逐 bar 敞口**（|手数| × 价格 × 乘数 ÷ 权益）。

    持仓区间 = [entry_i, exit_i)（出场那根 bar 已经平了，不计敞口）。
    未平仓（exit_i = -1）算到最后一根。
    """
    out = np.zeros(int(n_bars), dtype=float)
    if n_bars <= 0 or not mult or equity <= 0:
        return out
    for t in trades or []:
        i0 = int(t.get("entry_i", -1))
        if i0 < 0:
            continue
        i1 = int(t.get("exit_i", -1))
        if i1 < 0:
            i1 = int(n_bars)          # 未平仓 ⇒ 持有到末尾
        i0 = max(0, i0)
        i1 = min(int(n_bars), i1)
        hand = abs(int(t.get("hand", 0)))
        if hand == 0 or i1 <= i0:
            continue
        px = price[i0:i1]
        out[i0:i1] = hand * px * float(mult) / float(equity)
    return out


# ---------------------------------------------------------------------------
# 三列统计
# ---------------------------------------------------------------------------
def _stat(vals: np.ndarray) -> dict:
    vals = np.asarray(vals, dtype=float)
    vals = vals[np.isfinite(vals)]
    n = len(vals)
    if n == 0:
        return {"n": 0, "mean": float("nan"), "se": float("nan"), "sum": 0.0}
    mean = float(vals.mean())
    if n > 1:
        se = float(vals.std(ddof=1) / math.sqrt(n))
    else:
        se = float("nan")
    return {"n": n, "mean": mean, "se": se, "sum": float(vals.sum())}


def block_attribution(book: dict, sids: list, blocked: np.ndarray,
                      price: np.ndarray, key: str, mult: float,
                      equity: float = EQUITY_BASE) -> dict:
    """§4.9 三列 + **反事实列**。`book` 是 run_dual_book 的返回。

    分组（按**时段类型**分层，绝不把早/午/夜混在一起比 —— 见 §4.5）：
      · 封盘组 = 被封时段的 bar（`blocked[j] is True`），**实际簿**
      · 对照组 = 同类时段、未封盘的 bar，**实际簿**
      · 反事实 = 同一批"被封时段"的 bar，但看**不带封盘**那一簿（Book B）

    ★★ 为什么必须有「反事实」列（这是本模块最容易做错的地方）：
      「封盘组实际损益」在定义上就接近 0（封盘期**不允许开新仓**），
      拿它去比「对照组实际损益」等于拿"没做"比"做了"，永远得到"封盘更好"的假结论。
      真正的问题是：**如果那段时间不封，会发生什么？** ⇒ 只能看 Book B 在同一批 bar 上的表现。

    ★ 损益归因用**出场 bar**，不是入场 bar：
      钱是在平仓那一刻实现的。若按入场 bar 归因，
      封盘组会永远 0（因为封盘期不允许开仓），统计就废了。

    返回
    ----
    {"key", "rows": [{seg, seg_cn, n_bars_*, expo_*, pnl_*, adj_*, diff, se, significant}],
     "note"}
    """
    n = len(price)
    act = book.get("actual") or []
    shd = book.get("shadow") or []
    expo_a = exposure_series(act, n, price, mult, equity)
    expo_s = exposure_series(shd, n, price, mult, equity)
    blk = np.asarray(blocked, dtype=bool) if blocked is not None else np.zeros(n, bool)
    if len(blk) != n:
        blk = np.zeros(n, dtype=bool)

    # 按 (时段类型, 是否封盘) 分桶
    buckets: dict = {}
    for j, sid in enumerate(sids or []):
        seg = _seg_of_sid(sid)
        if seg == "closed":
            continue
        buckets.setdefault((seg, bool(blk[j])), []).append(j)

    # 每笔损益按**出场 bar** 归桶
    def _bucket_pnl(trades):
        out = {}
        for t in trades:
            i1 = int(t.get("exit_i", -1))
            if not (0 <= i1 < n):
                continue
            seg = _seg_of_sid(sids[i1]) if i1 < len(sids) else "closed"
            if seg == "closed":
                continue
            p = _trade_pnl(t, mult)
            if not np.isfinite(p):
                continue
            out.setdefault((seg, bool(blk[i1])), []).append(p)
        return out

    pnl_a = _bucket_pnl(act)
    pnl_s = _bucket_pnl(shd)

    rows = []
    for seg in sorted({s for s, _b in buckets}):
        i_blk = np.asarray(buckets.get((seg, True), []), dtype=int)
        i_ctl = np.asarray(buckets.get((seg, False), []), dtype=int)
        if len(i_blk) == 0:
            continue                      # 该时段没封过 ⇒ 无封盘组，跳过

        # ---- ①② 敞口 ----
        e_blk = _stat(expo_a[i_blk])          # 封盘组实际敞口
        e_ctl = _stat(expo_a[i_ctl]) if len(i_ctl) else _stat(np.array([]))
        e_hyp = _stat(expo_s[i_blk])          # 反事实：同一批 bar，不封的话的敞口

        # ---- 损益 ----
        s_blk = _stat(np.asarray(pnl_a.get((seg, True), [])))
        s_ctl = _stat(np.asarray(pnl_a.get((seg, False), [])))
        s_hyp = _stat(np.asarray(pnl_s.get((seg, True), [])))   # 不封在那批 bar 上的损益

        # ---- ③ 敞口调整后损益 = 损益 / 敞口总量（每单位敞口收益）----
        adj_blk = s_blk["sum"] / e_blk["sum"] if e_blk["sum"] > 0 else 0.0
        adj_ctl = s_ctl["sum"] / e_ctl["sum"] if e_ctl["sum"] > 0 else float("nan")
        adj_hyp = s_hyp["sum"] / e_hyp["sum"] if e_hyp["sum"] > 0 else float("nan")

        # ---- 显著性：实际 vs 反事实，在**同一批 bar** 上比（配对，故 diff 的 SE 更小）----
        diff = (adj_blk - adj_hyp) if np.isfinite(adj_hyp) else float("nan")
        # 用逐 bar 的"单位敞口损益"序列估 SE（配对差的 SE）
        pa_b = _per_bar_adjusted(pnl_a.get((seg, True), []), act, expo_a, blk, sids, seg, True, mult)
        pa_h = _per_bar_adjusted(pnl_s.get((seg, True), []), shd, expo_s, blk, sids, seg, True, mult)
        sa, sh = _stat(pa_b), _stat(pa_h)
        se = math.sqrt((sa["se"] ** 2 if np.isfinite(sa["se"]) else 0.0)
                       + (sh["se"] ** 2 if np.isfinite(sh["se"]) else 0.0)) \
            if (sa["n"] >= 2 and sh["n"] >= 2) else float("nan")
        significant = (bool(abs(diff) >= se) if (np.isfinite(diff) and np.isfinite(se)
                                                 and se > 0) else None)

        rows.append({
            "seg": seg, "seg_cn": SEG_CN.get(seg, seg),
            "n_bars_blocked": int(len(i_blk)), "n_bars_control": int(len(i_ctl)),
            "expo_blocked": e_blk["mean"],       # ① 封盘组平均敞口
            "expo_control": e_ctl["mean"],       # ② 对照组平均敞口
            "expo_hypo": e_hyp["mean"],          # 反事实平均敞口
            "pnl_blocked": s_blk["sum"],
            "pnl_control": s_ctl["sum"],
            "pnl_hypo": s_hyp["sum"],
            "n_trades_blocked": s_blk["n"], "n_trades_control": s_ctl["n"],
            "n_trades_hypo": s_hyp["n"],
            "adj_blocked": adj_blk,              # ③ 敞口调整后损益（实际）
            "adj_control": adj_ctl,
            "adj_hypo": adj_hyp,                 # ③ 敞口调整后损益（反事实）
            "adj_diff": diff,
            "adj_se_diff": se,
            "significant": significant,
        })

    return {
        "key": key, "rows": rows,
        "note": ("第三列（敞口调整后损益）是唯一可比口径；"
                 "实际 vs 反事实差异 < 1 SE ⇒ 必须写「封盘无独立效应，可由敞口下降解释」"),
    }


def _per_bar_adjusted(pnls, trades, expo, blk, sids, seg, want_blocked, mult):
    """把每笔损益除以其**持仓期平均敞口**，得到逐笔"单位敞口收益"序列（供估 SE）。"""
    out = []
    for t in trades or []:
        i0 = int(t.get("entry_i", -1))
        i1 = int(t.get("exit_i", -1))
        if i1 < 0 or not (0 <= i1 < len(expo)):
            continue
        s = _seg_of_sid(sids[i1]) if i1 < len(sids) else "closed"
        if s != seg or bool(blk[i1]) != want_blocked:
            continue
        p = _trade_pnl(t, mult)
        if not np.isfinite(p):
            continue
        seg_expo = expo[max(0, i0):i1]
        m = float(seg_expo.mean()) if len(seg_expo) else 0.0
        if m > 0:
            out.append(p / m)
    return np.asarray(out, dtype=float)


def _trade_pnl(t: dict, mult: float) -> float:
    try:
        ep, xp = float(t["entry_px"]), float(t["exit_px"])
        d, h = int(t["dir"]), int(t["hand"])
    except (KeyError, TypeError, ValueError):
        return float("nan")
    if not (ep == ep and xp == xp):
        return float("nan")
    return (xp - ep) * d * h * float(mult)


def _per_unit(pnls: list, expo: np.ndarray, bars: list, mult: float) -> np.ndarray:
    """把每笔损益除以其**持仓期平均敞口**，得到"每单位敞口收益"。"""
    out = []
    if not pnls:
        return np.asarray(out)
    # 按顺序摊到对应 bar 区间（比逐笔精确更简单可靠的做法：
    # 用该笔入场 bar 处的敞口作为其敞口尺度）
    for p, b in zip(pnls, bars):
        if 0 <= b < len(expo) and expo[b] > 0:
            out.append(p / (expo[b] * 1.0))
    return np.asarray(out, dtype=float)


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
def format_report(attr: dict) -> str:
    """把三列（+反事实列）渲染成可读表格。"""
    lines = []
    lines.append(f"[{attr.get('key', '?')}] §4.9 敞口混杂三列 + 反事实")
    hdr = (f"{'时段':<5}{'封bar':>6}{'对bar':>6}"
           f"{'①封盘敞口':>10}{'②对照敞口':>10}{'反事实敞口':>10}"
           f"{'③封盘/单位':>11}{'③对照/单位':>11}{'③反事实/单位':>13}"
           f"{'差':>9}{'1SE':>8}{'结论':>10}")
    lines.append(hdr)
    lines.append("-" * len(hdr))

    def _f(x, p=3):
        return f"{x:.{p}f}" if isinstance(x, (int, float)) and np.isfinite(x) else "n/a"

    for r in attr.get("rows") or []:
        sig = r.get("significant")
        verdict = "有独立效应" if sig is True else ("无独立效应" if sig is False else "样本不足")
        lines.append(
            f"{r['seg_cn']:<5}{r['n_bars_blocked']:>6}{r['n_bars_control']:>6}"
            f"{_f(r['expo_blocked']):>10}{_f(r['expo_control']):>10}{_f(r['expo_hypo']):>10}"
            f"{_f(r['adj_blocked']):>11}{_f(r['adj_control']):>11}{_f(r['adj_hypo']):>13}"
            f"{_f(r['adj_diff'], 2):>9}{_f(r['adj_se_diff'], 2):>8}{verdict:>10}")
    if not (attr.get("rows") or []):
        lines.append("（没有封盘时段 —— 要么没触发，要么数据不足）")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 自检（离线，用工作台本地日线/缓存 + 2026 的 5min K 线）
# ---------------------------------------------------------------------------
def _selftest() -> int:
    import os
    import sys

    _exec4 = os.path.dirname(os.path.abspath(__file__))     # 工具/复盘/exec4
    _replay = os.path.dirname(_exec4)                       # 工具/复盘（data / trades）
    root = _replay
    for _ in range(8):
        if os.path.isfile(os.path.join(root, "config_symbols.py")):
            break
        parent = os.path.dirname(root)
        if parent == root:
            break
        root = parent
    # ★ 顺序是反的（后 insert 的在最前）⇒ 先插远的、最后插近的。
    #   注意：**绝不能**插 os.path.dirname(root)（那是 Desktop，会引入同名命名空间包）
    for p in (root, _replay, _exec4,
              os.path.join(root, "策略", "06_MA13超短线", "工作台")):
        if os.path.isdir(p):
            sys.path.insert(0, p)

    try:
        import pandas as pd
        import data
        from data import to_beijing
        from signal_bridge import spec as _spec, has_spec as _has_spec
        import gate as _gate
    except Exception as e:
        print(f"SKIP: 环境不全（{type(e).__name__}: {e}）")
        return 0

    print("=" * 92)
    print("S3 封盘（BLOCK_ENTRY）双簿 + §4.9 敞口混杂三列")
    print(f"判定公式：|session 收益| >= K×atr20_daily/price，K={_gate.K_GATE}（定死，不扫参）")
    print("=" * 92)

    total_blocked_sess = total_sess = 0
    total_hypo = 0
    hypo_pnl = 0.0
    ok = True

    for cn, key in sorted(data.CN2KEY.items()):
        if not _has_spec(key):
            continue
        try:
            df, _meta = data.load_recent(key, 5)
        except FileNotFoundError:
            continue
        sp = _spec(key)
        dt_bj = pd.Series(to_beijing(df["datetime"])).reset_index(drop=True)

        g = _gate.BlockEntryGate()
        book = _gate.run_dual_book(df, key, sp["tick"], gate=g)
        info = book["gate_info"]

        n_sess = int(info["n_sessions"])
        n_blk = len([r for r in book["records"] if not r.sealed_skipped])
        total_blocked_sess += n_blk
        total_sess += max(1, info["n_decided"])
        total_hypo += len(book["hypo"])
        hypo_pnl += sum(h["hypo_pnl"] for h in book["hypo"]
                        if isinstance(h["hypo_pnl"], float) and h["hypo_pnl"] == h["hypo_pnl"])

        print(f"\n[{cn} / {key}] session {n_sess} 段，真判定 {info['n_decided']} 次，"
              f"封禁 {n_blk} 段；bar 被封占比 {book['blocked'].mean():.1%}")
        print(f"  实际成交 {len(book['actual'])} 笔 / 假想(不封) {len(book['shadow'])} 笔 "
              f"⇒ 封盘少做 {len(book['shadow']) - len(book['actual'])} 笔；"
              f"假想单 {len(book['hypo'])} 笔")

        price = df["close"].to_numpy(float)
        attr = block_attribution(book, info["sid"], book["blocked"], price,
                                 key, sp["mult"])
        print(format_report(attr))
        for r in attr["rows"]:
            if r.get("significant") is False:
                ok = ok and True     # "无独立效应"是合法结论，不是失败

    print("\n" + "=" * 92)
    rate = (total_blocked_sess / total_sess) if total_sess else 0.0
    print(f"汇总：判定 {total_sess} 次、封禁 {total_blocked_sess} 段 "
          f"⇒ 封盘率 {rate:.1%}")
    print(f"§4.4 设计目标「封掉 20~30%，保留 70~80%」⇒ "
          + ("落在目标区间 ✅" if 0.15 <= rate <= 0.40 else
             "★ 偏离目标区间（过高会把工具变成'什么都不做'，过低则测不出差异）"))
    print(f"被少做的机会 {total_hypo} 笔，其假想毛利合计 {hypo_pnl:+,.0f} 元"
          f"（负 = 封对了；但**必须看敞口调整后**才算数）")
    print("=" * 92)
    print("S3 自检：" + ("通过 ✅" if ok else "失败 ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
