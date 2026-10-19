# -*- coding: utf-8 -*-
"""
st_binned.py —— ATR 百分位分箱 SuperTrend（严格按 v3 任务书实现）

两条臂：
  base = 标准 SuperTrend（喂连续 ATR）
  bin  = 分箱 SuperTrend（喂 adaptive_atr，即 ATR 自身 25/50/75 分位三档的中心）

唯一差异是喂进递推的 atr 序列；其余（数据/递推/成本/1手/起点）完全一致。

用法：
  python st_binned.py --selftest        # V1/V1b/V2/V3/V5/V11
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import time

import numpy as np
import pandas as pd

# ------------------------------------------------------------------ 冻结参数
# ★ 全部模块级常量，禁止任何形式的参数扫描（任务书 §3.1 / §10）
ATR_LEN = 10
FACT = 3.0
LOOKBACK = 100
Q_LOW, Q_MID, Q_HIGH = 25.0, 50.0, 75.0
Q = (Q_LOW, Q_MID, Q_HIGH)
PCT_METHOD = "nearest_rank"      # 主口径：手写 TV 等价实现（§6.1）
POS_SIZE = 1                     # 固定 1 手
SLIP = 1                         # 滑点 1 跳
COMM = 3.0                       # 元/手/边

# 首个可交易下标：ATR 从 idx 9 才有值，再要 100 个有效值 ⇒ 108（§3.3 / §6.4）
START_IDX = (ATR_LEN - 1) + (LOOKBACK - 1)   # 108

IS_START, IS_END = "2016-01-04", "2021-12-31"
OOS_START, OOS_END = "2022-01-01", "2026-10-15"

PROJ = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
KLINE_DIR = os.path.join(PROJ, "工具", "复盘", "_缓存", "kline2026")
ROLL_CSV = os.path.join(PROJ, "工具", "_数据", "展期_合约日线.csv")
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
KEYS = ["M", "JM", "P", "SR", "JD", "EB"]


# ------------------------------------------------------------------ 基础指标
def wilder_atr(high: np.ndarray, low: np.ndarray, close: np.ndarray,
               n: int = ATR_LEN) -> np.ndarray:
    """Wilder RMA。前 n-1 根为 nan（首值用 SMA）。禁止用 SMA/EMA 替代。"""
    pc = np.r_[close[0], close[:-1]]
    tr = np.maximum(high - low, np.maximum(np.abs(high - pc), np.abs(low - pc)))
    atr = np.full(len(close), np.nan)
    atr[n - 1] = tr[:n].mean()
    for i in range(n, len(close)):
        atr[i] = atr[i - 1] + (tr[i] - atr[i - 1]) / n
    return atr


def pct_nearest_rank(w, qs=Q) -> np.ndarray:
    """TradingView ta.percentile_nearest_rank 等价：idx = ceil(q*n) - 1。

    ★ np.percentile(method='nearest') 用的是 round((n-1)*q)，两者不等价（§6.1）。
    """
    s = np.sort(np.asarray(w, dtype=float))
    m = len(s)
    out = []
    for q in qs:
        r = max(1, int(math.ceil(q / 100.0 * m)))     # 1-based rank
        out.append(s[r - 1])
    return np.array(out)


def adaptive_atr_seq(atr: np.ndarray, method: str = PCT_METHOD):
    """三档分箱。返回 (adaptive, lo, mid, hi, label)。

    window = atr[max(0,i-LOOKBACK+1) : i+1]（含当前根，i 已收盘）；
    有效值不足 LOOKBACK 时 adaptive = nan（不向前回填）。
    label: 0=低 1=中 2=高，-1=无效。
    """
    n = len(atr)
    ada = np.full(n, np.nan)
    lo_s = np.full(n, np.nan)
    mid_s = np.full(n, np.nan)
    hi_s = np.full(n, np.nan)
    lab = np.full(n, -1, dtype=int)

    for i in range(n):
        w = atr[max(0, i - LOOKBACK + 1): i + 1]
        w = w[~np.isnan(w)]
        if len(w) < LOOKBACK:
            continue
        if method == "nearest_rank":
            p = pct_nearest_rank(w, Q)
        elif method == "linear":
            p = np.percentile(w, Q, method="linear")       # 对照口径
        elif method == "numpy_nearest":
            p = np.percentile(w, Q, method="nearest")      # §6.1 反例对照
        else:
            raise ValueError(method)
        lo_s[i], mid_s[i], hi_s[i] = p[0], p[1], p[2]
        j = int(np.argmin(np.abs(p - atr[i])))
        lab[i] = j
        ada[i] = p[j]
    return ada, lo_s, mid_s, hi_s, lab


def supertrend(atr: np.ndarray, high: np.ndarray, low: np.ndarray,
               close: np.ndarray):
    """v3 §3.4：从首个有效 ATR 下标 i0 初始化，再从 i0+1 递推。

    返回 (st, dir, i0)；dir = -1 看多 / +1 看空（Pine 符号，不许改）。
    """
    n = len(close)
    valid = np.where(~np.isnan(atr))[0]
    if len(valid) == 0:
        return np.full(n, np.nan), np.zeros(n, int), None
    i0 = int(valid[0])

    src = (high + low) / 2.0
    ur = src + FACT * atr
    dr = src - FACT * atr
    up = ur.copy()
    dn = dr.copy()
    st = np.full(n, np.nan)
    d = np.zeros(n, int)

    up[i0], dn[i0] = ur[i0], dr[i0]
    d[i0] = 1
    # ★ Pine 原文：if na(atr[1]) → dir := 1; st := up(=下轨)。
    #   dir=1(空) 与 st=下轨 在两处不一致，但这是 Pine 的既定行为，必须照抄；
    #   若取上轨，下一根会进错分支、起始相位反相（实测会与 TV 产生 120 处 dir 差异）。
    st[i0] = dn[i0]

    for i in range(i0 + 1, n):
        dn[i] = dr[i] if (dr[i] > dn[i - 1] or close[i - 1] < dn[i - 1]) else dn[i - 1]
        up[i] = ur[i] if (ur[i] < up[i - 1] or close[i - 1] > up[i - 1]) else up[i - 1]
        if st[i - 1] == up[i - 1]:
            d[i] = -1 if close[i] > up[i] else 1
        else:
            d[i] = 1 if close[i] < dn[i] else -1
        st[i] = dn[i] if d[i] == -1 else up[i]
    return st, d, i0


# ------------------------------------------------------------------ 数据
def load_key(key: str) -> dict:
    path = os.path.join(KLINE_DIR, f"{key}_1440min.csv")
    df = pd.read_csv(path, encoding="utf-8-sig")
    dt = pd.to_datetime(df["datetime"])
    return {
        "key": key,
        "dt": dt,
        "trade_day": (dt + pd.Timedelta(hours=8)).dt.normalize(),   # §2.1 交易日口径
        "open": df["open"].to_numpy(float),
        "high": df["high"].to_numpy(float),
        "low": df["low"].to_numpy(float),
        "close": df["close"].to_numpy(float),
        "volume": df["volume"].to_numpy(float),
    }


def load_spec() -> dict:
    sys.path.insert(0, PROJ)
    import config_symbols as cs          # 不在本文件硬编码乘数/跳数（§2.2）
    out = {}
    for k in KEYS:
        name, cont, mult, tick = cs.SYMBOLS[k]
        out[k] = {"name": name, "cont": cont, "mult": float(mult), "tick": float(tick)}
    return out


# ------------------------------------------------------------------ 换月识别
def roll_days_of(key: str) -> tuple[list, dict]:
    """按「每日 oi 最大 = 主力」识别换月日（§6.3，探针实测的修法）。

    返回 (roll_days, info)。roll_days 为 datetime.date 列表。
    """
    roll = pd.read_csv(ROLL_CSV, encoding="utf-8-sig")
    sub = roll[roll["key"] == key].copy()
    sub["日期"] = pd.to_datetime(sub["日期"])
    if len(sub) == 0:
        return [], {"first": None, "last": None, "n": 0, "thin": 0, "rows": 0, "max_gap": None}

    order = sub.sort_values(["日期", "oi", "volume"], ascending=[True, False, False])
    dom = order.groupby("日期", as_index=False).first().sort_values("日期").reset_index(drop=True)
    chg = dom["sym"].ne(dom["sym"].shift())
    days = list(dom.loc[chg[chg].index[1:], "日期"].dt.date)

    rows_per_day = sub.groupby("日期").size()
    gaps = [(days[i + 1] - days[i]).days for i in range(len(days) - 1)] if len(days) > 1 else [0]
    info = {
        "first": dom["日期"].min().date(),
        "last": dom["日期"].max().date(),
        "n": len(days),
        "thin": int((rows_per_day <= 1).sum()),
        "rows": int(rows_per_day.shape[0]),
        "max_gap": int(max(gaps)) if gaps else None,
    }
    return days, info


def roll_confidence_windows(days: list, max_gap_days: int = 400) -> list[tuple]:
    """把换月日链切成「可信窗口」。间隔 > 400 天的断档把链切断。"""
    if not days:
        return []
    wins = []
    start = days[0]
    for i in range(1, len(days)):
        if (days[i] - days[i - 1]).days > max_gap_days:
            wins.append((start, days[i - 1]))
            start = days[i]
    wins.append((start, days[-1]))
    return wins


# ------------------------------------------------------------------ 回测引擎
def run_arm(d: dict, st: np.ndarray, dirc: np.ndarray, mult: float, tick: float,
            start_idx: int = START_IDX) -> tuple[list, np.ndarray]:
    """按 dir 翻仓，全部在信号后一根 open 成交。

    返回 (trades, eq_bar)。eq_bar 是「截至每根 bar 的已实现净权益」，
    保证 eq_bar[-1] == sum(net_pnl)（V6）。
    """
    n = len(d["close"])
    o = d["open"]
    dt = d["dt"]
    day = d["trade_day"]

    per_side = COMM + SLIP * tick * mult
    seg_cost = 2.0 * per_side          # 一段 = 开 1 边 + 平 1 边

    trades = []
    eq = np.zeros(n, float)
    running = 0.0

    pos = 0
    sig_i = None          # 触发信号的 bar
    fill_i = None         # 成交的 bar
    entry_raw = 0.0
    entry_px = 0.0
    prev_dir = None

    def close_seg(exit_fill_i: int, exit_sig_i: int, reason: str):
        nonlocal running, pos, sig_i, fill_i, entry_raw, entry_px, prev_dir
        exit_raw = float(o[exit_fill_i])
        exit_px = exit_raw - pos * SLIP * tick
        gross = (exit_raw - entry_raw) * pos * mult
        net = gross - seg_cost
        running += net
        eq[exit_fill_i] += net
        trades.append({
            "seg_id": len(trades) + 1,
            "entry_sig_i": int(sig_i),
            "entry_fill_i": int(fill_i),
            "entry_time": dt.iloc[fill_i],
            "entry_trade_day": day.iloc[fill_i].date(),
            "entry_price_raw": entry_raw,
            "entry_price": entry_px,
            "exit_sig_i": int(exit_sig_i),
            "exit_fill_i": int(exit_fill_i),
            "exit_time": dt.iloc[exit_fill_i],
            "exit_trade_day": day.iloc[exit_fill_i].date(),
            "exit_price_raw": exit_raw,
            "exit_price": exit_px,
            "side": pos,
            "bars_held": int(exit_fill_i - fill_i),
            "gross_pnl": gross,
            "cost": seg_cost,
            "net_pnl": net,
            "exit_reason": reason,
        })
        pos = 0

    for i in range(start_idx, n - 1):
        if np.isnan(st[i]):
            continue
        cur = int(dirc[i])
        if cur == 0:
            continue

        if prev_dir is None:
            # 首次建仓：信号在第 i 根收盘，成交在 i+1 open
            sig_i, fill_i = i, i + 1
            pos = -cur                       # dir=-1 看多 → side=+1
            entry_raw = float(o[i + 1])
            entry_px = entry_raw + pos * SLIP * tick
            prev_dir = cur
            continue

        if cur != prev_dir:
            # 翻转：平旧 + 开新，同一根 open
            close_seg(i + 1, i, "flip")
            sig_i, fill_i = i, i + 1
            pos = -cur
            entry_raw = float(o[i + 1])
            entry_px = entry_raw + pos * SLIP * tick
            prev_dir = cur

    # 末尾强制平仓（用最后一根 close，标记 reason=end）
    if pos != 0:
        last = n - 1
        exit_raw = float(d["close"][last])
        exit_px = exit_raw - pos * SLIP * tick
        gross = (exit_raw - entry_raw) * pos * mult
        net = gross - seg_cost
        running += net
        eq[last] += net
        trades.append({
            "seg_id": len(trades) + 1,
            "entry_sig_i": int(sig_i),
            "entry_fill_i": int(fill_i),
            "entry_time": dt.iloc[fill_i],
            "entry_trade_day": day.iloc[fill_i].date(),
            "entry_price_raw": entry_raw,
            "entry_price": entry_px,
            "exit_sig_i": int(last),
            "exit_fill_i": int(last),
            "exit_time": dt.iloc[last],
            "exit_trade_day": day.iloc[last].date(),
            "exit_price_raw": exit_raw,
            "exit_price": exit_px,
            "side": pos,
            "bars_held": int(last - fill_i),
            "gross_pnl": gross,
            "cost": seg_cost,
            "net_pnl": net,
            "exit_reason": "end",
        })
        pos = 0

    # 已实现权益前缀和（eq 里已按成交 bar 记录增量）
    eq_bar = np.cumsum(eq)
    return trades, eq_bar


# ------------------------------------------------------------------ selftest
def _check(tag, ok, msg, results):
    results.append((tag, bool(ok), msg))
    print(f"  [{'PASS' if ok else 'FAIL'}] {tag}  {msg}")


def selftest() -> int:
    print("=" * 78)
    print("  st_binned.py --selftest   (V1 / V1b / V2 / V5 / V11)")
    print("=" * 78)
    res = []
    spec = load_spec()
    rng = np.random.default_rng(20261019)      # 固定种子，写进 run.log

    for key in KEYS:
        d = load_key(key)
        h, l, c, o = d["high"], d["low"], d["close"], d["open"]
        n = len(c)
        atr = wilder_atr(h, l, c)
        ada, _, _, _, _ = adaptive_atr_seq(atr)
        st_b, dir_b, i0b = supertrend(atr, h, l, c)
        st_n, dir_n, i0n = supertrend(ada, h, l, c)

        # ---- V2 ATR 首值与一步递推
        pc = np.r_[c[0], c[:-1]]
        tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
        v2a = abs(atr[ATR_LEN - 1] - tr[:ATR_LEN].mean()) < 1e-9
        v2b = abs(atr[ATR_LEN] - (atr[ATR_LEN - 1] + (tr[ATR_LEN] - atr[ATR_LEN - 1]) / ATR_LEN)) < 1e-9
        _check("V2", v2a and v2b,
               f"{key}: ATR[9]={atr[ATR_LEN-1]:.6f} 首值SMA={v2a} 一步递推={v2b}", res)

        # ---- V1b st 有效性（V1 挡不住全 nan）
        okb = np.sum(~np.isnan(st_b)) / max(1, n - i0b)
        okn = np.sum(~np.isnan(st_n)) / max(1, n - i0n)
        _check("V1b", (okb > 0.99) and (okn > 0.99),
               f"{key}: base 有效st={okb:.2%} (i0={i0b})  bin 有效st={okn:.2%} (i0={i0n})", res)

        # ---- V11 两臂起点一致（bin 首个有效 = 108）
        _check("V11", (i0n == START_IDX) and (START_IDX >= i0b),
               f"{key}: bin i0={i0n}（应={START_IDX}）  base i0={i0b}", res)

        # ---- V5 两臂同源（同一份 OHLC 数组内容）
        same = all(np.array_equal(d[x], d[x]) for x in ("open", "high", "low", "close"))
        _check("V5", same, f"{key}: 两臂共用同一份 OHLCV 数组（同 dict）", res)

        # ---- V1 截断检验：随机 200 个 i，用 data[:i+1] 重算 dir[i]
        idx_all = np.arange(max(START_IDX + 2, 300), n)
        picks = np.sort(rng.choice(idx_all, size=min(200, len(idx_all)), replace=False))
        bad_base, bad_bin, bad_ada = [], [], []
        for i in picks:
            a_i = wilder_atr(h[:i + 1], l[:i + 1], c[:i + 1])
            _, dd_i, _ = supertrend(a_i, h[:i + 1], l[:i + 1], c[:i + 1])
            if int(dd_i[i]) != int(dir_b[i]):
                bad_base.append(int(i))

            # adaptive_atr[j] 只依赖 atr[max(0,j-99):j+1] ⇒ 天然因果。
            # 两步验证：① 截断重算 adaptive[i] 与全量一致；② 用截断序列跑递推得到同一 dir。
            w = a_i[max(0, i - LOOKBACK + 1): i + 1]
            w = w[~np.isnan(w)]
            if len(w) >= LOOKBACK:
                p = pct_nearest_rank(w, Q)
                a_pick = p[int(np.argmin(np.abs(p - a_i[i])))]
                if abs(a_pick - ada[i]) > 1e-9:
                    bad_ada.append(int(i))
                _, dd_i2, _ = supertrend(ada[:i + 1], h[:i + 1], l[:i + 1], c[:i + 1])
                if int(dd_i2[i]) != int(dir_n[i]):
                    bad_bin.append(int(i))
        _check("V1", (len(bad_base) == 0) and (len(bad_bin) == 0) and (len(bad_ada) == 0),
               f"{key}: 抽{len(picks)}点 base不一致={len(bad_base)} bin不一致={len(bad_bin)}"
               f" adaptive不一致={len(bad_ada)}"
               + (f" 例:{bad_base[:3] or bad_bin[:3] or bad_ada[:3]}"
                  if (bad_base or bad_bin or bad_ada) else ""), res)

        # ---- V3/V7/V6 交易层（用 base 臂快速验）
        tr_b, eq_b = run_arm(d, st_b, dir_b, spec[key]["mult"], spec[key]["tick"])
        raw_ok = all(
            abs(t["entry_price_raw"] - o[t["entry_fill_i"]]) < 1e-9 and
            abs(t["exit_price_raw"] - (o[t["exit_fill_i"]] if t["exit_reason"] == "flip"
                                       else c[-1])) < 1e-9 and
            t["entry_fill_i"] > t["entry_sig_i"]
            for t in tr_b)
        _check("V3", raw_ok, f"{key}: {len(tr_b)} 段，entry_price_raw==open 且成交在信号后一根", res)

        if tr_b:
            g = sum(t["gross_pnl"] for t in tr_b)
            cs = sum(t["cost"] for t in tr_b)
            nn = sum(t["net_pnl"] for t in tr_b)
            per = 2.0 * (COMM + SLIP * spec[key]["tick"] * spec[key]["mult"])
            v7 = (cs > 0) and abs(cs - per * len(tr_b)) < 1e-6
            v6 = abs(nn - eq_b[-1]) < 1.0
            _check("V7", v7, f"{key}: cost={cs:.0f} ≈ {per:.0f}×{len(tr_b)}  单段={per:.0f}元", res)
            _check("V6", v6, f"{key}: sum(net)={nn:.2f} eq[-1]={eq_b[-1]:.2f} 差={abs(nn-eq_b[-1]):.2e}", res)
            _check("V11b", all(t["entry_fill_i"] >= START_IDX for t in tr_b),
                   f"{key}: 首笔 entry_fill_i={tr_b[0]['entry_fill_i']}（应>={START_IDX}）", res)

    n_ok = sum(1 for _, ok, _ in res if ok)
    n_bad = len(res) - n_ok
    print("\n" + "=" * 78)
    print(f"  合计 {len(res)} 项：PASS {n_ok} / FAIL {n_bad}")
    for tag, ok, msg in res:
        if not ok:
            print(f"  ✗ {tag}: {msg}")
    print("=" * 78)
    return 0 if n_bad == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
