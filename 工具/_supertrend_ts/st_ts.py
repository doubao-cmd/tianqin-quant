# -*- coding: utf-8 -*-
"""st_ts.py —— ST 时间止损（ST-TS-01）核心：指标 + 两臂 + 自检

唯一改动（相对标准 SuperTrend）：
    持仓 >= N_TIME 根且浮亏（扣成本）⇒ 下一根 open 平仓，之后空仓到下次 dir 翻转。

指标函数逐字复制自 工具/_supertrend_分箱/st_binned.py（那份已用第二独立实现验证过等价）。
冻结参数全部模块级常量，禁止任何参数扫描。

用法：
    python st_ts.py --selftest
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

# ------------------------------------------------------------------ 冻结参数
ATR_LEN = 10
FACT = 3.0
LOOKBACK = 100
POS_SIZE = 1
SLIP = 1
COMM = 3.0

N_TIME = 30                       # ★ 预注册 §4 冻结，不许调
START_IDX = (ATR_LEN - 1) + (LOOKBACK - 1)      # 108

START_DAY = pd.Timestamp("2016-01-04")
END_DAY = pd.Timestamp("2026-10-15")            # 预注册 §6 冻结的区间末端

PROJ = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")

KEYS = ["AU", "V", "RU", "MA", "AG", "CU", "BU", "TA", "SN", "PP"]


# ------------------------------------------------------------------ 指标
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


def supertrend(atr: np.ndarray, high: np.ndarray, low: np.ndarray,
               close: np.ndarray):
    """从首个有效 ATR 下标 i0 初始化，再从 i0+1 递推。

    ★ Pine 原文：if na(atr[1]) → dir := 1; st := up(=下轨)。
      dir=1(空) 与 st=下轨 看似矛盾，但必须照抄；取上轨会让起始相位反相。
    返回 (st, dir, i0)；dir = -1 看多 / +1 看空。
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


def st_tv(atr, high, low, close):
    """第二独立实现：TradingView ta.supertrend 逐字转写（用于 V2 交叉验证）。"""
    n = len(close)
    hl2 = (high + low) / 2.0
    up = np.full(n, np.nan)      # Pine: up = 下轨
    dn = np.full(n, np.nan)      # Pine: dn = 上轨
    st = np.full(n, np.nan)
    dr = np.zeros(n, int)
    v = np.where(~np.isnan(atr))[0]
    if not len(v):
        return st, dr
    i0 = int(v[0])
    for i in range(i0, n):
        raw_up = hl2[i] - FACT * atr[i]
        raw_dn = hl2[i] + FACT * atr[i]
        if i == i0:
            up[i], dn[i] = raw_up, raw_dn
            dr[i] = 1
            st[i] = up[i]
            continue
        up[i] = max(raw_up, up[i - 1]) if close[i - 1] > up[i - 1] else raw_up
        dn[i] = min(raw_dn, dn[i - 1]) if close[i - 1] < dn[i - 1] else raw_dn
        if st[i - 1] == up[i - 1]:
            dr[i] = -1 if close[i] > up[i] else 1
            st[i] = up[i] if dr[i] == -1 else dn[i]
        else:
            dr[i] = 1 if close[i] < dn[i] else -1
            st[i] = dn[i] if dr[i] == 1 else up[i]
    return st, dr


# ------------------------------------------------------------------ 数据
def load_key(key: str) -> dict:
    path = os.path.join(DATA_DIR, f"{key}_1440min.csv")
    df = pd.read_csv(path, encoding="utf-8-sig")
    dt = pd.to_datetime(df["datetime"])
    td = (dt + pd.Timedelta(hours=8)).dt.normalize()     # 日线 label 是 UTC 16:00
    m = (td >= START_DAY) & (td <= END_DAY)
    df, td = df[m].reset_index(drop=True), td[m].reset_index(drop=True)
    return {
        "key": key,
        "dt": dt[m].reset_index(drop=True),
        "trade_day": td,
        "open": df["open"].to_numpy(float),
        "high": df["high"].to_numpy(float),
        "low": df["low"].to_numpy(float),
        "close": df["close"].to_numpy(float),
        "volume": df["volume"].to_numpy(float),
    }


def load_spec() -> dict:
    sys.path.insert(0, PROJ)
    import config_symbols as cs
    out = {}
    for k in KEYS:
        name, cont, mult, tick = cs.SYMBOLS[k]
        out[k] = {"name": name, "cont": cont, "mult": float(mult), "tick": float(tick)}
    return out


# ------------------------------------------------------------------ 回测引擎
def run_arm(d: dict, st: np.ndarray, dirc: np.ndarray, mult: float, tick: float,
            mode: str = "base", n_time: int = N_TIME) -> tuple[list, np.ndarray]:
    """按 dir 翻仓，全部在信号后一根 open 成交。

    mode="base"：标准 ST（唯一出场 = 反向翻转）
    mode="ts"  ：持仓 >= n_time 根且浮亏（扣成本）⇒ 下一根 open 平仓，空仓到下次翻转

    返回 (trades, eq_bar)；eq_bar[-1] == sum(net_pnl)（V6）。
    """
    n = len(d["close"])
    o, c = d["open"], d["close"]
    dt, day = d["dt"], d["trade_day"]

    seg_cost = 2.0 * (COMM + SLIP * tick * mult)

    trades = []
    eq = np.zeros(n, float)
    running = 0.0

    pos = 0
    sig_i = fill_i = None
    entry_raw = 0.0
    last_dir = None

    def open_pos(i_sig: int) -> None:
        nonlocal pos, sig_i, fill_i, entry_raw
        sig_i, fill_i = i_sig, i_sig + 1
        pos = -int(dirc[i_sig])                 # dir=-1 → 多
        entry_raw = float(o[i_sig + 1])

    def close_pos(i_sig: int, reason: str) -> None:
        nonlocal pos, running
        ex = i_sig + 1
        exit_raw = float(o[ex])
        gross = (exit_raw - entry_raw) * pos * mult
        net = gross - seg_cost
        running += net
        eq[ex] += net
        trades.append({
            "seg_id": len(trades) + 1,
            "entry_sig_i": int(sig_i), "entry_fill_i": int(fill_i),
            "entry_time": dt.iloc[fill_i], "entry_trade_day": day.iloc[fill_i].date(),
            "entry_price_raw": entry_raw, "entry_price": entry_raw + pos * SLIP * tick,
            "exit_sig_i": int(i_sig), "exit_fill_i": int(ex),
            "exit_time": dt.iloc[ex], "exit_trade_day": day.iloc[ex].date(),
            "exit_price_raw": exit_raw, "exit_price": exit_raw - pos * SLIP * tick,
            "side": pos, "bars_held": int(ex - fill_i),
            "gross_pnl": gross, "cost": seg_cost, "net_pnl": net,
            "exit_reason": reason,
        })
        pos = 0

    for i in range(START_IDX, n - 1):
        if np.isnan(st[i]):
            continue
        cur = int(dirc[i])
        if cur == 0:
            continue

        if last_dir is None:
            last_dir = cur
            open_pos(i)
            continue

        if cur != last_dir:                      # 翻转：平旧 + 反手（同一根 open）
            if pos != 0:
                close_pos(i, "flip")
            last_dir = cur
            open_pos(i)
            continue

        if mode == "ts" and pos != 0:
            if (i - fill_i) >= n_time:
                unreal = (c[i] - entry_raw) * pos * mult - seg_cost
                if unreal < 0:
                    close_pos(i, "time_stop")    # 平完 pos=0，下根起空仓
                    continue

    if pos != 0:                                  # 末尾强平
        last = n - 1
        exit_raw = float(c[last])
        gross = (exit_raw - entry_raw) * pos * mult
        net = gross - seg_cost
        running += net
        eq[last] += net
        trades.append({
            "seg_id": len(trades) + 1,
            "entry_sig_i": int(sig_i), "entry_fill_i": int(fill_i),
            "entry_time": dt.iloc[fill_i], "entry_trade_day": day.iloc[fill_i].date(),
            "entry_price_raw": entry_raw, "entry_price": entry_raw + pos * SLIP * tick,
            "exit_sig_i": int(last), "exit_fill_i": int(last),
            "exit_time": dt.iloc[last], "exit_trade_day": day.iloc[last].date(),
            "exit_price_raw": exit_raw, "exit_price": exit_raw - pos * SLIP * tick,
            "side": pos, "bars_held": int(last - fill_i),
            "gross_pnl": gross, "cost": seg_cost, "net_pnl": net,
            "exit_reason": "end",
        })
        pos = 0
    return trades, np.cumsum(eq)


# ------------------------------------------------------------------ 自检
def selftest(keys=None) -> int:
    keys = keys or KEYS
    spec = load_spec()
    fails = 0
    total = 0

    def chk(tag, ok, msg):
        nonlocal fails, total
        total += 1
        if not ok:
            fails += 1
        print(f"  [{'PASS' if ok else 'FAIL'}] {tag}: {msg}")

    rng = np.random.default_rng(20261019)
    print(f"{'key':4s} {'bars':>5s} {'i0':>4s} {'dir不一致':>9s} {'st最大差':>10s} "
          f"{'V1截断坏点':>10s} {'段数base':>9s} {'段数ts':>7s}")
    for k in keys:
        d = load_key(k)
        h, l, c = d["high"], d["low"], d["close"]
        n = len(c)
        atr = wilder_atr(h, l, c)
        chk("V2-ATR首值", abs(atr[ATR_LEN - 1] - np.mean(
            np.maximum(h[:ATR_LEN] - l[:ATR_LEN],
                       np.maximum(np.abs(h[:ATR_LEN] - np.r_[c[0], c[:-1]][:ATR_LEN]),
                                  np.abs(l[:ATR_LEN] - np.r_[c[0], c[:-1]][:ATR_LEN]))))) < 1e-9,
            f"{k} ATR[9] = mean(TR[0:10])")

        st, dd, i0 = supertrend(atr, h, l, c)
        st2, dd2 = st_tv(atr, h, l, c)
        m = ~np.isnan(st) & ~np.isnan(st2)
        bad = int((dd[m] != dd2[m]).sum())
        mxd = float(np.max(np.abs(st[m] - st2[m]))) if m.any() else float("nan")
        chk("V2-第二实现", bad == 0 and mxd < 1e-9, f"{k} dir不一致={bad} st最大差={mxd:.3e}")

        # dir 符号：dir=-1 时 close 应在 st 上方（★ 排除 i0：Pine 首根 dir=1 但 st=下轨，属既定怪癖）
        m2 = m.copy()
        m2[i0] = False
        ok_sign = bool(np.all((dd[m2] == -1) == (c[m2] > st[m2])))
        chk("V2b-dir符号", ok_sign, f"{k} dir=-1 ⇔ close>st（自 i0+1 起）")

        # V1 截断检验
        idx_all = np.arange(START_IDX + 2, n)
        picks = np.sort(rng.choice(idx_all, size=min(50, len(idx_all)), replace=False))
        bad_pts = []
        for i in picks:
            a_i = wilder_atr(h[:i + 1], l[:i + 1], c[:i + 1])
            _, d_i, _ = supertrend(a_i, h[:i + 1], l[:i + 1], c[:i + 1])
            if int(d_i[i]) != int(dd[i]):
                bad_pts.append(int(i))
        chk("V1-截断", len(bad_pts) == 0,
            f"{k} 抽{len(picks)}点 不一致={len(bad_pts)} {bad_pts[:3]}")

        tr_b, eq_b = run_arm(d, st, dd, spec[k]["mult"], spec[k]["tick"], "base")
        tr_t, eq_t = run_arm(d, st, dd, spec[k]["mult"], spec[k]["tick"], "ts")

        # V3 成交价
        v3 = True
        for t in tr_b + tr_t:
            if t["entry_price_raw"] not in set(d["open"].tolist()):
                v3 = False
                break
            if abs(abs(t["entry_price"] - t["entry_price_raw"])
                   - SLIP * spec[k]["tick"]) > 1e-9:
                v3 = False
                break
        chk("V3-成交价", v3, f"{k} entry_price_raw=某根open 且 entry_price=raw±1跳")

        # V6 台账可加总
        for arm, tr, eq in (("base", tr_b, eq_b), ("ts", tr_t, eq_t)):
            chk("V6-加总", abs(sum(t["net_pnl"] for t in tr) - eq[-1]) < 1.0,
                f"{k}/{arm} sum(net)={sum(t['net_pnl'] for t in tr):.2f} eq={eq[-1]:.2f}")
            # V7 成本
            per = 2 * (COMM + SLIP * spec[k]["tick"] * spec[k]["mult"])
            chk("V7-成本", abs(sum(t["cost"] for t in tr) - per * len(tr)) < 1e-6,
                f"{k}/{arm} cost={sum(t['cost'] for t in tr):.1f} 期望={per * len(tr):.1f} "
                f"（{per:.1f}/段 × {len(tr)}）")

        print(f"{k:4s} {n:5d} {i0:4d} {bad:9d} {mxd:10.1e} {len(bad_pts):10d} "
              f"{len(tr_b):9d} {len(tr_t):7d}")

    print(f"\n合计 {total} 项：PASS {total - fails} / FAIL {fails}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--keys", default="")
    a = ap.parse_args()
    if a.selftest:
        raise SystemExit(selftest([x.strip() for x in a.keys.split(",") if x.strip()] or None))
    ap.print_help()
