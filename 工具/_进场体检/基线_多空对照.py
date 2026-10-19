# -*- coding: utf-8 -*-
"""基线对照：把「永远做多 / 永远做空 / 随机多空」塞进同一把尺子
============================================================================
目的（2026-10-19，第三批 7 信号普查的配套检查）
----------------------------------------------------------------------------
macd / kdj / cci / roc / sar / ha_trend 这 6 个信号是**状态型**（几乎每根 bar 都有方向，
n ≈ N，per_day ≈ 0.98），不是「事件型」进场信号。对状态型信号，L0/L1 度量的其实是
「按某个方向一直持仓 12 根」的收益 ⇒ 在单边行情里，任何顺势状态都会得分，
分数主要来自**品种自身的漂移（beta）**，而不是进场条件的信息。

因此必须给出基线：同一窗口、同一 HOLD/止损/成本口径下，
  · 永远做多（sig ≡ +1）   —— 纯 beta 上界之一
  · 永远做空（sig ≡ −1）   —— 同上，反向
  · 随机多空（每根 50/50） —— 零信息对照，理论比值 ≈ 0

判读规则：**只有当某信号严格好于「同方向基线」时，它才可能含进场信息**；
若信号表现 ≈ 永远做多，则它只是「在牛市里一直做多」的beta 记录。

口径：完全复用 进场体检.py 的 screen_one（HOLD/STOP/成本/门槛一个不改），
只把 SIGNALS 里塞三个合成信号，不触碰任何冻结参数。
用法：
  python 基线_多空对照.py --period 1440min
"""
import os
import sys
import argparse
import importlib.util

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_SPEC = importlib.util.spec_from_file_location("jctj", os.path.join(_HERE, "进场体检.py"))
m = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(m)


def _always_long(o, h, l, c, atr, ma):
    return np.ones(len(c))


def _always_short(o, h, l, c, atr, ma):
    return -np.ones(len(c))


_COIN_CACHE = {}


def _coin(o, h, l, c, atr, ma):
    n = len(c)
    if n not in _COIN_CACHE:
        rng = np.random.default_rng(20261019)
        _COIN_CACHE[n] = rng.choice([-1.0, 1.0], size=n)
    return _COIN_CACHE[n]


m.SIGNALS.update({
    "BASE_long": ("基线·永远做多", _always_long),
    "BASE_short": ("基线·永远做空", _always_short),
    "BASE_coin": ("基线·随机多空（50/50，固定种子）", _coin),
})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", default="1440min")
    a = ap.parse_args()
    m.PERIOD = a.period

    keys = list(m.cs.SYMBOLS.keys())
    names = ["BASE_long", "BASE_short", "BASE_coin"]
    L = []
    L.append("=" * 118)
    L.append(f"基线对照  ｜  周期={a.period}  ｜  HOLD={m.HOLD} 根  ｜  止损={m.STOP_ATR_MULT}×ATR  ｜  "
             f"滑点={m.SLIP}跳  ｜  口径完全复用 进场体检.py")
    L.append("=" * 118)
    L.append(f"{'品种':<7}{'永远做多 毛/成本':>17}{'t':>8}{'净(点)':>11}"
             f"{'永远做空 毛/成本':>17}{'t':>8}{'净(点)':>11}"
             f"{'随机多空 毛/成本':>17}{'t':>8}{'净(点)':>11}")
    agg = {n: [] for n in names}
    for k in keys:
        df = m.load(k)
        if df is None or len(df) < 1000:
            continue
        cells = []
        for nm in names:
            r = m.screen_one(k, nm, df)
            agg[nm].append(r)
            rt = r.get("ratio")
            cells.append((rt, r["L1_t"], r["L1_net"]))
        L.append(f"{m.cs.get(k)[0]:<7}"
                 + "".join(f"{(c[0] if c[0] is not None else float('nan')):>17.2f}{c[1]:>8.2f}{c[2]:>11.2f}"
                           for c in cells))
    L.append("-" * 118)
    for nm in names:
        rs = [r for r in agg[nm] if r["n"] >= 30]
        if not rs:
            continue
        means = [r["ratio"] for r in rs if r.get("ratio") is not None]
        L.append(f"{m.SIGNALS[nm][0]:<34} 品种数 {len(rs):>2} ｜ 均毛/成本 {np.mean(means):>7.2f} ｜ "
                 f"毛/成本≥2.0 的品种 {sum(1 for x in means if x >= 2.0):>2} 个")
    txt = "\n".join(L)
    print(txt)
    out = os.path.join(_HERE, f"基线_多空对照_{a.period}.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write(txt + "\n")
    print(f"\n[done] {out}")


if __name__ == "__main__":
    main()
