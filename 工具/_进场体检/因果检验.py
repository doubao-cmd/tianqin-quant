# -*- coding: utf-8 -*-
"""因果检验（未来扰动法）—— 补 `--selftest` 截断检验的盲区
============================================================================
为什么需要它
------------
`进场体检.py --selftest` 用的是「截断重算」：从 i+1-W 起截一段窗口，重算末根。
这对**短记忆**信号等价，但对三类信号会产生两种误判：

  ① **长记忆递归**（ha_trend 的 HA_open、sar 的 EP/AF 状态、macd/kdj/roc 的 EMA/递推）
     截断会改掉递归的种子 ⇒ sig[i] 必然不同 ⇒ **假 FAIL**。
  ② **浮点边界**（boll_meanrev 的 `c <= lower`）
     全量 rolling 与切片 rolling 的累加误差不同，恰在 `c == 轨` 附近会翻转 ⇒ **假 FAIL**
     （实测：V 的 i=16307，末 20 根全部收在 11040.0，sd 真值 0，全量算出 5e-5 ⇒ 判定翻转）。

正确判据（本脚本）
------------------
「sig[i] 是否依赖第 i 根之后的数据」——直接把 **i 之后的 bar 全部替换成随机垃圾**，
保持 0..i 不变，重算整条序列；sig[i] 必须**逐值相等**。
  · 历史 0..i 完整 ⇒ 递归种子不变 ⇒ 长记忆信号也能过；
  · 未来被改写 ⇒ 只要读过未来，sig[i] 一定变 ⇒ 真未来函数必然被抓。
判读：
  · 本脚本 PASS 而 --selftest 报 FAIL ⇒ 是①②类误判，不是未来函数（结论以本脚本为准）。
  · 本脚本 FAIL ⇒ **真有问题，停下来查实现**。

用法：
  python 因果检验.py                 # 用 1440min（序列短、跑得快），7 个第三批信号
  python 因果检验.py --signals all   # 全部 24 个
  python 因果检验.py --period 5min   # 换周期
"""
import os
import argparse
import importlib.util

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_SPEC = importlib.util.spec_from_file_location("jctj", os.path.join(_HERE, "进场体检.py"))
m = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(m)

BATCH3 = ["boll_meanrev", "ha_trend", "macd", "kdj", "cci", "roc", "sar"]


def _garble(o, h, l, c, i, rng):
    """把 i 之后的 bar 换成随机游走垃圾（保持 OHLC 逻辑合法），0..i 原样不动。"""
    o2, h2, l2, c2 = o.copy(), h.copy(), l.copy(), c.copy()
    n = len(c)
    if i + 1 >= n:
        return o2, h2, l2, c2
    base = c[i]
    steps = rng.normal(0.0, 0.02, size=n - i - 1)
    path = base * np.exp(np.cumsum(steps))
    o2[i + 1:] = path
    c2[i + 1:] = path * np.exp(rng.normal(0.0, 0.005, size=n - i - 1))
    h2[i + 1:] = np.maximum(o2[i + 1:], c2[i + 1:]) * (1.0 + np.abs(rng.normal(0.0, 0.01, size=n - i - 1)))
    l2[i + 1:] = np.minimum(o2[i + 1:], c2[i + 1:]) * (1.0 - np.abs(rng.normal(0.0, 0.01, size=n - i - 1)))
    return o2, h2, l2, c2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", default="1440min")
    ap.add_argument("--signals", default="batch3", help="batch3 / all / 逗号分隔的信号名")
    ap.add_argument("--probes", type=int, default=60)
    a = ap.parse_args()
    m.PERIOD = a.period

    if a.signals == "batch3":
        names = BATCH3
    elif a.signals == "all":
        names = list(m.SIGNALS)
    else:
        names = [s.strip() for s in a.signals.split(",")]

    keys = list(m.cs.SYMBOLS.keys())
    print(f"因果检验（未来扰动法）｜ 周期={a.period} ｜ 每格抽查 {a.probes} 根 ｜ 信号 {len(names)} 个")
    print(f"{'信号':<15}{'品种':<7}{'抽查':>6}{'不一致':>8}  结论")
    bad_total = 0
    for nm in names:
        fn = m.SIGNALS[nm][1]
        for k in keys:
            df = m.load(k)
            if df is None or len(df) < 300:
                continue
            o = df["open"].to_numpy(float); h = df["high"].to_numpy(float)
            l = df["low"].to_numpy(float); c = df["close"].to_numpy(float)
            N = len(c)
            full = fn(o, h, l, c, m._atr(h, l, c, m.ATR_N), m._sma(c, m.MA_N))
            rng = np.random.default_rng(abs(hash((nm, k))) % (2 ** 32))
            probes = rng.integers(max(60, N // 4), N - 2, size=a.probes)
            bad = 0
            for i in probes:
                o2, h2, l2, c2 = _garble(o, h, l, c, i, rng)
                s2 = fn(o2, h2, l2, c2, m._atr(h2, l2, c2, m.ATR_N), m._sma(c2, m.MA_N))
                v1 = float(np.nan_to_num(full[i])); v2 = float(np.nan_to_num(s2[i]))
                if abs(v1 - v2) > 1e-9:
                    bad += 1
            bad_total += bad
            print(f"{nm:<15}{k:<7}{a.probes:>6}{bad:>8}  {'✅ 因果（不读未来）' if bad == 0 else '❌ 依赖未来数据！'}")
    print(f"\n合计不一致 {bad_total} 处 ⇒ {'全部通过 ✅' if bad_total == 0 else '存在依赖未来的信号 ❌'}")


if __name__ == "__main__":
    main()
